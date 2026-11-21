// --today pins this walk's day (#3200): its expectations depend on the week's completed runs.
const { test, expect, todayIn } = require("./helpers/today");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

// #3067: every step of the post-release walk runs against the fixture app, so a step whose selector
// or kind does not match the app's markup fails here and not first in production (#3065).
// A step the fixture cannot satisfy is named with its reason; it must keep skipping for that reason.
// The fixture's runs carry no running dynamics; how many runs the shown week holds depends on the day.
// The fixture stores no GPS tracks, so no run has a recognised route (#3763), and no strength sets (#3764).
const FIXTURE_SKIPS = { "Kalender: Laufdynamik": /^skip \(none of [1-9]\d* has it\)$/, "Kalender: Streckenhistorie": /^skip \(none of [1-9]\d* has it\)$/, "Analyse: Kraft": /^skip \(none of 1 has it\)$/ };

test("@responsive every UI walk step passes on the fixture app", async ({ page, request }) => {
  const { VIEWS, STEPS, runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
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
    data.training_calendar = calendar.map((entry) => (entry === stored ? moved(entry) : entry));
    return route.fulfill({ json: data });
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
