// --today pins this walk's day (#3200): its expectations depend on the week's completed runs.
const { test, expect, todayIn } = require("./helpers/today");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

// #3067: every step of the post-release walk runs against the fixture app, so a step whose selector
// or kind does not match the app's markup fails here and not first in production (#3065).
// A step the fixture cannot satisfy is named with its reason; it must keep skipping for that reason.
// The fixture's runs carry no running dynamics; how many runs the shown week holds depends on the day.
// The fixture stores no GPS tracks, so no run has a recognised route (#3763).
// The fixture configures no Intervals.icu key, so its card has no Wettkämpfe row to send from (#3838).
// The fixture stores no training plan, so "Einplanen" offers no plan choice (#4081).
// The fixture stores no adaptive replan preview, so the calendar shows no replan card (#4020).
// The fixture holds no competition with a sync conflict, so no card shows its choices (#4021).
// The fixture sends nothing to Garmin, so no planned unit has a Garmin copy to remove (#4022); how many units are tried depends on the day.
const FIXTURE_SKIPS = { "Kalender: Aus Garmin entfernen": /^skip \(none of [1-9]\d* has it\)$/,"Wettkämpfe: Konflikt": /^skip \(none of 1 has it\)$/,"Kalender: Plananpassung": /^skip \(none of 1 has it\)$/,"Bibliothek: Einplanen, Trainingsplan": /^skip \(none of 1 has it\)$/,"Kalender: Laufdynamik": /^skip \(none of [1-9]\d* has it\)$/, "Kalender: Streckenhistorie": /^skip \(none of [1-9]\d* has it\)$/, "Mehr: Wettkämpfe senden": /^skip \(none of 1 has it\)$/ };

test("@responsive every UI walk step passes on the fixture app", async ({ page, request }) => {
  // Every step of every view runs in one test; with the step test steps (#3833) it took 52 to 61 s in
  // six local desktop and mobile runs, so 60 s no longer fits: 90 s is the slowest run plus half again.
  test.setTimeout(90_000);
  const { VIEWS, STEPS, runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  // One test walks every step, so its time grows with STEPS: the 35 steps at #3866 took 55-59 s
  // of the default 60 s on a Windows Docker host. A budget per step keeps the margin as steps are added.
  test.setTimeout(Math.max(60_000, STEPS.length * 6_000));
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  // The app's own today: the profile's zone, else the browser's (the same process as this runner).
  const timeZone = (await (await request.get("/api/bootstrap")).json()).profile?.timezone || undefined;
  const today = todayIn(timeZone);
  // Only a release image carries the changelog (#2899); a release of only tooling changes still shows its entry.
  await page.context().route("**/changelog.json", (route) => route.fulfill({ json: { releases: [{ version: "9.9.9", commits: ["build(quality): a fixture change (#1)"] }] } }));
  // The fixture's current week plans nothing, and only a long upcoming unit gets fuelling (#2921):
  // the first later stored ride or run is shown today instead, long, so the server still knows its equipment (#3011).
  await page.context().route(/\/api\/plan(\?|$)/, async (route) => {
    const data = await (await route.fetch()).json();
    const calendar = data.training_calendar || [];
    const stored = calendar.find((entry) => entry.local_id && !entry.is_completed_activity && !entry.compliance?.actual_activity && entry.date > today && /ride|run/i.test(`${entry.type} ${entry.sport}`));
    const moved = (entry) => ({ ...entry, date: today, starts_at_local: entry.starts_at_local && `${today}${entry.starts_at_local.slice(10)}`, duration_minutes: 150, planned_duration_s: 9000 });
    // Completed units are not moved: on a Monday the walk's completed-unit steps page back themselves (`seek`).
    // The fixture plans no running step test (#3833): every planned and completed run acts as one.
    const stepTest = (entry) => (/run/i.test(`${entry.type} ${entry.sport}`) ? { ...entry, test_protocol: "run_step_test" } : entry);
    data.training_calendar = calendar.map((entry) => stepTest(entry === stored ? moved(entry) : entry));
    // The fixture's nights are ordinary, so today carries a synthetic infection hint (M14, #3762).
    const signal = (label, value, unit, lower, upper) => ({ label, value, unit, lower, upper, source: "Garmin Connect", unusual: true });
    const hint = { method: "infection-hint-v1", status: "hint", signals: [signal("Atemfrequenz", 17, "brpm", 14, 15), signal("Ruhepuls", 56, "bpm", 48, 51)], limits: "Keine Diagnose." };
    const days = (data.daily_planning_context || []).filter((day) => day.date !== today);
    data.daily_planning_context = [...days, { ...(data.daily_planning_context || []).find((day) => day.date === today), date: today, infection_hint: hint }];
    return route.fulfill({ json: data });
  });
  // Every session read carries a step test result, as a paired step-test run would (#3833).
  await page.context().route(/\/api\/activities\/[^/?]+(\?|$)/, async (route) => {
    const data = await (await route.fetch()).json();
    const lt1 = { status: "ok", heart_rate_bpm: 152, pace_seconds_per_km: 320, quality: "good" };
    const lt2 = { status: "not_reached", reason: "α1 blieb über 0,5." };
    return route.fulfill({ json: { ...data, step_test: { protocol: "run_step_test", status: "ok", lt1, lt2, stages: 10, analysable_stages: 9, artifact_share_pct: 1.2, source: "abgeleitet" } } });
  });
  await page.goto("/");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => AppState.state.initialStateLoaded);
  const outcomes = [];
  for (const route of VIEWS) {
    await page.evaluate((next) => new Promise((resolve) => {
      document.addEventListener("app:route-rendered", resolve, { once: true });
      setTimeout(resolve, 15000);
      if (globalThis.location.hash === `#${next}`) globalThis.location.hash = "";
      globalThis.location.hash = next;
    }), route);
    for (const step of STEPS.filter((item) => item.view === route)) {
      const outcome = await runStep(page, step);
      outcomes.push(`${step.name}: ${outcome.status}${outcome.why ? ` (${outcome.why})` : ""}`);
    }
  }
  // A named step must skip for its reason, every other step must pass; an unexpected pass fails too.
  const differing = STEPS.map((step, index) => [step, outcomes[index].slice(step.name.length + 2)])
    .filter(([step, outcome]) => (FIXTURE_SKIPS[step.name] ? !FIXTURE_SKIPS[step.name].test(outcome) : outcome !== "ok"))
    .map(([step, outcome]) => `${step.name}: ${outcome}`);
  // One line, so a CI summary that keeps only the first line still names the steps.
  expect(outcomes).toHaveLength(STEPS.length);
  expect(differing, `differing steps: ${differing.join(" | ")}`).toEqual([]);
});
