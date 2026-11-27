const { test, expect } = require("@playwright/test");

const DAY = "2026-11-05";
const component = (name, amount, unit, kcal, source) => ({ kind: "database", name, amount, unit, kcal, carbs_g: 10, protein_g: 5, fat_g: 1, nutrition_basis: { kind: "database", food: { id: `bls:${name}`, name, source } } });
const COMPONENTS = [
  component("Basmatireis, gekocht", 280, "g", 364, "BLS 4.0"),
  component("Tofu natur", 150, "g", 180, "Open Food Facts"),
  component("Olivenöl", 15, "g", 125, "BLS 4.0"),
].map((item, index) => ({ index, ...item }));
const MEAL = {
  id: "entry-meal", meal_date: DAY, meal_type: "lunch", logged_at: `${DAY}T12:00:00`, logged_time_known: true,
  description: "Reis mit Tofu", kcal: 669, carbs_g: 30, protein_g: 15, fat_g: 3, source: "manual", sync_state: "local",
  nutrition_basis: { kind: "composite", components: COMPONENTS },
};
const FOODS = [
  { id: "bls:tofu-gebacken", name: "Tofu gebacken", source: "BLS 4.0", basis_amount: 100, basis_unit: "g", per_100: { kcal: 160, carbs_g: 2, protein_g: 16, fat_g: 9 } },
  { id: "bls:tofu-saft", name: "Tofusaft", source: "BLS 4.0", basis_amount: 100, basis_unit: "ml", per_100: { kcal: 20, carbs_g: 1, protein_g: 2, fat_g: 1 } },
];

async function mockDiary(page) {
  const posts = [];
  let entry = MEAL;
  await page.route("**/api/nutrition/day**", (route) => route.fulfill({ json: { ok: true, date: DAY, entry_count: 1, entries: [entry], total_kcal: entry.kcal, total_carbs_g: 30, total_protein_g: 15, total_fat_g: 3 } }));
  await page.route("**/api/nutrition/products**", (route) => route.fulfill({ json: { ok: true, products: [] } }));
  await page.route("**/api/nutrition/entry/components?**", (route) => route.fulfill({ json: { ok: true, id: MEAL.id, revision: "rev-1", components: COMPONENTS } }));
  await page.route("**/api/nutrition/products/lookup", (route) => route.fulfill({ json: { ok: true, foods: FOODS, local: false } }));
  await page.route("**/api/nutrition/entry/components", (route) => {
    const body = route.request().postDataJSON();
    posts.push(body);
    // An added component (#4270) joins the stored ones, so the card lists it.
    const added = (body.edits || []).filter((edit) => edit.add).map(({ add }) => component("Tofu gebacken", add.amount, add.unit, 192, "BLS 4.0"));
    entry = { ...MEAL, kcal: 100, nutrition_basis: { kind: "composite", components: [...COMPONENTS, ...added] } };
    return route.fulfill({ json: { ok: true, entry, change: { id: "change-edit", entity_type: "nutrition_entry", action: "update", after_hash: "hash" } } });
  });
  await page.route("**/api/change-history/undo", (route) => { entry = MEAL; return route.fulfill({ json: { ok: true } }); });
  await page.route("**/api/nutrition/entry/delete", (route) => route.fulfill({ json: { ok: true, status: "ok", deleted_id: MEAL.id } }));
  return posts;
}

test.use({ serviceWorkers: "block" });

test("@responsive the pencil edits a meal's components: remove, restore, resize and swap with a receipt and undo", async ({ page }) => {
  const posts = await mockDiary(page);
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEntries .nutrition-card");
  await card.getByRole("button", { name: "Mahlzeit bearbeiten: Reis mit Tofu" }).click();
  const dialog = page.locator("#nutritionMealEditDialog");
  await expect(dialog).toBeVisible();
  const rows = dialog.locator(".edit-row");
  await expect(rows).toHaveCount(3);
  await expect(dialog.locator("#nutritionMealEditSave")).toBeDisabled();
  await expect(rows.nth(0)).toContainText("BLS 4.0 · 364 kcal");
  // The estimate mark and "Als Mahlzeit speichern" (#4305) work on the saved meal only: unsaved edits would be lost.
  const rare = dialog.locator("#nutritionMealEditEstimate, #nutritionMealEditKeep");
  await expect(rare).toHaveText(["Als geschätzt markieren", "Als Mahlzeit speichern"]);
  await expect(rare.first()).toBeEnabled();
  await expect(rare.last()).toBeEnabled();

  // A removed component stays visible until save and comes back.
  await rows.nth(2).getByRole("button", { name: "Olivenöl entfernen" }).click();
  await expect(rare.first()).toBeDisabled();
  await expect(rare.first()).toHaveAttribute("title", "Erst speichern");
  await expect(rows.nth(2)).toContainText("Wird entfernt");
  await rows.nth(2).getByRole("button", { name: "Wiederherstellen" }).click();
  await expect(dialog.locator("#nutritionMealEditSave")).toBeDisabled();
  await rows.nth(2).getByRole("button", { name: "Olivenöl entfernen" }).click();

  await rows.nth(0).getByRole("spinbutton").fill("300");
  await expect(rows.nth(0)).not.toContainText("kcal");

  // The swap search replaces the dialog content; only foods in the component's unit are offered.
  await rows.nth(1).getByRole("button", { name: "Tofu natur tauschen" }).click();
  await expect(dialog.locator("#nutritionMealEditMain")).toBeHidden();
  await dialog.getByLabel("Datenbank durchsuchen").fill("Tofu");
  await dialog.getByRole("button", { name: "Suchen" }).click();
  await expect(dialog.locator(".swap-result")).toHaveCount(1);
  await expect(dialog.locator(".swap-result")).toContainText("BLS 4.0");
  await dialog.locator(".swap-result").click();
  await expect(rows.nth(1)).toContainText("Tofu gebacken");
  await expect(rows.nth(1)).toContainText("statt Tofu natur");
  await expect(rows.nth(1).getByRole("spinbutton")).toHaveValue("150");

  await dialog.locator("#nutritionMealEditSave").click();
  await expect(dialog).toBeHidden();
  expect(posts).toEqual([{ id: MEAL.id, revision: "rev-1", edits: [{ index: 0, amount: 300 }, { index: 1, food_id: "bls:tofu-gebacken" }, { index: 2, remove: true }] }]);
  await expect(page.locator("#nutritionStatus")).toContainText("Mahlzeit geändert.");
  await expect(card).toContainText("100 kcal");
  await page.locator("#nutritionStatus").getByRole("button", { name: "Rückgängig" }).click();
  await expect(card).toContainText("669 kcal");
});

// #4270: "+ Zutat" under the rows reuses the swap search; the new row is stored with "Speichern" only.
test("@responsive the dialog adds a component from the search, and Abbrechen stores nothing", async ({ page }) => {
  const posts = await mockDiary(page);
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEntries .nutrition-card");
  const dialog = page.locator("#nutritionMealEditDialog");
  const rows = dialog.locator(".edit-row");
  const addTofu = async () => {
    await card.getByRole("button", { name: "Mahlzeit bearbeiten: Reis mit Tofu" }).click();
    await dialog.getByRole("button", { name: "+ Zutat" }).click();
    await expect(dialog.locator("#nutritionMealEditTitle")).toHaveText("Zutat hinzufügen");
    await dialog.getByLabel("Datenbank durchsuchen").fill("Tofu");
    await dialog.getByRole("button", { name: "Suchen" }).click();
    // Any unit: the new row asks for its amount in the food's own unit.
    await expect(dialog.locator(".swap-result")).toHaveCount(2);
    await dialog.locator(".swap-result", { hasText: "Tofu gebacken" }).click();
    await expect(rows).toHaveCount(4);
    await expect(rows.nth(3)).toContainText("BLS 4.0");
    await expect(rows.nth(3).getByRole("button", { name: /tauschen/ })).toHaveCount(0);
    await expect(rows.nth(3).getByRole("spinbutton")).toBeFocused();
  };
  await addTofu();
  await dialog.getByRole("button", { name: "Abbrechen" }).click();
  await expect(dialog).toBeHidden();
  expect(posts).toEqual([]);

  await addTofu();
  await rows.nth(3).getByRole("spinbutton").fill("120");
  await dialog.locator("#nutritionMealEditSave").click();
  await expect(dialog).toBeHidden();
  expect(posts).toEqual([{ id: MEAL.id, revision: "rev-1", edits: [{ add: { food_id: "bls:tofu-gebacken", amount: 120, unit: "g" } }] }]);
  await expect(page.locator("#nutritionStatus")).toContainText("Mahlzeit geändert.");
  await expect(card).toContainText("Tofu gebacken");
});

test("@responsive the dialog changes a meal's day, type and time with its components, then undoes them", async ({ page }) => {
  const posts = await mockDiary(page);
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEntries .nutrition-card");
  await card.getByRole("button", { name: "Mahlzeit bearbeiten: Reis mit Tofu" }).click();
  const dialog = page.locator("#nutritionMealEditDialog");
  const [date, time, type, unknown] = ["Date", "Time", "Type", "Unknown"].map((name) => dialog.locator(`#nutritionMealEdit${name}`));
  await expect(date).toHaveValue(DAY);
  await expect(time).toHaveValue("12:00");
  await expect(type).toHaveValue("lunch");
  await expect(dialog.locator("#nutritionMealEditSave")).toBeDisabled();

  // A default time follows the new type, a named one stays; a new day alone is a change.
  await type.selectOption("dinner");
  await expect(time).toHaveValue("18:00");
  await time.fill("19:30");
  await type.selectOption("snack");
  await expect(time).toHaveValue("19:30");
  await date.fill("2026-11-04");
  await dialog.locator(".edit-row").first().getByRole("spinbutton").fill("300");
  await dialog.locator("#nutritionMealEditSave").click();
  await expect(dialog).toBeHidden();
  expect(posts).toEqual([{ id: MEAL.id, revision: "rev-1", edits: [{ index: 0, amount: 300 }], meal: { meal_date: "2026-11-04", meal_type: "snack", meal_time: "19:30" } }]);
  await expect(page.locator("#nutritionStatus")).toContainText("Mahlzeit geändert.");
  await page.locator("#nutritionStatus").getByRole("button", { name: "Rückgängig" }).click();
  await expect(card).toContainText("669 kcal");

  // A type without a default time keeps the shown time, and the save names it; the meal change needs no component edit.
  await card.getByRole("button", { name: "Mahlzeit bearbeiten: Reis mit Tofu" }).click();
  await type.selectOption("snack");
  await type.selectOption("dinner");
  await expect(time).toHaveValue("12:00");
  await dialog.locator("#nutritionMealEditSave").click();
  await expect(dialog).toBeHidden();
  expect(posts[1]).toEqual({ id: MEAL.id, revision: "rev-1", meal: { meal_type: "dinner", meal_time: "12:00" } });

  // An unknown time keeps only the type and disables the time.
  await card.getByRole("button", { name: "Mahlzeit bearbeiten: Reis mit Tofu" }).click();
  await unknown.check();
  await expect(time).toBeDisabled();
  await expect(time).toHaveValue("");
  await dialog.locator("#nutritionMealEditSave").click();
  await expect(dialog).toBeHidden();
  expect(posts[2]).toEqual({ id: MEAL.id, revision: "rev-1", meal: { logged_time_known: false } });
});

test("@responsive removing every component deletes the meal only after its confirmation; offline saving is disabled", async ({ page, context }) => {
  const posts = await mockDiary(page);
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEntries .nutrition-card");
  await card.getByRole("button", { name: "Mahlzeit bearbeiten: Reis mit Tofu" }).click();
  const dialog = page.locator("#nutritionMealEditDialog");
  const rows = dialog.locator(".edit-row");
  await expect(rows).toHaveCount(3);
  for (const name of ["Basmatireis, gekocht", "Tofu natur", "Olivenöl"]) await rows.filter({ hasText: name }).getByRole("button", { name: `${name} entfernen` }).click();
  await dialog.locator("#nutritionMealEditSave").click();
  await expect(page.locator("#confirmationDialog")).toBeVisible();
  await page.locator("#confirmationDialogCancel").click();
  await expect(dialog).toBeVisible();
  expect(posts).toEqual([]);

  await rows.first().getByRole("button", { name: "Wiederherstellen" }).click();
  await rows.first().getByRole("spinbutton").fill("250");
  await expect(dialog.locator("#nutritionMealEditSave")).toBeEnabled();
  await context.setOffline(true);
  await expect(dialog.locator("#nutritionMealEditSave")).toBeDisabled();
  await expect(dialog.locator("#nutritionMealEditStatus")).toContainText("Offline");
  await context.setOffline(false);
  await expect(dialog.locator("#nutritionMealEditSave")).toBeEnabled();
});

test("@responsive a meal that changed meanwhile is refused and a failed reload still shows the receipt", async ({ page }) => {
  const posts = await mockDiary(page);
  let refuse = true;
  await page.route("**/api/nutrition/entry/components", (route) => {
    if (route.request().method() !== "POST" || !refuse) return route.fallback();
    refuse = false;
    return route.fulfill({ status: 409, json: { ok: false, error: "Die Mahlzeit hat sich geändert. Bitte erneut öffnen.", reason: "meal_changed" } });
  });
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEntries .nutrition-card");
  await card.getByRole("button", { name: "Mahlzeit bearbeiten: Reis mit Tofu" }).click();
  const dialog = page.locator("#nutritionMealEditDialog");
  await dialog.locator(".edit-row").first().getByRole("spinbutton").fill("1");
  await dialog.locator("#nutritionMealEditSave").click();
  await expect(dialog.locator("#nutritionMealEditStatus")).toContainText("Die Mahlzeit hat sich geändert.");
  await expect(dialog).toBeVisible();

  await page.unroute("**/api/nutrition/day**");
  await page.route("**/api/nutrition/day**", (route) => route.abort());
  await dialog.locator("#nutritionMealEditSave").click();
  await expect(dialog).toBeHidden();
  await expect(page.locator("#nutritionStatus")).toContainText("Mahlzeit geändert.");
  expect(posts).toHaveLength(1);
});

test("@responsive an error that arrives after the dialog was closed shows as a toast", async ({ page }) => {
  await mockDiary(page);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/nutrition/entry/components", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await gate;
    return route.fulfill({ status: 500, json: { ok: false, error: "Serverfehler" } });
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/#nutrition/diary");
  await page.locator("#nutritionEntries .nutrition-card").getByRole("button", { name: "Mahlzeit bearbeiten: Reis mit Tofu" }).click();
  const dialog = page.locator("#nutritionMealEditDialog");
  await dialog.locator(".edit-row").first().getByRole("spinbutton").fill("1");
  await dialog.locator("#nutritionMealEditSave").click();
  await dialog.locator("#nutritionMealEditCancel").click();
  await expect(dialog).toBeHidden();
  release();
  await expect(page.locator(".toast")).toContainText("Mahlzeit konnte nicht gespeichert werden");
  expect(errors).toEqual([]);
});
