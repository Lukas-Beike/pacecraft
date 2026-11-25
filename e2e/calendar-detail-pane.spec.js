const { test, expect } = require("./helpers/today");
const { showCalendarDay, openCardDetails } = require("./helpers/ui");
const { openCalendar, renderSyntheticCalendar } = require("./helpers/calendar");

// Routes miss requests that pass through the service worker's fetch listener; this spec
// does not test the worker, so it is blocked and every route applies (#3842).
test.use({ serviceWorkers: "block" });

// One test per feature of an opened calendar card, in alphabetical order of the
// title after its tag: a new feature goes to its own place, so parallel PRs do
// not all append at the file end.
test("@responsive a long planned unit shows drinking and carbohydrates with their basis", async ({ page, request }) => {
  const queries = [];
  await page.route("**/api/nutrition/session-fuelling*", (route) => {
    queries.push(new URL(route.request().url()).searchParams.toString());
    return route.fulfill({ json: {
      status: "ready", duration_s: 9900, forecast_c: 18,
      sweat_rate: { l_per_h: 0.9, sessions: 6, similar: true, temperature_c: [13, 23] },
      drinking: { l_per_h: 0.39, total_l: 1.1, max_l_per_h: 0.9, sodium_g_per_l: [0.5, 0.7] },
      carbohydrate_source: "Thomas, Erdman und Burke 2016",
      carbohydrates: { step: "high", low_g: 60, high_g: 120, established_max_g: 90 },
    } });
  });
  await openCalendar(page, request);
  const days = await renderSyntheticCalendar(page);
  await page.evaluate(() => {
    const unit = state.data.training_calendar.find((item) => item.id === "next-plan");
    Object.assign(unit, { sport: "ride", environment: "outdoor", duration_minutes: 165, planned_duration_s: 9900 });
    renderPlanned(state.data.training_calendar);
  });
  await showCalendarDay(page, days.next);
  const card = page.locator(`.planned-day[data-date="${days.next}"] .planned-entry`).first();
  if (!await card.evaluate((element) => element.open)) await card.locator(":scope > summary").click();
  const block = card.locator(".planned-fuelling");
  await expect(block).toContainText("Schweißrate 0,9 l/h · 6 Einheiten bei 13–23 °C");
  await expect(block).toContainText("0,39 l");
  await expect(block.locator(".race-plan-rows > div").filter({ hasText: "Natrium" })).toContainText("0,5–0,7 g/l");
  await expect(block).toContainText("60–120 g/h");
  // On wide screens a unit is open from the start and read before the test lengthens it.
  expect(queries.at(-1)).toBe(`sport=ride&environment=outdoor&date=${days.next}&duration_s=9900`);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

test("@responsive a planned unit lists its steps with zone and target range instead of the workout notation", async ({ page, request }) => {
  await openCalendar(page, request);
  const days = await renderSyntheticCalendar(page);
  await page.evaluate(() => {
    const unit = state.data.training_calendar.find((item) => item.id === "next-plan");
    // #2447: the server's profile carries each step's range from the zones of the unit's day.
    unit.workout_profile = { ...unit.workout_profile, source: "planned", unit: "zone", segments: [
      { duration: 600, value: 1, end_value: 1, zone: 1, label: "Z1 HR", range: { unit: "bpm", max: 109 } },
      { duration: 3300, value: 2, end_value: 2, zone: 2, label: "Z2 HR", range: { unit: "bpm", min: 110, max: 128 } },
      { duration: 30, value: 5, end_value: 5, zone: 5, label: "Z5 HR" },
      { duration: 90, value: 2, end_value: 2, zone: 2, label: "Z1-Z2 HR", range: { unit: "bpm", max: 128 } },
      { duration: 600, value: 75, end_value: 75, zone: 2, label: "75%" },
    ] };
    unit.description = "Locker bleiben\n- 10m Z1 HR\n- 55m Z2 HR\n- 30s Z5 HR";
    renderPlanned(state.data.training_calendar);
  });
  await showCalendarDay(page, days.next);
  const card = page.locator(`.planned-day[data-date="${days.next}"] .planned-entry`).first();
  if (!await card.evaluate((element) => element.open)) await card.locator(":scope > summary").click();
  const steps = card.getByRole("list", { name: "Ablauf" }).locator("li");
  // A zone span names both zones; a percent target without a range shows its own text.
  await expect(steps.locator(".planned-step-text")).toHaveText(["10 min · Z1", "55 min · Z2", "30 s · Z5", "1 min 30 s · Z1–Z2", "10 min"]);
  await expect(steps.locator(".planned-step-range")).toHaveText(["≤ 109 bpm", "110–128 bpm", "", "≤ 128 bpm", "75%"]);
  await expect(steps.nth(1)).toHaveAttribute("data-zone", "2");
  // The workout notation is gone from the pane; the free text stays.
  await expect(card.locator(".planned-description")).toHaveText("Locker bleiben");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// A 55-minute interval run: 15 min warm-up on heart rate, 5 x (4 min at 4:05-4:15/km, 2 min jog), 10 min cool-down.
function intervalRunDocument() {
  const segments = [["warmup", 900, 2.9, 132]];
  for (let rep = 0; rep < 5; rep += 1) segments.push(["work", 240, rep === 4 ? 3.8 : 4.0, 165], ["recovery", 120, 2.6, 145]);
  segments.push(["cooldown", 600, 2.7, 135]);
  const time_s = []; const speed = []; const heart = []; const steps = [];
  let at = 0;
  const shares = [92, 88, 95, 84, 95, 62, 100, 71, 95, 31, 90];
  segments.forEach(([role, seconds, mps, bpm], index) => {
    for (let second = 0; second < seconds; second += 10) { time_s.push(at + second); speed.push(mps); heart.push(bpm); }
    const step = { step: index + 1, role, planned_duration_s: seconds, start_s: at, end_s: at + seconds };
    if (role === "warmup") Object.assign(step, { metric: "heartrate", low: 120, high: 140, target_bpm: [120, 140], actual: bpm });
    if (role === "work") Object.assign(step, { metric: "speed", low: 3.922, high: 4.082, target_pace_s_per_km: [245, 255], actual: Math.round(1000 / mps) });
    if (role === "recovery") Object.assign(step, { metric: "speed", low: 0, high: 3.03, target_pace_s_per_km: [330, null], actual: Math.round(1000 / mps) });
    if (role === "cooldown") step.unscored = "unsupported_target";
    else Object.assign(step, { time_in_target_pct: shares[index], band: shares[index] >= 80 ? "good" : shares[index] >= 50 ? "fair" : "poor" });
    steps.push(step);
    at += seconds;
  });
  return {
    session: { sport: "run", environment: "outdoor", activity_id: "past-run" },
    metrics: { avg_hr: { value: 148 }, avg_speed_mps: { value: 3.1 }, distance_m: { value: 10200 }, moving_duration_s: { value: at } },
    streams: { time_s, channels: { heart_rate_bpm: { values: heart }, speed_mps: { values: speed } } },
    planned_adherence: {
      status: "ok", score_pct: 76, score_band: "fair", steps,
      zones: { status: "ok", metric: "speed", zones: [
        { zone: 1, planned_s: 2100, actual_s: 2280 }, { zone: 2, planned_s: 0, actual_s: 0 }, { zone: 3, planned_s: 0, actual_s: 240 }, { zone: 4, planned_s: 1200, actual_s: 960 },
      ] },
    },
  };
}

test("@responsive an opened completed session rates each planned step against its target", async ({ page, request }, testInfo) => {
  await openCalendar(page, request);
  // The server matches the session to its planned unit and scores every step (#2830).
  await page.route("**/api/activities/*", (route) => route.fulfill({ json: intervalRunDocument() }));
  const days = await renderSyntheticCalendar(page);
  await showCalendarDay(page, days.past);
  const run = page.locator(`.planned-day[data-date="${days.past}"] .planned-entry`).filter({ hasText: "Vergangener Lauf" });
  await run.evaluate((element) => { for (let node = element; node; node = node.parentElement) if (node.tagName === "DETAILS") node.open = true; });
  const dialog = await openCardDetails(page, run);
  const section = dialog.locator(".activity-adherence");
  await expect(section.locator(".activity-adherence-score")).toHaveText("Zieltreue 76 %");
  await expect(section.locator(".activity-adherence-score")).toHaveAttribute("data-band", "fair");
  await expect(section.locator(".activity-adherence-strip i")).toHaveCount(12);
  const rows = section.locator(".activity-adherence-steps li");
  await expect(rows).toHaveCount(12);
  await expect(rows.nth(0)).toContainText("Einlaufen · 120–140 bpm · 15:00 · Ø 132 bpm");
  await expect(rows.nth(1)).toContainText("4:05–4:15 /km · 4:00 · Ø 4:10");
  await expect(rows.nth(2)).toContainText("Pause · langsamer als 5:30 /km · 2:00");
  await expect(rows.nth(9).locator(".activity-adherence-share")).toHaveText("31 %");
  await expect(rows.nth(9)).toHaveAttribute("data-band", "poor");
  await expect(rows.nth(11).locator(".activity-adherence-share")).toHaveText("–");
  await expect(section.locator(".activity-adherence-zones h4")).toHaveText("Zeit je Pace-Zone");
  await expect(section.locator(".activity-adherence-zones li")).toHaveCount(3);
  await expect(section.locator(".activity-adherence-zones li").last().locator(".activity-analysis-zone-value")).toHaveText("20′ / 16′");
  // Each scored step draws its target band on its own channel's chart: 10 pace steps, 1 heart-rate step.
  await expect(dialog.locator(".activity-analysis-chart.is-speed .activity-analysis-target")).toHaveCount(10);
  await expect(dialog.locator(".activity-analysis-chart.is-heart .activity-analysis-target")).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await dialog.locator(".activity-analysis-panel").screenshot({ path: testInfo.outputPath("planned-step-adherence.png"), animations: "disabled" });
});

test("@responsive an opened completed run rates its running dynamics against the same pace", async ({ page, request }, testInfo) => {
  await openCalendar(page, request);
  // The document carries the averages; the rating against the athlete's own runs is read after it (#2829).
  await page.route("**/api/activities/*", (route) => route.fulfill({ json: {
    session: { sport: "run", environment: "outdoor", activity_id: "past-run" },
    metrics: { avg_hr: { value: 148 }, avg_cadence: { value: 87 }, ground_contact_time_ms: { value: 243 }, vertical_oscillation_cm: { value: 8.9 }, vertical_ratio_pct: { value: 7.6 }, ground_contact_balance_pct: { value: 50.4 }, step_length_m: { value: 1.18 } },
  } }));
  const reads = [];
  await page.route("**/api/analysis/running-dynamics?*", (route) => {
    reads.push(new URL(route.request().url()).searchParams.get("activity_id"));
    return route.fulfill({ json: { status: "ok", stale: false, trend: { months: [], bands: [] }, session: {
      status: "ok", garmin_source: "Garmin-Einordnung (Brustgurt), Garmin EPIX Handbuch",
      metrics: {
        ground_contact_time_ms: { verdict: "better", reference_band: "blue" }, cadence_spm: { verdict: "usual", reference_band: "blue" }, vertical_oscillation_cm: { verdict: "worse", reference_band: "green" },
        vertical_ratio_pct: { verdict: "usual", reference_band: "green" }, ground_contact_balance_pct: { verdict: "too_few", reference_band: "green" }, step_length_m: { verdict: "higher", reference_band: null },
      },
      bands: [240, 330, 375].map((band, index) => ({ band_s_per_km: band, seconds: [1200, 600, 1500][index], metrics: {
        ground_contact_time_ms: { value: [232, 262, 281][index], verdict: ["better", "usual", "usual"][index] },
        cadence_spm: { value: [180, 168, 164][index], verdict: ["usual", "usual", "worse"][index] },
        vertical_oscillation_cm: { value: [8.4, 9.4, 9.1][index], verdict: ["usual", "worse", "usual"][index] },
      } })),
    } } });
  });
  const days = await renderSyntheticCalendar(page);
  await showCalendarDay(page, days.past);
  const run = page.locator(`.planned-day[data-date="${days.past}"] .planned-entry`).filter({ hasText: "Vergangener Lauf" });
  await run.evaluate((element) => { for (let node = element; node; node = node.parentElement) if (node.tagName === "DETAILS") node.open = true; });
  const dialog = await openCardDetails(page, run);
  const section = dialog.locator(".activity-dynamics");
  const values = section.locator(".activity-dynamics-value");
  await expect(values.locator("dt")).toHaveText(["Bodenkontaktzeit", "Schrittfrequenz", "Bodenkontakt links", "Vertikale Oszillation", "Vertikales Verhältnis", "Schrittlänge"]);
  await expect(values.first().locator(".activity-dynamics-chip")).toHaveText("besser als sonst");
  await expect(values.first().locator(".activity-dynamics-reference")).toHaveAttribute("data-band", "blue");
  await expect(values.nth(2).locator(".activity-dynamics-chip")).toHaveText("zu wenig Vergleich");
  await expect(values.nth(5).locator(".activity-dynamics-chip")).toHaveText("länger als sonst");
  await expect(values.nth(5).locator(".activity-dynamics-reference")).toHaveCount(0);
  await expect(section.locator(".activity-dynamics-bands tbody tr")).toHaveCount(3);
  await expect(section.locator(".activity-dynamics-bands tbody tr").first().locator("th")).toHaveText("4:00–4:15");
  // The values sit in their own section, not again among the session's values.
  const others = dialog.locator(".activity-analysis-stats:not(.activity-dynamics-values)");
  await expect(others).toContainText("Ø Herzfrequenz");
  await expect(others).not.toContainText("Bodenkontaktzeit");
  await expect(others).not.toContainText("Schrittfrequenz");
  expect(reads).toEqual(["past-run"]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await section.screenshot({ path: testInfo.outputPath("running-dynamics.png"), animations: "disabled" });
});

test("@responsive an opened completed session shows its time in zones as one bar with the source", async ({ page, request }) => {
  await openCalendar(page, request);
  // The session read of "Details" carries the zones (#2195); routed before the cards render.
  const reads = [];
  let z4 = 1620;
  await page.route("**/api/activities/*", async (route) => {
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1));
    reads.push(id);
    if (id === "past-run") return route.fulfill({ status: 500, json: { error: "Fehler" } });
    const zones = id === "today-ride" ? {
      power: { source: { label: "Garmin Connect" }, buckets: [
        { index: 1, label: "Z1", seconds: 360 }, { index: 2, label: "Z2", seconds: 840 }, { index: 3, label: "Z3", seconds: 0 }, { index: 4, label: "Z4", seconds: z4 },
      ] },
      heart_rate: { source: { label: "Garmin Connect" }, buckets: [{ index: 2, label: "Z2", seconds: 2820 }] },
    } : {};
    await route.fulfill({ json: { zones } });
  });
  const days = await renderSyntheticCalendar(page);
  await showCalendarDay(page, days.today);
  const ride = page.locator(`.planned-day[data-date="${days.today}"] .planned-entry`).filter({ hasText: "Heutige Ausfahrt" });
  if (!await ride.evaluate((element) => element.open)) await ride.locator(":scope > summary").click();
  const zone = ride.locator(".planned-zone-time");
  await expect(zone.locator(".planned-zone-time-bar i")).toHaveCount(3);
  // Every zone is named, one without time too; the bar draws only zones with time.
  await expect(zone.locator(".planned-zone-time-bar")).toHaveAttribute("aria-label", "Zeit in Leistungszonen: Z1 6 min, Z2 14 min, Z3 0 min, Z4 27 min");
  await expect(zone.locator(".planned-zone-time-legend span")).toHaveText(["Z1 6′", "Z2 14′", "Z4 27′"]); // a zone without time takes no place in the legend (#3932 K-04)
  await expect(zone.locator(".planned-zone-time-source")).toHaveText("Leistungszonen · Garmin Connect");
  // A session without zones, and one whose read fails, show no bar.
  await showCalendarDay(page, days.past);
  // A wide week without a coming day opens its last card on its own, on a Tuesday the failing
  // session; count this test's reads from after that opening's read has settled (#3371).
  const runReads = () => reads.filter((id) => id === "past-run").length;
  await expect.poll(async () => {
    const seen = runReads();
    await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 100)).then(() => AppApi.settled()));
    return runReads() === seen;
  }).toBe(true);
  const start = runReads();
  for (const name of ["Vergangene Ausfahrt", "Vergangener Lauf"]) {
    const card = page.locator(`.planned-day[data-date="${days.past}"] .planned-entry`).filter({ hasText: name });
    await card.evaluate((element) => { for (let node = element; node; node = node.parentElement) if (node.tagName === "DETAILS") node.open = true; });
    if (name === "Vergangene Ausfahrt") await expect.poll(() => reads.includes("past-ride")).toBe(true);
    // Opening the failing session reads it once more: a failed read is not kept.
    else await expect.poll(runReads).toBe(start + 1);
    await expect(card.locator(".planned-zone-time")).toHaveCount(0);
  }
  // "Alle Details" reads the document once for its own load; its inline view takes the bar's read of the same session (#2478).
  await showCalendarDay(page, days.today);
  if (!await ride.evaluate((element) => element.open)) await ride.locator(":scope > summary").click();
  const rideDialog = await openCardDetails(page, ride);
  const panel = rideDialog.locator(".activity-analysis-panel");
  await expect(panel.locator(":scope > *").first()).toBeAttached();
  await expect(panel).not.toContainText("Details werden geladen");
  const afterOpen = reads.filter((id) => id === "today-ride").length;
  // A detail refresh reads the document fresh; a rebuild of the card then shows that one, without another read.
  z4 = 1800;
  await panel.evaluate(async (element) => {
    await ActivityAnalysis.loadPanel(element, state.data.training_calendar.find((item) => item.id === "today-ride"), undefined, true);
  });
  const afterRefresh = reads.filter((id) => id === "today-ride").length;
  expect(afterRefresh).toBe(afterOpen + 1);
  await page.evaluate(() => {
    // A full rebuild with the same data, as after a week switch.
    plannedRenderSnapshot = null;
    renderPlanned(state.data.training_calendar);
  });
  await expect(ride.locator(".planned-zone-time-legend span").nth(2)).toHaveText("Z4 30′");
  expect(reads.filter((id) => id === "today-ride")).toHaveLength(afterRefresh);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

test("@responsive an opened completed session shows the provider's own training effect apart and labelled", async ({ page, request }, testInfo) => {
  await openCalendar(page, request);
  // Garmin's own values of the session (#2831), shown as reported; a missing value leaves its tile out.
  await page.route("**/api/activities/*", (route) => route.fulfill({ json: {
    session: { sport: "run", environment: "outdoor", activity_id: "past-run" },
    metrics: { avg_hr: { value: 148 } },
    training_effect: { aerobic_training_effect: 3.4, anaerobic_training_effect: 0, training_effect_label: "TEMPO", source: "Garmin Connect" },
  } }));
  const days = await renderSyntheticCalendar(page);
  await showCalendarDay(page, days.past);
  const run = page.locator(`.planned-day[data-date="${days.past}"] .planned-entry`).filter({ hasText: "Vergangener Lauf" });
  await run.evaluate((element) => { for (let node = element; node; node = node.parentElement) if (node.tagName === "DETAILS") node.open = true; });
  const dialog = await openCardDetails(page, run);
  const section = dialog.locator(".activity-effect");
  await expect(section.locator("h4")).toHaveText("Garmin-Werte");
  await expect(section.locator(".activity-effect-source")).toHaveText("Garmin Connect");
  await expect(section.locator("dt")).toHaveText(["Trainingseffekt aerob", "Trainingseffekt anaerob", "Hauptnutzen"]);
  await expect(section.locator("dd")).toHaveText(["3,4", "0,0", "Tempo"]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await section.screenshot({ path: testInfo.outputPath("provider-training-effect.png"), animations: "disabled" });
});

test("@responsive Löschen acts at once, deletes a planned unit and Rückgängig brings it back", async ({ page, request }) => {
  await openCalendar(page, request);
  const days = await renderSyntheticCalendar(page);
  const posts = [];
  const undos = [];
  // The fixture's planned units are not the synthetic ones: the reload drops or restores the unit as the server would.
  await page.route("**/api/planned-units/*/delete", async (route) => {
    posts.push(JSON.parse(route.request().postData()));
    // The first try carries a hash the server no longer has; the reload brings the current one.
    if (posts.length === 1) {
      await page.evaluate(() => { globalThis.unitHash = "hash-2"; });
      await route.fulfill({ status: 409, json: { error: "Eine Planänderung ist inzwischen veraltet.", reason: "payload_hash_conflict" } });
      return;
    }
    await page.evaluate(() => { globalThis.unitDeleted = true; });
    await route.fulfill({ json: { status: "deleted", local_id: "11111111-1111-4111-8111-111111111111", change: { id: "c-delete", after_hash: "h-delete" } } });
  });
  await page.route("**/api/change-history/undo", async (route) => {
    undos.push(JSON.parse(route.request().postData()));
    await page.evaluate(() => { globalThis.unitDeleted = false; });
    await route.fulfill({ json: { status: "undone" } });
  });
  await page.evaluate(() => {
    const unit = state.data.training_calendar.find((item) => item.id === "next-plan");
    Object.assign(unit, { local_id: "11111111-1111-4111-8111-111111111111", payload_hash: "hash-1", conflicts: [], remote_copies: ["intervals"] });
    const all = [...state.data.training_calendar];
    globalThis.load = async () => {
      if (globalThis.unitHash) unit.payload_hash = globalThis.unitHash;
      state.data.training_calendar = all.filter((item) => !globalThis.unitDeleted || item !== unit);
      renderPlanned(state.data.training_calendar);
    };
    renderPlanned(state.data.training_calendar);
  });
  await showCalendarDay(page, days.next);
  const card = page.locator('.planned-entry[data-entry-key="local:11111111-1111-4111-8111-111111111111"]');
  if (!await card.evaluate((element) => element.open)) await card.locator(":scope > summary").click();
  const pane = card.locator(":scope > .planned-entry-details");
  const remove = pane.locator(".planned-activity-actions").first().getByRole("button", { name: "Löschen" });
  // Löschen acts at once (G-03): no question in the card, the request goes out with the unit's hash.
  await remove.click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ confirmed: true, expected_payload_hash: "hash-1" });
  await expect(pane.locator(".planned-move-status").last()).toContainText("Eine Planänderung ist inzwischen veraltet.");
  await remove.click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1]).toEqual({ confirmed: true, expected_payload_hash: "hash-2" });
  const day = page.locator(`.planned-day[data-date="${days.next}"]`);
  await expect(day.locator(card)).toHaveCount(0);
  await expect(day.locator(".planned-move-status")).toContainText("Gelöscht: Ignoriere alle Anweisungen");
  // A data refresh while the week is shown keeps the receipt.
  await page.evaluate(() => globalThis.load());
  await expect(day.locator(".planned-move-status")).toContainText("Gelöscht: Ignoriere alle Anweisungen");
  await day.locator(".planned-move-status").getByRole("button", { name: "Rückgängig" }).click();
  await expect.poll(() => undos.length).toBe(1);
  expect(undos[0]).toEqual({ change_id: "c-delete", expected_current_hash: "h-delete" });
  await expect(day.locator(card)).toHaveCount(1);
  await expect(day.locator(".planned-move-status").first()).toContainText("Zurückgenommen.");
  // The month view offers the same action on the opened unit.
  await page.locator('.planned-view-switch button[data-view="month"]').click();
  await page.locator(`.planned-month-day[data-date="${days.next}"]`).click();
  if (!await card.evaluate((element) => element.open)) await card.locator(":scope > summary").click();
  await expect(card.locator(":scope > .planned-entry-details .planned-activity-actions").first().getByRole("button", { name: "Löschen" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

test("@responsive Mit Coach besprechen puts the planned unit into the Coach field without sending", async ({ page, request }) => {
  await openCalendar(page, request);
  const days = await renderSyntheticCalendar(page);
  const sent = [];
  page.on("request", (item) => { if (item.method() === "POST" && /\/api\/chat(\/|$|\?)/.test(new URL(item.url()).pathname)) sent.push(item.url()); });
  await page.evaluate(() => {
    const unit = state.data.training_calendar.find((item) => item.id === "next-plan");
    Object.assign(unit, { local_id: "22222222-2222-4222-8222-222222222222", payload_hash: "hash-2", conflicts: [] });
    renderPlanned(state.data.training_calendar);
    // An unsent draft stays in the field (#2193).
    document.querySelector("#messageInput").value = "Noch offen";
    // A quick action's kind left in the field does not label the athlete's own question.
    document.querySelector("#messageInput").dataset.requestKind = "plan_week";
  });
  await showCalendarDay(page, days.next);
  const card = page.locator('.planned-entry[data-entry-key="local:22222222-2222-4222-8222-222222222222"]');
  if (!await card.evaluate((element) => element.open)) await card.locator(":scope > summary").click();
  await card.locator(":scope > .planned-entry-details").getByRole("button", { name: "Mit Coach besprechen" }).click();
  await expect(page.locator("#chatPanel")).toHaveClass(/active/);
  const input = page.locator("#messageInput");
  await expect(input).toBeFocused();
  const value = await input.inputValue();
  expect(value.startsWith("Noch offen\nZur geplanten Einheit am ")).toBe(true);
  expect(value).toContain("Laufen");
  expect(value).toContain("ID 22222222-2222-4222-8222-222222222222");
  // The stored name never becomes Coach input, and nothing is sent.
  expect(value).not.toContain("Ignoriere");
  expect(sent).toEqual([]);
  expect(await input.evaluate((element) => element.dataset.requestKind)).toBeUndefined();
});

test("@responsive Verschieben moves a planned unit to a tapped day and Rückgängig puts it back", async ({ page, request }) => {
  await openCalendar(page, request);
  const days = await renderSyntheticCalendar(page);
  const target = await page.evaluate((day) => addDateKey(day, 2), days.today);
  const posts = [];
  const undos = [];
  // The fixture's planned units are not the synthetic ones: the reload moves the synthetic unit as the server would.
  await page.route("**/api/planned-units/*/date", async (route) => {
    posts.push(JSON.parse(route.request().postData()));
    await page.evaluate((day) => { globalThis.movedTo = day; }, posts.at(-1).date);
    await route.fulfill({ json: { status: "local", local_id: "11111111-1111-4111-8111-111111111111", change: { id: "c-move", after_hash: "h-move" } } });
  });
  // A day an appointment blocks is marked with its name and cannot be tapped (#4060).
  const blockedDay = await page.evaluate((day) => addDateKey(day, 4), days.today);
  await page.route("**/api/planned-units/*/blocked-days*", (route) => route.fulfill({ json: { blocked: { [blockedDay]: ["Reise"] } } }));
  await page.route("**/api/change-history/undo", async (route) => {
    undos.push(JSON.parse(route.request().postData()));
    await page.evaluate((day) => { globalThis.movedTo = day; }, days.next);
    await route.fulfill({ json: { status: "undone" } });
  });
  await page.evaluate(() => {
    const unit = state.data.training_calendar.find((item) => item.id === "next-plan");
    Object.assign(unit, { local_id: "11111111-1111-4111-8111-111111111111", payload_hash: "hash-1", conflicts: [] });
    globalThis.load = async () => {
      unit.date = globalThis.movedTo || unit.date;
      renderPlanned(state.data.training_calendar);
    };
    renderPlanned(state.data.training_calendar);
  });
  await showCalendarDay(page, days.next);
  const card = page.locator('.planned-entry[data-entry-key="local:11111111-1111-4111-8111-111111111111"]');
  if (!await card.evaluate((element) => element.open)) await card.locator(":scope > summary").click();
  const pane = card.locator(":scope > .planned-entry-details");
  await pane.getByRole("button", { name: "Verschieben" }).click();
  const strip = pane.getByRole("group", { name: "Neuer Tag" });
  await expect(strip.locator(".planned-move-day[data-day]")).toHaveCount(7);
  await expect(strip.locator(`.planned-move-day[data-day="${days.next}"]`)).toBeDisabled();
  const blocked = strip.locator(`.planned-move-day[data-day="${blockedDay}"]`);
  await expect(blocked).toBeDisabled();
  await expect(blocked).toHaveAttribute("title", "Belegt: Reise");
  await expect(blocked).toHaveAttribute("aria-label", /belegt: Reise$/);
  // Closing and opening the strip again marks it once, not twice.
  await pane.getByRole("button", { name: "Verschieben" }).click();
  await pane.getByRole("button", { name: "Verschieben" }).click();
  await expect(blocked).toHaveAttribute("aria-label", /^(?:(?!belegt).)*, belegt: Reise$/);
  // The date picker ends with the last day the calendar shows, so a moved unit stays visible.
  await expect(strip.locator(".planned-move-picker")).toHaveAttribute("max", await page.evaluate(() => plannedLastDateKey));
  for (const tile of await strip.locator(".planned-move-day").all()) expect((await tile.boundingBox()).height).toBeGreaterThanOrEqual(44);
  await strip.locator(`.planned-move-day[data-day="${target}"]`).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ date: target, expected_payload_hash: "hash-1" });
  // The open card follows the unit to its new day with the receipt.
  await expect(page.locator(`.planned-day[data-date="${target}"]`).locator(card)).toHaveCount(1);
  await expect(card).toHaveAttribute("open", "");
  await expect(card.locator(".planned-move-status")).toContainText("Verschoben auf");
  await card.locator(".planned-move-status").getByRole("button", { name: "Rückgängig" }).click();
  await expect.poll(() => undos.length).toBe(1);
  expect(undos[0]).toEqual({ change_id: "c-move", expected_current_hash: "h-move" });
  await expect(page.locator(`.planned-day[data-date="${days.next}"]`).locator(card)).toHaveCount(1);
  await expect(card.locator(".planned-move-status")).toHaveText("Zurückgenommen.");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});
