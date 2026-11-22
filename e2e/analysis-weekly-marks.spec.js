const { test, expect } = require("@playwright/test");
const { waitForSettledState } = require("./helpers/settled-state");

// #3450: a Tuesday, so the current calendar week is unfinished.
const END = "2026-11-17";
const DAYS = Array.from({ length: 100 }, (_, index) => {
  const date = new Date(`${END}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() - 99 + index);
  return date.toISOString().slice(0, 10);
});

function weeklyHistory(performance) {
  const wave = (index, size) => Math.round(Math.sin(index / 4) * size);
  const daily = (value) => DAYS.map((date, index) => ({ date, value: value(index) }));
  const weekly = (value) => DAYS.filter((_, index) => index % 7 === 2 || index === DAYS.length - 1).map((date, index) => ({ date, value: value(index) }));
  return { history: {
    ...performance.history, start: DAYS[0], end: END, load: { source: "Garmin Connect", points: daily((index) => 300 + index) },
    training_time: { source: "Garmin Connect", points: daily((index) => 1 + (index % 3) / 2) },
    session_rpe_load: { days: DAYS.map((date, index) => ({ date, value: 300 + (index % 5) * 40, rated_sessions: 1, total_sessions: 1, rpe_sources: { provider: 0 } })) },
    smoothed_session_load: { days: DAYS.map((date, index) => ({ date, short: 400 + wave(index, 120), long: 350 + index })) },
    local_fitness_fatigue: { status: "estimate", days: DAYS.map((date, index) => ({ date, state: "loaded", load: 60, fitness: 40 + index / 4, fatigue: 55 + wave(index, 15), form: index / 4 - 15 - wave(index, 15) })) },
    daily_activity: { source: "Garmin Connect", steps: daily((index) => 6000 + (index % 7) * 700), floors: daily((index) => 5 + (index % 4)) },
    metrics: {
      running_vo2max_ml_kg_min: [{ source: "Garmin Connect", source_provider: "garmin", points: weekly((index) => 47 + index / 5) }],
      run_threshold_pace_seconds_per_km: [{ source: "Garmin Connect", source_provider: "garmin", points: weekly((index) => 275 - index * 2) }],
    },
    // The server dates LT1 at each past week's Sunday and the running week at today.
    first_thresholds: { status: "ready", ride: [], run: [...DAYS.filter((date) => new Date(`${date}T12:00:00Z`).getUTCDay() === 0).slice(-11), END]
      .map((date, index) => ({ date, value: 330 - index, hr_bpm: 140, origin: "own_sessions" })) },
  } };
}

// Per series: the mark centres (x) with their tops (y), and each value label with its gap to its own mark.
function chartGeometry(svg) {
  return svg.evaluate((element) => {
    const series = new Map();
    const add = (key, x, y) => series.set(key, [...(series.get(key) || []), { x, y }]);
    for (const bar of element.querySelectorAll("rect.analysis-series-bar")) add("bar", Number(bar.getAttribute("x")) + Number(bar.getAttribute("width")) / 2, Number(bar.getAttribute("y")));
    for (const dot of element.querySelectorAll("circle[data-series]")) add(dot.dataset.series, Number(dot.getAttribute("cx")), Number(dot.getAttribute("cy")));
    for (const points of series.values()) points.sort((a, b) => a.x - b.x);
    const labels = [...element.querySelectorAll(".analysis-point-value[data-value-series]")].map((label) => {
      // A label belongs to the mark of its series that it spans horizontally.
      const box = label.getBBox();
      const own = (series.get(label.dataset.valueSeries) || series.get("bar") || []).filter((mark) => mark.x >= box.x && mark.x <= box.x + box.width);
      const gap = Math.min(...own.map((mark) => Math.max(0, box.y - mark.y, mark.y - (box.y + box.height))));
      return { text: label.textContent, series: label.dataset.valueSeries, left: box.x, right: box.x + box.width, top: box.y, bottom: box.y + box.height, gap };
    });
    return { marks: [...series.entries()], labels };
  });
}

// Visits the six charts of the athlete report and the pace chart (top label) with the unfinished week.
async function eachWeeklyChart(page, visit) {
  await page.route("**/api/performance", async (request) => {
    const response = await request.fetch();
    const data = await response.json();
    Object.assign(data.performance, { metrics: {}, thresholds: {} }, weeklyHistory(data.performance));
    await request.fulfill({ response, json: data });
  });
  for (const [route, root, titles] of [
    ["load", "#analysisLoadCharts", [/^Trainingszeit/, /^RPE-Belastung/, /^RPE-Belastung kurz und lang/, /^Fitness, Ermüdung, Form/, /^Schritte/]],
    ["running", "#analysisHistoryCharts", [/^VO₂max$/, /^Schwellenpace$/]],
  ]) {
    await page.goto(`/#analysis/${route}`);
    await expect(page.locator("#appShell")).toBeVisible();
    await page.waitForFunction(() => state.loadedAreas.has("performance") && !state.loadPromise);
    await waitForSettledState(page);
    for (const title of titles) {
      const card = page.locator(root).locator(".analysis-chart-card, .analysis-subchart").filter({ has: page.getByRole("heading", { name: title }) }).last();
      await expect(card.locator("svg")).toBeVisible();
      await visit(String(title).replaceAll(/\W+/g, " ").trim(), card);
    }
  }
}

test("@responsive an unfinished week keeps one mark in its own week slot, labelled at the right-most mark", async ({ page }, testInfo) => {
  await eachWeeklyChart(page, async (name, card) => {
    if (testInfo.project.name === "mobile") await card.screenshot({ path: testInfo.outputPath(`${name.replaceAll(" ", "-")}.png`) });
    const { marks, labels } = await chartGeometry(card.locator("svg"));
    for (const [key, points] of marks) {
      // 3 Monate are 13 weeks; the LT1 line stays at the server's 12 weeks.
      expect.soft(points.length, name).toBe(key === "1" && name === "Schwellenpace" ? 12 : 13);
      const gaps = points.slice(1).map((point, index) => point.x - points[index].x);
      expect.soft(Math.max(...gaps) - Math.min(...gaps), `${name}: one mark per week slot`).toBeLessThan(1);
      const newest = points.at(-1);
      expect.soft(labels.some((label) => (label.series === key || key === "bar") && label.left <= newest.x && label.right >= newest.x && label.gap <= 20), `${name}: newest mark labelled`).toBe(true);
    }
  });
});

test("@responsive value labels stay next to their own mark and inside the chart", async ({ page }) => {
  await eachWeeklyChart(page, async (name, card) => {
    const { labels } = await chartGeometry(card.locator("svg"));
    expect.soft(labels.length, name).toBeGreaterThan(0);
    for (const label of labels) {
      expect.soft(label.gap, `${name}: ${label.text} next to its mark`).toBeLessThanOrEqual(20);
      expect.soft(label.top, `${name}: ${label.text} not clipped`).toBeGreaterThanOrEqual(0);
      expect.soft(label.bottom, `${name}: ${label.text} not clipped`).toBeLessThanOrEqual(200);
    }
  });
});
