const { test, expect } = require("@playwright/test");

// #3099, #3252: the energy table sits in the diary, its explanation behind the small "i". The
// fixture has no skinfold: with a weigh-in and a profile body fat, the formula rows fall back
// to the scale's or the profile's body fat and name it (#3362).
test("@responsive the diary shows the energy table and explains it behind the info button", async ({ page }) => {
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#nutritionEnergy [data-energy]")).toBeVisible();
  await page.evaluate(async () => {
    const day = new Date(Date.now() - 3 * 864e5).toISOString().slice(0, 10);
    await api("/api/body/weight", { method: "PUT", body: JSON.stringify({ date: day, kg: 72 }) });
    await api("/api/profile", { method: "PUT", body: JSON.stringify({ body_fat_pct: "18,5" }) });
  });
  const energy = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/body/energy");
  await page.reload();
  // #3323: the device row's name is the provider label the read gives it, never a client literal.
  const read = await (await energy).json();
  const device = read.rows.find((row) => row.source === "device");
  expect(device.label).toBeTruthy();
  expect(device.label).not.toContain("Schätzung");
  const card = page.locator("#nutritionEnergy [data-energy]");
  await expect(card.locator("tbody tr[data-energy-source]")).toHaveCount(4);
  expect(["device", "profile"]).toContain(read.body.source);
  const kfa = `KFA: ${read.body.label}`;
  await expect(card.locator("tbody th")).toHaveText([`Katch-McArdle${kfa}`, `Cunningham${kfa}`, device.label, "Aus deinen Daten"]);
  await expect(card.locator("[data-energy-source='katch_mcardle'] td").first()).toHaveText(/^[\d.]+ ± \d+$/);
  await expect(card.locator(".analysis-reference-note")).toHaveCount(0);
  const tooltip = card.locator(".analysis-info-tooltip");
  await expect(tooltip).toBeHidden();
  // #3336: the same small info icon as Analyse › Laufen › Entwicklung, not a yellow badge.
  const info = card.getByRole("button", { name: "Energieverbrauch: Informationen" });
  await expect(info).toHaveClass("analysis-legend-info analysis-info-icon");
  // The table re-renders when its read arrives, so the style check retries on the current node.
  await expect(info).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await info.click();
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText("Datenbasis zu gering");
  await expect(tooltip).toContainText(`(${read.body.label}`);
  await expect(page.locator("#nutritionExpenditure")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3336, #3362: without any body fat both formula rows name the reason and open its dialog.
test("@responsive formula rows without a body fat name the reason and open the dialog", async ({ page }) => {
  await page.route("**/api/body/energy", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    for (const row of data.rows) if (row.source === "katch_mcardle" || row.source === "cunningham") Object.assign(row, { resting_kcal: null, resting_band_kcal: null, total_kcal: null, total_band_kcal: null, split: null });
    await route.fulfill({ response, json: { ...data, body: null, skinfold_missing: "no_skinfold" } });
  });
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEnergy [data-energy]");
  const missing = card.locator("[data-energy-missing='no_skinfold']");
  await expect(missing).toHaveText(["Hautfalten fehlen", "Hautfalten fehlen"]);
  await expect(card.locator("tbody th small")).toHaveCount(0);
  await missing.first().click();
  await expect(page.locator("#skinfoldDialog")).toBeVisible();
  await page.locator("#skinfoldDialogCancel").click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3252: which source meets the weight stays under Körper; tapping the body-fat row opens the
// skinfold dialog, so body fat stays derived from the folds.
test("@responsive Körper keeps the fit note and opens the skinfold dialog from body fat", async ({ page }) => {
  await page.goto("/#nutrition/body");
  const root = page.locator("#bodyAnalysisCharts");
  await expect(root.locator("[data-energy-fit]")).toContainText("noch nicht unterscheidbar");
  await expect(root.locator("[data-energy]")).toHaveCount(0);
  await root.locator("[data-body-overview] tr[data-metric='body_fat_pct'] th").click();
  await expect(page.locator("#skinfoldDialog")).toBeVisible();
  await page.locator("#skinfoldDialogCancel").click();
  await expect(page.locator("#skinfoldDialog")).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3100 part 2: a diary card marks its amounts as estimated and clears the mark, with undo.
test("@responsive a diary card marks a meal as estimated and clears the mark", async ({ page }) => {
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#nutritionDate")).not.toHaveValue("");
  await page.evaluate(() => api("/api/nutrition/entry", { method: "POST", body: JSON.stringify({ meal_type: "dinner", meal_time: "19:30", description: "Pizza beim Italiener", kcal: 950 }) }));
  await page.evaluate(() => loadNutrition());
  const card = page.locator("#nutritionEntries .nutrition-card").filter({ hasText: "Pizza beim Italiener" });
  await card.getByRole("button", { name: "Als geschätzt markieren" }).click();
  await expect(card.locator("h3")).toContainText("Menge geschätzt");
  await expect(card).toContainText("950 kcal");
  await card.getByRole("button", { name: "Nicht geschätzt" }).click();
  await expect(card.locator("h3")).not.toContainText("Menge geschätzt");
  await expect(card.getByRole("button", { name: "Als geschätzt markieren" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});
