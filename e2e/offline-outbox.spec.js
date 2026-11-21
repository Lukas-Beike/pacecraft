const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// #3761: the athlete's own saves wait on the device while the server cannot be reached
// and are sent once when it can. The fixture runs on a real server, so the repeat is real.
const WEIGHT = "**/api/body/weight";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

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
  await page.route(WEIGHT, (route) => (route.request().method() === "PUT" ? route.abort("internetdisconnected") : route.fallback()));
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

  await page.unroute(WEIGHT);
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
  await expect(dialog.locator("li")).toContainText("Gewicht 81.2 kg");
  await expect(dialog.locator("li")).toContainText("wartet");
  expect(await new AxeBuilder({ page }).include("#outboxDialog").withTags(["wcag2aa"]).analyze()).toEqual(expect.objectContaining({ violations: [] }));
  await dialog.getByRole("button", { name: "Verwerfen" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator("#statusStrip")).not.toContainText("wartet");

  await page.unroute(WEIGHT);
  await page.evaluate(() => globalThis.dispatchEvent(new Event("online")));
  await expect(row.locator("td").first()).not.toContainText("81,2");
  expect(sent).toHaveLength(1);
});
