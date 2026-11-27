const { test, expect } = require("./helpers/today");

// Routes miss requests that pass through the service worker's fetch listener; this spec does not
// test the worker, so it is blocked and every route applies (#3842).
test.use({ serviceWorkers: "block" });

// #3028: a library entry is planned and deleted without the Coach, each with a receipt and "Rückgängig".
test("@responsive Einplanen and Archivieren work in the library card and Rückgängig takes each back", async ({ page, request }) => {
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
  // Edit and delete are the pencil and the trash icon, named by their label (#4279).
  await expect(actions.getByRole("button")).toHaveText(["", "Einplanen", "Archivieren"]);
  await expect(actions.getByRole("button", { name: /^Bibliothekseintrag bearbeiten: / })).toHaveCount(1);

  // Einplanen opens the calendar's day strip, which marks a day an entry blocks (#4060); one tap plans the entry locally.
  const blockedDay = await page.evaluate(() => addDateKey(timezoneDateKey(state.data?.profile?.timezone, new Date()), 3));
  await page.route("**/api/library/*/blocked-days*", (route) => route.fulfill({ json: { blocked: { [blockedDay]: ["Reise"] } } }));
  await actions.getByRole("button", { name: "Einplanen" }).click();
  const days = card.locator(".planned-move-days");
  await expect(days).toBeVisible();
  await expect(days.locator(`button[data-day="${blockedDay}"]`)).toBeDisabled();
  await expect(days.locator(`button[data-day="${blockedDay}"]`)).toHaveAttribute("title", "Belegt: Reise");
  await expect(days.getByRole("button", { name: "Anderes Datum" })).toBeVisible();
  // #3980: a background reload redraws the library; an open day strip stays open.
  await page.evaluate(() => renderLibrary(state.data.library));
  await expect(days).toBeVisible();
  const day = days.locator("button[data-day]").nth(1);
  const dayKey = await day.getAttribute("data-day");
  await day.click();
  const status = card.locator(":scope > .planned-move-status");
  await expect(status).toContainText("Eingeplant:");
  await expect(days).toBeHidden();
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/schedule$/), { date: dayKey }]);
  await status.getByRole("button", { name: "Rückgängig" }).click();
  await expect(status).toContainText("Zurückgenommen.");

  // Archivieren acts at once (G-03, #4307); "Rückgängig" brings the entry back.
  await actions.getByRole("button", { name: "Archivieren" }).click();
  const receipt = page.locator("#library > .planned-move-status");
  await expect(receipt).toContainText(`Archiviert: ${name}`);
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

// #3932 K-01: a refusal because of an appointment that still waits for its classification offers the sheet.
test("@responsive Einplanen refused for an unclassified appointment offers Termin einordnen", async ({ page, request }) => {
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  await page.goto("/#plan/library");
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.locator("#library details.library-sport").first().evaluate((section) => { section.open = true; });
  const card = page.locator("#library .library-card").first();
  const day = await page.evaluate(() => {
    const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
    const target = addDateKey(today, 2);
    // A multi-day appointment that began earlier still covers the day.
    state.data.external_calendar = { ...state.data.external_calendar, events: [{ uid: "open-1", name: "Zahnarzt", start_local: `${today}T09:00:00`, end_local: `${addDateKey(target, 1)}T11:00:00`, impact: "open" }] };
    return target;
  });
  await page.route("**/api/library/*/blocked-days*", (route) => route.fulfill({ json: { blocked: {} } }));
  await page.route("**/api/library/*/schedule", (route) => route.fulfill({ status: 409, json: { error: "Planung wegen bestehender Kalendereinheiten nicht möglich: Zahnarzt.", reason: "calendar_event_open" } }));
  await card.getByRole("button", { name: "Einplanen" }).click();
  await card.locator(`.planned-move-days button[data-day="${day}"]`).click();
  const status = card.locator(":scope > .planned-move-status");
  await expect(status).toContainText("Zahnarzt");
  await status.getByRole("button", { name: "Termin einordnen" }).click();
  await expect(page.locator(".planned-event-sheet")).toBeVisible();
  await expect(page.locator(".planned-event-sheet h3")).toHaveText("Zahnarzt");
  await page.unrouteAll({ behavior: "ignoreErrors" });
});
