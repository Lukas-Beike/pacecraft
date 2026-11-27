const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const SCRIPT = path.join(__dirname, "..", "scripts", "ui_walk.mjs");
const load = () => import(require("node:url").pathToFileURL(SCRIPT).href);

test("ui walk keeps files in the temp directory and needs an http(s) address", async () => {
  const walk = await load();
  assert.throws(() => walk.scratchPath(path.join(__dirname, "..", "shots")), /system temp directory/);
  assert.throws(() => walk.scratchPath(path.join(__dirname, "..")), /system temp directory/);
  assert.throws(() => walk.scratchPath(path.join(os.tmpdir(), "..", "elsewhere")), /system temp directory/);
  assert.equal(walk.scratchPath(path.join(os.tmpdir(), "walk")), path.resolve(os.tmpdir(), "walk"));
  // The password file may come from the T3 secret folder, read only (#2527).
  const t3 = path.join(os.homedir(), "t3-folder-for-a-test", "secrets");
  assert.equal(walk.secretPath(path.join(t3, "app-password"), t3), path.join(t3, "app-password"));
  assert.equal(walk.secretPath(path.join(os.tmpdir(), "secret"), t3), path.resolve(os.tmpdir(), "secret"));
  assert.throws(() => walk.secretPath(path.join(__dirname, "..", ".env"), t3), /T3 secret folder/);
  assert.throws(() => walk.secretPath(path.join(t3, "..", "other"), t3), /T3 secret folder/);
  assert.throws(() => walk.secretPath("app-password", t3), /must be absolute/);
  const root = path.resolve(__dirname, "..");
  assert.throws(() => walk.secretPath(path.join(root, ".env"), path.dirname(root)), /T3 secret folder/);
  // A link in the temp directory that leads into the repository is refused.
  const linkDir = fs.mkdtempSync(path.join(os.tmpdir(), "walk-link-"));
  const link = path.join(linkDir, "checkout");
  fs.symlinkSync(root, link, "junction");
  try {
    assert.throws(() => walk.secretPath(path.join(link, "package.json"), t3), /T3 secret folder/);
    // The scratch guard follows the link too, also for a directory not created yet (#2615).
    assert.throws(() => walk.scratchPath(path.join(link, "shots")), /system temp directory/);
    assert.throws(() => walk.scratchPath(path.join(link, "new", "deeper")), /system temp directory/);
    assert.equal(walk.scratchPath(path.join(linkDir, "shots")), path.join(linkDir, "shots"));
    // A link that leaves the temp directory without entering the repository, and a
    // dangling link, are refused as well; the drive letter's case does not matter.
    const away = path.join(linkDir, "home");
    fs.symlinkSync(os.homedir(), away, "junction");
    const dangling = path.join(linkDir, "dangling");
    fs.symlinkSync(path.join(linkDir, "missing"), dangling, "junction");
    try {
      assert.throws(() => walk.scratchPath(path.join(away, "shots")), /system temp directory/);
      assert.throws(() => walk.scratchPath(path.join(dangling, "shots")), /system temp directory/);
    } finally {
      fs.unlinkSync(away);
      fs.unlinkSync(dangling);
    }
    const other = path.join(linkDir, "shots");
    const swapped = other[0] === other[0].toLowerCase() ? other[0].toUpperCase() : other[0].toLowerCase();
    assert.equal(walk.scratchPath(swapped + other.slice(1)).toLowerCase(), other.toLowerCase());
  } finally {
    // Remove the link itself first so cleanup never descends into the checkout.
    fs.unlinkSync(link);
    fs.rmdirSync(linkDir);
  }
  assert.throws(() => walk.scratchPath(path.join(t3, "shots")), /system temp directory/);
  assert.equal(walk.baseUrl("https://app.example.test:8090/some/path"), "https://app.example.test:8090");
  assert.throws(() => walk.baseUrl("file:///etc/passwd"), /http\(s\) URL/);
  // #4125: the address from --url or from --url-file's one line in the temp directory, never both, never printed.
  const address = "https://app.example.test:8090";
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "walk-url-"));
  try {
    const file = path.join(dir, "app-url.txt");
    fs.writeFileSync(file, `  ${address}/x\n`);
    assert.equal(walk.appUrl({ url: `${address}/y` }), address);
    assert.equal(walk.appUrl({ "url-file": file }), address);
    fs.writeFileSync(file, `﻿${address}\r\n`);
    assert.equal(walk.appUrl({ "url-file": file }), address);
    assert.throws(() => walk.appUrl({}), /exactly one of --url and --url-file/);
    assert.throws(() => walk.appUrl({ url: address, "url-file": file }), /exactly one/);
    for (const content of ["", `${address}\n${address}\n`]) {
      fs.writeFileSync(file, content);
      assert.throws(() => walk.appUrl({ "url-file": file }), (error) => /one URL/.test(error.message) && !/app\.example\.test/.test(error.message));
    }
    assert.throws(() => walk.appUrl({ "url-file": path.join(__dirname, "..", "app-url.txt") }), /system temp directory/);
    assert.throws(() => walk.appUrl({ "url-file": path.join(dir, "missing.txt") }), (error) => error.message === "cannot read --url-file" && !error.syscall);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("ui walk reads the stamped cache name and asset versions", async () => {
  const walk = await load();
  const marks = walk.releaseMarks(
    'const CACHE = "pacecraft-abc123";\nconst ASSETS = [];',
    '<script src="app.js?v=b2"></script><link href="app.css?v=a1"><script src="api.js?v=b2"></script>',
  );
  assert.deepEqual(marks, { cache: "pacecraft-abc123", assetVersions: ["a1", "b2"] });
  assert.deepEqual(walk.releaseMarks("", ""), { cache: null, assetVersions: [] });
});

test("ui walk reports errors per view and compares load times with the previous walk", async () => {
  const walk = await load();
  const result = {
    loadMs: { "phone coach": 800, "phone plan": 1200 },
    errors: { "phone login": 1, "phone coach": 0, "phone plan": 2, "phone other": 0 },
    calendar: { phone: ["month", "next week"] },
    marks: { cache: "pacecraft-new", assetVersions: ["a"] },
    logout: { phone: 200 },
    measure: walk.LOAD_MEASURE,
    notReady: { "phone coach": ["GET /api/performance"], "phone more": [] },
  };
  const lines = walk.reportLines(result);
  // A view that never became ready names the requests of that view still open (#2581).
  assert.ok(lines.includes("info phone coach not ready within 15 s; still loading: GET /api/performance"));
  assert.ok(lines.includes("info phone more not ready within 15 s; still loading: only a loading marker"));
  assert.ok(!lines.some((line) => line.startsWith("info phone plan not ready")));
  // Only method and path are kept; other hosts keep their origin, blob and data URLs are skipped.
  assert.equal(walk.requestKey("GET", "https://app.test/api/plan?local=1", "https://app.test"), "GET /api/plan");
  assert.equal(walk.requestKey("GET", "https://tiles.test/a/b.png?k=1", "https://app.test"), "GET https://tiles.test/a/b.png");
  assert.equal(walk.requestKey("GET", "blob:https://app.test/1234", "https://app.test"), null);
  // A walk measured another way is not compared.
  assert.deepEqual(walk.compareLines(result, { marks: { cache: "pacecraft-new" }, loadMs: { "phone coach": 15000 } }), ["info service-worker cache unchanged since the previous walk", "info load times not compared: the previous walk measured them differently"]);
  assert.ok(lines.includes("ok phone coach 800 ms"));
  assert.ok(lines.includes("FAIL phone plan 1200 ms, 2 error(s)"));
  assert.ok(lines.includes("FAIL phone login 1 error(s)"));
  assert.ok(!lines.some((line) => line.includes("phone other")));
  assert.ok(lines.includes("info calendar phone: month, next week"));
  assert.ok(lines.includes("ok logout phone 200"));
  const compared = walk.compareLines(result, { measure: walk.LOAD_MEASURE, marks: { cache: "pacecraft-old" }, loadMs: { "phone coach": 700 } });
  assert.deepEqual(compared, ["info service-worker cache changed since the previous walk", "info load phone coach 800 ms (was 700 ms)"]);
  assert.deepEqual(walk.compareLines(result, null), []);
});

test("ui walk checks the week review under the running calendar week, not in Analyse (#3298)", async () => {
  const walk = await load();
  assert.ok(!walk.STEPS.some((step) => step.view === "analysis" && /Woche/.test(step.name)), "Analyse has no week tab since #3319");
  // The first calendar step, while the calendar still shows today: later steps select other weeks.
  const review = walk.STEPS.find((step) => step.view === "plan");
  assert.equal(review.name, "Kalender: Wochenrückblick");
  assert.match(review.ready, /\.week-review\[data-this-week="true"\] \.week-review-tile/);
});

test("ui walk steps press only opening controls and name the feature they cover (#3009)", async () => {
  const walk = await load();
  const names = walk.STEPS.map((step) => step.name);
  assert.equal(new Set(names).size, names.length);
  for (const step of walk.STEPS) {
    assert.ok(step.covers && step.ready && walk.STEP_KINDS.includes(step.kind), step.name);
    for (const selector of [...(step.pre || []), ...(step.opens || []), ...(step.post || []), ...(step.seek ? [step.seek, step.unseek] : []), ...(step.each ? [step.each] : [])]) {
      assert.ok(walk.isOpener(selector), `${step.name}: ${selector}`);
      assert.ok(!walk.refusedLabel(selector), `${step.name}: ${selector}`);
    }
  }
  // Controls that save, sync, send, delete, export, undo or open settings can be neither selected nor pressed.
  for (const selector of ["button", "#planSyncButton", ".planned-activity-feedback", "button.message-undo", "a[href]", "#privacyExportButton"]) {
    assert.equal(walk.isOpener(selector), false, selector);
  }
  for (const label of ["Speichern", "Woche synchronisieren", "Senden", "Löschen", "Daten exportieren", "Rückgängig", "Einstellungen", "Feedback speichern"]) {
    assert.equal(walk.refusedLabel(label), true, label);
  }
  for (const label of ["Details", "Nächste Woche", "Laufen", ""]) assert.equal(walk.refusedLabel(label), false, label);
});

// #3886: a step newer than the running release is skipped with an info line, not a FAIL.
test("ui walk skips a step whose since is newer than the running release", async () => {
  const walk = await load();
  const step = { name: "Neu", since: "2.2.12" };
  assert.deepEqual(walk.releaseSkip(step, "2.2.11"), { status: "skip", ms: 0, why: "not in 2.2.11", release: true });
  assert.deepEqual(walk.releaseSkip(step, "1.9.20"), { status: "skip", ms: 0, why: "not in 1.9.20", release: true });
  for (const running of ["2.2.12", "2.10.0", null]) assert.equal(walk.releaseSkip(step, running), null, running);
  assert.equal(walk.releaseSkip({ name: "Alt" }, "2.2.11"), null);
  const lines = walk.reportLines({ loadMs: {}, errors: {}, calendar: {}, marks: null, steps: { "phone Neu": { status: "skip", ms: 0, why: "not in 2.2.11", covers: "#1" } } });
  assert.ok(lines.includes("info phone Neu skipped: not in 2.2.11"));
  // A walk against an older release (a rollback) neither waits to retry nor reports a FAIL.
  let waited = 0;
  const skipped = await walk.retriedStep(async () => walk.releaseSkip(step, "2.2.11"), { status: "ok" }, async () => { waited += 1; });
  assert.equal(waited, 0);
  const compared = walk.compareLines({ marks: { cache: "a" }, measure: walk.LOAD_MEASURE, loadMs: {}, steps: { "phone Neu": skipped } }, { marks: { cache: "a" }, measure: walk.LOAD_MEASURE, loadMs: {}, steps: { "phone Neu": { status: "ok", ms: 10 } } });
  assert.ok(!compared.some((line) => line.startsWith("FAIL")), compared.join("\n"));
  for (const item of walk.STEPS.filter((entry) => entry.since)) assert.match(item.since, /^\d+\.\d+\.\d+$/, item.name);
});

// #4263: a checkout that is not the deployed release stops the walk before its first step.
test("ui walk stops when its checkout is not the deployed release", async () => {
  const walk = await load();
  assert.equal(walk.checkoutVersion('x = 1\nAPP_VERSION = "2.2.36"\n'), "2.2.36");
  assert.equal(walk.checkoutVersion("no version"), null);
  assert.match(walk.checkoutVersion(), /^\d+\.\d+\.\d+$/);
  assert.equal(walk.versionMismatch("2.2.36", "2.2.36"), null);
  assert.equal(walk.versionMismatch("2.2.36", "2.2.37"), "walk checkout 2.2.37 does not match deployed 2.2.36; run from the release commit");
  // An unreadable version on either side does not stop the walk.
  assert.equal(walk.versionMismatch(null, "2.2.37"), null);
  assert.equal(walk.versionMismatch("2.2.36", null), null);
  // The walk's guard logs out and stops with the text main prints; a matching checkout passes untouched.
  let loggedOut = 0;
  const page = { evaluate: async () => { loggedOut += 1; return 200; } };
  const checkout = walk.checkoutVersion();
  await walk.requireDeployedCheckout(page, checkout);
  await walk.requireDeployedCheckout(page, null);
  assert.equal(loggedOut, 0);
  await assert.rejects(walk.requireDeployedCheckout(page, "0.0.1"), { stopText: `walk checkout ${checkout} does not match deployed 0.0.1; run from the release commit` });
  assert.equal(loggedOut, 1);
});

test("ui walk reports each step and flags one that passed before and is skipped now (#3009)", async () => {
  const walk = await load();
  const result = {
    loadMs: {}, errors: {}, calendar: {}, marks: null, measure: walk.LOAD_MEASURE,
    steps: {
      "phone Kalender: Details": { status: "ok", ms: 200, covers: "#2258" },
      "phone Kalender: Zonenbalken": { status: "skip", ms: 0, why: "no stored data", covers: "#2258" },
      "phone Mehr: Ausrüstung": { status: "fail", ms: 8100, why: "missing or empty", covers: "#2935 equipment" },
      "phone Kalender: geplante Einheit": { status: "skip", ms: 0, why: "no such session", covers: "#2189 planned session" },
    },
  };
  const lines = walk.reportLines(result);
  assert.ok(lines.includes("ok phone Kalender: Details 200 ms"));
  assert.ok(lines.includes("info phone Kalender: Zonenbalken skipped: no stored data"));
  assert.ok(lines.includes("FAIL phone Mehr: Ausrüstung 8100 ms, missing or empty (covers #2935 equipment)"));
  const previous = { measure: walk.LOAD_MEASURE, marks: { cache: "a" }, loadMs: {}, steps: { "phone Kalender: Details": { status: "ok", ms: 150 }, "phone Kalender: Zonenbalken": { status: "ok", ms: 90 }, "phone Kalender: geplante Einheit": { status: "ok", ms: 120 } } };
  const compared = walk.compareLines({ ...result, marks: { cache: "a" } }, previous);
  assert.ok(compared.includes("info step phone Kalender: Details 200 ms (was 150 ms)"));
  assert.ok(compared.includes("FAIL step phone Kalender: Zonenbalken passed in the previous walk, skipped now (no stored data)"));
  // Every step that passed before and is skipped now is a finding, also for "no such session" (2.1.24).
  assert.ok(compared.includes("FAIL step phone Kalender: geplante Einheit passed in the previous walk, skipped now (no such session)"));
});

test("ui walk refuses missing arguments and an unreadable secret file without a browser", () => {
  const run = (...args) => spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8" });
  let done = run("--url", "http://127.0.0.1:9");
  assert.equal(done.status, 2);
  assert.match(done.stdout, /required/);
  // A malformed address keeps its own message, not the secret-file one.
  done = run("--url", "not a url", "--password-file", path.join(os.tmpdir(), "missing-secret-file"), "--out-dir", path.join(os.tmpdir(), "walk"));
  assert.equal(done.status, 2);
  assert.doesNotMatch(done.stdout, /secret missing|out-dir/);
  done = run("--url", "http://127.0.0.1:9", "--password-file", path.join(os.tmpdir(), "missing-secret-file"), "--out-dir", path.join(os.tmpdir(), "walk"));
  assert.equal(done.status, 2);
  // A missing secret names the request step, never the path (#3354).
  const missing = "FAIL secret missing, empty or unreadable: request a new one with request_secret (file delivery)";
  assert.ok(done.stdout.includes(missing));
  assert.doesNotMatch(done.stdout, /missing-secret-file/);
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "walk-test-"));
  const secret = path.join(scratch, "secret");
  fs.writeFileSync(secret, "  \n");
  done = run("--url", "http://127.0.0.1:9", "--password-file", secret, "--out-dir", path.join(scratch, "walk"));
  assert.equal(done.status, 2);
  assert.ok(done.stdout.includes(missing));
  fs.writeFileSync(secret, "fake-login-value");
  const notJson = path.join(scratch, "previous.json");
  fs.writeFileSync(notJson, "fake-login-value is not JSON");
  done = run("--url", "http://127.0.0.1:9", "--password-file", secret, "--out-dir", path.join(scratch, "walk"), "--compare", notJson);
  assert.equal(done.status, 2);
  assert.match(done.stdout, /cannot read the previous walk/);
  assert.doesNotMatch(done.stdout, /fake-login-value/);
  // A walk directory without its ui-walk.json names the file it expected (#3346).
  const emptyWalk = path.join(scratch, "empty-walk");
  fs.mkdirSync(emptyWalk);
  done = run("--url", "http://127.0.0.1:9", "--password-file", secret, "--out-dir", path.join(scratch, "walk2"), "--compare", emptyWalk);
  fs.rmSync(scratch, { recursive: true, force: true });
  assert.equal(done.status, 2);
  assert.ok(done.stdout.includes(`cannot read the previous walk at ${path.join(emptyWalk, "ui-walk.json")}`), done.stdout);
  // An out-dir that cannot be created (a file already holds its path) exits 2 without a stack trace (#2661).
  const blocked = fs.mkdtempSync(path.join(os.tmpdir(), "walk-blocked-"));
  fs.writeFileSync(path.join(blocked, "file"), "x");
  fs.writeFileSync(path.join(blocked, "secret"), "fake-login-value");
  done = run("--url", "http://127.0.0.1:9", "--password-file", path.join(blocked, "secret"), "--out-dir", path.join(blocked, "file"));
  fs.rmSync(blocked, { recursive: true, force: true });
  assert.equal(done.status, 2);
  assert.match(done.stdout, /^FAIL cannot create the out-dir$/m);
  assert.doesNotMatch(done.stderr, /at .*ui_walk/);
});

test("ui walk --compare takes the previous walk's out-dir or its ui-walk.json (#3346)", async () => {
  const walk = await load();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "walk-previous-"));
  try {
    const file = path.join(dir, "ui-walk.json");
    fs.writeFileSync(file, "{}");
    assert.equal(walk.previousWalkPath(dir), file);
    assert.equal(walk.previousWalkPath(file), file);
    assert.throws(() => walk.previousWalkPath(path.join(__dirname, "..", "package.json")));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("ui walk refuses a file, link or hard link at a fixed name instead of following it (#2661)", async () => {
  const walk = await load();
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "walk-out-"));
  const victimDir = fs.mkdtempSync(path.join(os.tmpdir(), "walk-victim-"));
  const victim = path.join(victimDir, "kept.txt");
  try {
    fs.writeFileSync(victim, "original");
    fs.linkSync(victim, path.join(outDir, "ui-walk.json"));
    assert.throws(() => walk.writeFresh(outDir, "ui-walk.json", "{}"), { code: "EEXIST" });
    try {
      fs.symlinkSync(victim, path.join(outDir, "mobile-coach.png"), "file");
      assert.throws(() => walk.writeFresh(outDir, "mobile-coach.png", "png"), { code: "EEXIST" });
    } catch (error) {
      if (error.code !== "EPERM") throw error; // Windows without the symlink privilege: the hard link covers it.
    }
    assert.equal(fs.readFileSync(victim, "utf8"), "original");
    // A fresh name is written; an out-dir outside the temp directory is refused before any write.
    walk.writeFresh(outDir, "desktop-coach.png", "png");
    assert.equal(fs.readFileSync(path.join(outDir, "desktop-coach.png"), "utf8"), "png");
    assert.throws(() => walk.writeFresh(path.join(__dirname, ".."), "ui-walk.json", "{}"), /system temp directory/);
    assert.match(walk.FRESH_OUT_DIR, /fresh --out-dir/);
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
    fs.rmSync(victimDir, { recursive: true, force: true });
  }
});

test("the walk waits while the curve build runs and prints only its state word (2.1.28)", async () => {
  const walk = await load();
  const reads = [];
  const page = (states) => ({
    request: { get: async (url) => { reads.push(url); const status = url === "/api/diagnostics" ? states.shift() : null; return { ok: () => true, json: async () => ({ curve_build: { status }, secret: "never printed" }) }; } },
    waitForTimeout: async () => {},
  });
  const ready = await walk.waitForCurves(page(["building", "building", "ready"]), 60000, 1);
  assert.equal(ready.status, "ready");
  // "pending" until the first all-time read: the walk makes that read once and waits for the build.
  reads.length = 0;
  assert.equal((await walk.waitForCurves(page(["pending", "pending", "building", "ready"]), 60000, 1)).status, "ready");
  assert.equal(reads.filter((url) => url === "/api/analysis/power-profiles").length, 1);
  // Bounded: a build that never ends stops the wait at the cap.
  assert.equal((await walk.waitForCurves(page(Array(50).fill("building")), 0, 1)).status, "building");
  // Anything but a state word is reported as unknown.
  assert.equal((await walk.waitForCurves(page(["<b>x</b>"]), 60000, 1)).status, "unknown");
  const lines = walk.reportLines({ loadMs: {}, errors: {}, calendar: {}, marks: null, curves: ready });
  assert.ok(lines.includes(`info curve build ready, waited ${ready.waited} s before the walk`));
});

test("a step that passed before and skips now is retried once before it is a FAIL (#3540)", async () => {
  const walk = await load();
  const waits = [];
  const wait = async (ms) => waits.push(ms);
  const runs = (...steps) => async () => steps.shift();
  const before = { status: "ok", ms: 40 };
  // A warm-up skip: the retry passes, the step is ok with an info note, no FAIL.
  const warm = await walk.retriedStep(runs({ status: "skip", why: "no data" }, { status: "ok", ms: 50 }), before, wait);
  assert.deepEqual(waits, [walk.RETRY_WAIT_MS]);
  assert.equal(warm.status, "ok");
  const result = { loadMs: {}, errors: {}, calendar: {}, marks: { assetVersions: [] }, steps: { "phone Kalender: Wochenrückblick": warm } };
  const lines = [...walk.reportLines(result), ...walk.compareLines(result, { steps: { "phone Kalender: Wochenrückblick": before } })];
  assert.ok(lines.includes(`info phone Kalender: Wochenrückblick retried once after ${walk.RETRY_WAIT_MS / 1000} s: first skipped (no data)`));
  assert.ok(!lines.some((line) => line.startsWith("FAIL")));
  // Skipped twice: still the FAIL, so a real loss stays visible.
  const lost = await walk.retriedStep(runs({ status: "skip", why: "no data" }, { status: "skip", why: "no data" }), before, wait);
  const compared = walk.compareLines({ marks: {}, loadMs: {}, steps: { s: lost } }, { steps: { s: before } });
  assert.ok(compared.includes("FAIL step s passed in the previous walk, skipped now (no data)"));
  // No retry without a previous pass, nor for a step that did not skip.
  waits.length = 0;
  assert.equal((await walk.retriedStep(runs({ status: "skip", why: "none" }), undefined, wait)).status, "skip");
  assert.equal((await walk.retriedStep(runs({ status: "fail", why: "x" }), before, wait)).status, "fail");
  assert.deepEqual(waits, []);
});
