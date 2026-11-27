const { test, expect } = require("@playwright/test");

test("@responsive nutrition overview shows consumed sugar and salt with honest missing values", async ({ page }) => {
  let mode = "complete";
  await page.route("**/api/nutrition/day**", (route) => route.fulfill({ json: {
    date: "2026-10-20", entry_count: mode === "empty" ? 0 : 1, entries: [],
    total_kcal: 200, total_carbs_g: 30, total_protein_g: 10, total_fat_g: 5,
    total_sugar_g: mode === "complete" ? 12.5 : null,
    total_salt_g: mode === "complete" ? 0 : null,
    known_intake_totals: { sugar_g: mode === "partial" ? 6.2 : null, salt_g: null },
  } }));
  await page.goto("/#nutrition/diary");
  // Zucker and Salz are one slim line under the tiles, not tiles (#4300).
  const line = page.locator("#nutritionTotals .nutrition-minor-line");
  await expect(page.locator("#nutritionTotals .nutrition-total")).toHaveCount(4);
  await expect(line).toHaveText("Zucker 12,5 g · Salz 0,0 g");
  await expect(line.locator(".nutrition-minor-partial")).toHaveCount(0);
  mode = "partial";
  await page.reload();
  await expect(line).toContainText("Zucker 6,2 g · Salz nicht erfasst");
  await expect(line.locator(".nutrition-minor-partial")).toHaveText("unvollständig");
  await expect(line.locator("strong.value-missing")).toHaveCount(1);
  await expect(line.locator("strong.value-missing")).toHaveText("nicht erfasst");
  mode = "empty";
  await page.reload();
  await expect(page.locator("#nutritionTotals")).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive an empty diary day shows only its sentence, with no button and no message sent (#4265)", async ({ page }) => {
  await page.route("**/api/nutrition/day**", (route) => route.fulfill({ json: { date: "2026-10-20", entry_count: 0, entries: [], total_kcal: 0 } }));
  let chats = 0;
  await page.route("**/api/chat/**", (route) => { if (route.request().method() === "POST") chats += 1; return route.fallback(); });
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#nutritionEmptyDay")).toHaveText("Noch keine Mahlzeiten erfasst.");
  await expect(page.locator("#nutritionDiary").getByRole("button", { name: "Dem Coach sagen" })).toHaveCount(0);
  expect(chats).toBe(0);
});
