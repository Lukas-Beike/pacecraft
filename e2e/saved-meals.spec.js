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
  const meals = page.locator("#nutritionEntries .nutrition-card").filter({ has: page.getByRole("button", { name: /^Mahlzeit bearbeiten: / }) });
  await expect(meals.first()).toBeVisible();
  // "Als Mahlzeit speichern" is in the meal's edit dialog, not on the card (#4305).
  await expect(meals.first().getByRole("button", { name: "Als Mahlzeit speichern" })).toHaveCount(0);
  await meals.first().getByRole("button", { name: /^Mahlzeit bearbeiten: / }).click();
  await page.locator("#nutritionMealEditDialog").getByRole("button", { name: "Als Mahlzeit speichern" }).click();
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

// #4304: "+ Mahlzeit" sits under the diary day, empty or not. Its sheet logs a searched product or database
// food with its amount, or a saved meal, as a new meal of the chosen type on the shown day.
test("@responsive + Mahlzeit logs a searched food or a saved meal on the shown day", async ({ page }) => {
  let empty = true;
  await page.route("**/api/nutrition/day**", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    await route.fulfill({ response, json: empty ? { ...json, entries: [], entry_count: 0 } : json });
  });
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#nutritionEmptyDay")).toHaveText("Noch keine Mahlzeiten erfasst.");
  const add = page.getByRole("button", { name: "+ Mahlzeit", exact: true });
  await expect(add).toBeVisible();
  empty = false;
  const ids = await page.evaluate(async () => {
    const product = await api("/api/nutrition/products", { method: "POST", body: JSON.stringify({ confirmed: true, name: "Skyr Fixture", basis_amount: 100, basis_unit: "g", kcal: 60, carbs_g: 4, protein_g: 11, fat_g: 0.2 }) });
    const entry = await api("/api/nutrition/entry", { method: "POST", body: JSON.stringify({ meal_date: todayIso(), description: "Porridge Fixture", kcal: 350, carbs_g: 55, protein_g: 12, fat_g: 7 }) });
    const template = await api("/api/nutrition/templates/from-entry", { method: "POST", body: JSON.stringify({ id: (entry.entry || entry).id, name: "Porridge gespeichert" }) });
    return { product: (product.product || product).id, template: (template.template || template).id };
  });
  // A database food comes from the search like a product (the lookup answers here).
  await page.route("**/api/nutrition/products/lookup", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    await route.fulfill({ response, json: { ...json, foods: [...(json.foods || []), { id: "bls:H861000", name: "Tofu", source: "BLS 4.0", basis_amount: 100, basis_unit: "g", per_100: { kcal: 115, carbs_g: 1, protein_g: 12, fat_g: 7 } }] } });
  });
  await page.reload();
  await expect(page.locator(".nutrition-card", { hasText: "Porridge Fixture" })).toBeVisible();
  await expect(add).toBeVisible();

  const dialog = page.locator("#nutritionTemplateDialog");
  for (const [query, name, amount] of [["Skyr Fixture", "Skyr Fixture", "200"], ["Tofu", "Tofu", "150"]]) {
    await add.click();
    await expect(dialog.getByRole("heading", { name: "Mahlzeit hinzufügen" })).toBeVisible();
    await dialog.getByLabel("Abendessen").check();
    await dialog.getByLabel("Produkt oder Lebensmittel").fill(query);
    await dialog.getByRole("button", { name: "Suchen" }).click();
    await dialog.locator(".swap-result", { hasText: name }).first().click();
    await expect(dialog.getByLabel(`Menge ${name}`)).toBeFocused();
    await dialog.getByLabel(`Menge ${name}`).fill(amount);
    await dialog.getByRole("button", { name: "Eintragen" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.locator("#nutritionStatus")).toContainText(`„${name}“ als Abendessen erfasst.`);
    await expect(page.locator(".nutrition-card", { hasText: name })).toContainText("Abendessen");
  }

  await add.click();
  await dialog.getByRole("button", { name: "Porridge gespeichert" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#nutritionStatus")).toContainText("„Porridge gespeichert“ eingetragen.");
  // Logged under the saved meal's own description, next to the meal it was made from.
  await expect(page.locator(".nutrition-card", { hasText: "Porridge Fixture" })).toHaveCount(2);
  // The saved meal and product of this test go again.
  await page.evaluate(({ template, product }) => Promise.all([
    api("/api/nutrition/templates/delete", { method: "POST", body: JSON.stringify({ id: template, confirmed: true }) }),
    api("/api/nutrition/products/archive", { method: "POST", body: JSON.stringify({ id: product, confirmed: true }) }),
  ]), ids);
});
