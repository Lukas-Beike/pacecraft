// Headless read-only walk through the running app at phone and desktop width.
//
//   node scripts/ui_walk.mjs --url <app address> --password-file <secret file>
//     --out-dir <new scratchpad dir> [--compare <previous ui-walk.json>]
//
// Logs in with the password read from the file (never printed or passed on a
// command line), opens each main view, switches the calendar between week and
// month and pages it, takes screenshots, measures time to rendered content,
// runs the sub-function STEPS of each view (open, assert non-empty, time),
// records the service-worker cache name and asset versions, and logs out.
// It presses no control that saves, syncs, deletes or sends. Exit 0 no error,
// 1 a view or step failed or logged errors, 2 bad input or login refused.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const VIEWS = ["coach", "plan", "analysis", "nutrition", "more"];
export const WIDTHS = { phone: { width: 412, height: 915 }, desktop: { width: 1280, height: 900 } };

// The password file is only read: the temp directory or the folder where a T3
// secret request delivers files, never the repository (#2527).
export const T3_SECRETS = path.join(os.homedir(), ".t3", "userdata", "secrets");
// Links are followed and Windows paths compare without case, as in prod_client.py.
// A path that does not exist yet resolves through its nearest existing parent, so
// a new directory under a link is judged by where the link leads (#2615).
const realPath = (target) => {
  const resolved = path.resolve(target);
  try {
    return fs.realpathSync.native(resolved);
  } catch (error) {
    // Only a missing path resolves through its parent; a dangling link, a loop or an
    // unreadable path resolves to nothing, which no guard accepts.
    if (error.code !== "ENOENT" || fs.lstatSync(resolved, { throwIfNoEntry: false })) return "";
    const parent = path.dirname(resolved);
    return parent === resolved ? resolved : path.join(realPath(parent), path.basename(resolved));
  }
};
const folded = (target) => (process.platform === "win32" ? target.toLowerCase() : target) + path.sep;

// The path as given and the file a link leads to must both lie in one of the
// roots and outside the repository; a path that does not exist yet is judged
// through its nearest existing parent (#2615).
function confined(target, roots, refused) {
  const resolved = path.resolve(os.tmpdir(), String(target));
  const repository = [folded(ROOT), folded(realPath(ROOT))];
  const checks = [
    [resolved, roots.map((root) => path.resolve(root))],
    [realPath(resolved), roots.map(realPath)],
  ];
  for (const [candidate, bases] of checks) {
    const where = folded(candidate);
    if (repository.some((repo) => where.startsWith(repo)) || !bases.some((base) => where.startsWith(folded(base)))) {
      throw refused;
    }
  }
  return resolved;
}

// Every file the walk reads or writes lives in the system temp directory (the
// session scratchpad), never in the repository or anywhere else a path could reach.
export function scratchPath(target) {
  return confined(target, [os.tmpdir()], new Error("files must be in the system temp directory"));
}

// Writes one file of the walk by its fixed name (#2661): the out-dir is
// checked again right before the write and the file is created exclusively, so
// any entry already at the name (a file, a link or a hard link) stops the walk
// instead of being followed. Each walk needs a fresh --out-dir.
export const FRESH_OUT_DIR = "the out-dir already holds walk files; use a fresh --out-dir";
const stopLine = (error, fallback) => `FAIL ${error.code === "EEXIST" ? FRESH_OUT_DIR : fallback}`;
export function writeFresh(outDir, name, data) {
  const file = path.join(scratchPath(outDir), name);
  fs.writeFileSync(scratchPath(file), data, { flag: "wx" });
}

export function secretPath(target, secrets = T3_SECRETS) {
  if (!path.isAbsolute(String(target))) throw new Error("the password file path must be absolute");
  const refused = new Error("the password file must be in the system temp directory or the T3 secret folder");
  return confined(target, [os.tmpdir(), secrets], refused);
}

export function baseUrl(text) {
  const url = new URL(text);
  if (!["http:", "https:"].includes(url.protocol)) throw new Error("the app address must be an http(s) URL");
  return url.origin;
}

// The cache name and asset versions are content hashes the server stamps.
export function releaseMarks(serviceWorker, indexHtml) {
  const cache = /const CACHE = "([^"]+)"/.exec(serviceWorker)?.[1] || null;
  const versions = [...indexHtml.matchAll(/\?v=([A-Za-z0-9_-]+)/g)].map((match) => match[1]);
  return { cache, assetVersions: [...new Set(versions)].sort((a, b) => a.localeCompare(b)) };
}

// How load times are measured; walks measured another way are not compared (#2581).
export const LOAD_MEASURE = "ready";
const EVENT_STREAM = "GET /api/state/events";
const READY_CAP_MS = 15000;
const QUIET_MS = 300;

export function compareLines(current, previous) {
  const lines = [];
  if (!previous) return lines;
  const same = current.marks.cache === previous.marks?.cache;
  lines.push(`info service-worker cache ${same ? "unchanged" : "changed"} since the previous walk`);
  // A step that passed before and is skipped now lost its data or its function: a finding, whatever the reason.
  for (const [key, step] of Object.entries(current.steps || {})) {
    const before = previous.steps?.[key];
    if (before?.status === "ok" && step.status === "skip") lines.push(`FAIL step ${key} passed in the previous walk, skipped now (${step.why})`);
    else if (before?.status === "ok" && step.status === "ok") lines.push(`info step ${key} ${step.ms} ms (was ${before.ms} ms)`);
  }
  if (previous.measure !== current.measure) {
    lines.push("info load times not compared: the previous walk measured them differently");
    return lines;
  }
  for (const [key, ms] of Object.entries(current.loadMs)) {
    const before = previous.loadMs?.[key];
    if (typeof before === "number") lines.push(`info load ${key} ${ms} ms (was ${before} ms)`);
  }
  return lines;
}

// Method and path of a request (no query or body); other hosts keep their origin, non-http(s) is skipped.
export function requestKey(method, url, appOrigin) {
  const parsed = new URL(url);
  if (!["http:", "https:"].includes(parsed.protocol)) return null;
  return `${method} ${parsed.origin === appOrigin ? "" : parsed.origin}${parsed.pathname}`;
}

// A view is loaded once its active panel shows no visible loading marker and no
// request started for it has been open for QUIET_MS (the event stream stays open
// by design). Network idle never came on the Coach view since 2.1.6 (#2581).
async function rendered(page, route, open) {
  const before = new Set(open.keys());
  const started = Date.now();
  await page.evaluate(async ([next, cap]) => {
    const done = new Promise((resolve) => {
      document.addEventListener("app:route-rendered", resolve, { once: true });
      setTimeout(resolve, cap);
    });
    if (globalThis.location.hash === `#${next}`) globalThis.location.hash = "";
    globalThis.location.hash = next;
    await done;
  }, [route, READY_CAP_MS]);
  const pending = () => [...new Set([...open].filter(([request, key]) => !before.has(request) && key !== EVENT_STREAM).map(([, key]) => key))];
  let quietSince = null;
  while (Date.now() - started < READY_CAP_MS) {
    const busy = await page.evaluate(() => {
      const panel = document.querySelector(".panel.active");
      return !panel || [...panel.querySelectorAll(".loading-state, [aria-busy='true'], .skeleton")].some((node) => node.checkVisibility());
    }).catch(() => true);
    if (busy || pending().length) quietSince = null;
    else if (quietSince === null) quietSince = Date.now();
    else if (Date.now() - quietSince >= QUIET_MS) return { ms: quietSince - started, capped: false, open: [] };
    await page.waitForTimeout(50);
  }
  return { ms: Date.now() - started, capped: true, open: pending() };
}

// Views render asynchronously: wait briefly for a visible control.
async function clickIfThere(locator) {
  const target = locator.filter({ visible: true }).first();
  try {
    await target.waitFor({ state: "visible", timeout: 3000 });
  } catch {
    return false;
  }
  await target.click();
  return true;
}

async function walkCalendar(page, shot) {
  const done = [];
  // Open a unit first: paging later may land on an empty week.
  if (await clickIfThere(page.locator(".planned-entry > summary"))) {
    await shot("plan-unit");
    done.push("unit opened");
  }
  if (await clickIfThere(page.locator('.planned-view-switch button[data-view="month"]'))) {
    await shot("plan-month");
    done.push("month");
    await clickIfThere(page.getByRole("button", { name: "Nächster Monat" }));
    await clickIfThere(page.locator('.planned-view-switch button[data-view="week"]'));
  }
  if (await clickIfThere(page.getByRole("button", { name: "Nächste Woche" }))) done.push("next week");
  if (await clickIfThere(page.getByRole("button", { name: "Vorherige Woche" }))) done.push("previous week");
  return done;
}

// One declarative step per sub-function (#3009): optional `pre` opens (once), `each` names candidates
// tried in turn (up to MAX_CANDIDATES), `then` opens inside the candidate; the step is ok once `ready`
// shows a visible element that is not empty by `kind` (chart: drawn marks, numbers: a digit,
// options: a select with two or more options, text: any). `absent` = [scope, text] marks a candidate
// whose data does not exist; a visible `never` element fails the step once it is ready. `optional`
// steps depend on recorded data: with none they are skipped, and a later walk compares ok before /
// skipped now as a FAIL. A new user-visible function adds its step here.
// Controls are limited to opening, expanding and paging ones (isOpener); the label guard stops the rest.
export const STEP_KINDS = ["chart", "numbers", "options", "text"];
const OPENERS = [
  /(^|\s)summary$/,
  /^a\[data-(analysis|more|nutrition|plan)-segment="[a-z]+"\]$/,
  /^\.planned-view-switch button\[data-view="(week|month)"\]$/,
  /^\.planned-month-day:not\(\[disabled\]\)(:has\(\.planned-month-mark\.is-[a-z]+\))?$/,
  /^button\[aria-label="(Nächste|Vorherige) Woche"\]$/,
];
const FORBIDDEN = /speicher|synchronis|send|lösch|exportier|rückgängig|einstellung|herunterlad|undo|save|delete|export/i;
export const isOpener = (selector) => OPENERS.some((pattern) => pattern.test(selector));
export const refusedLabel = (label) => FORBIDDEN.test(label || "");
const STEP_CAP_MS = 8000;
const CANDIDATE_CAP_MS = 2000;
const MAX_CANDIDATES = 14;
const DETAILS_OPEN = ".planned-entry[open] .planned-activity-more > summary";
const NO_DETAILS = [".planned-entry[open] .activity-analysis-panel", "Detaildaten noch nicht geladen"];
const NO_SESSION = "no such session";
// The longest one step may take over all its candidates.
const STEP_TOTAL_MS = 30000;
// The longest a step waits for the app's data loads to end before it starts.
const SETTLE_MS = 15000;
// Runs in the page: no data load of the app runs (a page without the app is always settled).
const settled = () => !globalThis.AppState || (!globalThis.AppState.state.loadPromise && !globalThis.AppState.state.loadingAreas?.size);
// Runs in the page: a data load runs or the shown view marks itself as loading (2.1.26 cold start).
const loading = () => Boolean(globalThis.AppState?.state.loadPromise || globalThis.AppState?.state.loadingAreas?.size)
  || [...document.querySelectorAll(".panel.active :is(.loading-state, [aria-busy='true'], .skeleton)")].some((node) => node.checkVisibility());
// While the view shows it is loading, an element is waited for up to this long instead of the step's cap.
const LOADING_CAP_MS = 60000;
const DONE = ".planned-entry.is-completed > summary";
// Today's unit is marked is-today; on a Sunday it is the week's only upcoming one.
const PLANNED = ".planned-entry:is(.is-planned, .is-today) > summary";
const MONTH = '.planned-view-switch button[data-view="month"]';
const BACK = 'button[aria-label="Vorherige Woche"]';
const NEXT = 'button[aria-label="Nächste Woche"]';
// How many weeks a `seek` step pages at most; `unseek` pages back as often afterwards.
const SEEK_WEEKS = 6;
// A step that needs a kind of unit looks for one that has its element, week by week, and returns.
const EARLIER = { seek: BACK, unseek: NEXT };
const LATER = { seek: NEXT, unseek: BACK };
const tab = (area, segment) => `a[data-${area}-segment="${segment}"]`;
const detail = (name, covers, ready, kind, extra) => ({ view: "plan", name, covers, each: DONE, ...EARLIER, opens: [DETAILS_OPEN], ready, kind, absent: NO_DETAILS, ...extra });
export const STEPS = [
  { view: "coach", name: "Coach: Verlauf", covers: "Coach history", ready: "#messages [data-message-id]", kind: "text" },
  // A planned unit with equipment, with "Löschen" or with fuelling advice may lie in a later week (2.1.26).
  { view: "plan", name: "Kalender: Schuh/Rad geplant", covers: "#3011 planned equipment chip", each: PLANNED, ...LATER, ready: '.planned-entry[open] .planned-equipment-chip[aria-label*=": "]', kind: "text", optional: true },
  { view: "plan", name: "Kalender: geplante Einheit", covers: "#2189 planned session", pre: ['button[aria-label="Nächste Woche"]'], each: PLANNED, ready: ".planned-entry[open] .planned-entry-details", kind: "text", optional: true, post: ['button[aria-label="Vorherige Woche"]'] },
  // Read-only: the walk checks that "Löschen" is there, it never presses it (#3027).
  { view: "plan", name: "Kalender: Löschen", covers: "#3027 calendar delete", each: PLANNED, ...LATER, ready: ".planned-entry[open] .planned-activity-actions .danger-button", kind: "text", optional: true },
  { view: "plan", name: "Kalender: Verpflegung", covers: "#2921 fuelling", each: PLANNED, ...LATER, ready: ".planned-entry[open] .planned-fuelling", kind: "numbers", optional: true },
  // Choosing a past day narrows the month's range, so the planned day comes first.
  { view: "plan", name: "Monat: geplante Einheit", covers: "#2189 month", pre: [MONTH], each: ".planned-month-day:not([disabled]):has(.planned-month-mark.is-planned)", opens: [PLANNED], ready: ".planned-entry[open] .planned-entry-details", kind: "text", optional: true },
  { view: "plan", name: "Monat: absolvierte Einheit", covers: "#2189 month", pre: [MONTH], each: ".planned-month-day:not([disabled]):has(.planned-month-mark.is-completed)", opens: [DONE], ready: ".planned-entry[open] .planned-entry-details", kind: "text" },
  { view: "plan", name: "Kalender: Wochenansicht", covers: "#2189 week and month switch", pre: ['.planned-view-switch button[data-view="week"]'], ready: ".planned-week .planned-day", kind: "text" },
  // Read-only as well: the library card offers "Einplanen" and "Löschen"; the walk presses neither (#3028).
  { view: "plan", name: "Bibliothek: Einplanen, Löschen", covers: "#3028 library actions", pre: [tab("plan", "library")], opens: ["#library details.library-sport > summary"], ready: "#library .library-card .planned-activity-actions .danger-button", kind: "text", optional: true, post: [tab("plan", "overview")] },
  // Completed units: on a Monday the current week has none yet, so each step looks back for a unit that
  // has its element (`seek`) and returns to the current week (`unseek`).
  { view: "plan", name: "Kalender: Einheit öffnen", covers: "#2189 calendar agenda and detail", each: DONE, ...EARLIER, ready: ".planned-entry[open] .planned-entry-details", kind: "text" },
  detail("Kalender: Details", "#2258 session details", ".planned-entry[open] .activity-analysis-stats", "numbers"),
  // The watch's temperature sensor reads body-warmed values, so no session charts it (#3036).
  detail("Kalender: Diagramm", "#2258 session charts", '.planned-entry[open] .activity-analysis-panel svg[role="img"]', "chart", { never: '.planned-entry[open] .activity-analysis-panel svg[aria-label^="Temperatur"]' }),
  detail("Kalender: Zonenbalken", "#2258 time in zones", ".planned-entry[open] .activity-analysis-zones", "numbers", { optional: true }),
  // The chip shows "…" until its read; once loaded its label names the choice ("Rad: …").
  { view: "plan", name: "Kalender: Schuh/Rad", covers: "#3011 equipment chip", each: DONE, ...EARLIER, ready: '.planned-entry[open] .planned-equipment-chip[aria-label*=": "]', kind: "text", optional: true },
  // A week with a run that shows running dynamics may lie further back.
  detail("Kalender: Laufdynamik", "#2910 running dynamics", ".planned-entry[open] .activity-dynamics-value", "numbers", { optional: true, each: ".planned-entry.is-completed[data-sport='Laufen'] > summary" }),
  // The sport cards are tables: a measured value is a cell with a digit; a missing one is marked and has none.
  ...[["running", "Laufen", "#runningBestEfforts td:not(.value-missing)", "numbers"], ["cycling", "Radfahren", "#cyclingPowerProfile td:not(.value-missing)", "numbers"], ["load", "Belastung", "#analysisLoadCharts svg", "chart"], ["recovery", "Erholung", "#personalRecovery svg", "chart"]]
    .map(([segment, label, ready, kind]) => ({ view: "analysis", name: `Analyse: ${label}`, covers: "Analyse tabs", pre: [tab("analysis", segment)], ready, kind })),
  { view: "analysis", name: "Analyse: Woche", covers: "#3000 week review", pre: [tab("analysis", "week")], ready: "#weekReview .week-review-card", kind: "numbers" },
  { view: "nutrition", name: "Ernährung: Verlauf", covers: "Nutrition week", pre: [tab("nutrition", "week")], ready: "#nutritionWeek", kind: "numbers" },
  // The skinfold card's edit icon is an svg too, so the chart step skips that card (#3099).
  { view: "nutrition", name: "Ernährung: Körper", covers: "Body analysis", pre: [tab("nutrition", "body")], ready: "#bodyAnalysisCharts .analysis-chart-card:not([data-skinfolds]) svg", kind: "chart" },
  { view: "nutrition", name: "Ernährung: Hautfalten", covers: "#3099 skinfold card", pre: [tab("nutrition", "body")], ready: "#bodyAnalysisCharts [data-skinfolds] tbody tr", kind: "text" },
  { view: "more", name: "Mehr: Ausrüstung", covers: "#2935 equipment", pre: [tab("more", "equipment")], ready: "#equipmentItems .equipment-card", kind: "text" },
  { view: "more", name: "Mehr: Ausrüstung Standard", covers: "#3014 default equipment", pre: [tab("more", "equipment")], ready: "#equipmentItems .equipment-rules select", kind: "options" },
  // The current release's entry: a listed change, or only its collapsed "Technik (n)" (2.1.20).
  { view: "more", name: "Mehr: Betrieb, Änderungen", covers: "#2908 changelog", pre: [tab("more", "operations")], ready: ".release-changelog > ul > li, .release-changelog > .release-technical > summary", kind: "text", optional: true },
];

// Runs in the page; "ready", "shown" (a `never` element), "absent" or "" (not yet).
function probe([selector, kind, absent, never]) {
  const blank = (paint) => !paint || paint === "none" || paint === "transparent" || paint === "rgba(0, 0, 0, 0)";
  const drawn = (svg) => [...svg.querySelectorAll("path[d], rect, circle, polyline")].some((mark) => {
    const style = getComputedStyle(mark);
    if (style.visibility === "hidden" || Number(style.opacity) === 0 || mark.closest('[aria-hidden="true"]')) return false;
    if (blank(style.fill) && blank(style.stroke)) return false;
    const box = mark.getBBox();
    return box.width > 0 || box.height > 0;
  });
  const filled = {
    chart: (el) => (el.matches("canvas") ? el.width > 0 && el.height > 0 : [...(el.matches("svg") ? [el] : el.querySelectorAll("svg"))].some(drawn)),
    numbers: (el) => /\d/.test(el.textContent),
    options: (el) => (el.matches("select") ? el : el.querySelector("select"))?.options.length >= 2,
    text: (el) => el.textContent.trim().length > 0,
  }[kind];
  const shown = (query) => [...document.querySelectorAll(query)].filter((el) => el.checkVisibility());
  if (shown(selector).some((el) => filled(el))) return never && shown(never).length ? "shown" : "ready";
  return absent && shown(absent[0]).some((el) => el.textContent.includes(absent[1])) ? "absent" : "";
}

async function press(page, selector, nth = 0) {
  if (!isOpener(selector)) throw new RangeError("not an opening control");
  const target = page.locator(selector).filter({ visible: true }).nth(nth);
  await target.waitFor({ state: "visible", timeout: 3000 });
  const { label, open } = await target.evaluate((el) => ({ label: el.tagName === "SUMMARY" ? "" : el.getAttribute("aria-label") ?? el.textContent, open: el.tagName === "SUMMARY" && el.parentElement.open }));
  if (refusedLabel(label)) throw new RangeError("refused control");
  if (!open) await target.click();
  // A tab counts as open once it is the current one, so the previous tab's content never satisfies the step.
  if (selector.startsWith("a[")) await page.locator(`${selector}[aria-current="page"]`).filter({ visible: true }).first().waitFor({ timeout: 3000 });
}

// Views render asynchronously: give the first candidate a moment to appear.
async function candidateCount(page, each) {
  if (!each) return 1;
  const candidates = page.locator(each).filter({ visible: true });
  await candidates.first().waitFor({ timeout: 3000 }).catch(() => null);
  return Math.min(await candidates.count(), MAX_CANDIDATES);
}

// The step's element, waited for while the view shows it is loading (up to LOADING_CAP_MS), else for `timeout`.
async function waitReady(page, step, timeout) {
  const deadline = Date.now() + LOADING_CAP_MS;
  for (;;) {
    const outcome = await page.waitForFunction(probe, [step.ready, step.kind, step.absent || null, step.never || null], { timeout, polling: 50 }).then((handle) => handle.jsonValue()).catch(() => "empty");
    if (outcome !== "empty" || Date.now() > deadline || !(await page.evaluate(loading))) return outcome;
  }
}

// One week's candidates: { result } once one is ready or shows a `never` element, else the counts.
async function tryWeek(page, step, capMs, began) {
  const count = await candidateCount(page, step.each);
  let empty = 0;
  for (let index = 0; index < count && Date.now() - began < STEP_TOTAL_MS; index += 1) {
    const started = Date.now();
    const outcome = await tryCandidate(page, step, index, capMs);
    const result = { ready: { status: "ok" }, shown: { status: "fail", why: `shows ${step.never}` } }[outcome];
    if (result) return { result: { ...result, ms: Date.now() - started }, count, empty };
    if (outcome === "empty") empty += 1;
  }
  return { count, empty };
}

// Opens candidate `index` with the other candidates closed, so the controls pressed and the element checked
// are its own. "ready", "shown", "absent", "empty", or "passed over" (an optional step's candidate without the control).
async function tryCandidate(page, step, index, capMs) {
  try {
    if (step.each) {
      await page.locator(step.each).evaluateAll((items) => items.forEach((item) => item.closest("details")?.removeAttribute("open")));
      await press(page, step.each, index);
    }
    for (const selector of step.opens || []) await press(page, selector);
  } catch (error) {
    // Any other step fails.
    if (error.name !== "TimeoutError" || !step.optional) throw error;
    return "passed over";
  }
  return waitReady(page, step, step.each ? Math.min(CANDIDATE_CAP_MS, capMs) : capMs);
}

// One step: { status: ok|fail|skip, ms (interaction to ready, of the candidate that passed), why }.
export async function runStep(page, { pre = [], post = [], unseek, ...step }, capMs = STEP_CAP_MS) {
  // After a restart the app reloads its data as each start sync ends, and a reload re-renders the view:
  // a step starts once no load runs, so it checks the cards it opens, not ones a reload replaces (2.1.25).
  await page.waitForFunction(settled, null, { timeout: SETTLE_MS, polling: 100 }).catch(() => null);
  const began = Date.now();
  let paged = 0;
  try {
    for (const selector of pre) await press(page, selector);
    // A `seek` step tries the next week (at most SEEK_WEEKS) while no candidate has its element.
    let count = 0;
    let empty = 0;
    for (;;) {
      const week = await tryWeek(page, step, capMs, began);
      if (week.result) return week.result;
      count += week.count;
      empty += week.empty;
      if (!step.seek || paged === SEEK_WEEKS || Date.now() - began >= STEP_TOTAL_MS) break;
      await press(page, step.seek);
      paged += 1;
    }
    // Nothing loaded and nothing empty: the data this step needs does not exist (info, not a failure).
    if (!empty) return { status: "skip", ms: 0, why: count ? "no stored data" : NO_SESSION };
    return step.optional ? { status: "skip", ms: 0, why: `none of ${count} has it` } : { status: "fail", ms: Date.now() - began, why: "missing or empty" };
  } catch (error) {
    return { status: "fail", ms: Date.now() - began, why: error.name === "TimeoutError" ? "control missing" : error.message.split("\n")[0] };
  } finally {
    // Undo the step's navigation (such as paging to next week), so later steps start where they expect.
    for (let back = 0; back < paged; back += 1) await press(page, unseek).catch(() => null);
    for (const selector of post) await press(page, selector).catch(() => null);
  }
}

async function walk({ chromium, url, password, outDir }) {
  const result = { measure: LOAD_MEASURE, loadMs: {}, notReady: {}, errors: {}, calendar: {}, steps: {}, marks: null, logout: {} };
  const browser = await chromium.launch();
  try {
    for (const [name, viewport] of Object.entries(WIDTHS)) {
      const context = await browser.newContext({ baseURL: url, viewport });
      const page = await context.newPage();
      const errors = [];
      page.on("console", (message) => message.type() === "error" && errors.push("console"));
      page.on("pageerror", () => errors.push("page"));
      page.on("response", (response) => response.status() >= 500 && errors.push(`http ${response.status()}`));
      // Paths only (no query or body) of requests still open when a view is ready; the event stream stays open by design.
      const open = new Map();
      page.on("request", (request) => {
        const key = requestKey(request.method(), request.url(), new URL(url).origin);
        if (key) open.set(request, key);
      });
      for (const event of ["requestfinished", "requestfailed"]) page.on(event, (request) => open.delete(request));
      await page.goto("/");
      await page.getByLabel("Passwort", { exact: true }).fill(password);
      await page.getByRole("button", { name: "Anmelden" }).click();
      await page.locator("#appShell").waitFor({ state: "visible", timeout: 30000 });
      // Every error counts against a step: login, a view (with its calendar walk), or the rest.
      result.errors[`${name} login`] = errors.length;
      let counted = errors.length;
      const shot = async (label) => writeFresh(outDir, `${name}-${label}.png`, await page.screenshot({ fullPage: false }));
      for (const route of VIEWS) {
        const view = await rendered(page, route, open);
        result.loadMs[`${name} ${route}`] = view.ms;
        if (view.capped) result.notReady[`${name} ${route}`] = view.open;
        await shot(route);
        // The steps start in the calendar's initial week; the calendar walk pages away from it.
        for (const step of STEPS.filter((item) => item.view === route)) result.steps[`${name} ${step.name}`] = { ...(await runStep(page, step)), covers: step.covers };
        if (route === "plan") result.calendar[name] = await walkCalendar(page, shot);
        result.errors[`${name} ${route}`] = errors.length - counted;
        counted = errors.length;
      }
      result.marks ??= releaseMarks(await (await page.request.get("/service-worker.js")).text(), await (await page.request.get("/")).text());
      const logout = await page.evaluate(async () => {
        const token = document.cookie.split("; ").find((item) => item.startsWith("ic_csrf="))?.slice(8) || "";
        const response = await fetch("/api/logout", { method: "POST", headers: { "X-CSRF-Token": decodeURIComponent(token), "Content-Type": "application/json" }, body: "{}" });
        return response.status;
      });
      result.logout[name] = logout;
      result.errors[`${name} other`] = errors.length - counted;
      await context.close();
    }
  } finally {
    await browser.close();
  }
  return result;
}

const STEP_LINE = {
  ok: (key, step) => `ok ${key} ${step.ms} ms`,
  skip: (key, step) => `info ${key} skipped: ${step.why}`,
  fail: (key, step) => `FAIL ${key} ${step.ms} ms, ${step.why} (covers ${step.covers})`,
};

export function reportLines(result) {
  const lines = [];
  for (const [key, ms] of Object.entries(result.loadMs)) {
    const errors = result.errors[key];
    lines.push(errors ? `FAIL ${key} ${ms} ms, ${errors} error(s)` : `ok ${key} ${ms} ms`);
  }
  for (const [key, errors] of Object.entries(result.errors)) {
    if (errors && !(key in result.loadMs)) lines.push(`FAIL ${key} ${errors} error(s)`);
  }
  for (const [key, requests] of Object.entries(result.notReady || {})) {
    lines.push(`info ${key} not ready within 15 s; still loading: ${requests.join(", ") || "only a loading marker"}`);
  }
  for (const [key, step] of Object.entries(result.steps || {})) lines.push(STEP_LINE[step.status](key, step));
  for (const [name, steps] of Object.entries(result.calendar)) lines.push(`info calendar ${name}: ${steps.join(", ") || "no controls found"}`);
  lines.push(`info service-worker cache ${result.marks?.cache || "missing"}, ${result.marks?.assetVersions.length || 0} asset versions`);
  for (const [name, status] of Object.entries(result.logout || {})) lines.push(`${status === 200 ? "ok" : "FAIL"} logout ${name} ${status}`);
  return lines;
}

async function main() {
  const { values } = parseArgs({
    options: { url: { type: "string" }, "password-file": { type: "string" }, "out-dir": { type: "string" }, compare: { type: "string" } },
  });
  let url;
  let outDir;
  let password;
  try {
    if (!values.url || !values["password-file"] || !values["out-dir"]) throw new Error("--url, --password-file and --out-dir are required");
    url = baseUrl(values.url);
    outDir = scratchPath(values["out-dir"]);
    password = fs.readFileSync(secretPath(values["password-file"]), "utf8").trim();
    if (!password) throw new Error("the secret file is empty");
  } catch (error) {
    // A file-system error names the secret file's path; never print it.
    console.log(`FAIL ${error.syscall ? "cannot read the secret file" : error.message}`);
    return 2;
  }
  try {
    fs.mkdirSync(outDir, { recursive: true });
  } catch {
    console.log("FAIL cannot create the out-dir");
    return 2;
  }
  let previous = null;
  try {
    previous = values.compare ? JSON.parse(fs.readFileSync(scratchPath(values.compare), "utf8")) : null;
  } catch {
    // The error text could quote the file's content; never print it.
    console.log("FAIL cannot read the previous walk");
    return 2;
  }
  const { chromium } = await import("@playwright/test");
  let result;
  try {
    result = await walk({ chromium, url, password, outDir });
  } catch (error) {
    console.log(stopLine(error, `walk stopped (${error.name})`));
    return 2;
  }
  try {
    writeFresh(outDir, "ui-walk.json", JSON.stringify(result, null, 2));
  } catch (error) {
    console.log(stopLine(error, `cannot write the walk result (${error.code})`));
    return 2;
  }
  const lines = [...reportLines(result), ...compareLines(result, previous)];
  for (const line of lines) console.log(line);
  return lines.some((line) => line.startsWith("FAIL")) ? 1 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
