const { test, expect } = require("@playwright/test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

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
    ], running: { distance: [{ distance_meters: 5000, seconds: 1250, date: "2026-10-03", source: "Garmin" }] } },
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

// The post-release walk's sport steps match what the cards render: tables, not charts (#3009).
test("@responsive the walk's Laufen and Radfahren steps pass on the rendered cards", async ({ page }) => {
  const { STEPS, runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  await mockReports(page);
  await waitForInitialState(page);
  await routeTo(page, "#analysis/running");
  for (const name of ["Analyse: Laufen", "Analyse: Radfahren"]) {
    const outcome = await runStep(page, STEPS.find((step) => step.name === name));
    expect(outcome.status, `${name}: ${outcome.why}`).toBe("ok");
  }
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

// Recurring sessions, as the Coach reads them, follow the profile cards (#2802); the pace at
// equal heart rate in the same answer is the Coach's only (#3153).
const TRENDS = "/api/analysis/trends";
const RUN_DAYS = ["2026-08-24", "2026-09-07", "2026-09-21", "2026-10-12", "2026-10-26", "2026-11-09"];
const TRENDS_PAYLOAD = {
  pace_at_heart_rate: { method: "pace-at-heart-rate-v2", unit: "s/km", run: {
    outdoor: { status: "ok", heart_rate_band_bpm: [140, 149], sources: ["Garmin original speed/heart-rate streams"],
      sessions: RUN_DAYS.map((date, index) => ({ date, activity_id: `act_${index}`, pace_s_per_km: [349, 345, 347, 340, 337, 336][index] })),
      earlier: { sessions: 3, pace_s_per_km: 347 }, later: { sessions: 3, pace_s_per_km: 338 }, change_s_per_km: -9 },
    indoor: { status: "insufficient_data" },
  } },
  recurring: { status: "observations", groups: [
    { target_sha256: "run-intervals", sport: "run", device: "Forerunner", observations: ["2026-10-17", "2026-10-31", "2026-11-14"].map((date, index) => ({
      date, activity_id: `act_r${index}`, name: "Intervalle", source: "derived from Garmin original streams",
      steps: [{ target: "1000 m @ 4:10 /km", unit: "m/s", mean: [3.97, 4.01, 4.06][index], status: "ok" }, { target: "Trabpause", unit: "m/s", mean: 2.5, status: "ok" }],
      repeat_groups: [{ target: "1000 m @ 4:10 /km", repetitions: 5, fade_percent: [2.4, 1.8, 1.1][index] }],
    })) },
    { target_sha256: "ride-sweet-spot", sport: "ride", device: "Edge", observations: ["2026-10-29", "2026-11-12"].map((date, index) => ({
      date, activity_id: `act_s${index}`, name: "Sweet Spot", source: "derived from Wahoo original streams",
      steps: [{ target: "10 min @ 250 W", unit: "W", mean: [249, 252][index], status: "ok" }],
      repeat_groups: [{ target: "10 min @ 250 W", repetitions: 3, fade_percent: [1.6, 0.9][index] }],
    })) },
  ] },
};

test("@responsive Analyse shows recurring sessions after the profile cards and no pace-at-heart-rate card", async ({ page }) => {
  await mockReports(page);
  const order = [];
  page.on("request", (request) => { const path = new URL(request.url()).pathname; if (path === POWER_PROFILES || path === TRENDS) order.push(`request ${path}`); });
  page.on("response", (response) => { if (new URL(response.url()).pathname === POWER_PROFILES) order.push("response power"); });
  await page.route(`**${TRENDS}`, (route) => route.fulfill({ json: TRENDS_PAYLOAD }));
  await waitForInitialState(page);
  await routeTo(page, "#analysis/running");
  const running = page.locator("#runningRecurringSessions");
  await expect(running.getByRole("heading", { name: "Wiederkehrende Einheiten" })).toBeVisible();
  // The answer still carries the run trend, but no card draws it (#3153).
  await expect(page.getByText(/Pace bei \d/)).toHaveCount(0);
  await expect(running.getByRole("heading", { name: /5 × 1000 m @ 4:10 \/km/ })).toContainText("Garmin");
  await expect(running.locator("tbody tr").first()).toHaveText(/14\.11\.2026\s*4:06 min\/km · Abfall 1,1 %/);
  await expect(page.locator("#cyclingRecurringSessions")).toBeHidden();
  // The trends read waits for the profile cards' answer, so it never holds them up.
  expect(order.indexOf(`request ${TRENDS}`)).toBeGreaterThan(order.indexOf("response power"));

  await routeTo(page, "#analysis/cycling");
  const cycling = page.locator("#cyclingRecurringSessions");
  await expect(cycling.getByRole("heading", { name: /3 × 10 min @ 250 W/ })).toContainText("Wahoo");
  await expect(cycling.locator("tbody tr").first()).toHaveText(/12\.11\.2026\s*252 W · Abfall 0,9 %/);
  await expect(page.locator("#runningRecurringSessions")).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test("@responsive a failed trends read shows a retry on Laufen and on Radfahren", async ({ page }) => {
  await mockReports(page);
  let fail = true;
  await page.route(`**${TRENDS}`, (route) => (fail ? route.fulfill({ status: 500, json: { error: "Interner Fehler." } }) : route.fulfill({ json: TRENDS_PAYLOAD })));
  await waitForInitialState(page);
  await routeTo(page, "#analysis/cycling");
  const cycling = page.locator("#cyclingRecurringSessions");
  await expect(cycling.getByRole("button", { name: /Erneut/ })).toBeVisible();
  // Opening Laufen reads the trends again (a failed read is not kept). That read must fail before
  // the retry is allowed, or it replaces the retry button under the click.
  const reread = page.waitForResponse((response) => new URL(response.url()).pathname === TRENDS);
  await routeTo(page, "#analysis/running");
  await reread;
  const running = page.locator("#runningRecurringSessions");
  await expect(running.getByRole("button", { name: /Erneut/ })).toBeVisible();
  fail = false;
  await running.getByRole("button", { name: /Erneut/ }).click();
  await expect(running.getByRole("heading", { name: "Wiederkehrende Einheiten" })).toBeVisible();
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
