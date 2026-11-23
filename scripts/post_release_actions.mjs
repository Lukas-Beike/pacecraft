// The allowed post-release Coach action: one question without a change.
//
//   node scripts/post_release_actions.mjs {--url <app address> | --url-file <file>} --password-file <secret file>
//
// Logs in with the password read from the secret file in this process (the
// password is never printed or passed on a command line, and the shell guard
// allows the secret path only here, #3758), asks the Coach one fixed question in the Coach view and checks
// that the turn left no change receipt and GET /api/plan is unchanged. Planning
// and moving a unit are not run until a Coach plan change is undoable as a whole
// turn (#2851). It presses nothing but the composer's send button: no sync, push,
// delete, export, settings, check-in or nutrition control. Output is ok/FAIL/info
// lines with counts and status words only. Exit 0 all ok, 1 a check failed, 2 bad
// input or the app is unreachable.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { appUrl, SECRET_MISSING, secretPath, T3_SECRETS } from "./ui_walk.mjs";

// The exact message; e2e/fixture_runtime.py answers the same text.
export const MESSAGES = { ask: "Was steht morgen in meinem Plan?" };
const TURN_MS = 180000;

// id -> content key of every unit in the local plan read; ids and dates never leave the script.
export function planSnapshot(planned) {
  return new Map((planned || []).map((unit) => [String(unit.id), [unit.date, unit.payload_hash, unit.archived, unit.local_deleted].join("|")]));
}

export function samePlan(before, after) {
  return before.size === after.size && [...before].every(([id, key]) => after.get(id) === key);
}

class Stop extends Error {}

// The secret file must lie in the temp directory or T3's secret folder (#3758).
export function parseInput(argv, secrets = T3_SECRETS) {
  const { values } = parseArgs({ args: argv, options: { url: { type: "string" }, "url-file": { type: "string" }, "password-file": { type: "string" } } });
  if (!values["password-file"]) throw new Error("--url or --url-file and --password-file are required");
  const url = appUrl(values);
  const file = secretPath(values["password-file"], secrets);
  let password;
  try {
    password = fs.readFileSync(file, "utf8").trim();
  } catch {
    throw new Error(SECRET_MISSING); // a file-system error names a path; never print it
  }
  if (!password) throw new Error(SECRET_MISSING);
  return { url, password };
}

async function readPlan(page) {
  const planned = await page.evaluate(async () => {
    const response = await fetch("/api/plan?local=1");
    return response.ok ? (await response.json()).planned : null;
  });
  if (!planned) throw new Stop("cannot read the plan");
  return planSnapshot(planned);
}

// One Coach turn in the composer; resolves with the latest answer's change receipts.
async function askCoach(page, message) {
  await page.goto("/#coach");
  await page.locator("#appShell").waitFor({ state: "visible", timeout: 30000 });
  await page.waitForFunction(() => !state.loadPromise, null, { timeout: 30000 });
  const answers = page.locator("#messages .message.assistant");
  const before = await answers.count();
  await page.locator("#messageInput").fill(message);
  await page.locator("#sendButton").click();
  await page.waitForFunction(() => !state.busy && !state.chatServerOperationId && !state.chatRequest, null, { timeout: TURN_MS });
  await page.waitForFunction((count) => document.querySelectorAll("#messages .message.assistant").length > count, before, { timeout: 30000 });
  return answers.last().locator(".message-changes");
}

async function runActions(page, report) {
  const start = await readPlan(page);
  report(`info plan holds ${start.size} units`);
  const receipt = await askCoach(page, MESSAGES.ask);
  if (await receipt.count()) throw new Stop("question turn carries a change receipt");
  if (!samePlan(start, await readPlan(page))) throw new Stop("plan changed by the question turn");
  report("ok question: no change receipt, plan unchanged");
}

async function logout(page) {
  return page.evaluate(async () => {
    const token = document.cookie.split("; ").find((item) => item.startsWith("ic_csrf="))?.slice(8) || "";
    const response = await fetch("/api/logout", { method: "POST", headers: { "X-CSRF-Token": decodeURIComponent(token), "Content-Type": "application/json" }, body: "{}" });
    return response.status;
  });
}

// Returns the exit code; every line goes through report.
export async function run({ chromium, url, password }, report = console.log) {
  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL: url, viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on("console", (message) => message.type() === "error" && errors.push("console"));
  page.on("pageerror", () => errors.push("page"));
  page.on("response", (response) => response.status() >= 500 && errors.push(`http ${response.status()}`));
  let code = 0;
  let loggedIn = false;
  try {
    try {
      await page.goto("/");
      await page.getByLabel("Passwort", { exact: true }).fill(password);
      await page.getByRole("button", { name: "Anmelden" }).click();
      await page.locator("#appShell").waitFor({ state: "visible", timeout: 30000 });
      loggedIn = true;
    } catch {
      report("FAIL cannot reach the app or log in");
      return 2;
    }
    await runActions(page, report);
    if (errors.length) throw new Stop(`${errors.length} browser error(s) during the actions`);
    report("ok no browser errors");
  } catch (error) {
    // Only our own check texts are printed; library errors may quote page content.
    const reason = error instanceof Stop ? error.message : `stopped (${error.name})`;
    report(`FAIL ${reason}`);
    code = 1;
  } finally {
    if (loggedIn) {
      const status = await logout(page).catch(() => 0);
      report(`${status === 200 ? "ok" : "FAIL"} logout ${status}`);
      if (status !== 200) code = 1;
    }
    await browser.close();
  }
  return code;
}

async function main() {
  let input;
  try {
    input = parseInput(process.argv.slice(2));
  } catch (error) {
    // A file-system error names a path; never print it.
    console.log(`FAIL ${error.syscall ? SECRET_MISSING : error.message}`);
    return 2;
  }
  const { chromium } = await import("@playwright/test");
  return run({ chromium, ...input });
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
