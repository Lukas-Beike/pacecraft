const assert = require("node:assert/strict");
const { test } = require("node:test");
const { loadPublicScript, fakeDocument, textOf } = require("./frontend-harness.cjs");

const context = loadPublicScript("plan-views.js", { AppFormat: require("../public/format.js") });

test("competition distances retain unknown, zero and free-form values", () => {
  for (const value of [null, undefined, "", "  ", -1]) assert.equal(context.competitionDistance(value), "Distanz unbekannt");
  assert.equal(context.competitionDistance(0), "0 km");
  assert.equal(context.competitionDistance(42195), "42,2 km");
  assert.equal(context.competitionDistance("Sprintdistanz"), "Sprintdistanz");
});

test("the competition dialog reads durations and kilometres the way the service stores them", () => {
  for (const [text, seconds] of [["1:30:00", 5400], ["25:30", 1530], ["0:45:10", 2710], ["", null], ["  ", null]]) assert.equal(context.competitionDurationSeconds(text), seconds);
  for (const text of ["90", "1:75", "a:10", "1:2:3:4", "1:-5"]) assert.equal(context.competitionDurationSeconds(text), undefined);
  assert.equal(context.competitionDistanceInput(42195), "42,195");
  assert.equal(context.competitionDistanceInput("10000"), "10");
  assert.equal(context.competitionDistanceInput("Sprintdistanz"), "Sprintdistanz");
  assert.equal(context.competitionDistanceInput(null), "");
  assert.equal(context.competitionDistanceValue("42,2"), "42,2 km");
  assert.equal(context.competitionDistanceValue("10 km"), "10 km");
  assert.equal(context.competitionDistanceValue("Sprintdistanz"), "Sprintdistanz");
  assert.equal(context.competitionSportChoice({ sport: "ride", environment: "virtual" }), "VirtualRide");
  assert.equal(context.competitionSportChoice({ sport: "ride", environment: "outdoor" }), "Ride");
  assert.equal(context.competitionSportChoice({ sport: "other", sport_detail: "Swimrun" }), null);
});

test("invalid event dates stay undated rather than rolling into another month", () => {
  for (const event_date of [null, "", "2026-02-30", "not-a-date"]) assert.equal(context.competitionDate({ event_date }, "2026-10-24"), false);
  assert.equal(context.competitionDate({ event_date: "2026-10-24" }, "2026-10-24"), true);
});

test("race results show time, distance, pace or power and their source", () => {
  assert.equal(context.competitionResult(null), null);
  assert.equal(
    context.competitionResult({ sport: "run", duration_s: 2400, distance_m: 10000, avg_speed_mps: 10000 / 2400, source: "Garmin Connect" }),
    "40:00 · 10,0 km · 4:00 /km (Garmin Connect)",
  );
  assert.equal(
    context.competitionResult({ sport: "ride", duration_s: 3725, distance_m: 40000, avg_power: 251.4, avg_speed_mps: 10.7 }),
    "1:02:05 · 40,0 km · 251 W",
  );
  assert.equal(
    context.competitionResult({ sport: "ride", duration_s: 3600, distance_m: 36000, avg_speed_mps: 10 }),
    "1:00:00 · 36,0 km · 36,0 km/h",
  );
  assert.equal(
    context.competitionResult({ sport: "swim", duration_s: 1500, distance_m: 1500, avg_speed_mps: 1 }),
    "25:00 · 1,5 km · 1:40 /100 m",
  );
  assert.equal(context.competitionResult({ sport: "run", source: "Garmin Connect" }), null);
});

const rendered = loadPublicScript("plan-views.js", { AppFormat: require("../public/format.js"), document: fakeDocument(), URLSearchParams });
const race = { event_date: "2026-11-28", starts_at_local: "2026-11-28T09:00:00" };

test("race plan pacing shows sections with pace or power and its rule", () => {
  const run = rendered.racePlanPacing({ status: "ready", method: "equal_effort", profile_rejected: false, sections: [
    { from_km: 8, to_km: 11, grade_pct: 4, pace_s_per_km: 364, time_s: 1092 },
    { from_km: 15, to_km: 21.1, grade_pct: 0, pace_s_per_km: 294, time_s: 1796 },
  ] }).map(textOf).join("|");
  assert.match(run, /gleiche Anstrengung/);
  assert.match(run, /km 8–11\+4 %6:04 \/km18:12/);
  assert.match(run, /km 15–21,10 %4:54 \/km29:56/);
  const ride = rendered.racePlanPacing({ status: "ready", method: "power_variation", profile_rejected: true, sections: [
    { from_km: 0, to_km: 40, grade_pct: -2, power_pct: -5, power_w: null },
  ] }).map(textOf).join("|");
  assert.match(ride, /± 5 % Leistung · Streckenprofil nicht erkannt/);
  assert.match(ride, /km 0–40−2 %−5 %/);
  assert.match(rendered.racePlanPacing({ status: "unavailable", reason: "duration" }).map(textOf).join(""), /Zielzeit oder erwartete Dauer fehlt/);
  assert.equal(rendered.racePlanPacing({ status: "unavailable", reason: "sport" }).length, 0);
});

test("race plan carbohydrates name the days, the meal window and the rate", () => {
  const text = rendered.racePlanCarbohydrates({
    source: "Thomas, Erdman und Burke 2016",
    weight: { kg: 70, source: "Garmin" },
    during: { step: "high", low_g: 60, high_g: 120, established_max_g: 90 },
    before: [
      { kind: "loading", days_before: 2, g_per_kg: [10, 12], grams: [700, 840] },
      { kind: "meal", hours_before: [4, 1], g_per_kg: [1, 4], grams: null },
    ],
  }, race).map(textOf).join("|");
  assert.match(text, /Thomas, Erdman und Burke 2016 · Gewicht Garmin/);
  assert.match(text, /Do\.? \+ Fr\.?700–840 g\/Tag/);
  assert.match(text, /Sa\.? 05:00–08:001–4 g\/kg/);
  assert.match(text, /Im Rennen60–120 g\/h · Glukose \+ Fruktose · über 90 g\/h unbelegt/);
  assert.match(rendered.racePlanMeal("2026-11-28", "2026-11-28T00:00:00", [4, 1]), /^Sa\.? 4–1 h vor dem Start$/);
  assert.match(rendered.racePlanMeal("2026-11-28", "2026-11-28T02:00:00", [4, 1]), /^Fr\.? 22:00 – Sa\.? 01:00$/);
  assert.equal(rendered.racePlanDuring({ step: "mouth_rinse" }), "kleine Mengen oder Mund spülen");
  assert.equal(rendered.racePlanDays("kein Datum", 1), "1 Tag davor");
});

test("long planned units ask for fuelling and show drinking and carbohydrates with their basis", () => {
  assert.equal(rendered.plannedFuellingQuery({ sport: "ride", environment: "outdoor", planned_duration_s: 9900 }, "2026-11-20"), "sport=ride&environment=outdoor&date=2026-11-20&duration_s=9900");
  // The card's duration in minutes leads, as for the shown duration.
  assert.match(rendered.plannedFuellingQuery({ sport: "run", duration_minutes: 150, planned_duration_s: 1800 }, "2026-11-20"), /duration_s=9000$/);
  for (const entry of [{ sport: "ride", planned_duration_s: 3599 }, { sport: "swim", planned_duration_s: 7200 }, { sport: "run" }]) {
    assert.equal(rendered.plannedFuellingQuery(entry, "2026-11-20"), null);
  }
  const text = rendered.plannedFuellingBlock({
    sweat_rate: { l_per_h: 0.9, sessions: 6, similar: true, temperature_c: [13, 23] },
    drinking: { l_per_h: 0.39, total_l: 1.1, max_l_per_h: 0.9, sodium_g_per_l: [0.5, 0.7] },
    carbohydrate_source: "Thomas, Erdman und Burke 2016",
    carbohydrates: { step: "high", low_g: 60, high_g: 120, established_max_g: 90 },
  }).map(textOf).join("|");
  assert.match(text, /Schweißrate 0,9 l\/h · 6 Einheiten bei 13–23 °C/);
  assert.match(text, /Pro Stunde0,39 l/);
  assert.match(text, /Gesamt1,1 l.*Natrium0,5–0,7 g\/l/);
  assert.match(text, /Thomas, Erdman und Burke 2016.*Pro Stunde60–120 g\/h/);
  const generic = rendered.plannedFuellingBlock({ sweat_rate: null, drinking: { generic_l_per_h: [0.4, 0.8] }, carbohydrates: { step: "moderate", low_g: 30, high_g: 60 } }).map(textOf).join("|");
  assert.match(generic, /Startwert ohne eigene Daten.*Pro Stunde0,4–0,8 l/);
  // A rinse is advice, not a number: no value column for the carbohydrates (#3932 K-12).
  const rinse = rendered.plannedFuellingBlock({ sweat_rate: null, drinking: { generic_l_per_h: [0.4, 0.8] }, carbohydrates: { step: "mouth_rinse" } });
  assert.equal(textOf(rinse.at(-1)), "kleine Mengen oder Mund spülen");
  assert.equal(rinse.at(-1).className, "muted");
  assert.equal(rendered.plannedFuellingRows({ max_l_per_h: 1.2 }).filter(Boolean).map((row) => row.join(":")).join("|"), "Pro Stunde:bis 1,2 l");
  // #3012: a loss within 2 % of the weight needs no drink; the card says so instead of "0 l".
  assert.equal(
    rendered.plannedFuellingRows({ max_l_per_h: 1, l_per_h: 0, total_l: 0, within_loss_share: true, loss_l: 1.33 }).filter(Boolean).map((row) => row.join(":")).join("|"),
    "Pro Stunde:nicht nötig|Verlust:1,33 l · unter 2 % des Gewichts",
  );
  // A small remaining need still shows its amounts; a missing loss leaves only "nicht nötig".
  assert.equal(
    rendered.plannedFuellingRows({ max_l_per_h: 1.08, l_per_h: 0.03, total_l: 0.04 }).filter(Boolean).map((row) => row.join(":")).join("|"),
    "Pro Stunde:0,03 l|Gesamt:0,04 l",
  );
  assert.equal(
    rendered.plannedFuellingRows({ within_loss_share: true }).filter(Boolean).map((row) => row.join(":")).join("|"),
    "Pro Stunde:nicht nötig",
  );
  // An hourly share that rounds to nothing is left out, the total stays.
  assert.equal(
    rendered.plannedFuellingRows({ max_l_per_h: 0.5, l_per_h: 0, total_l: 0.01 }).filter(Boolean).map((row) => row.join(":")).join("|"),
    "Gesamt:0,01 l",
  );
});

test("a planned unit's fuelling is read once per query and only after the view's own reads", async () => {
  // #2991: two renders of the same unit (stored then fresh start, the wide detail pane)
  // asked twice with identical parameters while the view's reads were still running.
  const calls = [];
  const replies = [];
  let settle;
  const settled = new Promise((resolve) => { settle = resolve; });
  const state = { sessionGeneration: 1, initialStateLoaded: false, loadPromise: null };
  const views = loadPublicScript("plan-views.js", {
    AppFormat: require("../public/format.js"),
    document: fakeDocument(),
    URLSearchParams,
    state,
    DATA_LOAD_TIMEOUT_MS: 15_000,
    AppApi: { settled: () => settled },
    api: (path) => { calls.push(path); return replies.shift() || Promise.resolve({ status: "not_long" }); },
  });
  const tick = () => new Promise((resolve) => setImmediate(resolve));
  const query = "sport=ride&environment=outdoor&date=2026-11-20&duration_s=9000";
  const [first, second] = [views.h("section"), views.h("section")];
  views.loadPlannedFuelling(first, query);
  views.loadPlannedFuelling(second, query);
  await tick();
  assert.equal(calls.length, 0, "the read waits while the view still loads");
  settle();
  await tick();
  assert.equal(calls.length, 1, "both renders share one read");
  assert.equal(calls[0], `/api/nutrition/session-fuelling?${query}`);

  // After the start a read goes out at once; a failed one is not kept, so the retry reads again.
  state.initialStateLoaded = true;
  const other = "sport=run&environment=outdoor&date=2026-11-21&duration_s=7200";
  replies.push(Promise.reject(new Error("offline")));
  const failing = views.h("section");
  views.loadPlannedFuelling(failing, other);
  await tick();
  assert.equal(calls.length, 2);
  assert.equal(failing.dataset.state, "failed");
  views.loadPlannedFuelling(failing, other);
  await tick();
  assert.equal(calls.length, 3, "the retry reads again");

  // A read that waited across a logout is dropped unsent.
  state.loadPromise = Promise.resolve();
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  views.AppApi = { settled: () => pending };
  const late = views.h("section");
  views.loadPlannedFuelling(late, "sport=ride&environment=indoor&date=2026-11-22&duration_s=3600");
  state.sessionGeneration = 2;
  release();
  await tick();
  assert.equal(calls.length, 3, "a read from an ended session is not sent");
});

test("race plan weather shows the race-day forecast or names what is missing", () => {
  const ready = rendered.racePlanWeather({
    status: "ready", name: "Musterstadt", country: "Deutschland",
    day: { icon: "🌧️", condition: "Leichter Regen", temperature_min: 6.2, temperature_max: 11, wind_speed_max: 14, precipitation_probability_max: 40 },
  }).map(textOf).join("|");
  assert.match(ready, /Open-Meteo · Musterstadt, Deutschland/);
  assert.match(ready, /🌧️ Leichter Regen · 6–11 °C · Wind 14 km\/h · Regen 40 %/);
  for (const [status, text] of [["pending", "Noch keine Vorhersage"], ["not_found", "Ort nicht gefunden"], ["unavailable", "Vorhersage nicht verfügbar"], ["ready", "Vorhersage nicht verfügbar"]]) {
    assert.match(rendered.racePlanWeather({ status }).map(textOf).join(""), new RegExp(text));
  }
  assert.equal(rendered.racePlanWeather({ status: "no_location" }).length, 0);
  assert.equal(rendered.racePlanWeather({ status: "not_in_horizon" }).length, 0);
  assert.equal(rendered.racePlanWeather(undefined).length, 0);
});
