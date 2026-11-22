const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// #3761: the athlete's own saves wait on the device while the server cannot be reached
// and are sent once when it can. The fixture runs on a real server, so the repeat is real.
const WEIGHT = "**/api/body/weight";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// The outbox does not use the service worker, which leaves /api/ requests to the network. Routing
// misses requests that pass through a worker's fetch listener, so a blocked save sometimes went out
// online (CI 44878160199, 44888018168); without the worker every route applies.
test.use({ serviceWorkers: "block" });

async function openWeightDialog(page) {
  // Under 500 px height only critical notices show text (#1784); the outbox line needs a tall screen.
  await page.setViewportSize({ width: page.viewportSize().width, height: 800 });
  await page.goto("/#nutrition/body");
  const row = page.locator("tr[data-metric='weight_kg']");
  await expect(row).toBeVisible();
  await expect.poll(() => page.evaluate(() => Boolean(state.data) && state.loadPromise === null)).toBe(true);
  return row;
}

async function saveWeightOffline(page, row, kg) {
  await page.context().route(WEIGHT, (route) => (route.request().method() === "PUT" ? route.abort("internetdisconnected") : route.fallback()));
  await row.getByRole("button", { name: "Gewicht eintragen" }).click();
  await expect(page.locator("#weightDialog")).toBeVisible();
  // The dialog fills the day's stored value after it opens; wait for that before typing.
  await expect.poll(async () => {
    await page.locator("#weightDialogKg").fill(kg);
    return page.locator("#weightDialogKg").inputValue();
  }).toBe(kg);
  await page.locator("#weightDialog").getByRole("button", { name: "Speichern" }).click();
  await expect(page.locator("#weightDialog")).toBeHidden();
  await expect(page.locator("#toast")).toContainText("wartet, noch nicht gesendet");
  await expect(page.locator("#statusStrip")).toContainText("1 Eintrag wartet");
}

async function removeWeight(page) {
  await page.evaluate(async () => {
    const day = new Date().toLocaleDateString("sv-SE");
    await api("/api/body/weight/remove", { method: "PUT", body: JSON.stringify({ date: day, confirmed: true }) }).catch(() => null);
  });
}

test("@responsive a weight saved offline waits, is sent once after reconnecting and a repeated key adds nothing", async ({ page }) => {
  const row = await openWeightDialog(page);
  const sent = [];
  page.on("request", (request) => {
    if (request.method() === "PUT" && new URL(request.url()).pathname === "/api/body/weight") sent.push(request.headers()["idempotency-key"]);
  });
  await saveWeightOffline(page, row, "123.4");
  await expect(row.locator("td").first()).not.toContainText("123,4");
  expect(sent).toHaveLength(1);
  expect(sent[0]).toMatch(UUID);

  await page.context().unroute(WEIGHT);
  await page.evaluate(() => globalThis.dispatchEvent(new Event("online")));
  await expect(row.locator("td").first()).toContainText("123,4");
  await expect(page.locator("#statusStrip")).not.toContainText("wartet");
  // The first attempt never reached the server; the replay is the second request with the same key.
  expect(sent).toHaveLength(2);
  expect(sent[1]).toBe(sent[0]);

  // The same key again, even with another value, answers the stored result and writes nothing.
  const again = await page.evaluate(async (key) => {
    const response = await fetch("/api/body/weight", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": cookie("ic_csrf"), "Idempotency-Key": key },
      body: JSON.stringify({ date: new Date().toLocaleDateString("sv-SE"), kg: 99 }),
    });
    return { status: response.status, entries: (await response.json()).entries };
  }, sent[0]);
  expect(again.status).toBe(200);
  expect(again.entries.map((entry) => entry.kg)).toContain(123.4);
  expect(again.entries.map((entry) => entry.kg)).not.toContain(99);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await removeWeight(page);
});

test("@responsive a waiting entry is listed and can be discarded, and nothing is sent afterwards", async ({ page }) => {
  const row = await openWeightDialog(page);
  const sent = [];
  page.on("request", (request) => {
    if (request.method() === "PUT" && new URL(request.url()).pathname === "/api/body/weight") sent.push(request.url());
  });
  await saveWeightOffline(page, row, "81.2");
  await page.locator("#statusStrip").getByRole("button", { name: "Anzeigen" }).click();
  const dialog = page.locator("#outboxDialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("li")).toHaveCount(1);
  await expect(dialog.locator("li")).toContainText("Gewicht 81,2 kg");
  await expect(dialog.locator("li")).toContainText("wartet");
  expect(await new AxeBuilder({ page }).include("#outboxDialog").withTags(["wcag2aa"]).analyze()).toEqual(expect.objectContaining({ violations: [] }));
  await dialog.getByRole("button", { name: "Verwerfen" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator("#statusStrip")).not.toContainText("wartet");

  await page.context().unroute(WEIGHT);
  await page.evaluate(() => globalThis.dispatchEvent(new Event("online")));
  await expect(row.locator("td").first()).not.toContainText("81,2");
  expect(sent).toHaveLength(1);
});

// #3817: a rejected entry opens in its own dialog, filled with the queued values; saving replaces
// it in place under a new key and sends the changed value once.
async function rejectQueued(page, url, method) {
  await page.context().unroute(url);
  await page.context().route(url, (route) => (route.request().method() === method ? route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ error: "Wert abgelehnt." }) }) : route.fallback()));
  await page.evaluate(() => globalThis.dispatchEvent(new Event("online")));
  await expect(page.locator("#statusStrip")).toContainText("1 abgelehnt");
  await page.locator("#statusStrip").getByRole("button", { name: "Anzeigen" }).click();
  const dialog = page.locator("#outboxDialog");
  await expect(dialog.locator("li")).toContainText("abgelehnt: Wert abgelehnt.");
  return dialog;
}

test("@responsive a rejected weight is edited in the Gewicht dialog and replaces the entry under a new key", async ({ page }) => {
  const row = await openWeightDialog(page);
  const sent = [];
  page.on("request", (request) => {
    if (request.method() === "PUT" && new URL(request.url()).pathname === "/api/body/weight") sent.push({ key: request.headers()["idempotency-key"], kg: request.postDataJSON().kg });
  });
  await saveWeightOffline(page, row, "123.4");
  const dialog = await rejectQueued(page, WEIGHT, "PUT");
  await dialog.getByRole("button", { name: "Bearbeiten" }).click();
  await expect(dialog).toBeHidden();
  const weightDialog = page.locator("#weightDialog");
  await expect(weightDialog).toBeVisible();
  await expect(page.locator("#weightDialogKg")).toHaveValue("123.4");
  await expect(page.locator("#weightDialogRemove")).toBeHidden();
  await page.locator("#weightDialogKg").fill("78.6");
  await page.context().unroute(WEIGHT);
  await weightDialog.getByRole("button", { name: "Speichern" }).click();
  await expect(weightDialog).toBeHidden();
  await expect(row.locator("td").first()).toContainText("78,6");
  await expect(page.locator("#statusStrip")).not.toContainText("abgelehnt");
  const changed = sent.filter((request) => Number(request.kg) === 78.6);
  expect(changed).toHaveLength(1);
  expect(changed[0].key).toMatch(UUID);
  expect(changed[0].key).not.toBe(sent[0].key);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await removeWeight(page);
});

test("@responsive a rejected meal is edited in the product dialog and replaces the entry under a new key", async ({ page }) => {
  const productName = `Outbox Riegel ${Date.now()}`;
  const ENTRY = "**/api/nutrition/entry";
  await page.setViewportSize({ width: page.viewportSize().width, height: 800 });
  await page.goto("/#nutrition/products");
  await page.locator("#nutritionProductManual").click();
  const editor = page.locator("#nutritionExtraction");
  await editor.locator('[name="name"]').fill(productName);
  await editor.locator('[name="kcal"]').fill("400");
  await editor.locator('[name="basis_amount"]').fill("100");
  await editor.locator('[name="basis_unit"]').selectOption("g");
  await editor.getByRole("button", { name: "Produkt lokal speichern" }).click();
  await expect.poll(() => page.evaluate(() => Boolean(state.data) && state.loadPromise === null)).toBe(true);
  const sent = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/nutrition/entry") sent.push({ key: request.headers()["idempotency-key"], amount: request.postDataJSON().amount });
  });
  await page.context().route(ENTRY, (route) => (route.request().method() === "POST" ? route.abort("internetdisconnected") : route.fallback()));
  // A newly saved product opens its use dialog.
  await expect(page.locator("#nutritionProductUseDialog")).toBeVisible();
  await page.locator("#nutritionProductUseAmount").fill("50");
  await page.locator("#nutritionProductUseAdd").click();
  await expect(page.locator("#statusStrip")).toContainText("1 Eintrag wartet");
  const dialog = await rejectQueued(page, ENTRY, "POST");
  await dialog.getByRole("button", { name: "Bearbeiten" }).click();
  await expect(dialog).toBeHidden();
  const useDialog = page.locator("#nutritionProductUseDialog");
  await expect(useDialog).toBeVisible();
  await expect(useDialog).toContainText(productName);
  await expect(page.locator("#nutritionProductUseAmount")).toHaveValue("50");
  await page.locator("#nutritionProductUseAmount").fill("60");
  await page.context().unroute(ENTRY);
  await page.locator("#nutritionProductUseAdd").click();
  await expect(useDialog).toBeHidden();
  await expect(page.locator("#statusStrip")).not.toContainText("abgelehnt");
  await expect.poll(() => sent.filter((request) => request.amount === 60).length).toBe(1);
  const changed = sent.find((request) => request.amount === 60);
  expect(changed.key).toMatch(UUID);
  expect(changed.key).not.toBe(sent[0].key);
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#nutritionEntries .nutrition-card").filter({ hasText: productName })).toHaveCount(1);
});
