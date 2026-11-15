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

// Running dynamics are read per session in the calendar details only; Analyse shows no monthly card (#2961).
test("@responsive running analysis shows no running technique card", async ({ page }) => {
  await mockReports(page);
  const reads = [];
  page.on("request", (request) => { if (request.url().includes("/api/analysis/running-dynamics")) reads.push(request.url()); });
  await waitForInitialState(page);
  const profiles = page.waitForResponse(`**${POWER_PROFILES}`);
  await routeTo(page, "#analysis/running");
  await expect(page.locator("#dataPanel")).toHaveClass(/active/);
  // The view's report reads start together, so once the best efforts are drawn any card read was sent.
  await profiles;
  await expect(page.locator("#runningBestEfforts")).toHaveAttribute("data-analysis-empty", /.+/);
  await expect(page.getByText("Lauftechnik je Monat")).toHaveCount(0);
  await expect(page.locator("#runningDynamicsTrend")).toHaveCount(0);
  expect(reads).toEqual([]);
});

// The week review (#2805): five cards in layout A, a switch between this and last week, an empty week shows the empty state.
const WEEK_REVIEW = {
  method: "local-week-review-v1", week_start: "2026-11-09", week_end: "2026-11-15", effective_end: "2026-11-14",
  plan: { planned: 4, completed: 3, missed: 1, open: 0, duration_share: 82, missed_units: [{ date: "2026-11-11", name: "Intervalle 5×4 min" }] },
  load: { this_week: { duration_s: 25200, sessions: 5, training_load: 412 }, week_before: { duration_s: 21600, sessions: 4, training_load: 380 }, shares: { low: 78, moderate: 14, high: 8 } },
  recovery: {
    sleep: { mean: 7.4, nights: 6, unit: "h", source: "Garmin", week_before: 7.1 },
    hrv: { mean: 58, nights: 6, unit: "ms", source: "Garmin", week_before: 55, position: "within" },
    resting_hr: { mean: 47, nights: 6, unit: "bpm", source: "Garmin", week_before: 48, position: "below" },
  },
  notable: [{ date: "2026-11-11", name: "Intervalle 5×4 min", reason: "missed" }, { date: "2026-11-14", name: "Langer Lauf", reason: "highest_load", training_load: 148 }],
  checkins: { entered: 5, days: 6, pain_days: 1, illness_days: 0, scores: { motivation: 7.2, stress: 4, soreness: 3.5 } },
};
const EMPTY_WEEK = {
  method: "local-week-review-v1", week_start: "2026-11-02", week_end: "2026-11-08", effective_end: "2026-11-08",
  plan: { planned: 0, completed: 0, missed: 0, open: 0, duration_share: null, missed_units: [] },
  load: { this_week: { duration_s: null, sessions: 0, training_load: null }, week_before: { duration_s: null, sessions: 0, training_load: null }, shares: null },
  recovery: {}, notable: [], checkins: { entered: 0, days: 7, pain_days: 0, illness_days: 0, scores: {} },
};

test("@responsive week review shows the cards and switches to last week", async ({ page }, testInfo) => {
  await mockReports(page);
  const weeks = [];
  await page.route("**/api/analysis/week-review?*", (route) => {
    const week = new URL(route.request().url()).searchParams.get("week");
    weeks.push(week);
    return route.fulfill({ json: week === "previous" ? EMPTY_WEEK : WEEK_REVIEW });
  });
  await waitForInitialState(page);
  await routeTo(page, "#analysis/week");
  const review = page.locator("#weekReview");
  await expect(review.locator("h3")).toHaveText("KW 46 · 9. Nov. – 15. Nov.");
  await expect(review.locator(".week-review-card h4")).toHaveText(["Plan", "Belastung", "Erholung", "Besondere Einheiten", "Check-ins"]);
  await expect(review.locator(".week-review-big")).toHaveText("3 von 4 Einheiten");
  await expect(review.locator(".week-review-row").first()).toHaveText("Zeit7:00 h +17 %");
  await expect(review.getByText("7:24 h +18 min")).toBeVisible();
  await expect(review.getByText("58 ms · in deinem Bereich")).toBeVisible();
  await expect(review.getByText("verpasst: Mi Intervalle 5×4 min")).toBeVisible();
  await expect(review.locator(".week-review-intensity")).toHaveAttribute("aria-label", "leicht 78 %, mittel 14 %, hart 8 %");
  await expect(review.getByText("Sa Langer Lauf · höchste Belastung (148)")).toBeVisible();
  await expect(review.getByText("Motivation 7,2 · Stress 4 · Muskelkater 3,5")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await review.screenshot({ path: testInfo.outputPath("week-review.png"), animations: "disabled" });

  await review.getByRole("button", { name: "Letzte" }).click();
  await expect(page.locator("#analysisEmptyState")).toContainText("Noch keine Wochendaten");
  await expect(review.locator(".week-review-card")).toHaveCount(0);
  await expect(review.locator("h3")).toHaveText("KW 45 · 2. Nov. – 8. Nov.");
  await review.getByRole("button", { name: "Diese" }).click();
  await expect(review.locator(".week-review-card")).toHaveCount(5);
  await expect(page.locator("#analysisEmptyState")).toBeHidden();
  expect(weeks).toEqual(["current", "previous"]);
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
