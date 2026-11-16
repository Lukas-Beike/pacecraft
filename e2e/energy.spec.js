const { test, expect } = require("@playwright/test");

// #3099, #3252: the energy table sits in the diary, its explanation behind the small "i"; the
// fixture's thin diary gives every source as unknown and names why, never a formula fallback.
test("@responsive the diary shows the energy table and explains it behind the info button", async ({ page }) => {
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEnergy [data-energy]");
  await expect(card.locator("tbody tr[data-energy-source]")).toHaveCount(4);
  await expect(card.locator("tbody th")).toHaveText(["Katch-McArdle", "Cunningham", "Garmin (Schätzung)", "Aus deinen Daten"]);
  await expect(card.locator(".analysis-reference-note")).toHaveCount(0);
  const tooltip = card.locator(".analysis-info-tooltip");
  await expect(tooltip).toBeHidden();
  await card.getByRole("button", { name: "Berechnung des Energieverbrauchs" }).click();
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText("Datenbasis zu gering");
  await expect(page.locator("#nutritionExpenditure")).toHaveCount(0);
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
