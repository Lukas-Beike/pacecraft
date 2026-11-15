const { test, expect } = require("@playwright/test");

// #3099: the small edit button on the current measurement opens the skinfold dialog; a day
// that has a measurement fills it and offers its removal.
test("@responsive skinfolds are entered, edited and removed through the dialog", async ({ page }) => {
  await page.goto("/#nutrition/body");
  await expect(page.locator("[data-skinfolds]")).toBeVisible();
  await page.evaluate(() => api("/api/profile", { method: "PUT", body: JSON.stringify({ birth_year: "1986" }) }));
  const card = page.locator("[data-skinfolds]");
  const dialog = page.locator("#skinfoldDialog");
  await expect(dialog).toBeHidden();

  await card.getByRole("button", { name: "Hautfalten eintragen" }).click();
  await expect(dialog).toBeVisible();
  await expect(page.locator("#skinfoldDialogRemove")).toBeHidden();
  await page.locator("#skinfoldDialogDate").fill("2026-11-10");
  await page.locator("#skinfoldDialogDate").dispatchEvent("change");
  await dialog.getByLabel("Brust (mm)").fill("16");
  await dialog.getByLabel("Bauch (mm)").fill("20");
  await dialog.getByLabel("Oberschenkel (mm)").fill("17");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  // Jackson-Pollock with the age in 2026 (40): 16/20/17 mm -> 17,1 %.
  const row = card.locator("tr[data-skinfold='2026-11-10']");
  await expect(row).toContainText("17,1 %");
  await expect(row).toContainText("Brust 16 · Bauch 20 · Oberschenkel 17 mm");

  await row.getByRole("button", { name: "Hautfalten eintragen" }).click();
  await page.locator("#skinfoldDialogDate").fill("2026-11-10");
  await page.locator("#skinfoldDialogDate").dispatchEvent("change");
  await expect(dialog.getByLabel("Brust (mm)")).toHaveValue("16");
  await page.locator("#skinfoldDialogRemove").click();
  await page.locator("#confirmationDialog").getByRole("button", { name: "Löschen" }).click();
  await expect(dialog).toBeHidden();
  await expect(card.locator("tr[data-skinfold]")).toHaveCount(0);
  await expect(card).toContainText("Noch keine Messung");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});
