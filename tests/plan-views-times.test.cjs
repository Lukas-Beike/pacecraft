const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions, fakeDocument, textOf } = require("./frontend-harness.cjs");

function loadHelpers() {
  // Marker stripping is covered elsewhere; these tests only exercise the time handling.
  return extractFunctions("plan-views.js", ["plannedAppointmentLabel", "calendarStartTime", "plannedUnitStartTime"], { stripCalendarMarkers: (text) => String(text || "") });
}

test("external appointments keep a genuine midnight start time", () => {
  const { plannedAppointmentLabel } = loadHelpers();
  assert.equal(
    plannedAppointmentLabel({ name: "Physio", all_day: false, start_local: "2026-10-12T00:00:00" }),
    "Physio · 00:00",
  );
});

test("all-day external appointments show no clock time", () => {
  const { plannedAppointmentLabel } = loadHelpers();
  assert.equal(
    plannedAppointmentLabel({ name: "Urlaub", all_day: true, start_local: "2026-10-12T00:00:00" }),
    "Urlaub · ganztägig",
  );
});

test("date-only planned units at midnight show no start time", () => {
  const { plannedUnitStartTime } = loadHelpers();
  assert.equal(plannedUnitStartTime("2026-10-12T00:00:00"), null);
  assert.equal(plannedUnitStartTime("2026-10-12"), null);
  assert.equal(plannedUnitStartTime("2026-10-12T07:30:00"), "07:30");
});

test("completed activities keep their recorded midnight start time", () => {
  const { calendarStartTime } = loadHelpers();
  assert.equal(calendarStartTime("2026-10-12T00:00:00"), "00:00");
});

test("a date-only value keeps its day, a moment takes the local day and an invalid value has none", () => {
  const { localDateKey } = extractFunctions("views.js", ["localDateKey"]);
  assert.equal(localDateKey("2026-10-12"), "2026-10-12");
  assert.equal(localDateKey(new Date(2026, 9, 12, 23, 30)), "2026-10-12");
  assert.equal(localDateKey("2026-10-12T23:30:00"), "2026-10-12");
  assert.equal(localDateKey("not a date"), "");
});

test("a personal event covers its days, without the end day at midnight", () => {
  const addDateKey = (key, days) => {
    const date = new Date(`${key}T12:00:00Z`);
    date.setUTCDate(date.getUTCDate() + days);
    return date.toISOString().slice(0, 10);
  };
  const { plannedEventDays, plannedEventTime } = extractFunctions("plan-views.js", ["plannedEventDays", "plannedEventTime"], { addDateKey });
  const days = (start, end) => [...plannedEventDays({ start_local: start, end_local: end })];
  assert.deepEqual(days("2026-10-12T19:00:00", "2026-10-12T21:00:00"), ["2026-10-12"]);
  assert.deepEqual(days("2026-10-12T00:00:00", "2026-10-14T00:00:00"), ["2026-10-12", "2026-10-13"]);
  assert.deepEqual(days("2026-10-12T22:00:00", "2026-10-13T01:00:00"), ["2026-10-12", "2026-10-13"]);
  assert.deepEqual(days("2026-10-12T00:00:00", "2026-10-12T00:00:00"), ["2026-10-12"]);
  assert.deepEqual(days("not a date", ""), []);
  assert.equal(plannedEventTime({ start_local: "2026-10-12T19:00:00", end_local: "2026-10-12T21:00:00" }), "19:00–21:00");
  assert.equal(plannedEventTime({ all_day: 1, start_local: "2026-10-12T00:00:00" }), "ganztägig");
});

// The week review under the calendar week (#2805, #3298): sleep and training time as h:mm, also under an hour.
test("the week review writes durations as hours and minutes", () => {
  const { weekReviewHours } = extractFunctions("plan-views.js", ["weekReviewHours"]);
  assert.deepEqual([1800, 26640, 25200, 89, -600].map(weekReviewHours), ["0:30", "7:24", "7:00", "0:01", "0:10"]);
});

const WEEK_REVIEW_FUNCTIONS = ["WEEK_REVIEW_SHORT_DAY", "WEEK_REVIEW_REASONS", "WEEK_REVIEW_POSITIONS", "WEEK_REVIEW_SCORES", "weekReviewDay", "weekReviewHours", "weekReviewChange", "weekReviewPercent", "weekReviewTile", "weekReviewPlanTile", "weekReviewLoadTiles", "weekReviewRecoveryTiles", "weekReviewCheckinTiles", "weekReviewNotableTile", "weekReviewTiles"];
const weekReviewTileTexts = (review, extra) => {
  const { weekReviewTiles } = extractFunctions("plan-views.js", WEEK_REVIEW_FUNCTIONS, { document: fakeDocument(), AppFormat: { number: (value) => String(value) } });
  return Array.from(weekReviewTiles(review, extra), (tile) => tile.children.map(textOf).join("|"));
};

// Layout B as approved: a tile per figure, a change only for a finished week, a tile without data left out.
test("a past week shows every tile with its change and source, in the approved order", () => {
  const review = {
    week_start: "2026-11-09", week_end: "2026-11-15", effective_end: "2026-11-15",
    plan: { planned: 6, completed: 5, missed: 1, open: 0, missed_units: [{ date: "2026-11-12", name: "Kraft" }] },
    load: { this_week: { duration_s: 27600, sessions: 6, training_load: 412 }, week_before: { duration_s: 24600, sessions: 5, training_load: 381 } },
    recovery: {
      sleep: { mean: 7 + 5 / 60, week_before: 7.25, unit: "h", source: "Garmin" },
      hrv: { mean: 62, unit: "ms", position: "within", source: "Garmin" },
      resting_hr: { mean: 46, unit: "bpm", position: "below", source: "Wahoo" },
    },
    notable: [{ date: "2026-11-15", name: "Langer Lauf", reason: "highest_load", training_load: 120 }, { date: "2026-11-14", name: "10 km", reason: "race", race: true }],
    checkins: { entered: 6, days: 7, pain_days: 1, illness_days: 0, scores: { motivation: 7, stress: 3 } },
  };
  assert.deepEqual(weekReviewTileTexts(review, 1), [
    "Plan|5 von 6|verpasst: Do Kraft",
    "Einheiten|6|davon 1 zusätzlich",
    "Zeit|7:40 h|+12 %",
    "Belastung|412|+8 %",
    "Schlaf Ø|7:05 h|−0:10|Garmin",
    "Ruhepuls Ø|46 bpm|unter Normalbereich · Wahoo",
    "HRV Ø|62 ms|im Normalbereich · Garmin",
    "Check-ins|6 von 7|Ø Motivation 7 · Stress 3",
    "Besondere Einheiten|So Langer Lauf · höchste Belastung (120)Sa 10 km · Wettkampf",
    "Beschwerden|1 Tag Schmerz",
  ]);
});

test("a running week shows no change in time and load, and a week without data shows no tile", () => {
  const review = {
    week_start: "2026-11-16", week_end: "2026-11-22", effective_end: "2026-11-17",
    plan: { planned: 5, completed: 1, missed: 0, open: 4, missed_units: [] },
    load: { this_week: { duration_s: 2700, sessions: 1, training_load: 38 }, week_before: { duration_s: 27600, sessions: 6, training_load: 412 } },
    recovery: {}, notable: [], checkins: { entered: 0, days: 2, scores: {} },
  };
  assert.deepEqual(weekReviewTileTexts(review, 0), ["Plan|1 von 5|4 noch offen", "Einheiten|1", "Zeit|0:45 h", "Belastung|38"]);
  const empty = { ...review, plan: { planned: 0 }, load: { this_week: { sessions: 0 }, week_before: {} } };
  assert.deepEqual(weekReviewTileTexts(empty, 0), []);
});
