const { test, expect } = require("@playwright/test");

// #3952: a diary meal becomes a saved meal, is logged again for two portions and deleted with its receipt.
// The section keeps its open state while the page stays loaded, so a click opens it only when it is closed.
async function openTemplates(page) {
  const section = page.locator("#nutritionTemplates details");
  await expect(section).toBeVisible();
  if (!(await section.evaluate((node) => node.open))) await section.locator("summary").click();
  return section;
}

test("@responsive a diary meal is saved, logged again and deleted with a receipt", async ({ page }) => {
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.evaluate(() => api("/api/nutrition/entry", { method: "POST", body: JSON.stringify({ meal_date: todayIso(), description: "Haferflocken", kcal: 400, carbs_g: 60, protein_g: 15, fat_g: 8 }) }));
  await page.reload();
  const meals = page.locator(".nutrition-card").filter({ has: page.getByRole("button", { name: "Als Mahlzeit speichern" }) });
  await expect(meals.first()).toBeVisible();
  await meals.first().getByRole("button", { name: "Als Mahlzeit speichern" }).click();
  const dialog = page.locator("#nutritionTemplateDialog");
  await dialog.getByLabel("Name").fill("Fixture Mahlzeit");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#nutritionStatus")).toContainText("Mahlzeit gespeichert.");

  await page.goto("/#nutrition/products");
  const section = await openTemplates(page);
  await expect(section.locator("summary")).toContainText("Gespeicherte Mahlzeiten (1)");
  const card = section.locator("article", { hasText: "Fixture Mahlzeit" });
  await expect(card).toContainText("kcal");
  await card.getByRole("button", { name: "Eintragen" }).click();
  await dialog.getByLabel("Portionen").fill("2");
  await dialog.getByRole("button", { name: "Eintragen" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#nutritionProductStatus")).toContainText("Mahlzeit eingetragen.");

  await page.goto("/#nutrition/diary");
  await expect(page.locator(".nutrition-card", { hasText: "Fixture Mahlzeit · 2 Portionen" })).toBeVisible();

  await page.goto("/#nutrition/products");
  await openTemplates(page);
  await page.getByRole("button", { name: "Gespeicherte Mahlzeit löschen: Fixture Mahlzeit" }).click();
  await expect(page.locator("#nutritionProductStatus")).toContainText("Gespeicherte Mahlzeit gelöscht.");
  await page.locator("#nutritionProductStatus").getByRole("button", { name: "Rückgängig" }).click();
  await expect(page.locator("#nutritionTemplates summary")).toContainText("Gespeicherte Mahlzeiten (1)");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive the compose dialog asks for a food before it logs or keeps a meal", async ({ page }) => {
  await page.goto("/#nutrition/products");
  await openTemplates(page);
  await page.getByRole("button", { name: "Mahlzeit zusammenstellen" }).click();
  const dialog = page.locator("#nutritionTemplateDialog");
  await dialog.getByLabel("Name").fill("Leer");
  await dialog.getByRole("button", { name: "Als Mahlzeit speichern" }).click();
  await expect(dialog.getByRole("status")).toContainText("Mindestens eine Zutat.");
  await dialog.getByRole("button", { name: "Abbrechen" }).click();
  await expect(dialog).toHaveCount(0);
});

test("@responsive a meal composed of a saved product is logged with the server's values", async ({ page }) => {
  await page.goto("/#nutrition/products");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.evaluate(() => api("/api/nutrition/products", { method: "POST", body: JSON.stringify({ confirmed: true, name: "Haferflocken Fixture", basis_amount: 100, basis_unit: "g", kcal: 370, carbs_g: 60, protein_g: 13, fat_g: 7 }) }));
  await page.reload();
  await openTemplates(page);
  await page.getByRole("button", { name: "Mahlzeit zusammenstellen" }).click();
  const dialog = page.locator("#nutritionTemplateDialog");
  await dialog.getByLabel("Name").fill("Mein Müsli");
  await dialog.getByLabel("Zutat suchen").fill("Haferflocken Fixture");
  await dialog.getByRole("button", { name: "Suchen" }).click();
  await dialog.locator(".swap-result", { hasText: "Haferflocken Fixture" }).click();
  await dialog.getByLabel("Menge Haferflocken Fixture").fill("50");
  await dialog.getByRole("button", { name: "Eintragen" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#nutritionProductStatus")).toContainText("Mahlzeit eingetragen.");
  await page.goto("/#nutrition/diary");
  await expect(page.locator(".nutrition-card", { hasText: "Mein Müsli" })).toContainText("185 kcal");
});
