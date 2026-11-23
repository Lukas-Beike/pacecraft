const { test, expect } = require("@playwright/test");

// #3951 and #3950: one editor makes a library entry and a planned unit without the Coach; each write shows a
// receipt with "Rückgängig", a deleted entry can be restored, and nothing goes to a provider.
async function openApp(page, request, route) {
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  await page.goto(route);
  await page.waitForFunction(() => state.initialStateLoaded);
}

test("@responsive the Bibliothek creates, edits, restores and deletes an entry", async ({ page, request }) => {
  test.setTimeout(180_000);
  const posts = [];
  page.on("request", (item) => {
    if (item.method() === "POST" && /^\/api\/library/.test(new URL(item.url()).pathname)) posts.push([new URL(item.url()).pathname, item.postDataJSON()]);
  });
  await openApp(page, request, "/#plan/library");
  const dialog = page.locator("#unitEditDialog");
  await page.locator("#library").getByRole("button", { name: "+ Einheit" }).click();
  await expect(dialog.getByRole("heading", { name: "Bibliothekseinheit hinzufügen" })).toBeVisible();

  // The steps are rows; the duration follows them.
  await dialog.getByLabel("Name", { exact: true }).fill("E2E Schwelle");
  await dialog.getByRole("button", { name: "+ Schritt" }).click();
  await dialog.getByRole("button", { name: "+ Wiederholung" }).click();
  await expect(dialog.locator(".unit-total")).toHaveText("Dauer 34 min");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  expect(posts.at(-1)).toEqual(["/api/library", expect.objectContaining({ name: "E2E Schwelle", duration_minutes: 34, sport: "Ride", description: "- 10m 60%\n\n3x\n- 5m 60%\n- 3m 50%" })]);
  const card = page.locator("#library .library-card", { has: page.locator("h4", { hasText: /^E2E Schwelle$/ }) });
  await expect(card).toBeVisible();
  await expect(card.locator(":scope > .planned-move-status")).toContainText("Angelegt: E2E Schwelle");

  // "Bearbeiten" opens the entry as rows again; the edit sends the whole content.
  await card.getByRole("button", { name: "Bearbeiten" }).click();
  await expect(dialog.getByLabel("Name", { exact: true })).toHaveValue("E2E Schwelle");
  await expect(dialog.locator(".unit-total")).toHaveText("Dauer 34 min");
  await dialog.getByLabel("Name", { exact: true }).fill("E2E Schwelle lang");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  expect(posts.at(-1)[0]).toMatch(/\/update$/);
  const renamed = page.locator("#library .library-card", { has: page.locator("h4", { hasText: "E2E Schwelle lang" }) });
  await expect(renamed.locator(":scope > .planned-move-status")).toContainText("Gespeichert: E2E Schwelle lang");

  // Löschen archives it; "Archiviert" lists it with "Wiederherstellen", then "Endgültig löschen" removes it for good.
  const entryCard = () => page.locator("#library .library-card", { has: page.locator("h4", { hasText: "E2E Schwelle lang" }) });
  const lead = page.locator("#library > .planned-move-status");
  const archiveAndShow = async () => {
    await entryCard().getByRole("button", { name: "Löschen" }).click();
    await expect(lead).toContainText("Gelöscht: E2E Schwelle lang");
    await page.locator("#library .chip", { hasText: "Archiviert" }).click();
    await expect(entryCard().getByRole("button", { name: "Wiederherstellen" })).toBeVisible();
  };
  await archiveAndShow();
  await expect(entryCard().getByRole("button")).toHaveText(["Wiederherstellen", "Endgültig löschen"]);
  await entryCard().getByRole("button", { name: "Wiederherstellen" }).click();
  await expect(entryCard().locator(":scope > .planned-move-status")).toContainText("Wiederhergestellt: E2E Schwelle lang");
  await archiveAndShow();
  await entryCard().getByRole("button", { name: "Endgültig löschen" }).click();
  await expect(lead).toContainText("Endgültig gelöscht: E2E Schwelle lang");
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/delete$/), { confirmed: true }]);
  await lead.getByRole("button", { name: "Rückgängig" }).click();
  await expect(entryCard()).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

test("@responsive the calendar edits and archives a unit", async ({ page, request }) => {
  test.setTimeout(180_000);
  const posts = [];
  page.on("request", (item) => {
    if (item.method() === "POST" && /^\/api\/planned-units/.test(new URL(item.url()).pathname)) posts.push([new URL(item.url()).pathname, item.postDataJSON()]);
  });
  await openApp(page, request, "/#plan/overview");
  const dialog = page.locator("#unitEditDialog");
  // The calendar has no add button: a unit is planned from the Bibliothek. It is made here through that path's own
  // routes, on a day of next week (today's session of the fixture would absorb it into its own row).
  await expect(page.getByRole("button", { name: "+ Einheit hinzufügen" })).toHaveCount(0);
  await page.evaluate(async () => {
    const made = await api("/api/library", { method: "POST", body: JSON.stringify({ name: "E2E Ausdauer", description: "- 10m 60%", sport: "Ride", duration_minutes: 10, target: "POWER" }) });
    const today = timezoneDateKey(state.data.profile.timezone, new Date());
    await api(`/api/library/${made.local_id}/schedule`, { method: "POST", body: JSON.stringify({ date: addDateKey(planWeekStart(today), 7) }) });
    await globalThis.load();
  });
  await page.getByRole("button", { name: "Nächste Woche" }).click();
  const entry = page.locator(".planned-entry", { hasText: "E2E Ausdauer" });
  // The wide layout opens its default entry itself: a click would close it again.
  if (!(await entry.first().evaluate((node) => node.open))) await entry.locator("summary").first().click();

  await entry.getByRole("button", { name: "Bearbeiten" }).click();
  await expect(dialog.getByRole("heading", { name: "Einheit bearbeiten" })).toBeVisible();
  await dialog.getByLabel("Name", { exact: true }).fill("E2E Ausdauer neu");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/update$/), expect.objectContaining({ name: "E2E Ausdauer neu", expected_payload_hash: expect.stringMatching(/^[0-9a-f]{64}$/) })]);
  const edited = page.locator(".planned-entry", { hasText: "E2E Ausdauer neu" });
  await expect(edited.locator(".planned-move-status")).toContainText("Gespeichert: E2E Ausdauer neu");

  // Archivieren acts at once and the day's receipt takes it back.
  await edited.getByRole("button", { name: "Archivieren" }).click();
  const day = page.locator(".planned-day").first();
  await expect(day.locator(".planned-day-content > .planned-move-status")).toContainText("Archiviert: E2E Ausdauer neu");
  await expect(page.locator(".planned-entry", { hasText: "E2E Ausdauer neu" })).toHaveCount(0);
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/archive$/), { confirmed: true, expected_payload_hash: expect.stringMatching(/^[0-9a-f]{64}$/) }]);
  await day.locator(".planned-day-content > .planned-move-status").getByRole("button", { name: "Rückgängig" }).click();
  await expect(page.locator(".planned-entry", { hasText: "E2E Ausdauer neu" })).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3950: the training plans sit at the top of the Bibliothek; a plan is created, archived, restored and deleted there.
test("@responsive the Bibliothek creates, archives, restores and deletes a training plan", async ({ page, request }) => {
  test.setTimeout(180_000);
  const posts = [];
  page.on("request", (item) => {
    if (item.method() === "POST" && /^\/api\/training-plans/.test(new URL(item.url()).pathname)) posts.push([new URL(item.url()).pathname, item.postDataJSON()]);
  });
  await openApp(page, request, "/#plan/library");
  const dialog = page.locator("#unitEditDialog");
  await page.locator("#library").getByRole("button", { name: "+ Plan" }).click();
  await expect(dialog.getByRole("heading", { name: "Plan hinzufügen" })).toBeVisible();
  await dialog.getByLabel("Name", { exact: true }).fill("E2E Aufbau");
  await dialog.getByLabel("Ziel", { exact: true }).fill("Grundlage");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  expect(posts.at(-1)).toEqual(["/api/training-plans", expect.objectContaining({ name: "E2E Aufbau", goal: "Grundlage", status: "planned" })]);
  const card = () => page.locator("#library .library-card", { has: page.locator("h4", { hasText: "E2E Aufbau" }) });
  const lead = page.locator("#library > .planned-move-status");
  await expect(card().locator(":scope > .planned-move-status")).toContainText("Angelegt: E2E Aufbau");
  await expect(card().locator(":scope > .planned-activity-actions").getByRole("button")).toHaveText(["Bearbeiten", "Archivieren", "Löschen"]);

  // The plan is above the units.
  const above = await page.locator("#library").evaluate((root) => {
    const plan = [...root.querySelectorAll(".library-card")].find((node) => node.querySelector("h4")?.textContent === "E2E Aufbau");
    const units = root.querySelector("details.library-sport");
    return Boolean(plan && units && plan.compareDocumentPosition(units) & Node.DOCUMENT_POSITION_FOLLOWING);
  });
  expect(above).toBeTruthy();

  await card().getByRole("button", { name: "Archivieren" }).click();
  await expect(lead).toContainText("Archiviert: E2E Aufbau");
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/update$/), { status: "archived", confirmed: true }]);
  await page.locator("#library .chip", { hasText: "Archiviert" }).click();
  await expect(card().locator(":scope > .planned-activity-actions").getByRole("button")).toHaveText(["Wiederherstellen", "Löschen"]);
  await card().getByRole("button", { name: "Wiederherstellen" }).click();
  await expect(lead).toContainText("Wiederhergestellt: E2E Aufbau");
  await page.locator("#library .chip", { hasText: "Aktiv" }).click();
  await expect(card().getByRole("button", { name: "Bearbeiten" })).toBeVisible();

  await card().getByRole("button", { name: "Löschen" }).click();
  await expect(lead).toContainText("Gelöscht: E2E Aufbau");
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/delete$/), { confirmed: true }]);
  await expect(card()).toHaveCount(0);
  await lead.getByRole("button", { name: "Rückgängig" }).click();
  await expect(card()).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});
