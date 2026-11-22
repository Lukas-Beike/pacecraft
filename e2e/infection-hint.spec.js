// --today pins this calendar's day (#3200): the hint sits on the app's today.
const { test, expect, todayIn } = require("./helpers/today");

// Routes miss requests that pass through the service worker's fetch listener; this spec
// does not test the worker, so it is blocked and every route applies (#3842).
test.use({ serviceWorkers: "block" });

// #3919: "Ich bin krank" expands the hint into the symptom chips; "Bestätigen" saves the illness with them.
test("@responsive the infection hint expands into symptom chips and saves the selected ones", async ({ page, request }) => {
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  const timeZone = (await (await request.get("/api/bootstrap")).json()).profile?.timezone || undefined;
  const today = todayIn(timeZone);
  // The fixture's nights are ordinary, so today carries a synthetic infection hint (M14, #3762).
  await page.context().route(/\/api\/plan(\?|$)/, async (route) => {
    // A reload still in flight when the test ends has nothing left to show.
    const response = await route.fetch().catch(() => null);
    if (!response) return;
    const data = await response.json();
    const signal = (label, value, unit, lower, upper) => ({ label, value, unit, lower, upper, source: "Garmin Connect", unusual: true });
    const hint = { method: "infection-hint-v1", status: "hint", signals: [signal("Atemfrequenz", 17, "brpm", 14, 15), signal("Ruhepuls", 56, "bpm", 48, 51)], limits: "Keine Diagnose." };
    const days = (data.daily_planning_context || []).filter((day) => day.date !== today);
    data.daily_planning_context = [...days, { ...(data.daily_planning_context || []).find((day) => day.date === today), date: today, infection_hint: hint }];
    return route.fulfill({ json: data });
  });
  await page.goto("/#plan/overview");
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.locator('.planned-view-switch button[data-view="week"]').click();
  const note = page.locator(".planned-day.is-today [data-infection-hint]");
  await expect(note).toBeVisible();
  await expect(note.getByRole("button")).toHaveText(["Mir geht's gut", "Ich bin krank"]);
  await note.getByRole("button", { name: "Ich bin krank" }).click();
  const labels = ["Schnupfen", "Halsschmerzen", "Husten", "Kopfschmerzen", "Fieber", "Gliederschmerzen", "Müdigkeit/Abgeschlagenheit", "Druck auf der Brust/Atemnot", "Magen-Darm"];
  await expect(note.locator(".chip")).toHaveText(labels);
  await expect(note.getByRole("button")).toHaveText(["Bestätigen"]);
  await note.getByLabel("Schnupfen").check();
  await note.getByLabel("Fieber").check();
  await note.getByRole("button", { name: "Bestätigen" }).click();
  await expect(note.locator(".planned-move-status")).toContainText("Krankheit für heute gespeichert.");
  const saved = (await (await request.get("/api/feedback")).json());
  const checkin = (saved.checkins || []).find((item) => item.checkin_date === today);
  expect(checkin?.symptoms).toEqual(["schnupfen", "fieber"]);
  // The reload after the answer may still be in flight when the test ends.
  await page.unrouteAll({ behavior: "ignoreErrors" });
});
