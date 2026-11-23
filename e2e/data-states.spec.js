const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { waitForSettledState } = require("./helpers/settled-state");

// Routes miss requests that pass through the service worker's fetch listener; this spec
// does not test the worker, so it is blocked and every route applies (#3842).
test.use({ serviceWorkers: "block" });

// #1783: loading, not configured, no data yet and failed reads look different.
const POWER_PROFILES = "/api/analysis/power-profiles";
const ANALYSIS_TABS = [
  ["running", "Noch keine Laufdaten"],
  ["cycling", "Noch keine Raddaten"],
  ["load", "Noch keine Belastungsdaten"],
  ["recovery", "Noch keine Erholungsdaten"],
];

async function openAnalysis(page, segment) {
  await page.goto(`/#analysis/${segment}`);
  await expect(page.locator("#appShell")).toBeVisible();
  await waitForSettledState(page);
}

test("@responsive an empty install shows one empty card with one action per analysis tab", async ({ page }) => {
  await openAnalysis(page, "load");
  const connected = await page.evaluate(() => Boolean(AppState.state.data?.garmin?.configured || AppState.state.data?.wahoo?.connected));
  const card = page.locator("#analysisEmptyState .empty-state");
  for (const [segment, title] of ANALYSIS_TABS) {
    await page.evaluate((target) => { location.hash = target; }, `#analysis/${segment}`);
    await expect(card).toBeVisible();
    await expect(card.locator("strong")).toHaveText(title);
    await expect(card.locator("a, button")).toHaveCount(1);
    await expect(card.locator("a, button")).toHaveText(connected ? "Jetzt abgleichen" : "Anbindung einrichten");
    await expect(page.locator("#dataPanel")).not.toContainText("Keine Daten:");
    // Without a connected source no period button stays above the empty card (#3936 G-13).
    if (!connected) await expect(page.locator("#analysisLoadPeriod:visible, #strengthPeriod:visible, #personalRecovery .analysis-trend-controls:visible")).toHaveCount(0);
    await expect(card).not.toContainText("Intervals.icu");
    for (const id of ["sessionPerformance", "analysisLoadCharts", "trainingZoneCharts", "currentPerformance", "analysisHistoryCharts"]) {
      await expect(page.locator(`#${id}`)).toBeHidden();
    }
  }
  expect((await new AxeBuilder({ page }).include("#dataPanel").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  // The action follows the connection state: no source leads to the connections, a source to a sync.
  // Laufen has no period window, so a connected source offers the sync.
  await page.evaluate(() => { location.hash = "#analysis/running"; });
  await expect(card).toBeVisible();
  await page.evaluate(() => { AppState.state.data.garmin = { ...AppState.state.data.garmin, configured: false }; AppState.state.data.wahoo = { ...AppState.state.data.wahoo, connected: false }; renderAnalysisSegments(AppState.state.route); });
  await expect(card.locator("span")).toHaveText("Garmin oder Wahoo ist nicht verbunden.");
  await expect(card.getByRole("link", { name: "Anbindung einrichten" })).toHaveAttribute("href", "#more/connections");
  await page.evaluate(() => { AppState.state.data.garmin = { ...AppState.state.data.garmin, configured: true }; renderAnalysisSegments(AppState.state.route); });
  await expect(card.locator("span")).toHaveText("Noch keine Daten abgeglichen.");
  await expect(card.getByRole("button", { name: "Jetzt abgleichen" })).toBeVisible();
});

test("@responsive a failed analysis read shows an error with retry instead of a permanent loading line", async ({ page }) => {
  let fail = true;
  let requests = 0;
  await page.route(`**${POWER_PROFILES}`, (route) => {
    requests += 1;
    return fail
      ? route.fulfill({ status: 503, json: { error: "E2E fixture: Leistungsprofil nicht erreichbar" } })
      : route.fulfill({ json: { all_time: { power: [{ sport: "Ride", duration_seconds: 5, watts: 640, status: "ok", date: "2026-10-01", source: "Garmin Connect" }] } } });
  });
  await openAnalysis(page, "cycling");
  const profile = page.locator("#cyclingPowerProfile");
  const error = profile.locator(".error-state");
  await expect(error).toBeVisible();
  // The page may read the profile again for each area its startup loads; count from the settled state.
  await waitForSettledState(page);
  const startupRequests = requests;
  expect(startupRequests).toBeGreaterThanOrEqual(1);
  await expect(error).toHaveAttribute("role", "alert");
  await expect(error.locator("strong")).toHaveText("Konnte nicht geladen werden.");
  await expect(profile).not.toContainText("wird geladen");
  // A failed read is not "no data": the tab keeps its sections and shows no empty card.
  await expect(page.locator("#analysisEmptyState")).toBeHidden();
  expect((await new AxeBuilder({ page }).include("#cyclingPowerProfile").analyze()).violations).toEqual([]);

  fail = false;
  await error.getByRole("button", { name: "Erneut versuchen" }).click();
  await expect(profile.locator("[data-power-duration='5'] td").first()).toHaveText("640 W");
  await expect(profile.locator("[data-power-duration='60'] td.value-missing")).toHaveText("nicht erfasst");
  await expect(profile.locator(".error-state")).toHaveCount(0);
  expect(requests - startupRequests, "the retry reads the profile once").toBe(1);
});

test("@responsive a hanging analysis read times out into the retry state", async ({ page }) => {
  test.setTimeout(60_000);
  await page.route(`**${POWER_PROFILES}`, () => { /* never answers */ });
  await openAnalysis(page, "cycling");
  const profile = page.locator("#cyclingPowerProfile");
  await expect(profile.locator(".loading-state")).toHaveText("Leistungsprofil wird geladen…");
  await expect(profile.locator(".loading-state")).toHaveAttribute("aria-busy", "true");
  await expect(profile.locator(".error-state")).toContainText("15 Sekunden", { timeout: 25_000 });
  await expect(profile.locator(".loading-state")).toHaveCount(0);
  await expect(profile.getByRole("button", { name: "Erneut versuchen" })).toBeVisible();
});

test("@responsive a failed equipment read offers a retry", async ({ page }) => {
  let fail = true;
  const failing = (message) => (route) => (fail ? route.fulfill({ status: 503, json: { error: message } }) : route.fallback());
  await page.route("**/api/analysis/training-records", failing("E2E fixture: Ausrüstung nicht erreichbar"));
  // J-201: the equipment read fails into the retry card. A session's details retry in the calendar (activity-details spec).
  await page.goto("/#more/equipment");
  const gear = page.locator("#equipmentItems");
  await expect(gear.locator(".error-state")).toContainText("Ausrüstung nicht erreichbar");
  fail = false;
  await gear.getByRole("button", { name: "Erneut versuchen" }).click();
  await expect(gear.locator(".equipment-tabs")).toBeVisible();
  await expect(gear.locator(".error-state")).toHaveCount(0);
  // A failed refresh keeps the rendered list.
  fail = true;
  await page.evaluate(async () => { invalidateAnalysisReport("/api/analysis/training-records"); await renderTrainingRecords(); });
  await expect(gear.locator(".equipment-tabs")).toBeVisible();
  await expect(gear.locator(".error-state")).toHaveCount(0);
});

test("@responsive an empty calendar without a source is one empty state with the next step (#3936 G-13)", async ({ page }) => {
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await waitForSettledState(page);
  const connected = await page.evaluate(() => Boolean(AppState.state.data?.garmin?.configured || AppState.state.data?.wahoo?.connected));
  test.skip(connected, "the fixture has a source");
  await page.evaluate(() => { AppState.state.data.training_calendar = []; renderPlanned(AppState.state.data.training_calendar); });
  const card = page.locator("#plannedCalendar .empty-state");
  await expect(card).toContainText("Noch keine Aktivitäten");
  await expect(card.getByRole("link", { name: "Datenquelle verbinden" })).toHaveAttribute("href", "#more/connections");
  await expect(page.locator(".planned-sport-filter, .planned-view-switch, .planned-week")).toHaveCount(0);
  // An appointment of the shared calendar keeps the week.
  await page.evaluate(() => { AppState.state.data.external_calendar = { ...AppState.state.data.external_calendar, events: [{ uid: "g13", name: "Termin", start_local: `${localDateKey(new Date())}T09:00:00`, impact: "none" }] }; renderPlanned([]); });
  await expect(page.locator(".planned-week")).toHaveCount(1);
  await page.evaluate(() => { AppState.state.data.external_calendar = { ...AppState.state.data.external_calendar, events: [] }; renderPlanned([]); });
  await expect(card).toContainText("Noch keine Aktivitäten");
  // Connecting a source changes the card without a new entry.
  await page.evaluate(() => { AppState.state.data.garmin = { ...AppState.state.data.garmin, configured: true }; renderPlanned([]); });
  await expect(page.locator(".planned-week")).toHaveCount(1);
});

test("@responsive the shared calendar without an address says what to do and refuses an unsafe one (#3936 M-03)", async ({ page }) => {
  await page.goto("/#more/connections");
  await expect(page.locator("#appShell")).toBeVisible();
  await waitForSettledState(page);
  const configured = await page.evaluate(() => Boolean(AppState.state.data?.external_calendar?.configured));
  test.skip(configured, "the fixture has a calendar address");
  await page.locator("#connection-calendar > summary").click();
  await expect(page.locator("#externalCalendarSyncButton")).toBeDisabled();
  await expect(page.locator("#externalCalendarConnectionDetail")).toHaveText("Adresse speichern, dann synchronisieren.");
  await page.locator("#cred-calendar_ical_url").fill("http://calendar.example.invalid/feed.ics");
  await page.locator("#cred-submit-calendar").click();
  await expect(page.locator("#cred-error-calendar")).toHaveText("Die Kalenderadresse muss eine HTTPS-URL ohne Zugangsdaten sein.");
  await expect(page.locator("#cred-calendar_ical_url")).toHaveValue("");
  await expect(page.locator("#externalCalendarSyncButton")).toBeDisabled();
});
