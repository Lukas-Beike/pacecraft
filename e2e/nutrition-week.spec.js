const { test, expect } = require("@playwright/test");

// Routes miss requests that pass through the service worker's fetch listener; this spec
// does not test the worker, so it is blocked and every route applies (#3842).
test.use({ serviceWorkers: "block" });

// The week read of a period, shaped here (the values come from the backend tests): `count` closed
// days ending 2026-10-06, every ninth without a logged intake.
const weekRead = (count) => {
  const days = Array.from({ length: count }, (_, index) => {
    const intake = index % 9 === 8 ? null : 2100 + ((index * 37) % 500);
    const expenditure = 2500 + ((index * 53) % 300);
    return { date: new Date(Date.UTC(2026, 9, 6 - (count - 1) + index)).toISOString().slice(0, 10), intake_kcal: intake, expenditure_kcal: expenditure, balance_kcal: intake == null ? null : intake - expenditure };
  });
  const mean = (key) => { const values = days.map((day) => day[key]).filter((value) => value != null); return { value: Math.round(values.reduce((sum, value) => sum + value, 0) / values.length), days: values.length }; };
  const balances = days.map((day) => day.balance_kcal).filter((value) => value != null);
  return { start_date: days[0].date, end_date: days.at(-1).date, days, balance_kcal: balances.reduce((sum, value) => sum + value, 0), balance_days: balances.length, expenditure_source: "own",
    averages: { intake_kcal: mean("intake_kcal"), expenditure_kcal: mean("expenditure_kcal"), carbs_g: { value: 250, days: 4 }, protein_g: { value: 100, days: 6 }, fat_g: { value: 80, days: 6 }, sugar_g: { value: null, days: 0 }, salt_g: { value: 5, days: 3 } } };
};

// A deficit card of a period: weigh-ins over `days`, the goal 300 away from it.
const deficitRead = (days, kcal) => ({ days, deficit_kcal: kcal, band_kcal: 200, method: "prior", span_days: days - 1, weight_start_kg: 92.2, weight_end_kg: 88.8, weight_change_kg: -3.4,
  fat_pct_start: 18, fat_pct_end: 14.1, fat_date_start: "2026-10-02", fat_date_end: "2026-11-17", weight_last_kg: 88.8, weight_date_start: "2026-10-22", weight_date_end: "2026-11-18",
  fat_change_kg: -2.4, fat_share_pct: 70, fat_share: { share_pct: 70, prior_pct: 70, measured_pct: 0 }, stored_kcal: -kcal * (days - 1), uncertainty: "fat_share",
  total_kcal: 2900, device_total_kcal: 3100, goal: { goal: "lose", goal_kcal: 300, status: "in", distance: "near" } });

const fitRead = (days, kcal) => ({ days, own_days: days, own_kcal: 2700, own_band_kcal: 300, weeks: days / 7, weeks_with_data: 3, span_start: null, span_end: null,
  sources: [{ source: "katch_mcardle", total_kcal: 2650 }, { source: "cunningham", total_kcal: 2900 }, { source: "device", label: "Garmin Connect", total_kcal: 3100 }], deficit: deficitRead(days, kcal) });

const GONE = /Response has been disposed|Target page, context or browser has been closed/;

// The colour a custom property resolves to, as the browser reports a colour.
const colourOf = (page, name) => page.evaluate((token) => {
  const probe = document.createElement("span");
  probe.style.color = `var(${token})`;
  document.body.append(probe);
  const colour = getComputedStyle(probe).color;
  probe.remove();
  return colour;
}, name);

// The server reads 6 and 12 months only for `period=182d` or `period=365d` (#3711), so the mock answers them only then.
async function routeVerlauf(page, periods = []) {
  await page.route("**/api/nutrition/week?days=*", (route) => route.fulfill({ json: weekRead(Number(new URL(route.request().url()).searchParams.get("days"))) }));
  await page.route("**/api/body/energy*", async (route) => {
    try {
      const period = new URL(route.request().url()).searchParams.get("period");
      periods.push(period);
      const response = await route.fetch();
      const read = await response.json();
      await route.fulfill({ response, json: { ...read, goal: { goal: "lose", goal_kcal: 300, source: "device", total_kcal: 2600, target_kcal: 2300, eaten_kcal: 0, remaining_kcal: 2300 },
        fit_scale: { "28d": fitRead(28, 565), "91d": fitRead(91, 480), ...(period === "182d" || period === "365d" ? { "182d": fitRead(182, 410), "365d": fitRead(365, 380) } : {}) } } });
    } catch (error) {
      if (!GONE.test(String(error))) throw error;
    }
  });
}

// #3704: one card layout, the three periods and the two colours.
test("@responsive Verlauf is five cards in one layout that follow the period (#3704)", async ({ page }) => {
  const periods = [];
  await routeVerlauf(page, periods);
  await page.goto("/#nutrition/week");
  const root = page.locator("#nutritionWeek");
  await expect(root.locator(".analysis-chart-card")).toHaveCount(5);
  // The order, and one title row each: the title left, the period small and muted right.
  await expect(root.locator(".analysis-chart-card > .verlauf-head > h3")).toHaveText(["Bilanz", "Makros", "Defizit aus Gewicht und Körperfett", "Welche Quelle trifft dein Gewicht?", "Tagesverlauf"]);
  await expect(root.locator(".analysis-chart-card > .verlauf-head > .verlauf-period")).toHaveText([/^\d{2}\.\d{2}\.–\d{2}\.\d{2}\.2026 · Bilanz an \d+ von 28 Tagen$/, "4 Wochen · Ø pro Tag", "4 Wochen", "4 Wochen", "4 Wochen"]);
  // One card class and the same look: no other tile or tool class, one width, one radius.
  await expect(root.locator(".nutrition-total, .nutrition-tools")).toHaveCount(0);
  const looks = await root.locator(".analysis-chart-card").evaluateAll((cards) => cards.map((card) => { const style = getComputedStyle(card); return [Math.round(card.getBoundingClientRect().width), style.borderRadius, style.backgroundColor]; }));
  expect(new Set(looks.map((look) => look.join("|"))).size).toBe(1);
  // The Bilanz: the mean balance per day with the goal, then Aufnahme, Verbrauch, Summe.
  const balance = root.locator('[data-verlauf-card="balance"]');
  await expect(balance.locator(".verlauf-goal")).toHaveText("Ziel −300");
  await expect(balance.locator(".stat-tile > span")).toHaveText(["Aufnahme", "Verbrauch", "Summe"]);
  await expect(balance.locator(".stat-tile").nth(1)).toContainText("aus deinen Daten");
  // The distance to the goal colours the hero (#3606): near is the accent, mid orange, far red.
  await expect(balance.locator(".verlauf-hero")).toHaveAttribute("data-goal-distance", /^(near|mid|far)$/);
  const distance = await balance.locator(".verlauf-hero").getAttribute("data-goal-distance");
  await expect(balance.locator(".verlauf-hero strong")).toHaveCSS("color", await colourOf(page, { near: "--accent", mid: "--orange", far: "--danger" }[distance]));
  // Makros as tiles; Zucker was not logged, so no empty tile, Salz was.
  await expect(root.locator('[data-verlauf-card="macros"] .stat-tile > span')).toHaveText(["Kohlenhydrate", "Protein", "Fett", "Salz"]);
  await expect(root.locator('[data-verlauf-card="macros"] .value-missing')).toHaveCount(0);
  await expect(root).not.toContainText("nicht erfasst");
  // The deficit card keeps its flow boxes and two text lines.
  const deficit = root.locator("[data-energy-deficit]");
  await expect(deficit.locator(".energy-deficit-hero strong")).toHaveText("≈ 565 kcal");
  await expect(deficit.locator("[data-deficit-box]")).toHaveCount(4);
  await expect(deficit.locator("[data-deficit-box]").nth(0).locator("strong, small")).toHaveText(["Gewicht", "−3,4 kg", "92,2 → 88,8 kg", "22.10. → 18.11."]);
  await expect(deficit.locator("[data-deficit-box]").nth(1).locator("strong, small")).toHaveText(["Körperfett", "−3,9 %-Pkt.", "18,0 → 14,1 %", "02.10. → 17.11."]);
  await expect(deficit.locator(".energy-deficit-text")).toHaveCount(1);
  // The sources: label left, one lane each, value right.
  const rows = root.locator("[data-energy-fit] .energy-fit-row");
  await expect(rows).toHaveCount(4);
  const [lane, value] = await rows.first().evaluate((row) => [row.querySelector(".energy-fit-lane").getBoundingClientRect(), row.querySelector(".energy-fit-value").getBoundingClientRect()].map((box) => [box.left, box.right]));
  expect(value[0]).toBeGreaterThanOrEqual(lane[1] - 1);
  // The Tagesverlauf: Aufnahme blue, Verbrauch accent green, as the two tokens say.
  const [blue, green] = await Promise.all([colourOf(page, "--energy-intake"), colourOf(page, "--energy-expenditure")]);
  expect(blue).not.toBe(green);
  await expect(root.locator(".is-intake .nutrition-week-chart-bar").first()).toHaveCSS("background-color", blue);
  await expect(root.locator(".is-expenditure .nutrition-week-chart-bar").first()).toHaveCSS("background-color", green);
  await expect(root.locator(".nutrition-week-chart-day").first().locator(".nutrition-week-chart-balance")).toHaveAttribute("data-goal-distance", /^(near|mid|far)$/);
  await expect(root.locator(".nutrition-week-chart-day")).toHaveCount(28);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await root.screenshot({ path: test.info().outputPath("Verlauf-4-Wochen.png") });

  // Everything follows the period: 3 months show a row per week, 6 months name their own deficit.
  await expect(root.getByRole("button", { name: "4 Wochen", exact: true })).toHaveAttribute("aria-pressed", "true");
  await root.getByRole("button", { name: "3 Monate", exact: true }).click();
  await expect(root.locator("#nutritionWeekChartHeading")).toHaveText("Wochendurchschnitte");
  await expect(root.locator(".nutrition-week-chart-day")).toHaveCount(14);
  await expect(root.locator(".analysis-chart-card > .verlauf-head > .verlauf-period").nth(2)).toHaveText("3 Monate");
  await expect(deficit.locator(".energy-deficit-hero strong")).toHaveText("≈ 480 kcal");
  await expect(root.locator("[data-energy-fit]")).toHaveAttribute("data-energy-fit", "91d");
  expect(periods.filter(Boolean)).toEqual([]);
  await root.getByRole("button", { name: "6 Monate", exact: true }).click();
  await expect(root.locator(".nutrition-week-chart-day")).toHaveCount(27);
  await expect(deficit.locator(".energy-deficit-hero strong")).toHaveText("≈ 410 kcal");
  await expect(root.locator("[data-energy-fit]")).toHaveAttribute("data-energy-fit", "182d");
  expect(periods.filter(Boolean)).toEqual(["182d"]);
  await expect(root.locator('[data-verlauf-card="balance"] .verlauf-period')).toContainText(/ von 182 Tagen$/);
  await root.getByText("Tageswerte anzeigen", { exact: true }).click();
  await expect(root.locator("#nutritionWeekDays tr")).toHaveCount(182);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await root.screenshot({ path: test.info().outputPath("Verlauf-6-Monate.png") });
  // 12 months: its own read (#3839), a row per week and its own deficit.
  await root.getByRole("button", { name: "12 Monate", exact: true }).click();
  await expect(root.locator(".nutrition-week-chart-day")).toHaveCount(53);
  await expect(deficit.locator(".energy-deficit-hero strong")).toHaveText("≈ 380 kcal");
  await expect(root.locator("[data-energy-fit]")).toHaveAttribute("data-energy-fit", "365d");
  expect(periods.filter(Boolean)).toEqual(["182d", "365d"]);
  await expect(root.locator('[data-verlauf-card="balance"] .verlauf-period')).toContainText(/ von 365 Tagen$/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await root.screenshot({ path: test.info().outputPath("Verlauf-12-Monate.png") });
});

test("@responsive nutrition week shows coverage, signed balance and missing days without fueling", async ({ page }) => {
  let fail = false;
  let fuelingRequests = 0;
  page.on("request", (request) => { if (request.url().includes("/nutrition/fueling")) fuelingRequests += 1; });
  await page.route("**/api/nutrition/week?days=*", (route) => {
    if (fail) return route.abort();
    const read = weekRead(28);
    // Six days with an intake, five of them with an expenditure, the others without data.
    read.days = read.days.map((day, index) => ({ ...day, intake_kcal: index < 6 ? 2200 : null, expenditure_kcal: index < 5 ? 2100 : null, balance_kcal: index < 5 ? 100 : null }));
    return route.fulfill({ json: { ...read, balance_kcal: 500, balance_days: 5,
      averages: { ...read.averages, intake_kcal: { value: 2200, days: 6 }, expenditure_kcal: { value: 2100, days: 5 }, carbs_g: { value: 250, days: 4 }, protein_g: { value: 100, days: 6 }, fat_g: { value: null, days: 0 }, sugar_g: { value: 25, days: 3 }, salt_g: { value: null, days: 0 } } } });
  });
  await page.goto("/#nutrition/week");
  const week = page.locator("#nutritionWeek");
  await expect(week).toBeVisible();
  await expect(page.locator("#nutritionDiary")).toBeHidden();
  const balance = week.locator('[data-verlauf-card="balance"]');
  await expect(balance.locator(".verlauf-period")).toHaveText("09.09.–06.10.2026 · Bilanz an 5 von 28 Tagen");
  await expect(balance.locator(".verlauf-hero strong")).toHaveText("+100 kcal");
  await expect(balance.locator(".stat-tile").nth(0)).toContainText("2.200 kcal");
  await expect(balance.locator(".stat-tile").nth(1)).toContainText("2.100 kcal");
  await expect(balance.locator(".stat-tile").nth(2)).toContainText("+500 kcal");
  // Tiles side by side in one row.
  const boxes = await balance.locator(".stat-tile").evaluateAll((tiles) => tiles.map((tile) => tile.getBoundingClientRect().y));
  expect(new Set(boxes.map(Math.round)).size).toBe(1);
  const macros = week.locator('[data-verlauf-card="macros"] .stat-tile');
  await expect(macros.filter({ hasText: "Kohlenhydrate" })).toContainText("an 4 von 28 Tagen");
  await expect(macros.filter({ hasText: "Fett" }).locator("strong.value-missing")).toHaveText("nicht erfasst");
  await expect(macros.filter({ hasText: "Zucker" })).toContainText("25 g");
  // Salz was never logged: no tile instead of an empty one.
  await expect(macros.filter({ hasText: "Salz" })).toHaveCount(0);
  // Six days with values and one row for the 22 following days without data.
  await expect(week.locator(".nutrition-week-chart-day")).toHaveCount(7);
  await expect(week.locator(".nutrition-week-chart-day.is-empty")).toHaveText(/15\.09–06\.10\s*keine Daten/);
  await expect(week.locator(".nutrition-week-chart-day").first()).toContainText("2.200 kcal");
  await expect(week.locator(".nutrition-week-chart-balance").first()).toHaveText("+100");
  await expect(week.locator(".nutrition-week-chart-track.is-missing")).toHaveCount(1);
  await week.getByText("Tageswerte anzeigen", { exact: true }).click();
  // The day table's rows.
  await expect(week.locator("#nutritionWeekDays tr")).toHaveCount(7);
  await expect(week.locator("#nutritionWeekDays tr").last().locator("td")).toHaveText(["keine Daten"]);
  await expect(page.locator("#nutritionFueling, #trainingFueling")).toHaveCount(0);
  expect(fuelingRequests).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.locator('[data-nutrition-segment="diary"]').click();
  await expect(week).toBeHidden();
  await expect(page.locator("#nutritionDiary")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Trainingsverpflegung" })).toHaveCount(0);
  fail = true;
  await page.locator('[data-nutrition-segment="week"]').click();
  // A failed refresh keeps the last loaded week on this device and says so.
  await expect(week).toContainText("Gespeicherter Stand. Aktualisierung fehlgeschlagen");
  await expect(balance.locator(".verlauf-hero strong")).toHaveText("+100 kcal");
  await expect(week.locator(".nutrition-week-chart-day")).toHaveCount(7);
  await page.evaluate(() => localStorage.removeItem("coach-view-cache"));
  await page.locator('[data-nutrition-segment="diary"]').click();
  await page.locator('[data-nutrition-segment="week"]').click();
  await expect(week).toContainText("Wochenübersicht konnte nicht geladen werden");
  await expect(week.locator("#nutritionWeekBalance")).toBeEmpty();
  await expect(week.locator("#nutritionWeekMacros")).toBeEmpty();
  await expect(week.locator("#nutritionWeekChart")).toBeEmpty();
  await expect(week.locator("#nutritionWeekDetails")).toBeHidden();
});

test("@responsive nutrition history switches between 4 weeks, 3, 6 and 12 months", async ({ page }) => {
  await page.goto("/#nutrition/week");
  const root = page.locator("#nutritionWeek");
  await expect(root.locator("#nutritionWeekChartHeading")).toHaveText("Tagesverlauf");
  await expect(root.getByRole("button")).toHaveText(["4 Wochen", "3 Monate", "6 Monate", "12 Monate"]);
  await expect(root.getByRole("button", { name: "4 Wochen", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(root.locator("#nutritionWeekChartPeriod")).toHaveText("4 Wochen");
  await root.getByRole("button", { name: "3 Monate", exact: true }).click();
  await expect(root.locator("#nutritionWeekChartHeading")).toHaveText("Wochendurchschnitte");
  await expect(root.locator("#nutritionWeekChartPeriod")).toHaveText("3 Monate");
  await root.getByText("Tageswerte anzeigen", { exact: true }).click();
  await expect(root.locator("#nutritionWeekDays tr")).toHaveCount(1);
  await root.getByRole("button", { name: "6 Monate", exact: true }).click();
  await expect(root.locator("#nutritionWeekChartPeriod")).toHaveText("6 Monate");
  await expect(root.locator("#nutritionWeekDays tr")).toHaveCount(1);
  await root.getByRole("button", { name: "12 Monate", exact: true }).click();
  await expect(root.locator("#nutritionWeekChartPeriod")).toHaveText("12 Monate");
  await expect(root.locator("#nutritionWeekDays tr")).toHaveCount(1);
  // The choice is remembered per tab across a reload (#3929 A-11).
  await page.reload();
  await expect(root.locator("#nutritionWeekChartHeading")).toHaveText("Wochendurchschnitte");
  await expect(root.getByRole("button", { name: "12 Monate", exact: true })).toHaveAttribute("aria-pressed", "true");
  await root.getByRole("button", { name: "4 Wochen", exact: true }).click();
  await expect(root.locator("#nutritionWeekChartHeading")).toHaveText("Tagesverlauf");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive nutrition gaps keep expenditure visible without inventing intake", async ({ page }) => {
  await page.route("**/api/nutrition/week?days=*", (route) => {
    const count = Number(new URL(route.request().url()).searchParams.get("days"));
    const days = Array.from({ length: count }, (_, index) => ({ date: new Date(Date.UTC(2026, 0, index + 1)).toISOString().slice(0, 10), intake_kcal: index === 0 ? 2100 : null, expenditure_kcal: 2400, balance_kcal: index === 0 ? -300 : null }));
    return route.fulfill({ json: { start_date: days[0].date, end_date: days.at(-1).date, days, balance_kcal: -300, balance_days: 1,
      averages: { intake_kcal: { value: 2100, days: 1 }, expenditure_kcal: { value: 2400, days: count }, carbs_g: { value: null, days: 0 }, protein_g: { value: null, days: 0 }, fat_g: { value: null, days: 0 }, sugar_g: { value: 25, days: 3 }, salt_g: { value: null, days: 0 } } } });
  });
  await page.goto("/#nutrition/week");
  const root = page.locator("#nutritionWeek");
  const balance = root.locator('[data-verlauf-card="balance"]');
  await expect(balance.locator(".verlauf-period")).toContainText("Bilanz an 1 von 28 Tagen");
  await expect(balance.locator(".stat-tile").nth(1)).toContainText("2.400 kcal");
  await expect(root.locator(".is-expenditure .is-missing")).toHaveCount(0);
  await expect(root.locator(".is-intake .is-missing")).toHaveCount(27);
  await root.getByRole("button", { name: "3 Monate", exact: true }).click();
  await expect(root.locator(".nutrition-week-chart-day")).toHaveCount(14);
  const first = root.locator(".nutrition-week-chart-day").first();
  await expect(first.locator(".is-intake")).toContainText("1 von 4 Tagen");
  await expect(first.locator(".is-expenditure")).toContainText("4 von 4 Tagen");
  await expect(root.locator(".is-expenditure .is-missing")).toHaveCount(0);
  await expect(root.locator(".is-intake .is-missing")).toHaveCount(13);
  await expect(balance.locator(".stat-tile").nth(2)).toContainText("−300 kcal");
  await expect(balance.locator(".verlauf-period")).toContainText("Bilanz an 1 von 91 Tagen");
  await expect(root).not.toContainText("Kaloriendefizit");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
