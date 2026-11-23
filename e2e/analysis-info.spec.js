const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { waitForSettledState } = require("./helpers/settled-state");

// Routes miss requests that pass through the service worker's fetch listener; this spec
// does not test the worker, so it is blocked and every route applies (#3842).
test.use({ serviceWorkers: "block" });

async function performanceFixture(page, changes, route = "running") {
  await page.route("**/api/performance", async (request) => {
    const response = await request.fetch();
    const data = await response.json();
    // The thresholds (LT1, #2427) derive from the metrics, so they are emptied with them.
    Object.assign(data.performance, { metrics: {}, thresholds: {} }, typeof changes === "function" ? changes(data.performance) : changes);
    await request.fulfill({ response, json: data });
  });
  await page.goto(`/#analysis/${route}`);
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.loadedAreas.has("performance") && !state.loadPromise);
  // A later reload of an area redraws every chart; start from the settled page, with the power profile read.
  await waitForSettledState(page);
  if (route === "running" || route === "cycling") await expect(page.locator("#cyclingPowerProfile")).not.toHaveAttribute("data-analysis-empty", "loading");
}

async function localFitnessFixture(page) {
  await performanceFixture(page, (performance) => {
    const end = performance.history.end;
    const days = Array.from({ length: 28 }, (_, index) => {
      const date = new Date(`${end}T12:00:00Z`);
      date.setUTCDate(date.getUTCDate() - 27 + index);
      return { date: date.toISOString().slice(0, 10), state: "loaded", load: 60, fitness: 50 + index / 10, fatigue: 55, form: index / 10 - 5 };
    });
    return { history: { ...performance.history, local_fitness_fatigue: { status: "estimate", days } } };
  }, "load");
  return page.locator("#analysisLoadCharts .analysis-chart-card").filter({ has: page.getByRole("heading", { name: /^Fitness, Ermüdung, Form/ }) });
}

test("@responsive running tolerance renders Garmin toleranceScore instead of missing data", async ({ page }) => {
  await page.goto("/#analysis/running");
  await page.waitForFunction(() => state.loadedAreas.has("performance") && !state.loadPromise && !!state.data?.performance?.history?.end);
  await page.evaluate(() => {
    const history = structuredClone(state.data.performance.history);
    history.provider_metrics.running_tolerance = {
      source: "Garmin Connect",
      status: "current",
      points: [{ date: history.end, values: { toleranceScore: 72 } }],
    };
    renderAnalysisHistory(history);
  });
  const running = page.locator("[data-analysis-section='development-Lauf']");
  const chart = running.locator(".analysis-subchart").filter({ has: page.getByRole("heading", { name: "Lauf-Belastbarkeit", exact: true }) });
  await expect(chart).toBeVisible();
  await chart.getByText("Werte ansehen", { exact: true }).click();
  await expect(chart.locator("tbody td")).toContainText(["72"]);
  await expect(running).not.toContainText("Keine Daten: Lauf-Belastbarkeit");
  await page.evaluate(() => {
    const history = structuredClone(state.data.performance.history);
    history.provider_metrics.running_tolerance = {
      source: "Garmin Connect",
      status: "current",
      points: [
        { date: addDateKey(history.end, -7), values: { toleranceScore: 70 } },
        { date: history.end, values: { toleranceScore: 72 } },
      ],
    };
    renderAnalysisHistory(history);
  });
  await expect(chart.locator("circle[data-series]")).toHaveCount(2);
  await expect(chart.locator("circle[data-series] title").last()).toContainText("72");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive standard demo shows one compact race table and sport-specific collapsible development", async ({ page }) => {
  test.setTimeout(180_000);
  expect((await page.request.get("/api/fixture/demo")).ok()).toBe(true);
  await page.goto("/#analysis/running");
  const table = page.locator("#performancePredictions");
  await expect(table.locator("tbody tr")).toHaveCount(4);
  await expect(table.locator("thead th")).toHaveCount(2);
  for (const distance of ["5 km", "10 km", "Halbmarathon", "Marathon"]) await expect(table.getByRole("rowheader", { name: distance, exact: true })).toBeVisible();
  await expect(table).not.toContainText("Garmin");
  await expect(page.locator("[data-race-distance], #existingPerformanceReports")).toHaveCount(0);
  await expect(page.locator("#analysisHistoryCharts")).not.toContainText("Historische Entwicklung");
  const cycling = page.locator("[data-analysis-section='development-Rad']");
  const running = page.locator("[data-analysis-section='development-Lauf']");
  await expect(running).not.toContainText("Keine Daten");
  await expect(running.locator("#performancePredictions")).toHaveCount(1);
  // The running bests moved to Rekorde; the critical-speed line stays below the race predictions (#3826).
  await expect(running.locator("#runningBestEfforts")).toHaveCount(0);
  await expect(running.locator("#runningCriticalSpeed")).toHaveCount(1);
  expect(await page.locator("#runningCriticalSpeed").evaluate((element) => element.previousElementSibling?.id)).toBe("performancePredictions");
  // Laufen shows no cycling part (#2265).
  await expect(cycling).toBeHidden();
  await expect(page.locator("#currentPerformance [data-metric='cycling_w_per_kg']")).toBeHidden();
  const endurance = page.locator("#performanceEnduranceCharts");
  // The demo has no endurance score, so the section stays empty and hidden (#1783).
  await expect(endurance).toBeHidden();
  expect(await endurance.evaluate((element) => element.previousElementSibling.id)).toBe("currentPerformance");
  await running.locator(":scope > summary").click();
  await expect(table).toBeHidden();
  await running.locator(":scope > summary").click();
  await expect(page.locator("#performanceWeightCharts")).toHaveCount(0);
  await page.evaluate(async () => { await AppRouter.navigate("analysis/cycling", { historyMode: "replace" }); });
  await expect(running).toBeHidden();
  // The power profile moved to Rekorde; the critical-power line stays in Radfahren (#3826).
  await expect(cycling.locator("#cyclingPowerProfile")).toHaveCount(0);
  await expect(cycling.locator("#cyclingCriticalPower")).toHaveCount(1);
  await expect(cycling.locator("[data-body-metric='cycling_w_per_kg']")).toHaveCount(0);
  await expect(page.locator("#currentPerformance [data-metric='cycling_w_per_kg']")).toBeVisible();
  await expect(cycling.locator(".analysis-group-title")).toHaveText("Entwicklung");
  for (const [route, group] of [["analysis/running", running], ["analysis/cycling", cycling]]) {
    await page.evaluate(async (route) => { await AppRouter.navigate(route, { historyMode: "replace" }); }, route);
    const title = await group.locator(".analysis-group-title").boundingBox();
    const help = await group.locator("xpath=../button[contains(@class, 'analysis-section-help')]").boundingBox();
    expect(help.x - title.x - title.width).toBeGreaterThanOrEqual(12);
    expect(help.width).toBeGreaterThanOrEqual(44);
    expect(help.height).toBeGreaterThanOrEqual(44);
    await group.locator("xpath=../button[contains(@class, 'analysis-section-help')]").click();
    await expect(group).toHaveAttribute("open", "");
    await page.keyboard.press("Escape");
  }
  await cycling.locator(":scope > summary").click();
  await expect(cycling).not.toHaveAttribute("open", "");
  await expect(cycling.locator("svg").first()).toBeHidden();
  await cycling.locator(":scope > summary").focus();
  await page.keyboard.press("Enter");
  await expect(cycling.locator("svg").first()).toBeVisible();
  await page.evaluate(() => renderAnalysisHistory(state.data.performance.history));
  await expect(cycling.locator("svg").first()).toBeVisible();
  await cycling.locator(":scope > summary").click();
  await page.evaluate(() => renderAnalysisHistory(state.data.performance.history));
  await expect(cycling.locator("svg").first()).toBeHidden();
  await cycling.locator(":scope > summary").click();
  // A render in the same task as the click runs before the asynchronous toggle event.
  for (const key of ["development-Rad", "current-performance"]) {
    await page.evaluate((key) => {
      document.querySelector(`[data-analysis-section='${key}'] > summary`).click();
      if (key === "current-performance") renderCurrentPerformance(state.data.performance?.metrics || {}, state.data.performance?.history, [], state.data.performance?.thresholds);
      else renderAnalysisHistory(state.data.performance.history);
    }, key);
    await expect(page.locator(`[data-analysis-section='${key}']`)).not.toHaveAttribute("open", "");
    await page.locator(`[data-analysis-section='${key}'] > summary`).click();
    await expect(page.locator(`[data-analysis-section='${key}']`)).toHaveAttribute("open", "");
  }
  expect((await new AxeBuilder({ page }).include("#dataPanel").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive current performance groups real values by sport before the charts", async ({ page }) => {
  await performanceFixture(page, { metrics: {
    cycling_ftp_watts: { value: 280, unit: "W", source: "Garmin Connect", observed_at: "2026-10-01", freshness: "stale" },
    run_threshold_pace_seconds_per_km: { value: 270, unit: "s/km", source: "Garmin Connect" },
    running_vo2max_ml_kg_min: { value: 52, unit: "ml/kg/min", source: "Garmin Connect" },
    run_5k_seconds: { value: 1200, unit: "s", source: "Garmin Connect" },
  } });
  const root = page.locator("#currentPerformance");
  await expect(root).toBeVisible();
  await expect(root.locator("caption")).toHaveText(["Laufen", "Radfahren"]);
  await expect(root.locator("[data-metric='cycling_ftp_watts']")).toContainText("280 W");
  await expect(root.locator("small")).toHaveCount(0);
  for (const table of await root.locator("table").all()) {
    await expect(table).not.toContainText(/Garmin|Intervals\.icu/);
  }
  await expect(root).not.toContainText("01.10.2026");
  await expect(root.locator("[data-metric='run_threshold_pace_seconds_per_km']")).toContainText("4:30");
  await expect(root.locator("[data-metric='run_threshold_watts']")).toHaveCount(0);
  await expect(root.locator("td").filter({ hasText: "\u2014" })).toHaveCount(0);
  await expect(root).not.toContainText("5 km");
  expect(await root.evaluate((element) => !!(element.compareDocumentPosition(document.getElementById("analysisHistoryCharts")) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  expect((await new AxeBuilder({ page }).include("#currentPerformance").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.evaluate(() => { location.hash = "#analysis/load"; });
  await expect(root).toBeHidden();
});

test("@responsive recovery uses independent scales, honest coverage over exactly four weeks", async ({ page }) => {
  // Only the injected baselines: the fixture's own Garmin readiness chart is left out.
  await performanceFixture(page, (performance) => ({ history: { ...performance.history, provider_metrics: { ...performance.history.provider_metrics, training_readiness_history: undefined } }, personal_recovery: {
    as_of: "2026-10-02", sleep_target_hours: 8,
    baselines: [
      { metric: "sleep", source: "Garmin Connect", measurement: "duration", observed_at: "2026-10-01", nights: 1,
        history: [{ date: "2026-09-30", value: 7 }, { date: "2026-10-01", value: 11 }] },
      { metric: "hrv", source: "Garmin Connect", measurement: "RMSSD", observed_at: "2026-10-01", nights: 28,
        status: "ok", lower: 50, upper: 60, position: "within", continuity: "unknown", context_changed_on: null,
        rolling: { method: "rolling-lnrmssd-v1", status: "provisional", window_nights: 3, mean_ms: 55.2, lower_ms: 53, upper_ms: 62.2, position: "within", cv_pct: 2.5 }, history: [{ date: "2026-09-30", value: 52 }, { date: "2026-10-01", value: 58 }] },
      { metric: "resting_hr", source: "Garmin Connect", measurement: "resting", observed_at: "2026-10-01", nights: 1,
        status: "insufficient_data", reason: "Mindestens 14 frühere passende Nächte erforderlich.", continuity: "known", context_changed_on: "2026-09-30", history: [{ date: "2026-10-01", value: 48 }] },
    ],
  } }), "recovery");
  const root = page.locator("#personalRecovery");
  await expect(root.locator(".analysis-chart-card")).toHaveCount(1);
  await expect(root.locator(".analysis-subchart")).toHaveCount(3);
  await expect(root).toContainText("Garmin Connect · RMSSD");
  await expect(root.locator("svg")).toHaveCount(2);
  await expect(root.locator(".analysis-secondary-axis, .analysis-extremum")).toHaveCount(0);
  await expect(root.getByRole("heading", { name: /^Erholung · Letzte 4 Wochen/ })).toBeVisible();
  await expect(root.getByRole("button", { name: "Erholung · Letzte 4 Wochen: Informationen", exact: true })).toBeVisible();
  // One period selector (#3839), no load period.
  await expect(root.locator(".analysis-trend-controls")).toHaveCount(1);
  await expect(root.locator(".analysis-period-controls")).toHaveCount(1);
  await root.getByRole("button", { name: "Erholung · Letzte 4 Wochen: Informationen", exact: true }).click();
  const info = root.locator(".analysis-info-tooltip:not([popover]):not([hidden])");
  await expect(info).toContainText("Vergleich mit 28 früheren Nächten.");
  await expect(info).toContainText("Mindestens 14 frühere passende Nächte erforderlich.");
  await expect(info).toContainText("7-Tage-Mittel 55,2 ms (3 Nächte) · im Normalbereich (53 ms–62,2 ms) · Schwankung 2,5 % · vorläufig");
  await expect(info).toContainText("Vergleich mit 28 früheren Nächten. Messkontext unbekannt.");
  await expect(info).toContainText(/Messkontext geändert am 30\.09\.2026\./);
  await expect(info).toContainText("Durchschnitt der angezeigten Werte");
  await page.keyboard.press("Escape");
  const recoveryText = await root.evaluate((element) => element.textContent);
  for (const forbidden of ["Grüner Bereich", "üblichen Bereich", "Basis", "bestätigt", "Status:"]) {
    expect(recoveryText).not.toContain(forbidden);
  }
  expect((await new AxeBuilder({ page }).include("#personalRecovery").analyze()).violations).toEqual([]);
  expect(await root.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
});

test("@responsive recovery comparison band is neutral and keeps series contrast in both themes", async ({ page }) => {
  await performanceFixture(page, { personal_recovery: {
    as_of: "2026-10-02", sleep_target_hours: 8,
    baselines: [
      { metric: "hrv", source: "Garmin Connect", measurement: "RMSSD", observed_at: "2026-10-01", nights: 28,
        status: "ok", lower: 50, upper: 60, position: "within", history: [{ date: "2026-09-30", value: 52 }, { date: "2026-10-01", value: 58 }] },
    ],
  } }, "recovery");
  const band = page.locator("#personalRecovery .analysis-baseline-band");
  await expect(band).toHaveCount(1);
  for (const theme of ["dark", "light"]) {
    await page.evaluate((selected) => { document.documentElement.dataset.theme = selected; }, theme);
    const result = await band.evaluate((element) => {
      const parse = (value) => { const [r, g, b, a = 1] = value.match(/[\d.]+/g).map(Number); return { r, g, b, a }; };
      const blend = (top, bottom) => ({ r: top.r * top.a + bottom.r * (1 - top.a), g: top.g * top.a + bottom.g * (1 - top.a), b: top.b * top.a + bottom.b * (1 - top.a), a: 1 });
      const luminance = ({ r, g, b }) => { const [lr, lg, lb] = [r, g, b].map((channel) => { const c = channel / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; }); return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb; };
      const ratio = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
      const probe = (index) => {
        const node = document.createElement("span");
        node.style.color = "var(--chart-series-" + index + ")";
        document.body.append(node);
        const color = parse(getComputedStyle(node).color);
        node.remove();
        return color;
      };
      const style = getComputedStyle(element);
      const page = blend(parse(getComputedStyle(document.body).backgroundColor), { r: 0, g: 0, b: 0, a: 1 });
      const surface = blend(parse(getComputedStyle(element.closest(".analysis-chart-card")).backgroundColor), page);
      const fill = parse(style.fill);
      const composite = blend({ ...fill, a: Number(style.opacity) }, surface);
      return {
        spread: Math.max(fill.r, fill.g, fill.b) - Math.min(fill.r, fill.g, fill.b),
        ratios: [1, 2, 3, 4].map((index) => ratio(probe(index), composite)),
      };
    });
    expect(result.spread, theme + " band fill is neutral").toBeLessThanOrEqual(8);
    for (const value of result.ratios) expect(value, theme + " series against band").toBeGreaterThanOrEqual(3);
  }
});

// Value labels never touch a mark, the line, an axis text or another label, and stay inside the chart (#1782).
async function chartLabelCollisions(svg) {
  return svg.evaluate((element) => {
    const rect = (node) => { const box = node.getBoundingClientRect(); return { left: box.left, right: box.right, top: box.top, bottom: box.bottom }; };
    const hit = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
    const labels = [...element.querySelectorAll(".analysis-point-value[data-value-series]")].map(rect);
    const marks = [...element.querySelectorAll("rect.analysis-series-bar, rect.recovery-sleep-bar, circle[data-series], .analysis-axis-unit, .analysis-value-tick, .analysis-date-tick")].map(rect);
    for (const path of element.querySelectorAll("path[data-series]")) {
      const matrix = path.getScreenCTM();
      for (let at = 0; at <= path.getTotalLength(); at += 2) {
        const point = path.getPointAtLength(at).matrixTransform(matrix);
        marks.push({ left: point.x - .5, right: point.x + .5, top: point.y - .5, bottom: point.y + .5 });
      }
    }
    const frame = rect(element);
    return {
      labels: labels.length,
      outside: labels.filter((box) => box.left < frame.left || box.right > frame.right || box.top < frame.top || box.bottom > frame.bottom).length,
      onMarks: labels.filter((box) => marks.some((mark) => hit(box, mark))).length,
      onLabels: labels.filter((box, index) => labels.slice(index + 1).some((other) => hit(box, other))).length,
    };
  });
}

test("@responsive Garmin acute load switches four weeks and three months with real gaps", async ({ page }) => {
  const points = Array.from({ length: 90 }, (_, index) => ({ date: new Date(Date.UTC(2026, 6, 3 + index)).toISOString().slice(0, 10), value: index >= 38 && index <= 44 ? null : 300 + index }));
  await performanceFixture(page, { history: { start: points[0].date, end: points.at(-1).date, load: { source: "Garmin Connect", points }, training_time: { source: "Garmin Connect, Wahoo", points: points.map((point) => ({ ...point, value: point.value == null ? null : 1 })) }, metrics: {} } }, "load");
  const loadRoot = page.locator("#analysisLoadCharts");
  await expect(loadRoot.locator("svg")).toHaveCount(2);
  const root = loadRoot.locator(".analysis-chart-card").first();
  // Acute load is a rolling state: one line with dots, no bars (#1782).
  await expect(root.locator("circle[data-series='0']")).toHaveCount(12);
  await expect(root.locator("rect.analysis-series-bar")).toHaveCount(0);
  await expect(root.locator(".analysis-axis-unit")).toHaveText("Belastungspunkte · Wochenstand");
  expect(await root.locator(".analysis-date-tick").allTextContents()).toEqual(expect.arrayContaining([expect.stringMatching(/^\d{2}\.\d{2}$/)]));
  await expect(root).not.toContainText("KW ");
  await expect(root.locator("path[data-series='0']")).toHaveAttribute("d", /M.*L/);
  await expect(root).not.toContainText("Fitness");
  const time = loadRoot.locator(".analysis-chart-card").nth(1);
  await expect(time.locator("rect.analysis-series-bar")).toHaveCount(12);
  await expect(time.locator("circle[data-series], path[data-series]")).toHaveCount(0);
  await expect(time.locator(".analysis-axis-unit")).toHaveText("h · Wochensumme");
  await page.locator("#analysisLoadPeriod").getByRole("button", { name: "4 Wochen", exact: true }).click();
  await expect(root.locator("circle[data-series='0']")).toHaveCount(28);
  await expect(root.locator(".analysis-axis-unit")).toHaveText("Belastungspunkte · Tageswert");
  for (const svg of await loadRoot.locator("svg").all()) {
    const collisions = await chartLabelCollisions(svg);
    expect(collisions.labels).toBeGreaterThan(0);
    expect(collisions.labels).toBeLessThanOrEqual(4);
    expect(collisions).toMatchObject({ outside: 0, onMarks: 0, onLabels: 0 });
  }
  await page.locator("#analysisLoadPeriod").getByRole("button", { name: "3 Monate", exact: true }).click();
  await expect(root.locator("circle[data-series='0']")).toHaveCount(12);
  const geometry = await root.locator("svg").evaluate((element) => {
    const dots = [...element.querySelectorAll("circle[data-series]")].map((dot) => [Number(dot.getAttribute("cx")), Number(dot.getAttribute("cy"))]);
    const path = element.querySelector("path[data-series]").getAttribute("d");
    return { dots, line: [...path.matchAll(/[ML](-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/g)].map((match) => [Number(match[1]), Number(match[2])]) };
  });
  expect(geometry.line.length).toBe(geometry.dots.length);
  geometry.dots.forEach((dot, index) => {
    expect(geometry.line[index][0]).toBeCloseTo(dot[0], 2);
    expect(geometry.line[index][1]).toBeCloseTo(dot[1], 2);
  });
  for (const svg of await loadRoot.locator("svg").all()) expect(await chartLabelCollisions(svg)).toMatchObject({ outside: 0, onMarks: 0, onLabels: 0 });
  expect((await new AxeBuilder({ page }).include("#analysisLoadCharts").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive steps and floors show per-day units, whole numbers and the plotted basis", async ({ page }) => {
  await performanceFixture(page, (performance) => {
    const end = performance.history.end;
    const days = Array.from({ length: 84 }, (_, index) => {
      const date = new Date(`${end}T12:00:00Z`);
      date.setUTCDate(date.getUTCDate() - 83 + index);
      return date.toISOString().slice(0, 10);
    });
    return { history: { ...performance.history, daily_activity: {
      source: "Garmin Connect",
      steps: days.map((date, index) => ({ date, value: 6000 + (index % 7) * 700 + 0.4 })),
      floors: days.map((date, index) => ({ date, value: 5 + (index % 4) * 1.5 })),
    } } };
  }, "load");
  const group = page.locator("#analysisLoadCharts .analysis-chart-stack").filter({ hasText: "Alltagsaktivität" });
  const steps = group.locator(".analysis-subchart").filter({ has: page.getByRole("heading", { name: /^Schritte · Ø [\d.]+ pro Tag$/ }) });
  const floors = group.locator(".analysis-subchart").filter({ has: page.getByRole("heading", { name: /^Stockwerke · Ø \d+ pro Tag$/ }) });
  await expect(steps.locator(".analysis-axis-unit")).toHaveText("Schritte/Tag · Wochenmittel");
  await expect(floors.locator(".analysis-axis-unit")).toHaveText("Stockwerke/Tag · Wochenmittel");
  await expect(group).not.toContainText("Belastungspunkte");
  for (const chart of [steps, floors]) {
    const values = [...await chart.locator(".analysis-point-value, .analysis-value-tick").allTextContents()];
    for (const value of values) expect(value).toMatch(/^[\d.]+$/);
    expect(await chartLabelCollisions(chart.locator("svg"))).toMatchObject({ outside: 0, onMarks: 0, onLabels: 0 });
  }
});

test("@responsive sparse performance shows measurements and the current value line", async ({ page }) => {
  await performanceFixture(page, { history: {
    start: "2026-09-01", end: "2026-09-08", load: { points: [] },
    metrics: {
      cycling_ftp_watts: [{ source: "Garmin Connect", points: [{ date: "2026-09-01", value: 200 }, { date: "2026-09-08", value: 210 }] }],
      run_threshold_pace_seconds_per_km: [{ source: "Garmin Connect", points: [{ date: "2026-09-01", value: 300 }, { date: "2026-09-08", value: 290 }] }],
      running_vo2max_ml_kg_min: [{ source: "Garmin Connect", points: [{ date: "2026-09-08", value: 49 }] }],
    },
  } }, "cycling");
  const root = page.locator("#analysisHistoryCharts");
  await expect(root.locator(".analysis-chart-card")).toHaveCount(2);
  await expect(root.locator("svg")).toHaveCount(2);
  await expect(root.locator(".analysis-current-line")).toHaveCount(0);
  await expect(root.locator("path[data-series]")).toHaveCount(2);
  expect(await root.locator(".analysis-point-value").allTextContents()).toEqual(expect.arrayContaining(["4:50", "210"]));
  const button = root.getByRole("button", { name: "Erklärung zu FTP", exact: true });
  await button.click();
  const info = root.getByRole("note").filter({ hasText: "FTP · Garmin Connect" });
  await expect(info).toBeVisible();
  // Value, date and source stand once, in the first line of the explanation (A-08).
  await expect(info).toContainText("08.09.2026");
  await expect(button).toHaveAttribute("aria-expanded", "true");
  const bounds = await info.boundingBox();
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(page.viewportSize().width);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("sparse FTP draws one connected series with its legend and no eFTP", async ({ page }) => {
  await performanceFixture(page, { history: {
    start: "2026-09-01", end: "2026-09-15", load: { points: [] }, metrics: {
      cycling_ftp_watts: [{ source: "Garmin Connect", points: [{ date: "2026-09-01", value: 200 }, { date: "2026-09-08", value: 210 }] }],
    },
  } }, "cycling");
  const root = page.locator("#analysisHistoryCharts");
  const chart = root.locator("svg");
  await expect(chart).toHaveCount(1);
  await expect(root.getByRole("heading", { name: "Leistungsschwelle · FTP", exact: true })).toHaveCount(1);
  await expect(chart.locator('circle[data-series="0"]')).toHaveCount(2);
  await expect(chart.locator('path[data-series="0"]')).toHaveCount(1);
  await expect(chart.locator('.analysis-current-line[data-series="0"]')).toHaveCount(0);
  await expect(chart.locator('[data-series="1"]')).toHaveCount(0);
  await expect(root.locator(".analysis-chart-legend .analysis-legend-info")).toHaveCount(1);
  await expect(root).toContainText("Seit 01.09.2026: +10 W");
  await expect(root).not.toContainText("eFTP");
});

test("@responsive pace ticks stay distinct and faster pace is higher", async ({ page }) => {
  await performanceFixture(page, { history: { start: "2026-09-01", end: "2026-09-15", load: { points: [] }, metrics: {
    run_threshold_pace_seconds_per_km: [{ source: "Garmin Connect", points: [{ date: "2026-09-01", value: 300 }, { date: "2026-09-08", value: 295 }, { date: "2026-09-15", value: 290 }] }],
  } } });
  const chart = page.locator("#analysisHistoryCharts svg");
  await expect(chart).toHaveCount(1);
  const ticks = await chart.locator(".analysis-value-tick").allTextContents();
  expect(new Set(ticks).size).toBe(ticks.length);
  const y = await chart.locator("circle").evaluateAll((dots) => dots.map((dot) => Number(dot.getAttribute("cy"))));
  expect(y.at(-1)).toBeLessThan(y[0]);
  await expect(chart.locator(".analysis-extremum, .analysis-secondary-axis")).toHaveCount(0);
  const dates = await chart.locator(".analysis-date-tick").allTextContents();
  expect(new Set(dates).size).toBe(dates.length);
  const labelSizes = await chart.locator("text").evaluateAll((labels) => labels.map((label) => Math.round(label.getBoundingClientRect().height * 1000) / 1000));
  expect(Math.min(...labelSizes)).toBeGreaterThanOrEqual(11);
});

test("@responsive a history chart is one tab stop with arrow-key navigation inside a named region", async ({ page }) => {
  await performanceFixture(page, { history: { start: "2026-09-01", end: "2026-09-15", load: { points: [] }, metrics: {
    run_threshold_pace_seconds_per_km: [{ source: "Garmin Connect", points: [{ date: "2026-09-01", value: 300 }, { date: "2026-09-08", value: 295 }, { date: "2026-09-15", value: 290 }] }],
  } } });
  await expect(page.getByRole("region", { name: "Leistungsentwicklung" })).toBeVisible();
  const chart = page.locator("#analysisHistoryCharts svg");
  const markers = chart.locator(".analysis-day-marker");
  await expect(markers).toHaveCount(3);
  await expect(chart.locator(".analysis-day-marker[tabindex='0']")).toHaveCount(1);
  await expect(markers.last()).toHaveAttribute("tabindex", "0");
  await markers.last().focus();
  await page.keyboard.press("ArrowLeft");
  await expect(markers.nth(1)).toBeFocused();
  await expect(markers.nth(1)).toHaveAttribute("tabindex", "0");
  await expect(chart.locator(".analysis-day-marker[tabindex='0']")).toHaveCount(1);
  await page.keyboard.press("Home");
  await expect(markers.first()).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(markers.first()).toBeFocused();
  await page.keyboard.press("End");
  await expect(markers.last()).toBeFocused();
});

test("@responsive repeated performance values are labelled once without a normal range", async ({ page }) => {
  const points = Array.from({ length: 30 }, (_, index) => ({ date: `2026-09-${String(index + 1).padStart(2, "0")}`, value: index % 2 ? 210 : 200 }));
  await performanceFixture(page, { history: { start: "2026-09-01", end: "2026-09-30", load: { points: [] }, metrics: {
    cycling_ftp_watts: [{ source: "Garmin Connect", points }],
  } } }, "cycling");
  const root = page.locator("#analysisHistoryCharts");
  // One period selector (#3839), no load period.
  await expect(root.locator(".analysis-trend-controls")).toHaveCount(1);
  await expect(root.locator(".analysis-period-controls")).toHaveCount(1);
  expect((await root.locator(".analysis-point-value").allTextContents()).sort()).toEqual(["200", "210"]);
  await expect(root.locator('circle[data-series="0"]')).toHaveCount(30);
  await expect(root.locator(".analysis-current-line")).toHaveCount(0);
  await expect(root.locator(".analysis-baseline-band")).toHaveCount(0);
  await root.getByRole("button", { name: "Erklärung zu FTP", exact: true }).click();
  await expect(root.locator(".analysis-info-tooltip:not([popover]):not([hidden])")).not.toContainText("Mittlere Hälfte deiner früheren Werte");
});

test("@responsive performance keeps dated FTP values and weekly medians independently of the load period", async ({ page }) => {
  const points = [
    { date: "2026-06-01", value: 999 },
    { date: "2026-09-01", value: 200 }, { date: "2026-09-02", value: 220 }, { date: "2026-09-03", value: 210 },
    { date: "2026-09-08", value: 220 }, { date: "2026-09-09", value: 210 },
  ];
  await performanceFixture(page, { history: { start: "2026-08-01", end: "2026-09-30", load: { points: [] }, metrics: {
    cycling_ftp_watts: [{ source: "Garmin Connect", points }],
    cycling_vo2max_ml_kg_min: [{ source: "Garmin Connect", points: points.map((point) => ({ ...point, value: point.value - 160 })) }],
    run_threshold_pace_seconds_per_km: [{ source: "Garmin Connect", points: points.slice(1).map((point) => ({ ...point, value: 500 - point.value })) }],
  } } }, "cycling");
  const root = page.locator("#analysisHistoryCharts");
  // One period selector (#3839), no load period.
  await expect(root.locator(".analysis-trend-controls")).toHaveCount(1);
  await expect(root.locator(".analysis-period-controls")).toHaveCount(1);
  const ftp = root.locator(".analysis-subchart").filter({ has: page.getByRole("heading", { name: "Leistungsschwelle · FTP", exact: true }) });
  await ftp.getByText("Werte ansehen", { exact: true }).click();
  await expect(ftp.locator("tbody tr")).toHaveCount(5);
  await expect(ftp.locator("tbody")).toContainText("03.09.2026");
  await expect(ftp.locator("tbody")).toContainText("09.09.2026");
  // Newest first (#3928).
  await expect(ftp.locator("tbody td:nth-child(2)")).toHaveText([/210 W/, /220 W/, /210 W/, /220 W/, /200 W/]);
  const vo2max = root.locator("[data-analysis-section='development-Rad'] .analysis-subchart").filter({ has: page.getByRole("heading", { name: "VO₂max", exact: true }) });
  await vo2max.getByText("Werte ansehen", { exact: true }).click();
  await expect(vo2max.locator("tbody td:nth-child(2)")).toHaveText([/55 ml\/kg\/min/, /50 ml\/kg\/min/]);
  await expect(vo2max.locator("tbody td:nth-child(3)")).toHaveText(["2", "3"]);
  await expect(vo2max.getByRole("columnheader")).toHaveText(["Woche ab", "Wert", "Messungen", "Quelle"]);
  await page.evaluate(async () => { await AppRouter.navigate("analysis/running", { historyMode: "replace" }); });
  const pace = root.locator(".analysis-subchart").filter({ has: page.getByRole("heading", { name: "Schwellenpace", exact: true }) });
  await pace.getByText("Werte ansehen", { exact: true }).click();
  await expect(pace.locator("tbody td:nth-child(2)")).toHaveText([/4:50 \/km/, /4:50 \/km/]);
  await page.evaluate(async () => { await AppRouter.navigate("analysis/load", { historyMode: "replace" }); });
  await page.locator("#analysisLoadPeriod").getByRole("button", { name: "4 Wochen", exact: true }).click();
  await page.evaluate(async () => { await AppRouter.navigate("analysis/cycling", { historyMode: "replace" }); });
  await expect(ftp.locator("tbody tr")).toHaveCount(5);
  await expect(vo2max.locator("tbody tr")).toHaveCount(2);
});

test("@responsive latest measurement stays visible without inventing measurements in empty weeks", async ({ page }) => {
  await performanceFixture(page, { history: { start: "2026-09-01", end: "2026-09-30", load: { points: [] }, metrics: {
    running_vo2max_ml_kg_min: [{ source: "Garmin Connect", points: [{ date: "2026-09-20", value: 49 }] }],
  } } });
  const root = page.locator("#analysisHistoryCharts");
  // One period selector (#3839), no load period.
  await expect(root.locator(".analysis-trend-controls")).toHaveCount(1);
  await expect(root.locator(".analysis-period-controls")).toHaveCount(1);
  await expect(root.locator("svg")).toHaveCount(0);
  await expect(root.locator(".analysis-current-line")).toHaveCount(0);
  await expect(root.locator(".analysis-point-value")).toHaveCount(0);
  await expect(root).toContainText("Ein einzelner Messwert zeigt noch keinen Verlauf");
  const values = root.locator(".analysis-subchart").filter({ hasText: "49" }).first().locator("details");
  await values.locator("summary").click();
  await expect(values.locator("tbody tr")).toHaveCount(1);
});

test("@responsive stale current value cannot float above the connected history line", async ({ page }) => {
  await performanceFixture(page, { history: { start: "2026-09-01", end: "2026-09-15", load: { points: [] }, metrics: {
    running_vo2max_ml_kg_min: [{ source: "Garmin Connect", points: [
      { date: "2026-09-01", value: 53 }, { date: "2026-09-02", value: 53 },
      { date: "2026-09-08", value: 53 }, { date: "2026-09-09", value: 53 }, { date: "2026-09-10", value: 53.5 },
    ] }],
  } } });
  const chart = page.locator("#analysisHistoryCharts svg");
  await expect(chart.locator(".analysis-current-line")).toHaveCount(0);
  const geometry = await chart.evaluate((svg) => {
    const path = svg.querySelector("path[data-series='0']").getAttribute("d");
    const line = [...path.matchAll(/[ML](-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/g)]
      .map((match) => [Number(match[1]).toFixed(2), Number(match[2]).toFixed(2)]);
    const markers = [...svg.querySelectorAll("circle[data-series='0']")]
      .map((circle) => [Number(circle.getAttribute("cx")).toFixed(2), Number(circle.getAttribute("cy")).toFixed(2)]);
    return { line, markers };
  });
  expect(geometry.line).toEqual(geometry.markers);
  await expect(chart).not.toContainText("53.5");
});

test("@responsive dense performance retains every measured week without overlapping labels", async ({ page }) => {
  const points = Array.from({ length: 12 }, (_, index) => ({ date: new Date(Date.UTC(2026, 6, 13 + index * 7)).toISOString().slice(0, 10), value: 47.6 + index * .15 }));
  await performanceFixture(page, { history: { start: "2026-07-13", end: "2026-09-30", load: { points: [] }, metrics: {
    running_vo2max_ml_kg_min: [{ source: "Garmin Connect", points }],
    cycling_ftp_watts: [{ source: "Garmin Connect", points: points.map((point, index) => ({ ...point, value: 266 + index })) }],
  } } });
  const charts = page.locator("#analysisHistoryCharts svg");
  const geometry = await charts.evaluateAll((charts) => charts.map((chart) => {
    const labels = [...chart.querySelectorAll(".analysis-point-value")].map((label) => {
      const box = label.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom };
    });
    return { count: chart.querySelectorAll("circle[data-series]").length, labels,
      overlap: labels.some((a, index) => labels.slice(index + 1).some((b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top)) };
  }));
  expect(geometry.map((chart) => chart.count)).toEqual([12, 12]);
  for (const chart of geometry) expect(chart.overlap).toBe(false);
  const vo2max = page.locator("#analysisHistoryCharts .analysis-subchart").filter({ has: page.getByRole("heading", { name: "VO₂max", exact: true }) }).filter({ has: page.locator("circle[data-series='0']") }).first();
  await vo2max.locator("details summary").click();
  await expect(vo2max.locator("tbody tr")).toHaveCount(12);
});

test("@responsive performance keeps predictions in the dedicated table", async ({ page }) => {
  await performanceFixture(page, { available: true, metrics: {
    cycling_ftp_watts: { value: 280, unit: "W" }, running_vo2max_ml_kg_min: { value: 52 },
    weight_kg: { value: 72, unit: "kg", source: "Garmin Connect" },
    run_5k_seconds: { value: 1200, unit: "s", source: "Garmin Connect", note: "Garmin Connect Laufprognose" },
  } });
  await expect(page.locator("#performancePredictions")).toContainText("Laufprognosen");
  await expect(page.locator("#performancePredictions")).toContainText("20:00");
  await expect(page.locator("#performanceSummary")).toBeHidden();
});

test("@responsive historical race estimates never create duplicate charts", async ({ page }) => {
  await performanceFixture(page, {
    metrics: { run_5k_seconds: { value: 1200, unit: "s", source: "Garmin Connect", note: "Garmin Connect Laufprognose" } },
    history: { start: "2026-09-01", end: "2026-10-01", load: { points: [] }, metrics: {
      run_5k_seconds: [{ source: "Garmin Connect", points: [{ date: "2026-09-01", value: 1250 }, { date: "2026-09-15", value: 1200 }] }],
    } },
  });
  await expect(page.locator("#performancePredictions")).toContainText("20:00");
  await expect(page.locator("#performancePredictions")).not.toContainText("Garmin");
  await expect(page.locator("#analysisHistoryCharts svg")).toHaveCount(0);
  await expect(page.locator("[data-race-distance]")).toHaveCount(0);
});

test("@responsive provider metrics render as charts without raw text", async ({ page }) => {
  await performanceFixture(page, {
    history: {
      start: "2026-09-01", end: "2026-10-01", load: { points: [] }, metrics: {},
      provider_metrics: {
        endurance_score: { source: "Garmin Connect", aggregation: "weekly", unit: "unknown", status: "stale", points: [{ date: "2026-09-28", values: { enduranceScore: 712 } }] },
        running_tolerance: { source: "Garmin Connect", aggregation: "weekly", unit: "unknown", status: "failed", points: [] },
        training_readiness_history: { source: "Garmin Connect", aggregation: "daily", unit: "unknown", status: "current", points: [{ date: "2026-09-30", value: 72 }] },
        weekly_training_status: { source: "Garmin Connect", aggregation: "weekly", unit: "unknown", status: "current", points: [{ date: "2026-09-28", value: 80 }] },
      },
    },
    personal_recovery: {
      as_of: "2026-10-01", sleep_target_hours: null, baselines: [], sleep_deficits: [],
      regularity: {
        method: "sleep-timing-variability-v3", timezone: "Europe/Berlin", status: "provisional", series: [{
          source: "Garmin Connect", method: "sleepStartTimestampGMT/sleepEndTimestampGMT", status: "provisional",
          coverage: { baseline_nights: 14, required_nights: 14 },
          points_14: [{ date: "2026-10-01", onset_at: "2026-09-30T22:15:00+02:00", wake_at: "2026-10-01T06:45:00+02:00", duration_hours: 8.5, onset_deviation_minutes: 15, wake_deviation_minutes: 10 }, { date: "2026-09-30" }],
          points_84: [],
        }],
      },
    },
  }, "load");
  const performance = page.locator("#analysisHistoryCharts");
  await expect(performance.getByRole("heading", { name: "Trainingsbereitschaft" })).toHaveCount(0);
  await expect(page.locator("#analysisLoadCharts").getByRole("heading", { name: "Trainingsbereitschaft" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Trainingsstatus · akute Belastung" })).toHaveCount(0);
  await expect(page.locator("#performanceEnduranceCharts").getByRole("heading", { name: "Ausdauer-Score" })).toBeVisible();
  await expect(performance.getByRole("heading", { name: "Lauf-Belastbarkeit" })).toHaveCount(0);
  await expect(performance).not.toContainText("Garmin-Provider-Metriken");
  await expect(performance).not.toContainText("Rohfeld");
  await page.evaluate(async () => { await AppRouter.navigate("analysis/recovery", { historyMode: "replace" }); });
  const recovery = page.locator("#personalRecovery");
  await expect(recovery.getByRole("heading", { name: "Trainingsbereitschaft" })).toBeVisible();
  await expect(recovery).not.toContainText("Schlafdefizit");
  await expect(recovery).not.toContainText("Schlafregelm");
  await expect(page.locator("#analysisTagImpact, #analysisComparisons")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive predictions list only race estimates and never body weight", async ({ page }) => {
  await performanceFixture(page, { metrics: {
    weight_kg: { value: 72.4, unit: "kg", source: "Garmin Connect" },
    run_10k_seconds: { value: 2500, unit: "s", source: "Garmin Connect", note: "Garmin Connect Laufprognose" },
  } });
  const predictions = page.locator("#performancePredictions");
  await expect(predictions.getByRole("heading", { name: "Laufprognosen" })).toBeVisible();
  await expect(predictions.locator("tbody tr")).toHaveCount(1);
  await expect(predictions).toContainText("10 km");
  await expect(predictions).not.toContainText("Garmin Connect");
  await expect(predictions).not.toContainText("Gewicht");
  await expect(predictions).not.toContainText("72");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive missing race predictions explain the reason instead of an empty block", async ({ page }) => {
  await performanceFixture(page, { metrics: {
    weight_kg: { value: 72.4, unit: "kg", source: "Garmin Connect" },
  } });
  const predictions = page.locator("#performancePredictions");
  await expect(predictions).toBeVisible();
  await expect(predictions.getByRole("heading", { name: "Laufprognosen" })).toBeVisible();
  await expect(predictions).toContainText("Noch keine Prognosen – dafür fehlen aktuelle Leistungswerte aus Garmin oder Wahoo.");
  await expect(predictions.locator("table")).toHaveCount(0);
  await expect(predictions).not.toContainText("Gewicht");
});

test("@responsive legend info buttons have a 44px layout box, a German name and an explanation that opens in place", async ({ page }) => {
  await performanceFixture(page, { history: {
    start: "2026-09-01", end: "2026-09-08", load: { points: [] },
    metrics: { cycling_ftp_watts: [{ source: "Garmin Connect", points: [{ date: "2026-09-01", value: 200 }, { date: "2026-09-08", value: 210 }] }] },
  } }, "cycling");
  const root = page.locator("#analysisHistoryCharts");
  const button = root.getByRole("button", { name: "Erklärung zu FTP", exact: true });
  await expect(button).toHaveAttribute("aria-expanded", "false");
  // Scroll, measure and hit-test in one synchronous step, so no layout change can fall between them;
  // the smooth-scroll preference is overridden so the button is at its final position at once.
  const layout = await button.evaluate((element) => {
    element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    const box = element.getBoundingClientRect();
    const hitLabel = (x, y) => document.elementFromPoint(x, y)?.closest(".analysis-legend-info")?.getAttribute("aria-label") ?? null;
    const center = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
    return {
      width: box.width,
      height: box.height,
      inViewport: box.left >= 0 && box.top >= 0 && box.right <= innerWidth && box.bottom <= innerHeight,
      centerHit: hitLabel(center.x, center.y),
      edgeHit: hitLabel(center.x + 20, center.y),
    };
  });
  expect(layout.width).toBeGreaterThanOrEqual(44);
  expect(layout.height).toBeGreaterThanOrEqual(44);
  expect(layout.inViewport).toBe(true);
  expect(layout.centerHit).toBe("Erklärung zu FTP");
  expect(layout.edgeHit).toBe("Erklärung zu FTP");
  await button.click();
  const info = root.getByRole("note").filter({ hasText: "FTP · Garmin Connect" });
  await expect(info).toBeVisible();
  await expect(button).toHaveAttribute("aria-expanded", "true");
  const viewport = page.viewportSize();
  const bounds = await info.boundingBox();
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive legend info buttons are at least 44x44 and never overlap each other", async ({ page }) => {
  // Fitness, fatigue and form give three neighbouring legend info buttons.
  const chart = await localFitnessFixture(page);
  const buttons = chart.locator(".analysis-chart-legend .analysis-legend-info");
  await expect(buttons).toHaveCount(3);
  const count = await buttons.count();
  const boxes = [];
  for (let index = 0; index < count; index += 1) {
    const box = await buttons.nth(index).boundingBox();
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
    boxes.push(box);
  }
  for (let first = 0; first < boxes.length; first += 1) {
    for (let second = first + 1; second < boxes.length; second += 1) {
      const a = boxes[first], b = boxes[second];
      const overlaps = a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
      expect(overlaps, `legend buttons ${first} and ${second} overlap`).toBe(false);
    }
  }
});

test("@responsive empty performance charts are omitted without a missing-data list", async ({ page }) => {
  await performanceFixture(page, { history: { start: "2026-09-01", end: "2026-09-08", load: { points: [] }, metrics: {
    cycling_ftp_watts: [{ source: "Garmin Connect", points: [{ date: "2026-09-01", value: 200 }, { date: "2026-09-08", value: 210 }] }],
    run_threshold_pace_seconds_per_km: [{ source: "Garmin Connect", points: [{ date: "2026-09-01", value: null }, { date: "2026-09-08", value: null }] }],
  } } }, "cycling");
  const root = page.locator("#analysisHistoryCharts");
  await expect(root.locator("svg")).toHaveCount(1);
  await expect(root.getByRole("heading", { name: "Schwellenpace", exact: true })).toHaveCount(0);
  await expect(root).not.toContainText("Keine Daten");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive performance without any history renders no empty sport groups or tiles", async ({ page }) => {
  await performanceFixture(page, { history: { load: { points: [] }, metrics: {} } });
  const root = page.locator("#analysisHistoryCharts");
  await expect(root.locator(".analysis-group-title")).toHaveCount(0);
  await expect(root.locator("svg")).toHaveCount(0);
  await expect(page.locator("#currentPerformance table")).toHaveCount(0);
  await expect(page.locator("#currentPerformance")).toContainText("Noch keine aktuellen Werte.");
  await expect(page.locator("#currentPerformance")).not.toContainText("—");
});

test("@responsive load tab draws the local fitness, fatigue and form chart", async ({ page }) => {
  await performanceFixture(page, (performance) => {
    const end = performance.history.end;
    const days = Array.from({ length: 28 }, (_, index) => {
      const date = new Date(`${end}T12:00:00Z`);
      date.setUTCDate(date.getUTCDate() - 27 + index);
      return { date: date.toISOString().slice(0, 10), state: "loaded", load: 60, fitness: 50 + index / 10, fatigue: 55, form: index / 10 - 5 };
    });
    days[20] = { ...days[20], state: "incomplete", sessions_without_load: 1, session_rpe_au_without_load: 270 };
    return { history: { ...performance.history, local_fitness_fatigue: { status: "estimate", incomplete_days: 1, incomplete_share: 0.036, days } } };
  }, "load");
  const chart = page.locator("#analysisLoadCharts .analysis-chart-card").filter({ has: page.getByRole("heading", { name: /^Fitness, Ermüdung, Form/ }) });
  await expect(chart).toBeVisible();
  await expect(chart).toContainText("1 Einheit ohne TSS");
  await expect(chart).toContainText("4 % der Tage unvollständig");
  await expect(chart).toContainText("Lokal berechnet");
  await expect(chart).not.toContainText("Intervals");
});

const strengthWeeks = (count, make) => Array.from({ length: count }, (_, index) => ({
  week_start: new Date(Date.UTC(2026, 7, 31 + index * 7)).toISOString().slice(0, 10), ...make(index),
}));

test("@responsive Kraft shows how often per week the upper body and the legs were trained", async ({ page }) => {
  // Athlete decision B2: one chart per side with its average, unknown sessions counted apart.
  const requested = [];
  await page.route("**/api/analysis/strength*", (route) => {
    requested.push(new URL(route.request().url()).searchParams.get("weeks"));
    const weeks = Number(new URL(route.request().url()).searchParams.get("weeks"));
    return route.fulfill({ json: {
      start: "2026-08-31", end: "2026-11-20",
      weeks: strengthWeeks(weeks, (index) => ({ sessions: 2, upper: index % 3 === 0 ? 0 : 2, legs: 1, unknown: index === 1 ? 1 : 0 })),
    } });
  });
  await performanceFixture(page, {}, "strength");
  const root = page.locator("#strengthAnalysis");
  await expect(page.locator('[data-analysis-segment="strength"]')).toHaveAttribute("aria-current", "page");
  await expect(root.getByRole("heading", { name: "Oberkörper" })).toBeVisible();
  await expect(root.getByRole("heading", { name: "Beine" })).toBeVisible();
  await expect(root.getByRole("heading", { name: "Top-Sätze" })).toHaveCount(0);
  await expect(root).toContainText("1 Einheit ohne Oberkörper- oder Beinschwerpunkt, nicht zugeordnet.");
  await expect(root).not.toContainText(/1RM|Kraftlast/);
  // The shared period selector reads the chosen number of weeks.
  const period = page.locator("#strengthPeriod");
  await expect(period.getByRole("button", { name: "3 Monate" })).toHaveAttribute("aria-pressed", "true");
  await period.getByRole("button", { name: "4 Wochen" }).click();
  await expect.poll(() => requested.at(-1)).toBe("4");
  await expect(period.getByRole("button", { name: "4 Wochen" })).toHaveAttribute("aria-pressed", "true");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive a short Kraft period without sessions offers the longer one", async ({ page }) => {
  await page.route("**/api/analysis/strength*", (route) => {
    const weeks = Number(new URL(route.request().url()).searchParams.get("weeks"));
    return route.fulfill({ json: { start: "2026-08-31", end: "2026-11-20", weeks: strengthWeeks(weeks, (index) => ({ sessions: weeks >= 13 && index === 0 ? 1 : 0, upper: weeks >= 13 && index === 0 ? 1 : 0, legs: 0, unknown: 0 })) } });
  });
  await performanceFixture(page, {}, "strength");
  const root = page.locator("#strengthAnalysis");
  // With a connected source the empty card names the period and widens it.
  await page.evaluate(() => { state.data.garmin = { ...state.data.garmin, configured: true }; });
  await page.locator("#strengthPeriod").getByRole("button", { name: "4 Wochen" }).click();
  // The controls stay reachable above the empty card, which also offers the longer period.
  await expect(page.locator("#analysisEmptyState")).toContainText("Keine Daten in den letzten 4 Wochen");
  await page.locator("#analysisEmptyState").getByRole("button", { name: "3 Monate anzeigen" }).click();
  await expect(root.getByRole("heading", { name: "Oberkörper" })).toBeVisible();
  await expect(page.locator("#strengthPeriod").getByRole("button", { name: "3 Monate" })).toHaveAttribute("aria-pressed", "true");
});

test("@responsive Kraft without stored sets shows the one empty card", async ({ page }) => {
  await page.route("**/api/analysis/strength*", (route) => route.fulfill({ json: {
    start: "2026-08-31", end: "2026-11-20",
    weeks: strengthWeeks(13, () => ({ sessions: 0, upper: 0, legs: 0, unknown: 0 })),
  } }));
  await performanceFixture(page, {}, "strength");
  await expect(page.locator("#analysisEmptyState")).toContainText("Noch keine Kraftdaten");
  await expect(page.locator("#strengthAnalysis")).toBeHidden();
});

const PERIOD_BUTTONS = ["4 Wochen", "3 Monate", "6 Monate", "12 Monate"];

// #3839: one period set on every Analyse and Ernährung selector; Vorjahr is gone.
test("@responsive every period control offers the same four periods and no Vorjahr", async ({ page }) => {
  await page.goto("/#analysis/load");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.loadedAreas.has("performance") && !state.loadPromise);
  const controls = [
    ["analysis/load", "Zeitraum für Belastung", "3 Monate", async (label, weekly) => {
      await expect(page.locator("#analysisLoadCharts .analysis-axis-unit").first()).toHaveText(`Belastungspunkte · ${weekly ? "Wochenstand" : "Tageswert"}`);
      await expect(page.locator("#sessionPerformance").getByText(new RegExp(`Letzte ${label}:`))).toHaveCount(1);
    }],
    ["analysis/running", "Zeitraum für Entwicklung", "3 Monate", async (label, weekly, weeks) => {
      await expect(page.locator("#analysisHistoryCharts").getByText(new RegExp(`Letzte ${weeks} Kalenderwochen`)).first()).toBeAttached();
    }],
    // Laufen and Radfahren share the one development period, left on 12 Monate by Laufen.
    ["analysis/cycling", "Zeitraum für Entwicklung", "12 Monate", async (label, weekly, weeks) => {
      await expect(page.locator("#analysisHistoryCharts").getByText(new RegExp(`Letzte ${weeks} Kalenderwochen`)).first()).toBeAttached();
    }],
    ["analysis/recovery", "Zeitraum für Erholung", "4 Wochen", async (label) => {
      await expect(page.locator("#personalRecovery").getByRole("heading", { name: new RegExp(`^Erholung · Letzte ${label}`) })).toBeVisible();
    }],
    ["nutrition/week", "Zeitraum für Ernährung", "4 Wochen", async (label) => {
      await expect(page.locator("#nutritionWeekChartPeriod")).toHaveText(label);
    }],
    ["nutrition/body", "Zeitraum für Körper", "3 Monate", async (label, weekly) => {
      await expect(page.locator("#bodyAnalysisCharts [data-body-metric]").first()).toBeVisible();
      await expect(page.locator("#bodyAnalysisCharts .analysis-week-caption")).toHaveCount(weekly ? await page.locator("#bodyAnalysisCharts [data-body-metric]").count() : 0);
    }],
  ];
  for (const [route, name, initial, follows] of controls) {
    await test.step(route, async () => {
      await page.evaluate(async (target) => { await AppRouter.navigate(target, { historyMode: "replace" }); }, route);
      const group = page.getByRole("group", { name, exact: true });
      await expect(group.getByRole("button")).toHaveText(PERIOD_BUTTONS);
      await expect(group.getByRole("button", { name: initial, exact: true })).toHaveAttribute("aria-pressed", "true");
      await expect(page.getByRole("button", { name: "Vorjahr" })).toHaveCount(0);
      for (const [index, label] of PERIOD_BUTTONS.entries()) {
        await group.getByRole("button", { name: label, exact: true }).click();
        for (const other of PERIOD_BUTTONS) {
          await expect(group.getByRole("button", { name: other, exact: true })).toHaveAttribute("aria-pressed", String(other === label));
        }
        await follows(label, index > 0, [4, 13, 26, 52][index]);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });
  }
});

test("@responsive longer trend periods are read only on demand", async ({ page, request }) => {
  // #3839: 4 Wochen (Erholung) and 3 Monate (the rest) come with the state; the rest asks once.
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  const requested = [];
  const shift = (key, days) => { const date = new Date(`${key}T12:00:00Z`); date.setUTCDate(date.getUTCDate() + days); return date.toISOString().slice(0, 10); };
  await page.route("**/api/analysis/trend-history**", async (route) => {
    const days = Number(new URL(route.request().url()).searchParams.get("days"));
    requested.push(days);
    // A failed longer read shows its note instead of an unexplained short window.
    if (days === 365) return route.fulfill({ status: 503, json: { error: "E2E fixture: nicht erreichbar" } });
    const performance = await page.evaluate(() => state.data.performance);
    const end = performance.personal_recovery.as_of || performance.history.end;
    await route.fulfill({ json: { start: shift(end, 1 - days), end, days, history: performance.history, training_focus: performance.training_focus, personal_recovery: performance.personal_recovery } });
  });
  await page.goto("/#analysis/recovery");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.loadedAreas.has("performance") && !state.loadPromise);
  const recovery = page.locator("#personalRecovery");
  await expect(recovery.getByRole("heading", { name: /^Erholung · Letzte 4 Wochen/ })).toBeVisible();
  expect(requested).toEqual([]);
  // The base read's baselines hold 84 days, so Erholung's 3 Monate read their own history.
  await recovery.getByRole("button", { name: "3 Monate", exact: true }).click();
  await expect(recovery.getByRole("heading", { name: /^Erholung · Letzte 3 Monate/ })).toBeVisible();
  await expect.poll(() => requested).toEqual([91]);
  await recovery.getByRole("button", { name: "6 Monate", exact: true }).click();
  await expect(recovery.getByRole("heading", { name: /^Erholung · Letzte 6 Monate/ })).toBeVisible();
  await expect.poll(() => requested).toEqual([91, 182]);
  await expect(recovery.getByRole("button", { name: "Vorjahr" })).toHaveCount(0);
  await page.evaluate(async () => { await AppRouter.navigate("analysis/running", { historyMode: "replace" }); });
  const development = page.locator("#analysisHistoryCharts");
  // 3 Monate need no long read: the state holds them.
  await development.getByRole("button", { name: "3 Monate", exact: true }).click();
  expect(requested).toEqual([91, 182]);
  await development.getByRole("button", { name: "12 Monate", exact: true }).click();
  await expect.poll(() => requested).toContain(365);
  await expect(development.getByText("Der längere Zeitraum konnte nicht geladen werden.")).toBeVisible();
  await expect(development.getByRole("button", { name: "12 Monate", exact: true })).toHaveAttribute("aria-pressed", "true");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

// #3824: a re-render after a longer period's read must keep the other sport's group hidden.
test("@responsive a longer period keeps each sport tab to its own sport", async ({ page, request }) => {
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  await page.route("**/api/analysis/trend-history**", async (route) => {
    const days = Number(new URL(route.request().url()).searchParams.get("days"));
    const performance = await page.evaluate(() => state.data.performance);
    const end = performance.history.end;
    const start = new Date(`${end}T12:00:00Z`); start.setUTCDate(start.getUTCDate() + 1 - days);
    await route.fulfill({ json: { start: start.toISOString().slice(0, 10), end, days, history: performance.history, training_focus: performance.training_focus, personal_recovery: performance.personal_recovery } });
  });
  await page.goto("/#analysis/running");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.loadedAreas.has("performance") && !state.loadPromise);
  const development = page.locator("#analysisHistoryCharts");
  await expect(page.locator("[data-analysis-section='development-Lauf']")).toBeVisible();
  // Checked right after the long read's re-render, not polled: a later unrelated re-render must not hide the bug.
  const answered = page.waitForResponse((response) => response.url().includes("/api/analysis/trend-history"));
  await development.getByRole("button", { name: "6 Monate", exact: true }).click();
  await answered;
  await page.waitForTimeout(300);
  await expect(development.getByRole("button", { name: "6 Monate", exact: true })).toHaveAttribute("aria-pressed", "true");
  expect(await page.locator("[data-analysis-section='development-Lauf']").isVisible()).toBe(true);
  expect(await page.locator("[data-analysis-section='development-Rad']").isVisible()).toBe(false);
  expect(await development.getByRole("heading", { name: /Leistungsschwelle · FTP/ }).isVisible()).toBe(false);
  // Every drawn series in the running tab is a running one.
  const shownLabels = await page.evaluate(() => [...document.querySelectorAll("#analysisHistoryCharts svg circle title")]
    .filter((title) => title.closest("[data-analysis-sport]")?.checkVisibility())
    .map((title) => title.textContent));
  expect(shownLabels.length).toBeGreaterThan(0);
  expect(shownLabels.filter((label) => !label.startsWith("Lauf"))).toEqual([]);
  // The mirror case: Radfahren with a longer period shows no running group.
  await page.evaluate(async () => { await AppRouter.navigate("analysis/cycling", { historyMode: "replace" }); });
  const longer = page.waitForResponse((response) => response.url().includes("/api/analysis/trend-history?days=365"));
  await development.getByRole("button", { name: "12 Monate", exact: true }).click();
  await longer;
  await page.waitForTimeout(300);
  expect(await page.locator("[data-analysis-section='development-Rad']").isVisible()).toBe(true);
  expect(await page.locator("[data-analysis-section='development-Lauf']").isVisible()).toBe(false);
  // The running part of the sport tabs is the critical-speed line since the bests moved to Rekorde (#3826).
  expect(await page.locator("#runningCriticalSpeed").isVisible()).toBe(false);
});

// #3828: a bare history redraw (as the LT1 background refresh does) applies the shown tab itself, so no
// caller has to remember the segment pass.
test("a bare history redraw keeps each sport tab to its own sport", async ({ page, request }) => {
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  for (const [route, own, other] of [["running", "Lauf", "Rad"], ["cycling", "Rad", "Lauf"]]) {
    await page.goto(`/#analysis/${route}`);
    await expect(page.locator("#appShell")).toBeVisible();
    await page.waitForFunction(() => state.loadedAreas.has("performance") && !state.loadPromise);
    await expect(page.locator(`[data-analysis-section='development-${own}']`)).toBeVisible();
    // Checked once, in the same task as the redraw, so no later re-render can hide the bug.
    const shown = await page.evaluate(([mine, theirs]) => {
      renderAnalysisHistory(state.data.performance.history);
      const visible = (name) => Boolean(document.querySelector(`[data-analysis-section='development-${name}']`)?.checkVisibility());
      return { own: visible(mine), other: visible(theirs) };
    }, [own, other]);
    expect(shown, route).toEqual({ own: true, other: false });
  }
});

// #3928 A-01: at 12 months the date labels of a daily chart stay apart; the weekly caption sits clear of the first label.
test("@responsive date labels do not overprint at 12 months", async ({ page, request }) => {
  test.setTimeout(120_000);
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  for (const segment of ["recovery", "cycling", "running"]) {
    await page.goto(`/#analysis/${segment}`);
    await expect(page.locator("#dataPanel.active")).toBeVisible();
    await page.locator("#dataPanel .analysis-period-controls:visible").getByRole("button", { name: "12 Monate", exact: true }).first().click();
    await expect(page.locator("#dataPanel .loading-state")).toHaveCount(0);
    const overlaps = await page.locator("#dataPanel svg").evaluateAll((charts) => charts.flatMap((chart) => {
      const boxes = [...chart.querySelectorAll(".analysis-date-tick, .analysis-week-caption")].map((node) => node.getBoundingClientRect()).filter((box) => box.width).sort((a, b) => a.left - b.left);
      return boxes.slice(1).map((box, index) => (box.left < boxes[index].right - 0.5 && box.top < boxes[index].bottom && box.bottom > boxes[index].top ? `${Math.round(boxes[index].left)}-${Math.round(boxes[index].right)} meets ${Math.round(box.left)}` : "")).filter(Boolean);
    }));
    expect(overlaps, segment).toEqual([]);
  }
});
