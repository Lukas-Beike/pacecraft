const { test, expect } = require("@playwright/test");

// #3028: a library entry is planned and deleted without the Coach, each with a receipt and "Rückgängig".
test("@responsive Einplanen and Löschen work in the library card and Rückgängig takes each back", async ({ page, request }) => {
  test.setTimeout(180_000);
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  const posts = [];
  page.on("request", (item) => {
    if (item.method() === "POST" && /^\/api\/library\//.test(new URL(item.url()).pathname)) posts.push([new URL(item.url()).pathname, item.postDataJSON()]);
  });
  await page.goto("/#plan/library");
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.locator("#library details.library-sport").first().evaluate((section) => { section.open = true; });
  const first = page.locator("#library .library-card").first();
  const name = (await first.locator("h4").textContent()).trim();
  const card = page.locator("#library .library-card", { has: page.locator("h4", { hasText: name }) }).first();
  const actions = card.locator(":scope > .planned-activity-actions");
  await expect(actions.getByRole("button")).toHaveText(["Einplanen", "Löschen"]);

  // Einplanen opens the calendar's day strip; one tap plans the entry locally.
  await actions.getByRole("button", { name: "Einplanen" }).click();
  const days = card.locator(".planned-move-days");
  await expect(days).toBeVisible();
  await expect(days.getByRole("button", { name: "Anderes Datum" })).toBeVisible();
  const day = days.locator("button[data-day]").nth(1);
  const dayKey = await day.getAttribute("data-day");
  await day.click();
  const status = card.locator(":scope > .planned-move-status");
  await expect(status).toContainText("Eingeplant:");
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/schedule$/), { date: dayKey }]);
  await status.getByRole("button", { name: "Rückgängig" }).click();
  await expect(status).toContainText("Zurückgenommen.");

  // Löschen acts at once (G-03); the server archives the entry and "Rückgängig" brings it back.
  await actions.getByRole("button", { name: "Löschen" }).click();
  const receipt = page.locator("#library > .planned-move-status");
  await expect(receipt).toContainText(`Gelöscht: ${name}`);
  await expect(card).toHaveCount(0);
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/archive$/), { confirmed: true }]);
  await receipt.getByRole("button", { name: "Rückgängig" }).click();
  // The entry's sport section comes back closed when the entry was its only card.
  await expect(card).toHaveCount(1);
  await page.locator("#library details.library-sport:not([open])").evaluateAll((sections) => sections.forEach((section) => { section.open = true; }));
  await expect(card.locator(":scope > .planned-move-status")).toContainText("Zurückgenommen.");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await page.unrouteAll({ behavior: "ignoreErrors" });
});
