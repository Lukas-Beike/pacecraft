// Headless read-only walk through the running app at phone and desktop width.
//
//   node scripts/ui_walk.mjs {--url <app address> | --url-file <file>} --password-file <secret file>
//     --out-dir <new scratchpad dir> [--compare <previous walk's out-dir or its ui-walk.json>]
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

// --compare takes the previous walk's out-dir or its ui-walk.json (#3346).
export function previousWalkPath(compare) {
  const given = scratchPath(compare);
  return fs.statSync(given, { throwIfNoEntry: false })?.isDirectory() ? scratchPath(path.join(given, "ui-walk.json")) : given;
}

// Writes one file of the walk by its fixed name (#2661): the out-dir is
// checked again right before the write and the file is created exclusively, so
// any entry already at the name (a file, a link or a hard link) stops the walk
// instead of being followed. Each walk needs a fresh --out-dir.
export const FRESH_OUT_DIR = "the out-dir already holds walk files; use a fresh --out-dir";
const stopLine = (error, fallback) => `FAIL ${error.stopText ?? (error.code === "EEXIST" ? FRESH_OUT_DIR : fallback)}`;
export function writeFresh(outDir, name, data) {
  const file = path.join(scratchPath(outDir), name);
  fs.writeFileSync(scratchPath(file), data, { flag: "wx" });
}

// Names the fix, the release skill's request step (#3354); no path or content.
export const SECRET_MISSING = "secret missing, empty or unreadable: request a new one with request_secret (file delivery)";
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

// The app address from --url, or from --url-file's one line in the scratchpad (#4125): a file keeps the address out
// of the command, which no substitution may bring in. The address is never printed.
export function appUrl(values) {
  if (Boolean(values.url) === Boolean(values["url-file"])) throw new Error("give exactly one of --url and --url-file");
  if (values.url) return baseUrl(values.url);
  const file = scratchPath(values["url-file"]);
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    // A fixed message: the callers map an fs error to their own wording (secret, out-dir).
    throw new Error("cannot read --url-file");
  }
  const lines = text.trim().split(/\r?\n/);
  if (lines.length !== 1 || !lines[0].trim()) throw new Error("--url-file must hold one URL");
  return baseUrl(lines[0].trim());
}

// The cache name and asset versions are content hashes the server stamps.
export function releaseMarks(serviceWorker, indexHtml) {
  const cache = /const CACHE = "([^"]+)"/.exec(serviceWorker)?.[1] || null;
  const versions = [...indexHtml.matchAll(/\?v=([A-Za-z0-9_-]+)/g)].map((match) => match[1]);
  return { cache, assetVersions: [...new Set(versions)].sort((a, b) => a.localeCompare(b)) };
}

// How load times are measured; walks measured another way are not compared (#2581);
// since #3969 a loading text line also counts as not ready.
export const LOAD_MEASURE = "ready-text";
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
    // A release skip (an older release, e.g. after a rollback) lost nothing (#3886).
    if (before?.status === "ok" && step.status === "skip" && !step.release) lines.push(`FAIL step ${key} passed in the previous walk, skipped now (${step.why})`);
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

// A view is loaded once its active panel shows no visible loading marker or
// "… wird geladen…" line (some views load behind a text line, #3969) and no
// request started for it has been open for QUIET_MS (the event stream stays open
// by design). Network idle never came on the Coach view since 2.1.6 (#2581).
export async function rendered(page, route, open) {
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
      // A loading line is a text element of its own, not a button label or a Coach message quoting one.
      const line = (node) => !node.children.length && node.textContent.trim().endsWith("wird geladen…") && !node.closest("button, #messages");
      return !panel || [...panel.querySelectorAll(".loading-state, [aria-busy='true'], .skeleton")].some((node) => node.checkVisibility()) || [...panel.querySelectorAll("p, div, span, small")].some((node) => line(node) && node.checkVisibility());
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
// skipped now as a FAIL. A new user-visible function adds its step here, with `since`: the first
// release that carries it, so a walk of an older release skips it instead of failing (#3886).
// Controls are limited to opening, expanding and paging ones (isOpener); the label guard stops the rest.
export const STEP_KINDS = ["chart", "numbers", "options", "text"];
const OPENERS = [
  /(^|\s)summary$/,
  /^a\[data-(analysis|more|nutrition|plan)-segment="[a-z]+"\]$/,
  /^\.planned-view-switch button\[data-view="(week|month)"\]$/,
  /^\.planned-month-day:not\(\[disabled\]\)(:has\(\.planned-month-mark\.is-[a-z]+\))?$/,
  /^button\[aria-label="(Nächste Woche|Vorherige Woche|Nächster Monat|Vorheriger Monat)"\]$/,
  // The library card's "Einplanen" only unfolds and folds the day strip; no day is pressed (#4081).
  /^#library \.library-card \.planned-activity-actions \.secondary-button\[aria-expanded="(false|true)"\]$/,
  // "Alle Details" of an opened session only opens its details dialog; the walk closes it again (#4221).
  /^\.planned-entry\[open\] \.planned-activity-details$/,
];
const FORBIDDEN = /speicher|synchronis|send|lösch|exportier|rückgängig|einstellung|herunterlad|undo|save|delete|export/i;
export const isOpener = (selector) => OPENERS.some((pattern) => pattern.test(selector));
export const refusedLabel = (label) => FORBIDDEN.test(label || "");
const STEP_CAP_MS = 8000;
const CANDIDATE_CAP_MS = 2000;
const MAX_CANDIDATES = 14;
const DETAILS_OPEN = ".planned-entry[open] .planned-activity-details";
const DETAILS_DIALOG = "dialog.activity-detail-dialog[open]";
const NO_DETAILS = [`${DETAILS_DIALOG} .activity-analysis-panel`, "Detaildaten noch nicht geladen"];
const NO_SESSION = "no such session";
// The longest one step may take over all its candidates.
const STEP_TOTAL_MS = 30000;
// The longest a step waits for the app's data loads to end before it starts.
const SETTLE_MS = 15000;
// Runs in the page: no data load of the app runs (a page without the app is always settled).
export const settled = () => !globalThis.AppState || (!globalThis.AppState.state.loadPromise && !globalThis.AppState.state.loadingAreas?.size);
// Runs in the page: a data load runs or the shown view marks itself as loading (2.1.26 cold start);
// a unit's fuelling read marks only its section (2.1.37: 2.0-2.1 s against the candidate cap, #3298).
const loading = () => Boolean(globalThis.AppState?.state.loadPromise || globalThis.AppState?.state.loadingAreas?.size)
  || [...document.querySelectorAll(".panel.active :is(.loading-state, [aria-busy='true'], .skeleton, .planned-fuelling[data-state='loading'])")].some((node) => node.checkVisibility());
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
// Month by month: a planned day of the next month switches the grid there, where no unit is done yet (#4095).
const EARLIER_MONTH = { seek: 'button[aria-label="Vorheriger Monat"]', unseek: 'button[aria-label="Nächster Monat"]' };
const tab = (area, segment) => `a[data-${area}-segment="${segment}"]`;
const detail = (name, covers, ready, kind, extra) => ({ view: "plan", name, covers, each: DONE, ...EARLIER, opens: [DETAILS_OPEN], ready, kind, absent: NO_DETAILS, ...extra });
export const STEPS = [
  { view: "coach", name: "Coach: Verlauf", covers: "Coach history", ready: "#messages [data-message-id]", kind: "text" },
  // The week review left Analyse for a card under the selected week (#3298). It is the first calendar
  // step, while the calendar still shows today; later steps select other weeks and days. A running
  // week without any data yet (no plan, session, recovery value or check-in, early on a Monday) has
  // no card, so the step is optional.
  { view: "plan", name: "Kalender: Wochenrückblick", covers: "#3298 week review (formerly Analyse › Woche, #3000)", pre: ['.planned-view-switch button[data-view="week"]'], ready: '.planned-week .week-review[data-this-week="true"] .week-review-tile', kind: "numbers", optional: true,
    saw: [["still loading", ".week-review .loading-state"], ["nicht verfügbar", ".week-review[data-failed]"], ["plan line instead", ".planned-week > .planned-week-totals"], ["card missing", null]] },
  // Shown only on a morning whose night values are clearly unusual (M14), so the step is optional. The values
  // come with the two answers, "Mir geht's gut" (neutral) and "Ich bin krank" (filled); they are never pressed.
  { view: "plan", name: "Kalender: Infekt-Hinweis", covers: "#3762 infection hint, #3821 answers", pre: ['.planned-view-switch button[data-view="week"]'], ready: ".planned-day.is-today [data-infection-hint]:has(.infection-hint-actions > button.secondary-button + button:not(.secondary-button))", kind: "numbers", optional: true },
  // Read-only: the walk checks that today's day offers the check-in form, it opens none and saves nothing (#3953).
  { view: "plan", name: "Kalender: Check-in", covers: "#3953 daily check-in", pre: ['.planned-view-switch button[data-view="week"]'], ready: ".planned-day.is-today [data-checkin] > button", kind: "text" },
  // A planned unit with equipment, with "Löschen" or with fuelling advice may lie in a later week (2.1.26).
  { view: "plan", name: "Kalender: Schuh/Rad geplant", covers: "#3011 planned equipment chip", each: PLANNED, ...LATER, ready: '.planned-entry[open] .planned-equipment-chip[aria-label*=": "]', kind: "text", optional: true },
  { view: "plan", name: "Kalender: geplante Einheit", covers: "#2189 planned session", pre: ['button[aria-label="Nächste Woche"]'], each: PLANNED, ready: ".planned-entry[open] .planned-entry-details", kind: "text", optional: true, post: ['button[aria-label="Vorherige Woche"]'] },
  // Read-only: the walk checks that "Löschen" is there, it never presses it (#3027).
  { view: "plan", name: "Kalender: Löschen", covers: "#3027 calendar delete", each: PLANNED, ...LATER, ready: ".planned-entry[open] .planned-activity-actions .danger-button", kind: "text", optional: true },
  { view: "plan", name: "Kalender: Verpflegung", covers: "#2921 fuelling", each: PLANNED, ...LATER, ready: ".planned-entry[open] .planned-fuelling", kind: "numbers", optional: true },
  // Only a planned running step test carries its precondition note, so the step is optional (#3833).
  { view: "plan", name: "Kalender: Stufentest geplant", covers: "#3833 running step test", each: ".planned-entry:is(.is-planned, .is-today)[data-sport='Laufen'] > summary", ...LATER, ready: ".planned-entry[open] [data-step-test-note]", kind: "text", optional: true },
  // Choosing a past day narrows the month's range, so the planned day comes first.
  { view: "plan", name: "Monat: geplante Einheit", covers: "#2189 month", pre: [MONTH], each: ".planned-month-day:not([disabled]):has(.planned-month-mark.is-planned)", opens: [PLANNED], ready: ".planned-entry[open] .planned-entry-details", kind: "text", optional: true },
  { view: "plan", name: "Monat: absolvierte Einheit", covers: "#2189 month", pre: [MONTH], each: ".planned-month-day:not([disabled]):has(.planned-month-mark.is-completed)", ...EARLIER_MONTH, opens: [DONE], ready: ".planned-entry[open] .planned-entry-details", kind: "text" },
  { view: "plan", name: "Kalender: Wochenansicht", covers: "#2189 week and month switch", pre: ['.planned-view-switch button[data-view="week"]'], ready: ".planned-week .planned-day", kind: "text" },
  // Read-only as well: the library card offers "Einplanen" and "Löschen"; the walk presses neither (#3028).
  { view: "plan", name: "Bibliothek: Einplanen, Löschen", covers: "#3028 library actions", pre: [tab("plan", "library")], opens: ["#library details.library-sport > summary"], ready: "#library .library-card .planned-activity-actions .danger-button", kind: "text", optional: true, post: [tab("plan", "overview")] },
  // Read-only: the walk checks that "+ Einheit" and "Bearbeiten" are there, it opens no editor and saves nothing (#3951).
  { view: "plan", name: "Bibliothek: Einheit anlegen, bearbeiten", covers: "#3951 library editor", pre: [tab("plan", "library")], opens: ["#library details.library-sport > summary"], ready: "#library .library-toolbar .secondary-button", kind: "text", post: [tab("plan", "overview")] },
  { view: "plan", name: "Bibliothek: Bearbeiten", covers: "#4081 library card edit", pre: [tab("plan", "library")], opens: ["#library details.library-sport > summary"], ready: '#library .library-card .planned-activity-actions > .secondary-button:first-child', kind: "text", optional: true, post: [tab("plan", "overview")] },
  // A unit is planned from the Bibliothek (#4053): "Einplanen" only opens its day strip, no day is pressed (#4081).
  { view: "plan", name: "Bibliothek: Einplanen, Tagesauswahl", covers: "#4081 library Einplanen strip", pre: [tab("plan", "library")], opens: ["#library details.library-sport > summary", '#library .library-card .planned-activity-actions .secondary-button[aria-expanded="false"]'], ready: "#library .library-card .planned-move-days:not([hidden])", kind: "text", optional: true, post: ['#library .library-card .planned-activity-actions .secondary-button[aria-expanded="true"]', tab("plan", "overview")] },
  // The plan choice shows only with an active training plan (#4054).
  { view: "plan", name: "Bibliothek: Einplanen, Trainingsplan", covers: "#4054 Einplanen plan choice", pre: [tab("plan", "library")], opens: ["#library details.library-sport > summary", '#library .library-card .planned-activity-actions .secondary-button[aria-expanded="false"]'], ready: "#library .library-card .library-plan-field:not([hidden]) select", kind: "text", optional: true, saw: [["no active plan", null]], post: [tab("plan", "overview")] },
  // The plan select of "Einplanen" needs an active plan: a plan card that is not archived, completed or cancelled tells a skip
  // of that step from a missing control (#4222).
  { view: "plan", name: "Pläne: aktiver Plan", covers: "#4222 active plan", since: "2.2.30", pre: [tab("plan", "plans")], ready: '#plans .library-card:is([data-plan-status="draft"], [data-plan-status="planned"], [data-plan-status="active"], [data-plan-status="paused"]) h4', kind: "text", optional: true, saw: [["no active plan", null]], post: [tab("plan", "overview")] },
  // Read-only: the walk checks that the Pläne tab shows "+ Plan" and its list, it opens no form (#3950, #4133).
  { view: "plan", name: "Pläne: Plan anlegen", covers: "#4133 Pläne tab", since: "2.2.24", pre: [tab("plan", "plans")], ready: "#plans .library-toolbar .secondary-button", kind: "text", post: [tab("plan", "overview")] },
  // Only a unit with a Garmin copy offers "Aus Garmin entfernen" and the fixture sends nothing to Garmin, so the step is optional; it is never pressed (#4022).
  { view: "plan", name: "Kalender: Aus Garmin entfernen", covers: "#4022 Garmin removal", each: PLANNED, ...LATER, ready: ".planned-entry[open] .planned-garmin-remove", kind: "text", optional: true },
  // Only a competition with a sync conflict shows its choices and the fixture holds none, so the step is optional; neither is pressed (#4021).
  { view: "plan", name: "Wettkämpfe: Konflikt", covers: "#4021 competition conflict", pre: [tab("plan", "season")], ready: ".competition-conflict .planned-activity-actions button", kind: "text", optional: true, post: [tab("plan", "overview")] },
  // Only a plan card offers "Zeitraum ersetzen" and the fixture holds no plan, so the step is optional; the dialog is never opened (#4024).
  { view: "plan", name: "Bibliothek: Zeitraum ersetzen", covers: "#4024 period replacement", pre: [tab("plan", "library")], ready: "#library .library-card .planned-activity-actions button:nth-child(2)", kind: "text", optional: true, post: [tab("plan", "overview")] },
  // Only a stored adaptive preview with changes shows the card, so the step is optional; "Übernehmen" is never pressed (#4020).
  { view: "plan", name: "Kalender: Plananpassung", covers: "#4020 replan card", pre: ['.planned-view-switch button[data-view="week"]'], ready: ".planned-replan .planned-activity-actions .push-button", kind: "text", optional: true },
  // Read-only: the walk checks that "Hinzufügen" is there, it opens no dialog and saves nothing (#3837).
  { view: "plan", name: "Wettkämpfe: Hinzufügen", covers: "#3837 competition entry", pre: [tab("plan", "season")], ready: "#competitionAdd", kind: "text", post: [tab("plan", "overview")] },
  // Completed units: on a Monday the current week has none yet, so each step looks back for a unit that
  // has its element (`seek`) and returns to the current week (`unseek`).
  { view: "plan", name: "Kalender: Einheit öffnen", covers: "#2189 calendar agenda and detail", each: DONE, ...EARLIER, ready: ".planned-entry[open] .planned-entry-details", kind: "text" },
  detail("Kalender: Details", "#2258 session details", `${DETAILS_DIALOG} .activity-analysis-stats`, "numbers"),
  // The watch's temperature sensor reads body-warmed values, so no session charts it (#3036).
  detail("Kalender: Diagramm", "#2258 session charts", `${DETAILS_DIALOG} .activity-analysis-panel svg[role="img"]`, "chart", { never: `${DETAILS_DIALOG} .activity-analysis-panel svg[aria-label^="Temperatur"]` }),
  detail("Kalender: Zonenbalken", "#2258 time in zones", `${DETAILS_DIALOG} .activity-analysis-zones`, "numbers", { optional: true }),
  // The chip shows "…" until its read; once loaded its label names the choice ("Rad: …").
  { view: "plan", name: "Kalender: Schuh/Rad", covers: "#3011 equipment chip", each: DONE, ...EARLIER, ready: '.planned-entry[open] .planned-equipment-chip[aria-label*=": "]', kind: "text", optional: true },
  // The result block of a completed running step test: LT1 and LT2 with heart rate and pace (#3833).
  { view: "plan", name: "Kalender: Stufentest-Ergebnis", covers: "#3833 step test result", each: ".planned-entry.is-completed[data-sport='Laufen'] > summary", ...EARLIER, ready: ".planned-entry[open] .planned-step-test tbody td:nth-child(2)", kind: "numbers", optional: true },
  // A week with a run that shows running dynamics may lie further back.
  // A recognised route needs a repeated outdoor track, so the walk may find none (#3763).
  detail("Kalender: Streckenhistorie", "#3763 route history", `${DETAILS_DIALOG} .activity-route .route-visits tbody tr`, "text", { optional: true, each: ".planned-entry.is-completed[data-sport='Laufen'] > summary" }),
  detail("Kalender: Laufdynamik", "#2910 running dynamics", `${DETAILS_DIALOG} .activity-dynamics-value`, "numbers", { optional: true, each: ".planned-entry.is-completed[data-sport='Laufen'] > summary" }),
  // The sport cards are tables: a measured value is a cell with a digit; a missing one is marked and has none.
  ...[["running", "Laufen", "#currentPerformance table[data-analysis-sport='run'] td", "numbers"], ["cycling", "Radfahren", "#currentPerformance table[data-analysis-sport='ride'] td", "numbers"], ["load", "Belastung", "#analysisLoadCharts svg", "chart"], ["recovery", "Erholung", "#personalRecovery svg", "chart"]]
    .map(([segment, label, ready, kind]) => ({ view: "analysis", name: `Analyse: ${label}`, covers: "Analyse tabs", pre: [tab("analysis", segment)], ready, kind })),
  // Each sport tab ends with its records card (#3826, #3929).
  ...[["running", "Laufen", "#runRecordsCard td:not(.value-missing)"], ["cycling", "Radfahren", "#rideRecordsCard td:not(.value-missing)"]]
    .map(([segment, label, ready]) => ({ view: "analysis", name: `Analyse: ${label}-Rekorde`, covers: "#3826 records", pre: [tab("analysis", segment)], ready, kind: "numbers" })),
  // Kraft needs stored strength sets, so the step is optional (#3764).
  { view: "analysis", name: "Analyse: Kraft", covers: "#3929 strength sessions per week", pre: [tab("analysis", "strength")], ready: "#strengthAnalysis .analysis-chart-card svg", kind: "chart", optional: true },
  { view: "nutrition", name: "Ernährung: Verlauf", covers: "Nutrition week", pre: [tab("nutrition", "week")], ready: "#nutritionWeek", kind: "numbers" },
  // The body values' edit icons are svgs too, so the chart step waits for a body chart.
  { view: "nutrition", name: "Ernährung: Produktzähler", covers: "#3435 product use counts", pre: [tab("nutrition", "products")], opens: ['#nutritionProducts details[data-section="all"] > summary'], ready: "#nutritionProducts details[data-section='all'] .nutrition-usage", kind: "text" },
  { view: "nutrition", name: "Ernährung: Zuletzt genutzt", covers: "#3444 product last use", pre: [tab("nutrition", "products")], opens: ['#nutritionProducts details[data-section="all"] > summary'], ready: "#nutritionProducts details[data-section='all'] .nutrition-last-use", kind: "text" },
  // Read-only: the walk checks that a meal card has its pencil, it opens no dialog and saves nothing (#3948).
  { view: "nutrition", name: "Ernährung: Mahlzeit bearbeiten", covers: "#3948 meal pencil", since: "2.2.15", pre: [tab("nutrition", "diary")], ready: ".nutrition-card:has(.nutrition-card-head button[aria-label^='Mahlzeit bearbeiten']) h3", kind: "text", optional: true },
  // Read-only: the walk checks that the saved meals section is there, it opens no dialog and saves nothing (#3952).
  { view: "nutrition", name: "Ernährung: Gespeicherte Mahlzeiten", covers: "#3952 saved meals", pre: [tab("nutrition", "products")], ready: "#nutritionTemplates > details > summary", kind: "text" },
  { view: "nutrition", name: "Ernährung: Körper", covers: "Body analysis", pre: [tab("nutrition", "body")], ready: "#bodyAnalysisCharts [data-body-metric] svg", kind: "chart" },
  { view: "nutrition", name: "Ernährung: Körperwerte", covers: "#3402 weight and body fat with their pencils", pre: [tab("nutrition", "body")], ready: "#bodyAnalysisCharts [data-body-overview] [data-metric='weight_kg'] th small", kind: "text" },
  { view: "nutrition", name: "Ernährung: Energieverbrauch", covers: "#3099 #3252 energy card in the diary, #3550 übriger Alltag, #3577 Übriger Alltag, #3703 one source, #3723 provisional Übriger Alltag", pre: [tab("nutrition", "diary")], ready: "#nutritionEnergy [data-energy] .energy-usage", kind: "text" },
  { view: "nutrition", name: "Ernährung: Welche Quelle trifft", covers: "#3397 fit scale under Verlauf, #3723 mit Alltag ring, #3739 deficit edge means", pre: [tab("nutrition", "week")], ready: "#nutritionFitScale [data-energy-fit] [data-energy-fit-note]", kind: "text" },
  { view: "more", name: "Mehr: Ausrüstung", covers: "#2935 equipment", pre: [tab("more", "equipment")], ready: "#equipmentItems .equipment-card", kind: "text" },
  { view: "more", name: "Mehr: Ausrüstung Standard", covers: "#3014 default equipment", pre: [tab("more", "equipment")], ready: "#equipmentItems .equipment-rules select", kind: "options" },
  // The current release's entry: a listed change, or only its collapsed "Technik (n)" (2.1.20).
  // Read-only: the walk checks that "Wettkämpfe senden" is there and never presses it (#3838).
  { view: "more", name: "Mehr: Wettkämpfe senden", covers: "#3838 competition send button", since: "2.2.12", pre: [tab("more", "connections")], opens: ["#connection-intervals > summary"], ready: "#competitionSendButton", kind: "text", optional: true },
  // The App tab: colour scheme and the push row (#3914); the walk never presses "Benachrichtigungen aktivieren".
  { view: "more", name: "Mehr: App", covers: "#3914 App tab and push notifications", since: "2.2.15", pre: [tab("more", "app")], ready: "#notificationSettings #notificationStatus", kind: "text" },
  // Read-only: the walk checks that the switch is there, it never toggles it (#4274).
  { view: "more", name: "Mehr: Morgen-Check-in automatisch", covers: "#4274 automatic morning check-in", since: "2.2.38", pre: [tab("more", "app")], ready: "#coachAutomaticTurns label.chip > span", kind: "text" },
  { view: "more", name: "Mehr: Einheiten automatisch analysieren", covers: "#4275 automatic session analysis", since: "2.2.39", pre: [tab("more", "app")], ready: '#coachAutomaticTurns input[data-automatic-turn="sessions"] + span', kind: "text" },
  // Read-only: the walk checks that the history section is there, it opens none and undoes nothing (#3954).
  { view: "more", name: "Mehr: Verlauf der Änderungen", covers: "#3954 change history", pre: [tab("more", "operations")], ready: "#changeHistorySection > summary", kind: "text" },
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
  let waited = false;
  for (;;) {
    const outcome = await page.waitForFunction(probe, [step.ready, step.kind, step.absent || null, step.never || null], { timeout, polling: 50 }).then((handle) => handle.jsonValue()).catch(() => "empty");
    if (outcome !== "empty" || Date.now() > deadline) return outcome;
    const still = await page.evaluate(loading);
    // After a load it waited out, the step looks once more: the element may come with the load's end.
    if (!still && !waited) return outcome;
    waited = still;
  }
}

// One week's candidates: { result } once one is ready or shows a `never` element, else the counts.
async function tryWeek(page, step, capMs, began) {
  const count = await candidateCount(page, step.each);
  let empty = 0;
  for (let index = 0; index < count && Date.now() - began < STEP_TOTAL_MS; index += 1) {
    const started = Date.now();
    const outcome = await tryCandidate(page, step, index, capMs);
    const leaked = outcome === "ready" ? await leakedValues(page) : "";
    const result = leaked ? { status: "fail", why: `shows ${leaked}` } : { ready: { status: "ok" }, shown: { status: "fail", why: `shows ${step.never}` } }[outcome];
    if (result) return { result: { ...result, ms: Date.now() - started }, count, empty };
    if (outcome === "empty") empty += 1;
  }
  return { count, empty };
}

// Texts written by people that may quote any word: the Coach chat (German prose says "null") and the commit titles
// of the release notes, which quote values such as "null" (#4313). Their headings and counts stay scanned.
const QUOTING_TEXT = "#messages, .release-changelog li";

// A value that leaked into the text (#4285): the visible text names `null`, `undefined`, `NaN` or `[object Object]`
// as a word of its own, outside QUOTING_TEXT (never scanned, even under `root`). `root` limits the scan to one element. Returns up to three "<word> in <element>" names, never the text around them.
export async function leakedValues(page, root = "body") {
  return page.evaluate(([rootSelector, quoting]) => {
    const word = /(?:^|[^\p{L}\p{N}_])(null|undefined|NaN|\[object Object\])(?![\p{L}\p{N}_])/u;
    const hits = [];
    const walker = document.createTreeWalker(document.querySelector(rootSelector) || document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node && hits.length < 3; node = walker.nextNode()) {
      const parent = node.parentElement;
      const found = word.exec(node.textContent);
      if (!found || !parent || ["SCRIPT", "STYLE"].includes(parent.tagName) || parent.closest(quoting) || !parent.checkVisibility()) continue;
      hits.push(`${found[1]} in ${[parent.tagName.toLowerCase(), ...parent.classList].join(".")}`);
    }
    return hits.join(", ");
  }, [root, QUOTING_TEXT]);
}

// A details dialog left open by a candidate is closed with Escape and its history entry is gone before the next press.
async function closeDetails(page) {
  if (!(await page.locator("dialog.activity-detail-dialog[open]").count())) return;
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.activity-detail-dialog[open]") && !history.state?.activityDetail, null, { timeout: 3000 }).catch(() => null);
}

// Opens candidate `index` with the other candidates closed, so the controls pressed and the element checked
// are its own. "ready", "shown", "absent", "empty", or "passed over" (an optional step's candidate without the control).
async function tryCandidate(page, step, index, capMs) {
  try {
    await closeDetails(page);
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

// Whether a `seek` step pages on after a week whose `count` candidates all lacked the element.
async function maySeek(page, step, count, paged, began) {
  if (!step.seek || paged === SEEK_WEEKS || Date.now() - began >= STEP_TOTAL_MS) return false;
  // A required step pages only past weeks without a candidate: an empty element of a shown unit is a finding.
  if (!step.optional && count) return false;
  // The calendar pages only so far (four weeks ahead): a disabled control ends the search.
  return page.locator(step.seek).filter({ visible: true }).first().isEnabled().catch(() => false);
}

// What the page shows instead of an optional step's element, named in its skip: the label of the first
// `saw` pair [label, selector] with a visible match, else the pair without a selector.
async function sawLabel(page, saw) {
  if (!saw) return "";
  const label = await page.evaluate((pairs) => pairs.find(([, selector]) => selector && [...document.querySelectorAll(selector)].some((node) => node.checkVisibility()))?.[0], saw).catch(() => null);
  return ` (${label ?? saw.find(([, selector]) => !selector)?.[0] ?? "nothing"})`;
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
      if (!(await maySeek(page, step, week.count, paged, began))) break;
      // A dialog a candidate left open would block the paging control.
      await closeDetails(page);
      await press(page, step.seek);
      paged += 1;
    }
    // Nothing loaded and nothing empty: the data this step needs does not exist (info, not a failure).
    if (!empty) return { status: "skip", ms: 0, why: count ? "no stored data" : NO_SESSION };
    return step.optional ? { status: "skip", ms: 0, why: `none of ${count} has it${await sawLabel(page, step.saw)}` } : { status: "fail", ms: Date.now() - began, why: "missing or empty" };
  } catch (error) {
    return { status: "fail", ms: Date.now() - began, why: error.name === "TimeoutError" ? "control missing" : error.message.split("\n")[0] };
  } finally {
    // A dialog the last candidate left open would block every control below.
    await closeDetails(page).catch(() => null);
    // Undo the step's navigation (such as paging to next week), so later steps start where they expect.
    for (let back = 0; back < paged; back += 1) await press(page, unseek).catch(() => null);
    for (const selector of post) await press(page, selector).catch(() => null);
  }
}

// A step newer than the running release is not checked (#3886): a checkout at the deployed APP_VERSION
// (see requireDeployedCheckout) may be ahead of the release by features whose steps already exist.
// Without a readable version every step runs.
// The skip outcome for such a step, else null (the step runs).
export function releaseSkip(step, running) {
  if (!step.since || !running) return null;
  const parts = (version) => version.split(".").map(Number);
  const [have, need] = [parts(running), parts(step.since)];
  for (let index = 0; index < Math.max(have.length, need.length); index += 1) {
    const diff = (have[index] || 0) - (need[index] || 0);
    if (diff) return diff > 0 ? null : { status: "skip", ms: 0, why: `not in ${running}`, release: true };
  }
  return null;
}

// The running release, from the diagnostics the logged-in walk may read; null when unreadable.
async function runningVersion(page) {
  const version = await page.request.get("/api/diagnostics").then((response) => (response.ok() ? response.json() : null)).then((report) => report?.app?.version).catch(() => null);
  return typeof version === "string" && /^\d+(\.\d+){1,3}$/.test(version) ? version : null;
}

// The walk's steps match the checkout it runs from, so a checkout that is not the deployed release would
// report FAILs for UI production does not have yet (#4263). APP_VERSION of the checkout's server.py.
export function checkoutVersion(source = fs.readFileSync(path.join(ROOT, "server.py"), "utf8")) {
  return /^APP_VERSION = "(\d+(?:\.\d+){1,3})"/m.exec(source)?.[1] ?? null;
}

// The stop text when both versions are known and differ, else null (an unreadable version does not stop the walk).
export function versionMismatch(deployed, checkout) {
  if (!deployed || !checkout || deployed === checkout) return null;
  return `walk checkout ${checkout} does not match deployed ${deployed}; run from the release commit`;
}

// Logs out and stops the walk before its first step when the checkout is not the deployed release.
export async function requireDeployedCheckout(page, deployed) {
  const mismatch = versionMismatch(deployed, checkoutVersion());
  if (!mismatch) return;
  await logout(page);
  throw Object.assign(new Error(mismatch), { stopText: mismatch });
}

// A step that passed in the previous walk and skips now runs once more after RETRY_WAIT_MS: right after a
// deploy its data may still be loading (2.1.50). Skipped twice stays the compare FAIL (#3540).
export const RETRY_WAIT_MS = 20000;
export async function retriedStep(run, before, wait) {
  const first = await run();
  if (first.status !== "skip" || first.release || before?.status !== "ok") return first;
  await wait(RETRY_WAIT_MS);
  return { ...(await run()), retried: first.why };
}

// After a schema release the app rebuilds the all-time curves once, which slows every read for minutes
// (2.1.28: 265 s). The walk reads only the build's state word from the diagnostics and starts once it is
// no longer "building", at most CURVES_CAP_MS later.
const CURVES_CAP_MS = 600000;
export async function waitForCurves(page, capMs = CURVES_CAP_MS, pollMs = 10000) {
  const started = Date.now();
  let asked = false;
  for (;;) {
    const status = await page.request.get("/api/diagnostics").then((response) => (response.ok() ? response.json() : null)).then((report) => report?.curve_build?.status).catch(() => null);
    const waited = Math.round((Date.now() - started) / 1000);
    if (!["building", "pending"].includes(status) || Date.now() - started >= capMs) return { status: typeof status === "string" && /^[a-z_]{1,20}$/.test(status) ? status : "unknown", waited };
    // "pending" lasts until the first all-time read starts the build: the walk makes that read (the
    // Analyse view's own GET) once, so the build runs now and not during the walk's Analyse visit.
    if (status === "pending" && !asked) {
      asked = true;
      await page.request.get("/api/analysis/power-profiles").catch(() => null);
    }
    await page.waitForTimeout(pollMs);
  }
}

// Login through the login dialog and logout with the CSRF token; ui_explore.mjs uses the same two.
export async function login(page, password) {
  await page.goto("/");
  await page.getByLabel("Passwort", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Anmelden" }).click();
  await page.locator("#appShell").waitFor({ state: "visible", timeout: 30000 });
}

export async function logout(page) {
  return page.evaluate(async () => {
    const token = document.cookie.split("; ").find((item) => item.startsWith("ic_csrf="))?.slice(8) || "";
    const response = await fetch("/api/logout", { method: "POST", headers: { "X-CSRF-Token": decodeURIComponent(token), "Content-Type": "application/json" }, body: "{}" });
    return response.status;
  });
}

async function walk({ chromium, url, password, outDir, earlier = {} }) {
  const result = { measure: LOAD_MEASURE, loadMs: {}, notReady: {}, errors: {}, calendar: {}, steps: {}, marks: null, logout: {} };
  const browser = await chromium.launch();
  try {
    for (const [name, viewport] of Object.entries(WIDTHS)) {
      const context = await browser.newContext({ baseURL: url, viewport });
      const page = await context.newPage();
      const pause = (ms) => page.waitForTimeout(ms);
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
      await login(page, password);
      // Every error counts against a step: login, a view (with its calendar walk), or the rest.
      result.errors[`${name} login`] = errors.length;
      result.version ??= await runningVersion(page);
      await requireDeployedCheckout(page, result.version);
      result.curves ??= await waitForCurves(page);
      let counted = errors.length;
      const shot = async (label) => writeFresh(outDir, `${name}-${label}.png`, await page.screenshot({ fullPage: false }));
      for (const route of VIEWS) {
        const view = await rendered(page, route, open);
        result.loadMs[`${name} ${route}`] = view.ms;
        if (view.capped) result.notReady[`${name} ${route}`] = view.open;
        await shot(route);
        // The steps start in the calendar's initial week; the calendar walk pages away from it.
        for (const step of STEPS.filter((item) => item.view === route)) {
          const key = `${name} ${step.name}`;
          const run = async () => releaseSkip(step, result.version) ?? runStep(page, step);
          result.steps[key] = { ...(await retriedStep(run, earlier[key], pause)), covers: step.covers };
        }
        if (route === "plan") result.calendar[name] = await walkCalendar(page, shot);
        result.errors[`${name} ${route}`] = errors.length - counted;
        counted = errors.length;
      }
      result.marks ??= releaseMarks(await (await page.request.get("/service-worker.js")).text(), await (await page.request.get("/")).text());
      result.logout[name] = await logout(page);
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

function stepLines(key, step) {
  const line = STEP_LINE[step.status](key, step);
  return step.retried ? [`info ${key} retried once after ${RETRY_WAIT_MS / 1000} s: first skipped (${step.retried})`, line] : [line];
}

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
  for (const [key, step] of Object.entries(result.steps || {})) lines.push(...stepLines(key, step));
  if (result.curves) lines.push(`info curve build ${result.curves.status}, waited ${result.curves.waited} s before the walk`);
  for (const [name, steps] of Object.entries(result.calendar)) lines.push(`info calendar ${name}: ${steps.join(", ") || "no controls found"}`);
  lines.push(`info service-worker cache ${result.marks?.cache || "missing"}, ${result.marks?.assetVersions.length || 0} asset versions`);
  for (const [name, status] of Object.entries(result.logout || {})) lines.push(`${status === 200 ? "ok" : "FAIL"} logout ${name} ${status}`);
  return lines;
}

async function main() {
  const { values } = parseArgs({
    options: { url: { type: "string" }, "url-file": { type: "string" }, "password-file": { type: "string" }, "out-dir": { type: "string" }, compare: { type: "string" } },
  });
  let url;
  let outDir;
  let password;
  let readingSecret = false;
  try {
    if (!values["password-file"] || !values["out-dir"]) throw new Error("--url or --url-file, --password-file and --out-dir are required");
    url = appUrl(values);
    outDir = scratchPath(values["out-dir"]);
    readingSecret = true;
    password = fs.readFileSync(secretPath(values["password-file"]), "utf8").trim();
    if (!password) throw new Error(SECRET_MISSING);
  } catch (error) {
    // A file-system error names a path; never print it.
    const fsError = readingSecret ? SECRET_MISSING : "cannot use the out-dir";
    console.log(`FAIL ${error.syscall ? fsError : error.message}`);
    return 2;
  }
  try {
    fs.mkdirSync(outDir, { recursive: true });
  } catch {
    console.log("FAIL cannot create the out-dir");
    return 2;
  }
  let previous = null;
  let expected = values.compare;
  try {
    if (values.compare) {
      expected = previousWalkPath(values.compare);
      previous = JSON.parse(fs.readFileSync(expected, "utf8"));
    }
  } catch {
    // The error text could quote the file's content; never print it.
    console.log(`FAIL cannot read the previous walk at ${expected} (its out-dir or ui-walk.json)`);
    return 2;
  }
  const { chromium } = await import("@playwright/test");
  let result;
  try {
    result = await walk({ chromium, url, password, outDir, earlier: previous?.steps });
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
