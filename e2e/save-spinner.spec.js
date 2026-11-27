const { test, expect } = require("@playwright/test");

// The held writes are answered with a refusal, so the fixture keeps its state; routes need the worker blocked (#3842).
test.use({ serviceWorkers: "block" });

// Holds every matching write until release(), then refuses it with a known error text.
async function holdWrites(page, pattern, method) {
  const held = [];
  let release;
  const released = new Promise((resolve) => { release = resolve; });
  await page.route(pattern, async (route) => {
    if (route.request().method() !== method) return route.fallback();
    held.push(route.request().url());
    await released;
    await route.fulfill({ status: 400, json: { error: "Testfehler beim Speichern." } });
  });
  return { held, release };
}

// #4268: from the press until the answer the button shows the spinner and "Speichert…", is busy and
// disabled, so a second press sends nothing; afterwards its label and the error text are back.
async function expectSavingButton(button, writes, label) {
  await button.click();
  await expect(button).toHaveText("Speichert…");
  await expect(button).toHaveAttribute("aria-busy", "true");
  await expect(button).toBeDisabled();
  await expect(button.locator(".button-spinner")).toBeVisible();
  await button.click({ force: true });
  await expect.poll(() => writes.held.length).toBe(1);
  writes.release();
  await expect(button).toHaveText(label);
  await expect(button).not.toHaveAttribute("aria-busy", "true");
  await expect(button).toBeEnabled();
  expect(writes.held).toHaveLength(1);
}

test("@responsive adding a product to a meal shows the saving state", async ({ page }) => {
  const productName = `Spinnerprodukt ${Date.now()}`;
  // The product save is answered here, so no product stays in the fixture.
  await page.route(/\/api\/nutrition\/products$/, (route) => (route.request().method() === "POST"
    ? route.fulfill({ json: { product: { id: "spinner-product", name: productName, kcal: 86, basis_amount: 100, basis_unit: "g", origin: "own" } } })
    : route.fallback()));
  await page.goto("/#nutrition/products");
  await page.locator("#nutritionProductManual").click();
  const editor = page.locator("#nutritionExtraction");
  await editor.locator('[name="name"]').fill(productName);
  await editor.locator('[name="kcal"]').fill("86");
  await editor.locator('[name="basis_amount"]').fill("100");
  await editor.locator('[name="basis_unit"]').selectOption("g");
  await editor.getByRole("button", { name: "Produkt lokal speichern" }).click();
  await expect(page.locator("#nutritionProductUseDialog")).toBeVisible();
  await page.locator("#nutritionProductUseAmount").fill("50");
  const writes = await holdWrites(page, /\/api\/nutrition\/entry(\/add-product)?$/, "POST");
  const add = page.locator("#nutritionProductUseAdd");
  await expectSavingButton(add, writes, "Ins Tagebuch eintragen");
  await expect(page.locator("#nutritionProductUseStatus")).toContainText("Testfehler beim Speichern.");
});

test("@responsive saving a weight shows the saving state", async ({ page }) => {
  await page.goto("/#nutrition/body");
  await page.getByRole("button", { name: "Gewicht eintragen" }).first().click();
  await expect(page.locator("#weightDialog")).toBeVisible();
  await page.locator("#weightDialogKg").fill("77.7");
  const writes = await holdWrites(page, /\/api\/body\/weight$/, "PUT");
  const save = page.locator("#weightDialogSave");
  await expectSavingButton(save, writes, "Speichern");
  await expect(page.locator("#weightDialogStatus")).toContainText("Testfehler beim Speichern.");
});

test("@responsive saving a check-in shows the saving state", async ({ page }) => {
  // The reminder's link opens today's form.
  await page.goto("/#plan/checkin");
  const dialog = page.locator("#checkinDialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Stress (0–10)").fill("6");
  const writes = await holdWrites(page, /\/api\/checkins$/, "POST");
  await expectSavingButton(dialog.getByRole("button", { name: /Speichern|Speichert…/ }), writes, "Speichern");
  await expect(dialog).toContainText("Testfehler beim Speichern.");
});

test("@responsive saving a unit shows the saving state", async ({ page }) => {
  await page.goto("/#plan/library");
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.locator("#library").getByRole("button", { name: "+ Einheit" }).click();
  const dialog = page.locator("#unitEditDialog");
  await dialog.getByLabel("Name", { exact: true }).fill("Spinner-Einheit");
  await dialog.getByRole("button", { name: "+ Schritt" }).click();
  const writes = await holdWrites(page, /\/api\/library$/, "POST");
  await expectSavingButton(dialog.locator("button[type=submit]"), writes, "Speichern");
  await expect(dialog).toContainText("Testfehler beim Speichern.");
});
