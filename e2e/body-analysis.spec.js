const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

function bodyHistory() {
  const start = "2026-07-15";
  const dates = Array.from({ length: 84 }, (_, index) => new Date(Date.parse(`${start}T12:00:00Z`) + index * 86400000).toISOString().slice(0, 10));
  const make = (source, metric, transform) => ({ source, source_provider: source.startsWith("Garmin") ? "garmin" : "intervals", unit: metric === "weight_kg" ? "kg" : metric === "body_fat_pct" ? "%" : "W/kg",
    ...(metric === "cycling_w_per_kg" ? { power_method: "FTP" } : {}),
    points: dates.map((date, index) => {
      const missingWeek = index >= 35 && index <= 41;
      const value = missingWeek || (metric === "body_fat_pct" && source === "Intervals.icu Wellness") ? null : transform(index);
      if (value == null) return { date, value: null };
      const point = { date, value: Math.round(value * 1000) / 1000, observed_at: date, synced_at: `${source} sync` };
      if (metric === "cycling_w_per_kg") Object.assign(point, {
        ftp_watts: 280 + index, ftp_observed_at: date, ftp_synced_at: `${source} FTP sync`,
        weight_kg: 70, weight_observed_at: dates[Math.max(0, index - 1)], weight_synced_at: "Garmin weight sync",
        weight_source: "Garmin Connect", weight_age_days: 1,
      });
      return point;
    }),
  });
  const metrics = {
    weight_kg: [make("Intervals.icu Wellness", "weight_kg", (index) => 70 + index / 100), make("Garmin Connect", "weight_kg", (index) => 71 + index / 100)],
    body_fat_pct: [make("Intervals.icu Wellness", "body_fat_pct", () => 18), make("Garmin Connect", "body_fat_pct", (index) => 19 + index / 100)],
    cycling_w_per_kg: [make("Garmin Connect", "cycling_w_per_kg", (index) => (280 + index) / 70)],
  };
  const end = dates.at(-1);
  return { start, end, load: { points: [] }, metrics: {}, body: { version: 1, default_window: "91d", preferred_providers: { weight_kg: "garmin", body_fat_pct: "garmin", cycling_w_per_kg: "garmin" }, windows: {
    "28d": { start: dates.at(-28), end, days: 28, metrics: Object.fromEntries(Object.entries(metrics).map(([key, values]) => [key, values.map((item) => ({ ...item, points: item.points.slice(-28) }))])) },
    "91d": { start, end, days: 84, metrics },
  } } };
}

test("@responsive Body plots measurements and W/kg appears only in the cycling table", async ({ page }) => {
  await page.route("**/api/performance", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.performance.history = bodyHistory();
    await route.fulfill({ response, json: data });
  });
  await page.goto("/#nutrition/body");
  await expect(page.locator("#appShell")).toBeVisible();
  const root = page.locator("#bodyAnalysisCharts");
  await expect(root).toBeVisible();
  await expect(page.locator('[data-analysis-segment="body"]')).toHaveCount(0);
  await expect(page.locator('[data-nutrition-segment="body"]')).toHaveAttribute("aria-current", "page");
  await expect(page.locator("#nutritionDiary")).toBeHidden();
  await expect(root.locator("[data-body-metric]")).toHaveCount(2);
  await expect(root.locator("[data-body-metric='weight_kg'] svg")).toHaveCount(1);
  const overview = root.locator("[data-body-overview]");
  // Weight, body fat and the energy goal (#3547).
  await expect(overview.locator("tbody tr")).toHaveCount(3);
  await expect(overview.locator("[data-metric='energy_goal'] td").first()).toHaveText("nicht gesetzt");
  await expect(overview.locator("[data-metric='weight_kg'] td").first()).toHaveText("71,8 kg");
  // #3336, #3402, #3930: the fixture has no own value, so the row shows the device value in use with its source.
  await expect(overview.locator("[data-metric='body_fat_pct'] td").first()).toHaveText("19,8 %");
  await expect(overview.locator("th small")).toHaveText(["Garmin Connect · 06.10.2026", "Garmin Connect · 06.10.2026"]);
  await expect(overview.locator("tbody tr")).toHaveCount(3);
  // The skinfold and energy reads re-render the view, so the check re-resolves the overview.
  await expect.poll(() => overview.evaluate((element) => element.closest("section")?.parentElement?.firstElementChild.contains(element) ?? false)).toBe(true);
  // The body charts name no provider; only the energy cards label the Garmin estimate (#3099),
  // and the Körperwerte name each value's source (#3402).
  const bodyCards = root.locator(":scope > :not([data-energy], [data-energy-fit]):not(:has([data-body-overview]))");
  await expect(bodyCards.filter({ hasText: "Intervals.icu" })).toHaveCount(0);
  await expect(bodyCards.filter({ hasText: "Garmin" })).toHaveCount(0);
  const missing = root.locator("[data-body-metric='weight_kg']");
  await expect(missing.locator("circle[data-series='0']")).toHaveCount(11);
  // #3450: a weekly mark sits on its rolling week's start, so the axis reads in weeks and keeps the first mark off the value axis.
  await expect(missing.locator(".analysis-week-caption")).toHaveText("Woche ab");
  expect(Math.min(...await missing.locator("circle[data-series='0']").evaluateAll((dots) => dots.map((dot) => Number(dot.getAttribute("cx")))))).toBeGreaterThan(60);
  await root.getByRole("button", { name: "4 Wochen", exact: true }).click();
  await expect(root.locator("[data-body-metric='weight_kg'] circle[data-series='0']")).toHaveCount(28);
  await expect(root.locator("[data-body-metric='body_fat_pct'] circle[data-series='0']")).toHaveCount(28);
  await expect(root.locator("circle[data-series='1']")).toHaveCount(0);
  await expect(overview.locator("[data-metric='weight_kg'] td").first()).toHaveText("71,8 kg");
  await page.evaluate(() => {
    state.data.profile.body_fat_pct = "17.5";
    renderBodyAnalysis(state.data.performance.history.body);
  });
  // A profile body fat is not KFA (#3252, #3336): the row keeps the device value, not the profile's 17,5 %.
  await expect(root.locator("[data-body-overview] [data-metric='body_fat_pct'] td").first()).toHaveText("19,8 %");
  await expect(root.locator("[data-body-overview] [data-metric='body_fat_pct'] th small")).toHaveText("Garmin Connect · 06.10.2026");
  await expect(root.locator("[data-body-metric='body_fat_pct'] circle[data-series='0']")).toHaveCount(28);
  await page.evaluate(() => { state.data.profile.body_fat_pct = ""; });
  await page.evaluate(() => {
    const body = structuredClone(state.data.performance.history.body);
    for (const window of Object.values(body.windows)) {
      for (const metric of ["weight_kg", "body_fat_pct"]) {
        window.metrics[metric] = window.metrics[metric].filter((item) => item.source_provider !== "garmin");
      }
    }
    renderBodyAnalysis(body);
  });
  await expect(overview.locator("td:first-of-type")).toHaveText(["—", "—", "nicht gesetzt"]);
  await expect(root.locator("circle[data-series]")).toHaveCount(0);
  await expect(root.locator("[data-body-metric]")).toHaveCount(0);
  await expect(root).not.toContainText("Keine Daten");
  expect(await new AxeBuilder({ page }).include("#bodyAnalysisCharts").analyze()).toEqual(expect.objectContaining({ violations: [] }));
  await expect(root.locator("[data-body-metric='cycling_w_per_kg']")).toHaveCount(0);
  await page.evaluate(() => { location.hash = "#analysis/cycling"; });
  const weightRoot = page.locator("#analysisHistoryCharts [data-analysis-section='development-Rad']");
  await expect(weightRoot).toBeVisible();
  await expect(root).toBeHidden();
  await expect(weightRoot.locator("[data-body-metric='cycling_w_per_kg']")).toHaveCount(0);
  await expect(page.locator("#currentPerformance [data-metric='cycling_w_per_kg'] td")).toHaveText("5,2 W/kg");
  expect((await new AxeBuilder({ page }).include("#analysisHistoryCharts").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive Body keeps old measurements dated and long gaps empty", async ({ page }) => {
  await page.route("**/api/performance", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    const history = bodyHistory();
    for (const window of Object.values(history.body.windows)) {
      for (const metric of ["weight_kg", "body_fat_pct"]) {
        for (const series of window.metrics[metric]) {
          series.points = series.points.filter((point) => ["2026-07-15", "2026-08-14"].includes(point.date));
        }
      }
    }
    data.performance.history = history;
    await route.fulfill({ response, json: data });
  });
  await page.goto("/#nutrition/body");
  const root = page.locator("#bodyAnalysisCharts");
  await expect(root.locator("[data-body-overview] caption")).toHaveText("Körperwerte");
  await expect(root.locator("[data-body-overview] th small")).toHaveText(["Garmin Connect · 14.08.2026", "Garmin Connect · 14.08.2026"]);
  await expect(root.locator("[data-body-metric='weight_kg'] circle[data-series='0']")).toHaveCount(2);
  await root.getByRole("button", { name: "4 Wochen", exact: true }).click();
  await expect(root.locator("[data-body-metric]")).toHaveCount(0);
  await expect(root).not.toContainText("Keine Daten");
  await expect(root.locator("[data-body-overview] th small")).toHaveText(["Garmin Connect · 14.08.2026", "Garmin Connect · 14.08.2026"]);
});


// #3640: weight and body fat are measurements Körper enters; the profile has no field for them.
test("@responsive the profile holds no weight or body fat and Körper still enters both", async ({ page }) => {
  await page.goto("/#more/profile");
  const form = page.locator("#profileForm");
  await expect(form).toBeVisible();
  // Every profile section starts closed (#2262).
  await form.locator("> details.profile-section").first().locator(":scope > summary").click();
  await expect(form.locator('[name="height_cm"]')).toBeVisible();
  await expect(form.locator('[name="weight_kg"], [name="body_fat_pct"]')).toHaveCount(0);
  // Körper's weigh-in rule decides the current values, so no source choice is left (#3640).
  await expect(form.locator('[name="preferred_source_body"]')).toHaveCount(0);
  await expect(page.locator("#retryBodyGarmin")).toHaveCount(0);
  await page.goto("/#nutrition/body");
  const overview = page.locator("#bodyAnalysisCharts [data-body-overview]");
  await expect(overview.getByRole("button", { name: "Gewicht eintragen" })).toBeVisible();
  await expect(overview.getByRole("button", { name: "Körperfett eintragen" })).toBeVisible();
});

// #3829: the Gewicht dialog reads the day before it opens, so a slow read never wipes a typed weight.
test("@responsive a weight typed right after the dialog opens is kept on a slow connection", async ({ page }) => {
  await page.goto("/#nutrition/body");
  const row = page.locator("tr[data-metric='weight_kg']");
  await expect(row).toBeVisible();
  await page.context().route("**/api/body/weight", async (route) => {
    if (route.request().method() === "GET") await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.fallback();
  });
  await row.getByRole("button", { name: "Gewicht eintragen" }).click();
  const dialog = page.locator("#weightDialog");
  await expect(dialog).toBeVisible();
  await page.locator("#weightDialogKg").fill("77.7");
  // Longer than the delayed read: a fill after opening would have cleared the field by now.
  await page.waitForTimeout(2000);
  await expect(page.locator("#weightDialogKg")).toHaveValue("77.7");
  await page.context().unroute("**/api/body/weight");
  await dialog.getByRole("button", { name: "Abbrechen" }).click();
});

// #4047: the weight row carries one line when the energy read names probable water for the shown weigh-in.
test("@responsive the weight row names probable water under its source line, only for the shown weigh-in", async ({ page }) => {
  // Playwright disposes a response whose page moved on; such a read has nobody to answer (#3449).
  const GONE = /Response has been disposed|Target page, context or browser has been closed/;
  const answer = async (route, change) => {
    try {
      const response = await route.fetch();
      await route.fulfill({ response, json: change(await response.json()) });
    } catch (error) {
      if (!GONE.test(String(error))) throw error;
    }
  };
  await page.route("**/api/performance", (route) => answer(route, (data) => { data.performance.history = bodyHistory(); return data; }));
  let hint = { date: "2026-10-06", direction: "up", causes: ["salt", "carbs"] };
  await page.route("**/api/body/energy*", (route) => answer(route, (read) => ({ ...read, water_hint: hint })));
  await page.goto("/#nutrition/body");
  const weight = page.locator("#bodyAnalysisCharts [data-body-overview] [data-metric='weight_kg'] th small");
  await expect(weight).toHaveText(["Garmin Connect · 06.10.2026", "Wahrscheinlich Wasser: gestern viel Salz und Kohlenhydrate"]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  // A hint of another weigh-in says nothing.
  hint = { date: "2026-09-01", direction: "up", causes: ["salt"] };
  await page.evaluate(() => { resetEnergy(); renderBodyAnalysis(state.data.performance.history.body); });
  await expect(weight).toHaveText(["Garmin Connect · 06.10.2026"]);
});
