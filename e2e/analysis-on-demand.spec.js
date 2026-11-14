const { test, expect } = require("@playwright/test");

const ENDURANCE = "/api/analysis/endurance";
const POWER_PROFILES = "/api/analysis/power-profiles";
const TRAINING_RECORDS = "/api/analysis/training-records";
const REPORT_PATHS = [ENDURANCE, POWER_PROFILES, TRAINING_RECORDS];

function trackReportRequests(page) {
  const counts = new Map();
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (REPORT_PATHS.includes(path)) counts.set(path, (counts.get(path) || 0) + 1);
  });
  return () => Object.fromEntries(REPORT_PATHS.map((path) => [path, counts.get(path) || 0]));
}

// pending: answers that the all-time bests are still being read before the first real answer.
async function mockReports(page, { pending = 0 } = {}) {
  // The status strip's deferred equipment read (#2809) waits for an idle callback that never comes here,
  // so these counts show only what the views themselves load; status-strip.spec.js covers that read.
  await page.addInitScript(() => { globalThis.requestIdleCallback = () => 0; });
  await page.route(`**${ENDURANCE}`, (route) => route.fulfill({ json: { activities: [] } }));
  await page.route(`**${POWER_PROFILES}`, (route) => (pending-- > 0 ? route.fulfill({ json: { all_time: { status: "pending" } } }) : route.fulfill({ json: {
    all_time: { power: [
      ...[5, 60, 300, 1200].map((duration_seconds) => ({ sport: "Ride", duration_seconds, watts: 250, status: "ok", date: "2026-10-01", source: "Intervals.icu" })),
      { sport: "VirtualRide", duration_seconds: 3600, watts: 210, status: "ok", date: "2026-10-02", source: "Intervals.icu" },
    ] },
  } })));
  await page.route(`**${TRAINING_RECORDS}`, (route) => route.fulfill({ json: {
    equipment: { items: [], garmin_items: [] },
  } }));
}

async function waitForInitialState(page) {
  await page.goto("/#coach");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => AppState.state.initialStateLoaded);
}

async function routeTo(page, hash) {
  await page.evaluate((target) => { location.hash = target; }, hash);
}

test("@responsive cycling power profile loads only on performance and keeps unknown durations empty", async ({ page }) => {
  await mockReports(page, { pending: 1 });
  const reportCounts = trackReportRequests(page);
  await waitForInitialState(page);
  expect(reportCounts()).toEqual({ [ENDURANCE]: 0, [POWER_PROFILES]: 0, [TRAINING_RECORDS]: 0 });

  await routeTo(page, "#analysis/cycling");
  await expect(page.locator("#dataPanel")).toHaveClass(/active/);
  await expect(page.locator("#cyclingPowerProfile")).toBeVisible();
  await expect(page.locator("#cyclingPowerProfile tbody tr")).toHaveCount(5);
  await expect(page.locator("#cyclingPowerProfile [data-power-duration='5'] td")).toHaveText(["250 W", "01.10.2026"]);
  await expect(page.locator("#cyclingPowerProfile [data-power-duration='3600'] td")).toHaveText(["210 W", "02.10.2026"]);
  await expect(page.locator("#cyclingPowerProfile")).not.toContainText("90 Tage");
  expect(await page.locator("#cyclingPowerProfile").evaluate((element) => element.closest("[data-analysis-section]").dataset.analysisSection)).toBe("development-Rad");
  // The first answer said the all-time bests were still being read, so the card asked again.
  expect(reportCounts()).toEqual({ [ENDURANCE]: 0, [POWER_PROFILES]: 2, [TRAINING_RECORDS]: 0 });
});

test("@responsive cold start on performance fetches only the power profile", async ({ page }) => {
  await mockReports(page);
  const reportCounts = trackReportRequests(page);
  await page.goto("/#analysis/cycling");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => AppState.state.initialStateLoaded);
  await expect(page.locator("#cyclingPowerProfile")).toBeVisible();
  await expect(page.locator("#cyclingPowerProfile tbody tr")).toHaveCount(5);
  expect(reportCounts()).toEqual({ [ENDURANCE]: 0, [POWER_PROFILES]: 1, [TRAINING_RECORDS]: 0 });
});

test("@responsive cycling power profile stays cached across navigation and chart rerenders", async ({ page }) => {
  await mockReports(page);
  const reportCounts = trackReportRequests(page);
  await waitForInitialState(page);

  await routeTo(page, "#analysis/cycling");
  await expect(page.locator("#cyclingPowerProfile")).toBeVisible();
  await expect(page.locator("#cyclingPowerProfile tbody tr")).toHaveCount(5);
  const afterFirstVisit = reportCounts();
  await page.evaluate(() => renderAnalysisHistory(state.data.performance.history));
  await expect(page.locator("#cyclingPowerProfile tbody tr")).toHaveCount(5);

  await routeTo(page, "#coach");
  await expect(page.locator("#chatPanel")).toHaveClass(/active/);
  await routeTo(page, "#analysis/cycling");
  await expect(page.locator("#dataPanel")).toHaveClass(/active/);
  await expect(page.locator("#cyclingPowerProfile")).toBeVisible();
  expect(reportCounts()).toEqual(afterFirstVisit);
});

// Monthly medians per pace band of the running dynamics (#2829); the first answer is still being built.
test("@responsive running technique draws monthly medians per pace band and switches the value", async ({ page }, testInfo) => {
  await mockReports(page);
  const months = ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"];
  let pending = 1;
  await page.route("**/api/analysis/running-dynamics", (route) => (pending-- > 0 ? route.fulfill({ json: { status: "pending" } }) : route.fulfill({ json: {
    status: "ok", stale: false, session: null,
    trend: { months, bands: [
      { band_s_per_km: 240, series: { ground_contact_time_ms: [238, 236, null, 234, 233, 231], cadence_spm: [180, 181, null, 182, 182, 183] } },
      { band_s_per_km: 360, series: { ground_contact_time_ms: [282, 281, 279, 279, 278, 277], cadence_spm: [164, 165, 165, 166, 166, 167] } },
    ] },
  } })));
  await waitForInitialState(page);
  await routeTo(page, "#analysis/running");
  const card = page.locator("#runningDynamicsTrend");
  await expect(card.getByRole("heading", { name: "Lauftechnik je Monat" })).toBeVisible();
  const select = card.getByRole("combobox", { name: "Laufdynamik-Wert" });
  await expect(select.locator("option")).toHaveText(["Bodenkontaktzeit", "Schrittfrequenz"]);
  await expect(card.locator(".analysis-chart-legend li button")).toHaveText(["4:00–4:15/km", "6:00–6:15/km"]);
  await expect(card.locator("h3")).toHaveText("Bodenkontaktzeit");
  // Monthly points name their months on the axis (#2922).
  await expect(card.locator(".analysis-date-tick").last()).toHaveText("Nov");
  await card.getByText("Werte ansehen").click();
  await expect(card.locator(".analysis-chart-table thead th").first()).toHaveText("Monat");
  await expect(card.locator(".analysis-chart-table tbody th").first()).toHaveText("Juni 2026");
  await select.selectOption("cadence_spm");
  await expect(card.locator("h3")).toHaveText("Schrittfrequenz");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await card.screenshot({ path: testInfo.outputPath("running-technique-trend.png"), animations: "disabled" });
});

test("@responsive equipment records load only on the equipment route and stay cached", async ({ page }) => {
  await mockReports(page);
  const reportCounts = trackReportRequests(page);
  await waitForInitialState(page);
  expect(reportCounts()[TRAINING_RECORDS]).toBe(0);

  await routeTo(page, "#more/equipment");
  await expect(page.locator("#equipmentItems").getByRole("heading", { name: "Ausrüstung und Wartung" })).toBeVisible();
  // The heading shows while the read is still starting, so the count is polled.
  await expect.poll(() => reportCounts()[TRAINING_RECORDS]).toBe(1);

  const layout = await page.evaluate(() => {
    const nav = document.querySelector("#settingsPanel > .more-segment-nav").getBoundingClientRect();
    const panel = document.querySelector('[data-more-segment-panel="equipment"]').getBoundingClientRect();
    return { desktop: innerWidth >= 1100, nav: { right: nav.right, top: nav.top, width: nav.width }, panel: { left: panel.left, top: panel.top, width: panel.width }, overflow: document.documentElement.scrollWidth > innerWidth };
  });
  expect(layout.overflow).toBe(false);
  if (layout.desktop) {
    expect(layout.panel.left).toBeGreaterThan(layout.nav.right);
    expect(layout.panel.width).toBeGreaterThan(layout.nav.width * 2);
    expect(Math.abs(layout.panel.top - layout.nav.top)).toBeLessThan(2);
  }

  await routeTo(page, "#coach");
  await expect(page.locator("#chatPanel")).toHaveClass(/active/);
  await routeTo(page, "#more/equipment");
  await expect(page.locator("#equipmentItems").getByRole("heading", { name: "Ausrüstung und Wartung" })).toBeVisible();
  expect(reportCounts()[TRAINING_RECORDS]).toBe(1);
});
