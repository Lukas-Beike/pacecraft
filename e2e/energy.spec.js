const { test, expect } = require("@playwright/test");

// #3099: the energy table reads the real view; the fixture's thin diary gives every source
// as unknown and names why, instead of falling back to a formula.
test("@responsive the energy table names its sources and why it has no basis", async ({ page }) => {
  await page.goto("/#nutrition/body");
  const card = page.locator("[data-energy]");
  await expect(card.locator("tbody tr")).toHaveCount(4);
  await expect(card.locator("tbody th")).toHaveText(["Katch-McArdle", "Cunningham", "Garmin (Schätzung)", "Aus deinen Daten"]);
  await expect(card).toContainText("Datenbasis zu gering");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});
