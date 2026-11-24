const { test, expect } = require("@playwright/test");

// The Einplanen test fails an /api request, so the service worker is blocked (#3842).
test.use({ serviceWorkers: "block" });

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
    // The fixture calendar blocks today + 2 and today + 5 with [NO_TRAINING] markers: a day of next week skips them.
    const free = (offset) => { let day = addDateKey(planWeekStart(today), offset); while ([2, 5].some((n) => addDateKey(today, n) === day)) day = addDateKey(day, 1); return day; };
    await api(`/api/library/${made.local_id}/schedule`, { method: "POST", body: JSON.stringify({ date: free(7) }) });
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
  // The unit sits on the first free day of next week; the receipt shows on its day.
  const day = page.locator(".planned-day", { has: page.locator(".planned-day-content > .planned-move-status") });
  await expect(day.locator(".planned-day-content > .planned-move-status")).toContainText("Archiviert: E2E Ausdauer neu");
  await expect(page.locator(".planned-entry", { hasText: "E2E Ausdauer neu" })).toHaveCount(0);
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/archive$/), { confirmed: true, expected_payload_hash: expect.stringMatching(/^[0-9a-f]{64}$/) }]);
  await day.locator(".planned-day-content > .planned-move-status").getByRole("button", { name: "Rückgängig" }).click();
  await expect(page.locator(".planned-entry", { hasText: "E2E Ausdauer neu" })).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3950, #4133: the training plans have their own tab after the Bibliothek; a plan is created, archived, restored and deleted there.
test("@responsive the Pläne tab creates, archives, restores and deletes a training plan", async ({ page, request }) => {
  test.setTimeout(180_000);
  const posts = [];
  page.on("request", (item) => {
    if (item.method() === "POST" && /^\/api\/training-plans/.test(new URL(item.url()).pathname)) posts.push([new URL(item.url()).pathname, item.postDataJSON()]);
  });
  await openApp(page, request, "/#plan/library");
  // The tabs: the Pläne tab follows the Bibliothek, and the Bibliothek holds only library workouts.
  await expect(page.locator("#workoutsPanel .plan-segment-nav a")).toHaveText(["Übersicht", "Bibliothek", "Pläne", "Wettkämpfe"]);
  await expect(page.locator("#library").getByRole("button", { name: "+ Plan" })).toHaveCount(0);
  await page.getByRole("link", { name: "Pläne", exact: true }).click();
  await expect(page).toHaveURL(/#plan\/plans$/);
  await expect(page.locator("#plans .context-empty")).toHaveText("Noch kein Plan.");
  const dialog = page.locator("#unitEditDialog");
  await page.locator("#plans").getByRole("button", { name: "+ Plan" }).click();
  await expect(dialog.getByRole("heading", { name: "Plan hinzufügen" })).toBeVisible();
  await dialog.getByLabel("Name", { exact: true }).fill("E2E Aufbau");
  await dialog.getByLabel("Ziel", { exact: true }).fill("Grundlage");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  expect(posts.at(-1)).toEqual(["/api/training-plans", expect.objectContaining({ name: "E2E Aufbau", goal: "Grundlage", status: "planned" })]);
  const card = () => page.locator("#plans .library-card", { has: page.locator("h4", { hasText: "E2E Aufbau" }) });
  const lead = page.locator("#plans > .planned-move-status");
  await expect(card().locator(":scope > .planned-move-status")).toContainText("Angelegt: E2E Aufbau");
  await expect(card().locator(":scope > .planned-activity-actions").getByRole("button")).toHaveText(["Bearbeiten", "Zeitraum ersetzen", "Archivieren", "Löschen"]);
  // The plan never shows in the Bibliothek.
  await expect(page.locator("#library .library-card h4", { hasText: "E2E Aufbau" })).toHaveCount(0);

  await card().getByRole("button", { name: "Archivieren" }).click();
  await expect(lead).toContainText("Archiviert: E2E Aufbau");
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/update$/), { status: "archived", confirmed: true }]);
  await page.locator("#plans .chip", { hasText: "Archiviert" }).click();
  await expect(card().locator(":scope > .planned-activity-actions").getByRole("button")).toHaveText(["Wiederherstellen", "Löschen"]);
  await card().getByRole("button", { name: "Wiederherstellen" }).click();
  await expect(lead).toContainText("Wiederhergestellt: E2E Aufbau");
  await page.locator("#plans .chip", { hasText: "Aktiv" }).click();
  await expect(card().getByRole("button", { name: "Bearbeiten" })).toBeVisible();

  await card().getByRole("button", { name: "Löschen" }).click();
  await expect(lead).toContainText("Gelöscht: E2E Aufbau");
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/delete$/), { confirmed: true }]);
  await expect(card()).toHaveCount(0);
  await lead.getByRole("button", { name: "Rückgängig" }).click();
  await expect(card()).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #4054, #4055: Einplanen offers the active plans and, when the illness check blocks, "Trotzdem einplanen" resends with the accepted code.
test("@responsive Einplanen sends the chosen plan and offers Trotzdem einplanen on an illness block", async ({ page, request }) => {
  test.setTimeout(180_000);
  const posts = [];
  await page.route("**/api/library/*/schedule", async (route) => {
    posts.push(route.request().postDataJSON());
    if (posts.length === 1) await route.fulfill({ status: 409, json: { error: "Krankheit gemeldet: In den nächsten Tagen keine harte oder lange Einheit.", reason: "illness_hard" } });
    else await route.fulfill({ json: { status: "local", planned: {}, change: { id: "c1", after_hash: "h1" } } });
  });
  await openApp(page, request, "/#plan/library");
  await page.evaluate(() => {
    state.data.plans = [{ id: "11111111-1111-1111-1111-111111111111", name: "E2E Plan", status: "planned" }, { id: "22222222-2222-2222-2222-222222222222", name: "E2E Alt", status: "archived" }];
    globalThis.load = async () => {};
    renderLibrary(state.data.library);
  });
  await page.locator("#library details.library-sport").first().evaluate((section) => { section.open = true; });
  const card = page.locator("#library .library-card", { has: page.getByRole("button", { name: "Einplanen" }) }).first();
  await card.getByRole("button", { name: "Einplanen" }).click();
  const field = card.getByLabel("Trainingsplan");
  await expect(field.locator("option")).toHaveText(["Ohne Plan", "E2E Plan"]);
  await field.selectOption({ label: "E2E Plan" });
  const day = card.locator(".planned-move-days button[data-day]").nth(1);
  const dayKey = await day.getAttribute("data-day");
  await day.click();
  expect(posts[0]).toEqual({ date: dayKey, plan_id: "11111111-1111-1111-1111-111111111111" });
  await expect(card.locator(":scope > .planned-move-status")).toContainText("Krankheit gemeldet");
  await card.getByRole("button", { name: "Trotzdem einplanen" }).click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1]).toEqual({ date: dayKey, plan_id: "11111111-1111-1111-1111-111111111111", accept_plan_findings: ["illness_hard"] });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

// #4020: a stored adaptive preview shows as a card above the week; "Übernehmen" applies it and the receipt undoes it as one batch.
test("@responsive the calendar applies a shown replan preview and Rückgängig takes it back as one batch", async ({ page, request }) => {
  test.setTimeout(180_000);
  const posts = [];
  await page.route("**/api/planning/replan/apply", async (route) => {
    posts.push(["apply", route.request().postDataJSON()]);
    // The first tap meets the illness check; "Trotzdem übernehmen" resends once, with the acceptance.
    if (posts.length === 1) await route.fulfill({ status: 409, json: { error: "Krankheit gemeldet: In den nächsten Tagen keine harte oder lange Einheit.", reason: "illness_hard" } });
    else await route.fulfill({ json: { status: "ok", id: "p1", updated: 2, plan_check_notes: [], changes: [{ id: "c1", after_hash: "h1" }, { id: "c2", after_hash: "h2" }] } });
  });
  await page.route("**/api/change-history/undo-batch", async (route) => {
    posts.push(["undo", route.request().postDataJSON()]);
    await route.fulfill({ json: { status: "undone", count: 2, remote_untouched: true } });
  });
  await openApp(page, request, "/#plan/overview");
  const day = (offset) => new Date(Date.now() + offset * 86_400_000).toISOString().slice(0, 10);
  await page.evaluate((dates) => {
    state.data.planning = { ...state.data.planning, latest_replan: {
      id: "p1", status: "preview",
      illness_pause: { approved: false, end_date: dates[1] },
      changes: [
        { date: dates[0], name: "Grundlage", blocking_triggers: ["illness"], before: { duration_minutes: 60 }, after: { duration_minutes: 40, rationale: "Adaptive adjustment: recovery signal suggests reducing intensity. Rest" } },
        { date: dates[1], name: "Lauf", blocking_triggers: ["calendar"], before: { duration_minutes: 90 }, after: { duration_minutes: 0, rationale: "Calendar [NO_TRAINING]: busy. The original stays." } },
      ],
    } };
    // The server does not know the injected preview, so a reload only redraws.
    globalThis.load = async () => renderPlanned(state.data.training_calendar);
    renderPlanned(state.data.training_calendar);
  }, [day(1), day(3)]);
  const card = page.locator(".planned-replan");
  await expect(card.locator(".replan-row")).toHaveCount(2);
  await expect(card.locator(".replan-row").first()).toContainText("Grundlage 60 min → 40 min · Krankheit gemeldet");
  await expect(card.locator(".replan-row").nth(1)).toContainText("Lauf 90 min → entfällt · Kalendertermin");
  // The model-facing rationale never reaches the row.
  await expect(card.locator(".replan-rows")).not.toContainText(/Adaptive|NO_TRAINING/);
  await expect(card).toContainText("trägt den heutigen Check-in als krank ein");

  await card.getByRole("button", { name: "Übernehmen" }).click();
  await expect(card.getByRole("button", { name: "Trotzdem übernehmen" })).toBeVisible();
  expect(posts).toEqual([["apply", { adjustment_id: "p1", confirmed: true }]]);
  await card.getByRole("button", { name: "Trotzdem übernehmen" }).click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts.at(-1)).toEqual(["apply", { adjustment_id: "p1", confirmed: true, accept_plan_findings: ["illness_hard"] }]);
  const receipt = page.locator("#plannedCalendar .planned-move-status").first();
  await expect(receipt).toContainText("Angepasst: 2 Einheiten");
  await receipt.getByRole("button", { name: "Rückgängig" }).click();
  expect(posts.at(-1)).toEqual(["undo", { changes: [{ id: "c1", after_hash: "h1" }, { id: "c2", after_hash: "h2" }] }]);
  await expect(receipt).toContainText("Zurückgenommen.");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

// #4024: "Zeitraum ersetzen" on a plan card lists what is archived and what is new and replaces them as one change.
test("@responsive a plan's period is replaced in one change and Rückgängig takes the whole change back", async ({ page, request }) => {
  test.setTimeout(240_000);
  const posts = [];
  page.on("request", (item) => {
    const path = new URL(item.url()).pathname;
    if (item.method() === "POST" && /\/replace$|undo-batch$/.test(path)) posts.push([path, item.postDataJSON()]);
  });
  await openApp(page, request, "/#plan/plans");
  const dialog = page.locator("#unitEditDialog");
  await page.locator("#plans").getByRole("button", { name: "+ Plan" }).click();
  await dialog.getByLabel("Name", { exact: true }).fill("E2E Ersatzplan");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator("#plans .library-card", { has: page.locator("h4", { hasText: "E2E Ersatzplan" }) })).toBeVisible();

  // A unit of next week joins the plan.
  // It is planned from the Bibliothek into the plan, through the routes "Einplanen" sends (the calendar has no add button).
  await page.evaluate(async () => {
    const plan = (state.data.plans || []).find((item) => item.name === "E2E Ersatzplan");
    const made = await api("/api/library", { method: "POST", body: JSON.stringify({ name: "E2E Alt", description: "- 10m 60%", sport: "Ride", duration_minutes: 10, target: "POWER" }) });
    const today = timezoneDateKey(state.data.profile.timezone, new Date());
    // The fixture calendar blocks today + 2 and today + 5 with [NO_TRAINING] markers: a day of next week skips them.
    const free = (offset) => { let day = addDateKey(planWeekStart(today), offset); while ([2, 5].some((n) => addDateKey(today, n) === day)) day = addDateKey(day, 1); return day; };
    await api(`/api/library/${made.local_id}/schedule`, { method: "POST", body: JSON.stringify({ date: free(9), plan_id: plan.id }) });
    await globalThis.load();
  });

  await page.goto("/#plan/plans");
  const card = page.locator("#plans .library-card", { has: page.locator("h4", { hasText: "E2E Ersatzplan" }) });
  await card.getByRole("button", { name: "Zeitraum ersetzen" }).click();
  await expect(dialog.getByRole("heading", { name: "Zeitraum ersetzen" })).toBeVisible();
  await expect(dialog.getByText("Wird archiviert · 1 Einheit")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Ersetzen" })).toBeDisabled();

  // The new unit is made with the unit editor and kept for the replacement.
  await dialog.getByRole("button", { name: "+ Einheit" }).click();
  await expect(dialog.getByRole("heading", { name: "Einheit hinzufügen" })).toBeVisible();
  await dialog.getByLabel("Name", { exact: true }).fill("E2E Neu");
  await dialog.getByRole("button", { name: "+ Schritt" }).click();
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog.getByText("Neu · 1 Einheit")).toBeVisible();
  await dialog.getByRole("button", { name: "Ersetzen" }).click();
  await expect(dialog).toBeHidden();
  expect(posts.at(-1)).toEqual([expect.stringMatching(/\/replace$/), expect.objectContaining({ confirmed: true, replaced_ids: [expect.any(String)], workouts: [expect.objectContaining({ name: "E2E Neu", description: "- 10m 60%" })] })]);

  // The plan stays what it is: its card carries the receipt, and no second plan appears.
  const lead = card.locator(":scope > .planned-move-status", { hasText: "Ersetzt: 1 → 1 Einheiten" });
  await expect(lead).toBeVisible();
  await expect(page.locator("#plans .library-card h4", { hasText: "E2E Ersatzplan" })).toHaveCount(1);
  await lead.getByRole("button", { name: "Rückgängig" }).click();
  expect(posts.at(-1)[0]).toMatch(/undo-batch$/);
  expect(posts.at(-1)[1].changes.length).toBeGreaterThan(1);
  await expect(page.locator("#plans .planned-move-status", { hasText: "Zurückgenommen." }).first()).toBeVisible();
});

// #4021: a competition with a sync conflict offers "Lokal behalten" and, behind a confirmation card, "Von dort übernehmen".
test("@responsive a competition conflict is settled at its card", async ({ page, request }) => {
  test.setTimeout(180_000);
  const posts = [];
  await page.route("**/api/competitions/*/resolve", async (route) => {
    posts.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true, status: "resolved" } });
  });
  await openApp(page, request, "/#plan/season");
  await page.evaluate(() => {
    const base = { event_date: "2099-01-06", sport: "run", priority: "A", sync_state: "conflict", sync_conflict: "{\"type\":\"changed\"}", external_id: "x" };
    state.data.competitions = [{ ...base, id: "c-1", name: "E2E Halbmarathon" }, { ...base, id: "c-2", name: "E2E Marathon", event_date: "2099-02-06" }];
    globalThis.load = async () => {};
    renderCompetitions(state.data.competitions);
  });
  const card = page.locator(".competition-card", { hasText: "E2E Halbmarathon" });
  await expect(card.locator(".competition-conflict")).toContainText("Bei Intervals.icu geändert");
  await expect(card.locator(".competition-conflict > .planned-activity-actions").getByRole("button")).toHaveText(["Lokal behalten", "Von dort übernehmen"]);

  // Adopting needs the confirmation card; nothing is sent before it.
  await card.getByRole("button", { name: "Von dort übernehmen" }).click();
  await expect(card.getByText("Deine lokalen Werte werden durch die von Intervals.icu ersetzt.")).toBeVisible();
  expect(posts).toEqual([]);
  await card.getByRole("button", { name: "Ersetzen" }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ strategy: "adopt_remote", confirmed: true });
  const other = page.locator(".competition-card", { hasText: "E2E Marathon" });
  await other.getByRole("button", { name: "Lokal behalten" }).click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1]).toEqual({ strategy: "keep_local" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

// #4022: "Aus Garmin entfernen" asks first, then runs the athlete's own Garmin removal job; the unit stays.
test("@responsive Aus Garmin entfernen asks first and removes only the Garmin copy", async ({ page, request }) => {
  test.setTimeout(180_000);
  const posts = [];
  await openApp(page, request, "/#plan/overview");
  // A unit is planned from the Bibliothek, through the routes "Einplanen" sends (the calendar has no add button).
  await page.evaluate(async () => {
    const made = await api("/api/library", { method: "POST", body: JSON.stringify({ name: "E2E Garmin", description: "- 10m 60%", sport: "Ride", duration_minutes: 10, target: "POWER" }) });
    const today = timezoneDateKey(state.data.profile.timezone, new Date());
    // The fixture calendar blocks today + 2 and today + 5 with [NO_TRAINING] markers: a day of next week skips them.
    const free = (offset) => { let day = addDateKey(planWeekStart(today), offset); while ([2, 5].some((n) => addDateKey(today, n) === day)) day = addDateKey(day, 1); return day; };
    await api(`/api/library/${made.local_id}/schedule`, { method: "POST", body: JSON.stringify({ date: free(10) }) });
    await globalThis.load();
  });
  await page.getByRole("button", { name: "Nächste Woche" }).click();
  const entry = page.locator(".planned-entry", { hasText: "E2E Garmin" });
  if (!(await entry.first().evaluate((node) => node.open))) await entry.locator("summary").first().click();
  await expect(entry.getByRole("button", { name: "Bearbeiten" })).toBeVisible();
  await expect(entry.getByRole("button", { name: "Aus Garmin entfernen" })).toHaveCount(0);

  // The fixture has no Garmin copy; the control is built for a unit the read says has one, outside the calendar's redraws.
  await page.route("**/api/sync/jobs", async (route) => {
    posts.push(route.request().postDataJSON());
    await route.fulfill({ json: { id: "j1" } });
  });
  await page.evaluate(() => {
    state.data.garmin = { ...state.data.garmin, configured: true, can_write_workouts: true };
    globalThis.waitForSyncJob = async () => ({ status: "completed" });
    globalThis.load = async () => {};
    const box = document.createElement("div");
    box.id = "garminBox";
    box.append(...plannedGarminRemoval({ local_id: "12345678-1234-5678-1234-567812345678", name: "E2E Garmin", remote_copies: [providerIds().garmin] }));
    document.body.prepend(box);
    window.scrollTo(0, 0);
  });
  const box = page.locator("#garminBox");
  await box.getByRole("button", { name: "Aus Garmin entfernen" }).click();
  await expect(box.getByText("Die Einheit bleibt in Pacecraft.")).toBeVisible();
  expect(posts).toEqual([]);
  await box.getByRole("button", { name: "Abbrechen" }).click();
  await expect(box.getByText("Die Einheit bleibt in Pacecraft.")).toBeHidden();
  await box.getByRole("button", { name: "Aus Garmin entfernen" }).click();
  await box.getByRole("button", { name: "Entfernen", exact: true }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ provider: "garmin", type: "plan_push", payload: { entries: [{ library_workout_id: "12345678-1234-5678-1234-567812345678", remove: true }] } });
  await page.unrouteAll({ behavior: "ignoreErrors" });
});
