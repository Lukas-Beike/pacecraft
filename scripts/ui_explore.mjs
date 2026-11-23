// Read-only explorer of the running app for UI reviews (#3913): runs the steps of a plan file.
//
//   node scripts/ui_explore.mjs {--url <app address> | --url-file <file>} --password-file <secret file>
//     --out-dir <new scratchpad dir> --plan - < plan.json
//
// Plan: {"viewports": ["phone", "desktop"], "colorScheme": "light|dark", "textZoom": 100-200,
// "steps": [{"go": "analysis/running"}, {"click": "<css>", "nth": 0}, {"scroll": "<css>" | pixels},
// {"shot": "<name>", "full": false}, {"text": "<css>", "max": 4000}, {"wait": ms | "<css>"}]}.
// A period or filter switch is a click; "go" also waits out a "… wird geladen…" line.
// Logs in and out like ui_walk.mjs. Every request other than GET or HEAD is aborted, except the
// login and logout; service workers are blocked. A click on a form's submit control or on a
// control whose label saves, sends, syncs, deletes or confirms is refused. Writes the screenshots
// and explore.json (steps, texts read, error counts, aborted requests by method and path; no
// response bodies, cookies or address) to --out-dir. Exit 0 no failure, 1 a step failed or a
// request was aborted, 2 bad input or login refused.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

import { appUrl, FRESH_OUT_DIR, login, logout, refusedLabel, rendered, scratchPath, SECRET_MISSING, secretPath, settled, WIDTHS, writeFresh } from "./ui_walk.mjs";

// Login and logout are the only writes the explorer lets through.
const SESSION_PATHS = new Set(["/api/login", "/api/logout"]);
// Reads that hand over the whole database, the logs or an OAuth grant are aborted like writes.
const WHOLE_DATA_PATHS = new Set(["/api/privacy/backup", "/api/privacy/export", "/api/logs/download", "/api/diagnostics", "/api/wahoo/callback"]);
const own = (object, key) => Object.hasOwn(object ?? {}, key);
// Beyond ui_walk.mjs's refused labels: sync, confirm, sign-out, transfer, archive and remove.
const DENIED = /bestätig|confirm|sync|abgleich|abmeld|logout|übertrag|archiv|entfern|remove/i;
const LABEL = /^[\w-]{1,60}$/;
const named = (value) => typeof value === "string" && value.length > 0;
// Each step names one action (its value checked here) and only that action's options.
const STEPS = {
  go: (value) => typeof value === "string" && /^[a-z0-9/_-]*$/i.test(value),
  click: named,
  scroll: (value) => named(value) || Number.isInteger(value),
  shot: (value) => typeof value === "string" && LABEL.test(value),
  text: named,
  // Milliseconds, or a CSS selector that must become visible (#3969).
  wait: (value) => named(value) || (Number.isInteger(value) && value >= 100 && value <= WAIT_MAX),
};
const STEP_OPTIONS = {
  click: { nth: (value) => Number.isInteger(value) && value >= 0 },
  shot: { full: (value) => typeof value === "boolean" },
  text: { max: (value) => Number.isInteger(value) && value > 0 && value <= TEXT_MAX },
};
// A text step keeps TEXT_CAP characters unless its "max" asks for up to TEXT_MAX.
const TEXT_CAP = 4000;
const TEXT_MAX = 50000;
const SETTLE_MS = 15000;
const WAIT_MAX = 15000;

// Aborts every request except GET, HEAD (not the whole-data reads) and the app's own login and logout;
// returns the aborted ones' list.
export async function guardRequests(context, origin) {
  const aborted = [];
  await context.route("**/*", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const same = url.origin === origin;
    const read = ["GET", "HEAD"].includes(request.method()) && !(same && WHOLE_DATA_PATHS.has(url.pathname));
    if (read || (same && request.method() === "POST" && SESSION_PATHS.has(url.pathname))) return route.continue();
    aborted.push(`${request.method()} ${same ? url.pathname : "(other origin)"}`);
    return route.abort("blockedbyclient");
  });
  return aborted;
}

export async function exploreContext(browser, { url, viewport, colorScheme = "light", textZoom = 100 }) {
  const context = await browser.newContext({ baseURL: url, viewport: WIDTHS[viewport], colorScheme, serviceWorkers: "block" });
  if (textZoom !== 100) {
    await context.addInitScript((zoom) => {
      // The init script runs before the document has a root element. An inline style, not a
      // <style> element: the app's CSP (style-src 'self') blocks the element (#3969).
      document.addEventListener("DOMContentLoaded", () => document.documentElement.style.setProperty("font-size", `${zoom}%`, "important"), { once: true });
    }, textZoom);
  }
  return { context, aborted: await guardRequests(context, new URL(url).origin) };
}

export function parsePlan(plan) {
  const fail = (why) => {
    throw new Error(`plan: ${why}`);
  };
  if (!plan || typeof plan !== "object" || !Array.isArray(plan.steps) || !plan.steps.length || plan.steps.length > 300) fail("steps must be a list of 1 to 300 steps");
  const viewports = plan.viewports ?? Object.keys(WIDTHS);
  if (!Array.isArray(viewports) || !viewports.length || viewports.some((name) => !own(WIDTHS, name)) || new Set(viewports).size !== viewports.length) fail(`viewports are ${Object.keys(WIDTHS).join(" and/or ")}, each once`);
  const colorScheme = plan.colorScheme ?? "light";
  if (!["light", "dark"].includes(colorScheme)) fail("colorScheme is light or dark");
  const textZoom = plan.textZoom ?? 100;
  if (!Number.isInteger(textZoom) || textZoom < 100 || textZoom > 200) fail("textZoom is a whole percent from 100 to 200");
  for (const [index, step] of plan.steps.entries()) {
    const keys = Object.keys(step || {});
    const kind = keys.find((key) => own(STEPS, key));
    if (!kind || keys.some((key) => key !== kind && !own(STEP_OPTIONS[kind], key))) fail(`step ${index + 1} needs exactly one of ${Object.keys(STEPS).join(", ")}`);
    if (!STEPS[kind](step[kind]) || keys.some((key) => key !== kind && !STEP_OPTIONS[kind][key](step[key]))) fail(`step ${index + 1} has a bad value`);
  }
  // Each screenshot is written once under its name.
  const shots = plan.steps.filter((step) => own(step, "shot")).map((step) => step.shot);
  if (new Set(shots).size !== shots.length) fail("each shot name is used once");
  return { viewports, colorScheme, textZoom, steps: plan.steps };
}

// Why a click is refused, else "": the target's control submits a form or its label is on a deny list.
export async function refusedClick(target) {
  const control = await target.evaluate((el) => {
    const owner = el.closest("button, input, a, summary, select, [role=button]") || el;
    // A summary only expands its section; its text may carry a changing status line (as ui_walk.mjs).
    const text = owner.tagName === "SUMMARY" ? "" : owner.textContent;
    const label = [owner.getAttribute("aria-label"), owner.getAttribute("title"), owner.value, text].filter(Boolean).join(" ");
    const submits = owner.matches("input[type=submit], input[type=image]") || (owner.tagName === "BUTTON" && owner.type === "submit" && Boolean(owner.form));
    return { label, submits };
  });
  if (control.submits) return "refused: a form's submit control";
  return refusedLabel(control.label) || DENIED.test(control.label) ? "refused: denied label" : "";
}

// One runner per step kind; each returns the step's result.
const RUN = {
  async go(page, item) {
    const view = await rendered(page, item.go, new Map());
    return view.capped ? { status: "fail", why: "not ready within 15 s" } : { status: "ok", ms: view.ms };
  },
  async click(page, item) {
    const target = page.locator(item.click).filter({ visible: true }).nth(item.nth ?? 0);
    await target.waitFor({ state: "visible", timeout: 3000 });
    // The element checked is the element clicked, even if the view re-renders in between.
    const handle = await target.elementHandle({ timeout: 3000 });
    const why = await refusedClick(handle);
    if (why) return { status: "fail", why };
    await handle.click();
    await page.waitForFunction(settled, null, { timeout: SETTLE_MS, polling: 100 }).catch(() => null);
    await page.waitForTimeout(300);
    return { status: "ok" };
  },
  async scroll(page, item) {
    if (typeof item.scroll === "number") await page.evaluate((y) => [document.scrollingElement, document.querySelector(".panel.active")].forEach((el) => el?.scrollBy(0, y)), item.scroll);
    else await page.locator(item.scroll).first().scrollIntoViewIfNeeded({ timeout: 3000 });
    return { status: "ok" };
  },
  async shot(page, item, shot) {
    await shot(item.shot, Boolean(item.full));
    return { status: "ok" };
  },
  async wait(page, item) {
    if (typeof item.wait === "number") await page.waitForTimeout(item.wait);
    else await page.locator(item.wait).filter({ visible: true }).first().waitFor({ state: "visible", timeout: SETTLE_MS });
    return { status: "ok" };
  },
  async text(page, item) {
    const text = await page.locator(item.text).evaluateAll((nodes) => nodes.filter((node) => node.checkVisibility()).map((node) => node.innerText).join("\n-----\n"));
    const cap = item.max ?? TEXT_CAP;
    if (!text.trim()) return { status: "fail", text: "", why: "no visible text" };
    return { status: "ok", text: text.slice(0, cap), truncated: text.length > cap || undefined };
  },
};

// Runs every step; a failed step is recorded and the next one runs.
export async function runPlan(page, steps, shot = async () => null) {
  const results = [];
  for (const item of steps) {
    const kind = Object.keys(item).find((key) => own(STEPS, key));
    try {
      results.push({ step: `${kind} ${item[kind]}`, ...(await RUN[kind](page, item, shot)) });
    } catch (error) {
      if (error.code === "EEXIST") throw error;
      results.push({ step: `${kind} ${item[kind]}`, status: "fail", why: error.name === "TimeoutError" ? "control missing" : error.message.split("\n")[0] });
    }
  }
  return results;
}

export const exitCode = (runs) => (runs.some((run) => run.aborted.length || run.logout !== 200 || run.steps.some((item) => item.status === "fail")) ? 1 : 0);

async function explore({ chromium, url, password, outDir, plan }) {
  const runs = [];
  const browser = await chromium.launch();
  try {
    for (const viewport of plan.viewports) {
      const { context, aborted } = await exploreContext(browser, { url, viewport, colorScheme: plan.colorScheme, textZoom: plan.textZoom });
      const page = await context.newPage();
      const errors = [];
      page.on("console", (message) => message.type() === "error" && errors.push("console"));
      page.on("pageerror", () => errors.push("page"));
      page.on("response", (response) => response.status() >= 500 && errors.push(`http ${response.status()}`));
      const run = { viewport, aborted, steps: [], errors };
      runs.push(run);
      const tag = `${viewport}-${plan.colorScheme}`;
      let loggedIn = false;
      try {
        await login(page, password);
        loggedIn = true;
        run.steps = await runPlan(page, plan.steps, async (label, full) => writeFresh(outDir, `${tag}-${label}.png`, await page.screenshot({ fullPage: full })));
      } catch (error) {
        error.login = !loggedIn;
        throw error;
      } finally {
        // The session ends however the run ended, from the app's own page.
        if (new URL(page.url()).origin !== new URL(url).origin) await page.goto("/").catch(() => null);
        run.logout = await logout(page).catch(() => "failed");
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }
  return runs;
}

export function reportLines(runs) {
  const lines = [];
  for (const run of runs) {
    for (const item of run.steps) lines.push(item.status === "ok" ? `ok ${run.viewport} ${item.step}` : `FAIL ${run.viewport} ${item.step}: ${item.why}`);
    for (const request of run.aborted) lines.push(`FAIL ${run.viewport} aborted ${request}`);
    if (run.errors.length) lines.push(`info ${run.viewport} ${run.errors.length} error(s): ${[...new Set(run.errors)].join(", ")}`);
    lines.push(`${run.logout === 200 ? "ok" : "FAIL"} ${run.viewport} logout ${run.logout}`);
  }
  return lines;
}

async function main() {
  let values;
  let url;
  let outDir;
  let plan;
  let password;
  let reading = "";
  try {
    ({ values } = parseArgs({ options: { url: { type: "string" }, "url-file": { type: "string" }, "password-file": { type: "string" }, "out-dir": { type: "string" }, plan: { type: "string" } } }));
    if (!values["password-file"] || !values["out-dir"] || !values.plan) throw new Error("--url or --url-file, --password-file, --out-dir and --plan are required");
    url = appUrl(values);
    outDir = scratchPath(values["out-dir"]);
    // The plan comes on standard input, so no file path from the command line is read for it (Sonar S8707).
    if (values.plan !== "-") throw new Error("--plan - reads the plan JSON from standard input");
    reading = "cannot read the plan on standard input";
    plan = parsePlan(JSON.parse(fs.readFileSync(0, "utf8")));
    reading = SECRET_MISSING;
    password = fs.readFileSync(secretPath(values["password-file"]), "utf8").trim();
    if (!password) throw new Error(SECRET_MISSING);
    reading = "cannot create the out-dir";
    fs.mkdirSync(outDir, { recursive: true });
  } catch (error) {
    // An argument, file-system or JSON error can name a path or quote content; never print it.
    let why = error.message;
    if (error.syscall || error instanceof SyntaxError) why = reading || "cannot use the out-dir";
    if (error.code?.startsWith("ERR_PARSE_ARGS")) why = "unknown option or missing value: --url, --password-file, --out-dir and --plan take one value each";
    console.log(`FAIL ${why}`);
    return 2;
  }
  const { chromium } = await import("@playwright/test");
  let runs;
  try {
    runs = await explore({ chromium, url, password, outDir, plan });
    writeFresh(outDir, "explore.json", JSON.stringify(runs.map(({ errors, ...run }) => ({ ...run, errors: errors.length })), null, 2));
  } catch (error) {
    let why = `explore stopped (${error.name})`;
    if (error.login) why = "login refused or app shell missing";
    if (error.code === "EEXIST") why = FRESH_OUT_DIR;
    console.log(`FAIL ${why}`);
    return 2;
  }
  const lines = reportLines(runs);
  for (const line of lines) console.log(line);
  return exitCode(runs);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
