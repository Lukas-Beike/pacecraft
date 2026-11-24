// --today pins this calendar's day (#3200): its week reads yesterday and today.
const { test, expect } = require("./helpers/today");
const { setEntryOpen, showCalendarDay } = require("./helpers/ui");
const { openCalendar, renderSyntheticCalendar, waitForOlderSessions } = require("./helpers/calendar");

// Routes miss requests that pass through the service worker's fetch listener; this spec
// does not test the worker, so it is blocked and every route applies (#3842).
test.use({ serviceWorkers: "block" });

test("@responsive compact calendar week shows one week, groups repeats and keeps one conflict style", async ({ page, request }, testInfo) => {
  await openCalendar(page, request);
  const days = await renderSyntheticCalendar(page);
  // The week view shows one week: a past day keeps its units as cards; a missed unit shows its execution.
  // Both outlines are read on closed cards: an open card carries the accent border (#2994).
  const completed = page.locator(".planned-day.is-today .planned-entry.is-completed").first();
  await setEntryOpen(completed, false);
  const completedBorder = await completed.evaluate((element) => getComputedStyle(element.parentElement).borderTopStyle);
  // Repeated extras share one row; the week summary is a fixed grid without dash-only load rows.
  await expect(page.locator(".planned-day.is-today .planned-group > summary .planned-session-title")).toHaveText("2× Trainingseinheit");
  for (const metrics of await page.locator(".planned-week-metrics").all()) {
    await expect(metrics).not.toContainText("Belastung absolviert");
    await expect(metrics).not.toContainText(/ohne \d+ Aktivit/);
  }
  await showCalendarDay(page, days.past);
  await expect(page.locator(`.planned-day[data-date="${days.past}"] .planned-entry`)).toHaveCount(2);
  await showCalendarDay(page, days.missed);
  await expect(page.locator(`.planned-day[data-date="${days.missed}"] .planned-entry-status`)).toHaveText("✕ Nicht absolviert");
  // A planned session is outlined apart from completed ones and needs no status word.
  await showCalendarDay(page, days.next);
  const planned = page.locator(`.planned-day[data-date="${days.next}"] .planned-entry`);
  await planned.scrollIntoViewIfNeeded();
  // #2985: on a Sunday this card is the default detail pane of the shown week.
  await setEntryOpen(planned, false);
  expect([await planned.evaluate((element) => getComputedStyle(element.parentElement).borderTopStyle), completedBorder]).toEqual(["dashed", "solid"]);
  await expect(planned.locator(".planned-entry-status")).toHaveCount(0);
  for (const row of await page.locator(".planned-entry > summary").all()) {
    if (!(await row.isVisible())) continue;
    // A row stays compact, its 16 px zone bar included; a conflict sits beside it, outside the summary.
    expect((await row.boundingBox()).height).toBeLessThan(130);
  }
  // Conflicts: one component with one action, no marker or conflict pills, calendar text not link-coloured.
  await expect(page.locator(".planned-badge.is-conflict, .planned-badge.is-marker")).toHaveCount(0);
  const conflict = planned.locator("xpath=..").locator(".planned-conflict");
  await expect(conflict).toContainText("Ein Termin an diesem Tag erlaubt kein Training");
  await expect(conflict.getByRole("button")).toHaveCount(1);
  await expect(conflict.getByRole("button")).toHaveText("Mit Coach besprechen");
  const result = await conflict.evaluate((element) => {
    let sent = null;
    const original = globalThis.askCoach;
    const wasOpen = element.closest(".planned-card").querySelector("details").open;
    globalThis.askCoach = (text) => { sent = text; };
    element.querySelector("button").click();
    globalThis.askCoach = original;
    return { sent, toggled: element.closest(".planned-card").querySelector("details").open !== wasOpen };
  });
  // The message names the session by date, sport and duration only; the stored name never becomes Coach input.
  expect(result.sent).toMatch(/^Die geplante Einheit am .+ \(Laufen, 40 min\) kollidiert mit: Kein Training\. Schlage eine Anpassung vor\.$/);
  expect(result.sent).not.toContain("Ignoriere");
  expect(result.toggled).toBe(false);
  const note = page.locator(`.planned-day[data-date="${days.next}"] .planned-day-calendar`);
  await expect(note).toHaveText("📅 Kalender: Arzt · 09:00 · Kein Training");
  const colours = await note.evaluate((element) => [getComputedStyle(element).color, getComputedStyle(document.querySelector(".planned-week-summary")).color]);
  expect(colours[0]).toBe(colours[1]);
  // Weeks with workout profiles show a legend of the zones they use.
  const profileWeeks = page.locator(".planned-week").filter({ has: page.locator(".calendar-workout-profile") });
  expect(await profileWeeks.count()).toBeGreaterThan(0);
  for (const profileWeek of await profileWeeks.all()) await expect(profileWeek.locator(".planned-zone-legend")).toHaveText("Z1Z4");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  // #2985: on a Sunday the next day is in the next week; step back to today's week.
  await showCalendarDay(page, days.today);
  await page.locator(".planned-day.is-today").screenshot({ path: testInfo.outputPath("calendar-week-compact.png") });
  // #2258: an opened session shows each value once, per sport; a value without data is left out.
  const yesterday = await page.evaluate(() => {
    const day = addDateKey(timezoneDateKey(state.data?.profile?.timezone, new Date()), -1);
    state.data.training_calendar = [
      { id: "act_run", date: day, name: "Lauf", sport: "run", is_completed_activity: true, started_at_local: `${day}T07:00:00`, moving_duration_s: 3600, distance_m: 12000, training_load: 80, avg_hr: 150, avg_power: 270, avg_cadence: 86, ground_contact_time_ms: 248.6, elevation_gain_m: 40 },
      { id: "act_ride", date: day, name: "Rad", sport: "ride", environment: "indoor", is_completed_activity: true, started_at_local: `${day}T18:00:00`, moving_duration_s: 3600, distance_m: 30000, training_load: 70, rpe: 6, avg_hr: 140, avg_power: 210, normalized_power: 225, intensity: 0.86, avg_cadence: 90, elevation_gain_m: 0 },
    ];
    renderPlanned(state.data.training_calendar);
    return day;
  });
  // #2994: on a Monday yesterday is in the previous week.
  await showCalendarDay(page, yesterday);
  const facts = await page.evaluate(() => [...document.querySelectorAll(".planned-actual-facts")].map((node) => [...node.children].map((item) => item.textContent)));
  expect(facts).toEqual([
    ["Pace 5:00 /km", "Ø Herzfrequenz 150 bpm", "Schrittfrequenz 172 spm", "Ø Leistung 270 W", "Belastung 80", "Bodenkontaktzeit 249 ms", "Höhenmeter 40 m"],
    ["Normalized Power 225 W", "Ø Leistung 210 W", "Intensitätsfaktor 0,86", "Ø Herzfrequenz 140 bpm", "Trittfrequenz 90 rpm", "Belastung 70", "RPE 6/10", "Ø Geschwindigkeit 30,0 km/h", "Höhenmeter 0 m"],
  ]);
});

test("@responsive opening a single calendar entry and a grouped row shows their details", async ({ page, request }) => {
  await openCalendar(page, request);
  const entry = page.locator(".planned-day.is-today .planned-entry, .planned-day.is-today .planned-group").first();
  await entry.scrollIntoViewIfNeeded();
  // On wide screens a unit of the week is already open as the detail pane.
  await setEntryOpen(entry, false);
  await entry.locator(":scope > summary").click();
  await expect(entry).toHaveAttribute("open", "");
  // The detail pane starts level with the week card, not with its own card (#3156).
  if (page.viewportSize().width >= 1100 && await entry.evaluate((element) => element.classList.contains("planned-entry"))) {
    await page.evaluate(() => window.scrollTo(0, 0));
    const pane = entry.locator(":scope > .planned-entry-details");
    await expect.poll(async () => Math.round((await pane.boundingBox()).y - (await page.locator(".planned-week").boundingBox()).y)).toBe(0);
  }
  const group = page.locator(".planned-group").first();
  if (await group.count()) {
    await expect(group.locator(":scope > summary .planned-session-title")).toHaveText(/^\d+× /);
    await group.evaluate((element) => {
      for (let node = element; node; node = node.parentElement) if (node.tagName === "DETAILS") node.open = true;
    });
    const items = group.locator(".planned-group-items > .planned-card > .planned-entry");
    expect(await items.count()).toBeGreaterThan(1);
    await items.first().locator(":scope > summary").click();
    await expect(items.first().locator(".planned-entry-details")).toBeVisible();
  }
});

test("the desktop detail pane starts at the top of the week for its top and bottom unit and stays in view", async ({ page, request }, testInfo) => {
  // #3156 (athlete, option A): a low unit no longer pulls the pane down below an empty area.
  test.skip(testInfo.project.name !== "desktop", "the detail pane is the wide layout only");
  await page.setViewportSize({ width: 1280, height: 900 });
  await openCalendar(page, request);
  await page.evaluate(() => {
    const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
    const monday = new Date(`${today}T12:00:00`);
    monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
    const day = (offset) => {
      const date = new Date(monday);
      date.setDate(monday.getDate() + offset);
      return date.toISOString().slice(0, 10);
    };
    state.data.training_calendar = [
      ["u-top", 0, "Synthetic top unit", "Run"], ["u-2", 1, "Synthetic strength", "WeightTraining"],
      ["u-3", 2, "Synthetic ride", "Ride"], ["u-4", 3, "Synthetic intervals", "Run"],
      ["u-5", 5, "Synthetic long ride", "Ride"], ["u-bottom", 6, "Synthetic bottom unit", "Run"],
    ].map(([id, offset, name, type]) => ({ id, local_id: id, date: day(offset), name, type, duration_minutes: 60 }));
    renderPlanned(state.data.training_calendar);
  });
  const week = page.locator(".planned-week");
  for (const name of ["Synthetic top unit", "Synthetic bottom unit"]) {
    const card = page.locator(".planned-entry").filter({ hasText: name }).first();
    await page.evaluate(() => {
      document.querySelectorAll(".planned-entry[open]").forEach((element) => { element.open = false; });
      window.scrollTo(0, 0);
    });
    await setEntryOpen(card, true);
    const pane = card.locator(":scope > .planned-entry-details");
    await expect(pane).toBeVisible();
    // Opening by click may scroll the card into view; at the page's top the pane starts with the week.
    await page.evaluate(() => window.scrollTo(0, 0));
    // The pane sits beside the week, level with its top; the chosen unit stays highlighted on the left.
    await expect.poll(async () => Math.round((await pane.boundingBox()).y - (await week.boundingBox()).y)).toBe(0);
    expect((await pane.boundingBox()).x).toBeGreaterThan((await week.boundingBox()).x + (await week.boundingBox()).width);
    expect(await card.evaluate((element) => getComputedStyle(element.parentElement).borderTopStyle)).toBe("solid");
  }
  // Scrolled down, the pane stays in view near the window's top, but not below the week's end.
  await page.setViewportSize({ width: 1280, height: 600 });
  await page.evaluate(() => window.scrollTo(0, document.querySelector(".planned-week").getBoundingClientRect().top + window.scrollY + 200));
  const pane = page.locator(".planned-entry[open] > .planned-entry-details");
  await expect.poll(async () => Math.round((await pane.boundingBox()).y)).toBe(24);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const [paneBox, weekBox] = [await pane.boundingBox(), await week.boundingBox()];
  expect(paneBox.y + paneBox.height).toBeLessThanOrEqual(weekBox.y + weekBox.height + 1);
});

test("extra activities show no Ungeplant badge in a week without a plan", async ({ page, request }) => {
  await openCalendar(page, request);
  await page.evaluate(() => {
    const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
    const entries = [
      { id: "extra-a", date: today, name: "Freie Runde", type: "Run", is_completed_activity: true, moving_duration_s: 1800, distance_m: 5000 },
      { id: "extra-b", date: today, name: "Trainingseinheit", type: "Other", is_completed_activity: true },
      { id: "extra-c", date: today, name: "Trainingseinheit", type: "Other", is_completed_activity: true },
    ];
    state.data.training_calendar = entries;
    renderPlanned(entries);
  });
  const day = page.locator(".planned-day.is-today");
  await expect(day).not.toContainText("Ungeplant");
  await expect(day.locator(".planned-group > summary .planned-session-title")).toHaveText("2× Trainingseinheit");
  // A week without a plan counts no session as extra.
  await expect(page.locator(".week-review")).not.toContainText("zusätzlich");
});

test("@responsive a unit missed on a confirmed rest day is dropped, missing weather is one hint and long titles hyphenate", async ({ page, request }) => {
  await openCalendar(page, request);
  const missed = await page.evaluate(() => {
    const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
    const day = addDateKey(today, -1);
    state.data.weather = null;
    state.data.daily_planning_context = [{ date: day, day_status: "rest" }];
    state.data.training_calendar = [
      { id: "rest-missed", date: day, name: "Radausfahrt mit einem sehr langen Bezeichnungswort", type: "Ride", duration_minutes: 45, compliance: { status: "missed", percentage: 0 } },
    ];
    renderPlanned(state.data.training_calendar);
    return day;
  });
  await expect(page.locator(".planned-weather-hint")).toHaveCount(1);
  await showCalendarDay(page, missed);
  const entry = page.locator(`.planned-day[data-date="${missed}"] .planned-entry`);
  await expect(entry.locator(".planned-entry-status")).toHaveText("Entfallen (Ruhetag)");
  const title = entry.locator(".planned-session-title").first();
  await expect(title).toHaveCSS("overflow-wrap", "break-word");
  await expect(title).toHaveCSS("hyphens", "auto");
  // Its link opens the location field in the profile (#3932 K-03).
  await page.locator(".planned-weather-hint").getByRole("link", { name: "Wetterort angeben" }).click();
  await expect(page).toHaveURL(/#more\/profile$/);
  await expect(page.locator('#profileForm [name="weather_location"]')).toBeFocused();
  await page.goBack();
  await page.evaluate(() => { state.data.library = []; renderLibrary([]); });
  await expect(page.locator("#library .context-empty")).toHaveText("Noch keine Einheiten in der Bibliothek. Der Coach legt hier Einheiten ab, wenn du sie planst.");
});

test("@responsive the Monat switch shows the month grid and the calendar returns to the week", async ({ page, request }) => {
  await openCalendar(page, request);
  const days = await renderSyntheticCalendar(page);
  await expect(page.locator(".planned-view-switch [data-view='week']")).toHaveAttribute("aria-pressed", "true");
  await page.locator(".planned-view-switch [data-view='month']").click();
  await waitForOlderSessions(page);
  const month = page.locator(".planned-month");
  await expect(month).toBeVisible();
  await expect(page.locator(".planned-view-switch")).toHaveCount(1);
  await expect(month.locator(".planned-view-switch [data-view='month']")).toHaveAttribute("aria-pressed", "true");
  // Whole weeks Monday to Sunday: every day of the month, the neighbouring ones muted.
  const cells = month.locator(".planned-month-day");
  expect((await cells.count()) % 7).toBe(0);
  const monthDays = await page.evaluate((today) => new Date(Number(today.slice(0, 4)), Number(today.slice(5, 7)), 0).getDate(), days.today);
  await expect(month.locator(".planned-month-day:not(.is-outside)")).toHaveCount(monthDays);
  const today = month.locator(`.planned-month-day[data-date="${days.today}"]`);
  await expect(today).toHaveClass(/is-today/);
  await expect(today).toHaveAttribute("aria-pressed", "true");
  await expect(today.locator(".planned-month-mark")).toHaveCount(3);
  await expect(month.locator(".planned-month-row.is-current")).toHaveCount(1);
  // The missed unit is marked red, a planned one outlined.
  const missed = month.locator(`.planned-month-day[data-date="${days.missed}"] .planned-month-mark`);
  if (await missed.count()) await expect(missed).toHaveClass(/is-missed/);
  const next = month.locator(`.planned-month-day[data-date="${days.next}"]`);
  if (await next.count()) {
    await expect(next.locator(".planned-month-mark")).toHaveClass(/is-planned/);
    await next.click();
    await waitForOlderSessions(page);
    await expect(page.locator(`.planned-day[data-date="${days.next}"]`)).toHaveCount(1);
    await expect(month.locator(`.planned-month-day[data-date="${days.next}"]`)).toHaveAttribute("aria-pressed", "true");
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  // One measurement of the settled grid; no cells counts as 0.
  expect(await cells.evaluateAll((items) => Math.min(...items.map((item) => item.getBoundingClientRect().height), items.length ? Infinity : 0))).toBeGreaterThanOrEqual(44);
  await page.locator(".planned-view-switch [data-view='week']").click();
  await expect(page.locator(".planned-month")).toHaveCount(0);
  // Entering the calendar again always opens the week view.
  await page.locator(".planned-view-switch [data-view='month']").click();
  await page.evaluate(async () => {
    await AppRouter.navigate("coach", { historyMode: "replace" });
    await AppRouter.navigate("plan/overview", { historyMode: "push" });
  });
  await expect(page.locator(".planned-month")).toHaveCount(0);
  await expect(page.locator(".planned-day.is-today")).toHaveCount(1);
});

test("@responsive the sport filter hides other sports and their figures, keeps appointments and the week sync, and is remembered", async ({ page, request }, testInfo) => {
  await openCalendar(page, request);
  const days = await renderSyntheticCalendar(page);
  const filter = page.locator(".planned-week .planned-sport-filter");
  await expect(filter.getByRole("radio")).toHaveCount(5);
  // The check mark of a chosen chip is not part of its name (#2859).
  await expect(filter.getByRole("radio", { name: "Alle", exact: true })).toBeChecked();
  // The week sync is built from every unit of the week, whatever the filter shows (#2810).
  await page.evaluate(() => {
    const original = plannedWeekSyncButton;
    globalThis.syncedWeekUnits = [];
    globalThis.plannedWeekSyncButton = (weekKey, entries, ...rest) => { syncedWeekUnits.push(entries.length); return original(weekKey, entries, ...rest); };
  });
  await filter.getByRole("radio", { name: "Lauf" }).check();
  await expect(page.locator(".planned-week .planned-sport-filter").getByRole("radio", { name: "Lauf" })).toBeChecked();
  await expect(page.locator(".planned-day.is-today .planned-entry")).toHaveCount(0);
  await expect(page.locator(".planned-week-totals")).toHaveCount(0);
  const synced = await page.evaluate(() => syncedWeekUnits.at(-1));
  await showCalendarDay(page, days.next);
  const next = page.locator(`.planned-day[data-date="${days.next}"]`);
  await expect(next.locator(".planned-entry")).toHaveCount(1);
  await expect(next.locator(".planned-day-calendar")).toHaveText("📅 Kalender: Arzt · 09:00 · Kein Training");
  await showCalendarDay(page, days.past);
  await expect(page.locator(`.planned-day[data-date="${days.past}"] .planned-entry`)).toHaveCount(1);
  await expect(page.locator(`.planned-day[data-date="${days.past}"] .planned-entry`)).toContainText("Vergangener Lauf");
  expect(synced).toBe(await page.evaluate(() => {
    const start = planWeekStart(timezoneDateKey(state.data?.profile?.timezone, new Date()));
    return state.data.training_calendar.filter((item) => plannedEventDate(item) >= start && plannedEventDate(item) <= addDateKey(start, 6)).length;
  }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  // The month view filters its marks with the same chips.
  await page.locator(".planned-view-switch [data-view='month']").click();
  await waitForOlderSessions(page);
  const month = page.locator(".planned-month");
  await expect(month.locator(".planned-sport-filter").getByRole("radio", { name: "Lauf" })).toBeChecked();
  await expect(month.locator(`.planned-month-day[data-date="${days.today}"] .planned-month-mark`)).toHaveCount(0);
  await month.locator(".planned-sport-filter").getByRole("radio", { name: "Rad" }).check();
  await expect(page.locator(`.planned-month-day[data-date="${days.today}"] .planned-month-mark`)).toHaveCount(1);
  // The choice stays on this device after a reload.
  await page.reload();
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.evaluate(async () => { await AppRouter.navigate("plan/overview", { historyMode: "replace" }); });
  await expect(page.locator(".planned-week .planned-sport-filter").getByRole("radio", { name: "Rad" })).toBeChecked();
});

// The week review sits under the selected past or running week, never a future one, and follows the
// week stepper (#3298, layout B). A late answer for a week stepped away from is dropped.
const weekAfter = (key, days) => new Date(Date.parse(`${key}T00:00:00Z`) + days * 86400000).toISOString().slice(0, 10);
function weekReviewFor(week, today) {
  const end = weekAfter(week, 6);
  const running = end >= today;
  return {
    method: "local-week-review-v1", week_start: week, week_end: end, effective_end: running ? today : end,
    plan: running ? { planned: 5, completed: 1, missed: 0, open: 4, missed_units: [] } : { planned: 6, completed: 5, missed: 1, open: 0, missed_units: [{ date: weekAfter(week, 2), name: "Kraft" }] },
    load: { this_week: running ? { duration_s: 2700, sessions: 1, training_load: 38 } : { duration_s: 27600, sessions: 6, training_load: 412 }, week_before: { duration_s: 24600, sessions: 5, training_load: 381 }, shares: null },
    recovery: running ? {} : { sleep: { mean: 7 + 5 / 60, nights: 7, unit: "h", source: "Garmin", week_before: 7.25 }, hrv: { mean: 62, nights: 7, unit: "ms", source: "Garmin", position: "within" } },
    notable: running ? [] : [{ date: end, name: "Langer Lauf", reason: "highest_load", training_load: 120 }],
    checkins: running ? { entered: 0, days: 2, scores: {} } : { entered: 6, days: 7, pain_days: 2, illness_days: 0, scores: { motivation: 7 }, complaints: [
      { area: "achilles", side: "right", days: 3, dates: [weekAfter(week, 0), weekAfter(week, 2), weekAfter(week, 4)], first_severity: 3, last_severity: 5 },
      { area: "knee", side: "left", days: 1, dates: [weekAfter(week, 0)], first_severity: 2, last_severity: 2 },
    ] },
  };
}

test("@responsive the week review sits under a past or running week and follows the week stepper", async ({ page, request }, testInfo) => {
  const weeks = [];
  let release = () => {};
  let hold = null;
  await page.route("**/api/analysis/week-review?*", async (route) => {
    const asked = new URL(route.request().url()).searchParams.get("week");
    weeks.push(asked);
    const [today, monday] = await page.evaluate(() => {
      const day = timezoneDateKey(state.data?.profile?.timezone, new Date());
      return [day, planWeekStart(day)];
    });
    // The running week asks for "current", any other week for its Monday.
    const week = asked === "current" ? monday : asked;
    if (hold === week) await new Promise((resolve) => { release = resolve; });
    await route.fulfill({ json: weekReviewFor(week, today) });
  });
  await openCalendar(page, request);
  const current = await page.evaluate(() => planWeekStart(timezoneDateKey(state.data?.profile?.timezone, new Date())));
  const review = page.locator(".planned-week .week-review");
  const labels = review.locator(".week-review-tile > small:first-child");
  // The running week: "bis heute", no change against the week before, and no plan line under the week.
  await expect(review.locator(".week-review-head")).toHaveText(/^WochenrückblickKW \d+ · bis heute$/);
  await expect(labels).toHaveText(["Plan", "Einheiten", "Zeit", "Belastung"]);
  await expect(review.locator(".week-review-tile").first()).toHaveText("Plan1 von 54 noch offen");
  await expect(review.locator(".week-review-change")).toHaveCount(0);
  await expect(page.locator(".planned-week-totals")).toHaveCount(0);

  // The previous week answers only after a step back to the running week: its tiles never show.
  hold = weekAfter(current, -7);
  await page.getByRole("button", { name: "Vorherige Woche" }).click();
  await expect.poll(() => weeks.includes(hold)).toBe(true);
  await page.getByRole("button", { name: "Nächste Woche" }).click();
  await expect(review.locator(".week-review-head")).toHaveText(/bis heute$/);
  const late = page.waitForResponse((response) => response.url().includes(`week=${hold}`));
  release();
  await late;
  // One task later the dropped answer would have been drawn; nothing in the DOM marks it.
  await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 0)));
  await expect(review.locator(".week-review-tile").first()).toHaveText("Plan1 von 54 noch offen");

  // The past week: every tile with data, the changes, sources and positions.
  hold = null;
  await page.getByRole("button", { name: "Vorherige Woche" }).click();
  await expect(review.locator(".week-review-head span")).toHaveText(/^KW \d+$/);
  await expect(labels).toHaveText(["Plan", "Einheiten", "Zeit", "Belastung", "Schlaf Ø", "HRV Ø", "Check-ins", "Besondere Einheiten", "Beschwerden"]);
  await expect(review.locator(".week-review-tile").first()).toHaveText(/^Plan5 von 6verpasst: Mi Kraft$/);
  await expect(review.locator(".week-review-change")).toHaveText(["+12 % ggü. Vorwoche", "+8 % ggü. Vorwoche", "−0:10 ggü. Vorwoche", "3 → 5"]);
  await expect(review.getByText("im Normalbereich · Garmin")).toBeVisible();
  await expect(review.locator(".week-review-wide").first().locator("li")).toHaveText(/^So Langer Lauf · höchste Belastung \(120\)$/);
  // #3766 (athlete: layout A): one line per body area and side, a worsening in the danger colour.
  const complaints = review.locator(".week-review-wide").last();
  await expect(complaints.locator("li")).toHaveText(["Achillessehne rechts · 3 Tage · Stärke 3 → 5", "Knie links · 1 Tag · Stärke 2/10"]);
  await expect(complaints.locator('.week-review-change[data-direction="down"]')).toHaveText("3 → 5");
  await expect(review).not.toContainText("Schätzung");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await review.screenshot({ path: testInfo.outputPath("week-review.png"), animations: "disabled" });

  // A future week has no review.
  await page.getByRole("button", { name: "Nächste Woche" }).click();
  await page.getByRole("button", { name: "Nächste Woche" }).click();
  await expect(page.locator(".planned-week-number")).toHaveText(/^KW \d+$/);
  await expect(review).toHaveCount(0);
  expect(weeks).toContain("current");
  expect(weeks).not.toContain(weekAfter(current, 7));
});

// A review that cannot be read (offline, a server error) leaves the plan line in its place, and a
// sport filter hides both, as they count the whole week (#3298).
// #3391: a failed read never removes the card; it names the gap and retries on tap.
test("@responsive a failed week review keeps the card with its reason and a retry, and a sport filter hides it", async ({ page, request }) => {
  let fail = true;
  await page.route("**/api/analysis/week-review?*", async (route) => {
    if (fail) return route.fulfill({ status: 500, json: { error: "Serverfehler" } });
    const [today, monday] = await page.evaluate(() => {
      const day = timezoneDateKey(state.data?.profile?.timezone, new Date());
      return [day, planWeekStart(day)];
    });
    return route.fulfill({ json: weekReviewFor(monday, today) });
  });
  await openCalendar(page, request);
  const review = page.locator(".planned-week .week-review");
  await expect(review).toContainText("Wochenrückblick gerade nicht verfügbar");
  await expect(page.locator(".planned-week .error-state")).toHaveCount(0);
  await page.locator(".planned-week .planned-sport-filter").getByRole("radio", { name: "Lauf" }).check();
  await expect(page.locator(".planned-week .planned-week-totals, .planned-week .week-review")).toHaveCount(0);
  await page.locator(".planned-week .planned-sport-filter").getByRole("radio", { name: "Alle", exact: true }).check();
  await expect(review).toContainText("Wochenrückblick gerade nicht verfügbar");
  // A keyboard retry that fails again leaves the focus on the new retry button.
  await review.getByRole("button", { name: "Erneut laden" }).focus();
  await page.keyboard.press("Enter");
  await expect(review.getByRole("button", { name: "Erneut laden" })).toBeFocused();
  fail = false;
  await review.getByRole("button", { name: "Erneut laden" }).click();
  await expect(review).toBeFocused();
  await expect(review.locator(".week-review-tile").first()).toHaveText("Plan1 von 54 noch offen");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3391: a slow read (the server under load after a deploy) keeps the card in its loading state.
test("@responsive a slow week review keeps the card loading until it arrives", async ({ page, request }) => {
  let release = () => {};
  const held = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/analysis/week-review?*", async (route) => {
    const [today, monday] = await page.evaluate(() => {
      const day = timezoneDateKey(state.data?.profile?.timezone, new Date());
      return [day, planWeekStart(day)];
    });
    await held;
    await route.fulfill({ json: weekReviewFor(monday, today) });
  });
  await openCalendar(page, request);
  const review = page.locator(".planned-week .week-review");
  await expect(review).toContainText("Wochenrückblick wird geladen");
  await page.waitForTimeout(1_500);
  await expect(review).toContainText("Wochenrückblick wird geladen");
  release();
  await expect(review.locator(".week-review-tile").first()).toHaveText("Plan1 von 54 noch offen");
});

test("@responsive an old link to the Analyse week opens the calendar", async ({ page, request }) => {
  await openCalendar(page, request);
  await page.goto("/#analysis/week");
  await expect(page).toHaveURL(/#plan\/overview$/);
  await expect(page.locator("#workoutsPanel")).toHaveClass(/active/);
  await expect(page.locator("[data-analysis-segment='week']")).toHaveCount(0);
});

// #4129 K-12: a planned run counted in distance shows the sum of its steps in the header.
test("@responsive a planned run counted in distance shows its total distance", async ({ page, request }) => {
  await openCalendar(page, request);
  await page.evaluate(() => {
    const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
    const day = addDateKey(today, 1);
    const profile = { source: "planned", axis: "distance", unit: "zone", distance_m: 8000, segments: [
      { distance: 2000, value: 1, end_value: 1, zone: 1, label: "Z1" }, { distance: 5000, value: 2, end_value: 2, zone: 2, label: "Z2" }, { distance: 1000, value: 4, end_value: 4, zone: 4, label: "Z4" }] };
    state.data.training_calendar = [{ id: "dist-run", local_id: "dist-run", date: day, name: "Streckenlauf", type: "Run", sport: "run", duration_minutes: 45, workout_profile: profile }];
    renderPlanned(state.data.training_calendar);
  });
  const day = await page.evaluate(() => addDateKey(timezoneDateKey(state.data?.profile?.timezone, new Date()), 1));
  await showCalendarDay(page, day);
  await expect(page.locator(`.planned-day[data-date="${day}"] .planned-session-value`)).toContainText("8,0 km");
});
