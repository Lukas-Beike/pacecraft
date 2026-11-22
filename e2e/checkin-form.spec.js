const { test, expect } = require("@playwright/test");

// #3953: today's calendar day opens the check-in form; a save shows its receipt and "Rückgängig" restores the day.
test("@responsive the calendar day's check-in form saves a correction and undoes it", async ({ page }) => {
  await page.goto("/#plan");
  const row = page.locator(".planned-day.is-today [data-checkin]");
  await expect(row).toBeVisible();
  await row.getByRole("button").click();
  const dialog = page.locator("#checkinDialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Stress (0–10)").fill("7");
  await dialog.getByRole("button", { name: "+ Beschwerde" }).click();
  await dialog.locator("select[name=area]").selectOption("achilles");
  await dialog.locator("select[name=side]").selectOption("left");
  await dialog.locator("input[name=severity]").fill("4");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row).toContainText("Check-in gespeichert.");
  await expect(row).toContainText("Stress 7");
  await expect(page.locator(".planned-day.is-today")).toContainText("Achillessehne links 4/10");
  await row.getByRole("button", { name: "Rückgängig" }).click();
  await expect(row).toContainText("Zurückgenommen.");
  await expect(row).not.toContainText("Stress 7");
  await expect(page.locator(".planned-day.is-today")).not.toContainText("Achillessehne");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive an unchanged form saves nothing and a refused save keeps the form open", async ({ page }) => {
  const posts = [];
  page.on("request", (request) => { if (request.method() === "POST" && request.url().includes("/api/checkins")) posts.push(request.url()); });
  await page.goto("/#plan");
  const row = page.locator(".planned-day.is-today [data-checkin]");
  await row.getByRole("button").click();
  const dialog = page.locator("#checkinDialog");
  await dialog.getByRole("button", { name: "Abbrechen" }).click();
  await expect(dialog).toHaveCount(0);
  await row.getByRole("button").click();
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toHaveCount(0);
  expect(posts).toHaveLength(0);
  await row.getByRole("button").click();
  // The same area and side twice is refused by the server; the entered values stay.
  for (const index of [0, 1]) {
    await dialog.getByRole("button", { name: "+ Beschwerde" }).click();
    await dialog.locator("input[name=severity]").nth(index).fill("2");
  }
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog.getByRole("status")).toContainText("jeden Bereich mit Seite nur einmal");
  await expect(dialog.locator("input[name=severity]").first()).toHaveValue("2");
  await expect(dialog).toBeVisible();
});
