const { test, expect } = require("@playwright/test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

// #3067: every step of the post-release walk runs against the fixture app, so a step whose selector
// or kind does not match the app's markup fails here and not first in production (#3065).
// A step the fixture cannot satisfy is named with its reason; it must keep skipping.
const FIXTURE_SKIPS = { "Kalender: Laufdynamik": "none of 5 has it" };

test("@responsive every UI walk step passes on the fixture app", async ({ page, request }) => {
  const { VIEWS, STEPS, runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  // The app's own today: the profile's zone, else the browser's (the same process as this runner).
  const timeZone = (await (await request.get("/api/bootstrap")).json()).profile?.timezone || undefined;
  const today = new Intl.DateTimeFormat("en-CA", { timeZone }).format(new Date());
  // Only a release image carries the changelog (#2899).
  await page.context().route("**/changelog.json", (route) => route.fulfill({ json: { releases: [{ version: "9.9.9", commits: ["feat(calendar): a fixture change (#1)"] }] } }));
  // The fixture's current week plans nothing, and only a long upcoming unit gets fuelling (#2921).
  await page.context().route(/\/api\/plan(\?|$)/, async (route) => {
    const data = await (await route.fetch()).json();
    data.training_calendar = [...(data.training_calendar || []), { id: "walk-long-ride", local_id: "walk-long-ride", date: today, name: "Lange Ausfahrt", type: "Ride", sport: "ride", environment: "outdoor", duration_minutes: 150, planned_duration_s: 9000 }];
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
  const expected = STEPS.map((step) => `${step.name}: ${step.name in FIXTURE_SKIPS ? `skip (${FIXTURE_SKIPS[step.name]})` : "ok"}`);
  // One line, so a CI summary that keeps only the first line still names the steps.
  expect(outcomes, `differing steps: ${outcomes.filter((line) => !expected.includes(line)).join(" | ")}`).toEqual(expected);
});
