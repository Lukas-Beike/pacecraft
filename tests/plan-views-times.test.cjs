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
  const { weekReviewHours } = extractFunctions("plan-views.js", ["weekReviewHours"], { AppFormat: require("../public/format.js") });
  assert.deepEqual([1800, 26640, 25200, 89, -600].map(weekReviewHours), ["30 min", "7 h 24 min", "7 h", "1 min", "10 min"]);
});

const WEEK_REVIEW_FUNCTIONS = ["WEEK_REVIEW_SHORT_DAY", "WEEK_REVIEW_REASONS", "WEEK_REVIEW_POSITIONS", "WEEK_REVIEW_SCORES", "weekReviewDay", "weekReviewHours", "weekReviewChange", "weekReviewPercent", "weekReviewTile", "weekReviewPlanTile", "weekReviewLoadTiles", "weekReviewRecoveryTiles", "weekReviewCheckinTiles", "weekReviewNotableTile", "weekReviewTiles"];
const weekReviewTileTexts = (review, extra, running = false) => {
  const { weekReviewTiles } = extractFunctions("plan-views.js", WEEK_REVIEW_FUNCTIONS, { document: fakeDocument(), AppFormat: { number: (value) => String(value), duration: require("../public/format.js").duration } });
  return Array.from(weekReviewTiles(review, extra, running), (tile) => tile.children.map(textOf).join("|"));
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
    "Zeit|7 h 40 min|+12 % ggü. Vorwoche",
    "Belastung|412|+8 % ggü. Vorwoche",
    "Schlaf Ø|7 h 5 min|−10 min ggü. Vorwoche|Garmin",
    "Ruheherzfrequenz Ø|46 bpm|unter Normalbereich · Wahoo",
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
  assert.deepEqual(weekReviewTileTexts(review, 0, true), ["Plan|1 von 5|4 noch offen", "Einheiten|1", "Zeit|45 min", "Belastung|38"]);
  // On its Sunday the review already reaches the week's end, but the week still runs (CI pinned day, #3298).
  const sunday = { ...review, effective_end: review.week_end };
  assert.deepEqual(weekReviewTileTexts(sunday, 0, true), ["Plan|1 von 5|4 noch offen", "Einheiten|1", "Zeit|45 min", "Belastung|38"]);
  assert.deepEqual(weekReviewTileTexts(sunday, 0, false).slice(2), ["Zeit|45 min|−90 % ggü. Vorwoche", "Belastung|38|−91 % ggü. Vorwoche"]);
  const empty = { ...review, plan: { planned: 0 }, load: { this_week: { sessions: 0 }, week_before: {} } };
  assert.deepEqual(weekReviewTileTexts(empty, 0, true), []);
});

// #3766 (athlete: layout A): a wide tile, one line per area and side, a worsening in red.
test("structured complaints show one line per area with days and severity", () => {
  const functions = [...WEEK_REVIEW_FUNCTIONS, "COMPLAINT_AREA_LABELS", "COMPLAINT_SIDE_LABELS", "complaintLabel", "weekReviewComplaintTile"];
  const { weekReviewCheckinTiles } = extractFunctions("plan-views.js", functions, { document: fakeDocument(), AppFormat: { number: (value) => String(value) } });
  const checkins = {
    entered: 5, days: 7, pain_days: 3, illness_days: 1, scores: {},
    complaints: [
      { area: "achilles", side: "right", days: 3, dates: ["2026-11-09", "2026-11-11", "2026-11-13"], first_severity: 3, last_severity: 5 },
      { area: "knee", side: "left", days: 1, dates: ["2026-11-09"], first_severity: 2, last_severity: 2 },
      { area: "lower_back", side: "none", days: 2, dates: ["2026-11-11", "2026-11-12"], first_severity: 4, last_severity: 3 },
    ],
  };
  const tile = weekReviewCheckinTiles(checkins)[1];
  assert.match(tile.className, /week-review-wide/);
  const lines = tile.children[1].children;
  assert.deepEqual(lines.map(textOf), [
    "Achillessehne rechts · 3 Tage · Stärke 3 → 5",
    "Knie links · 1 Tag · Stärke 2/10",
    "unterer Rücken · 2 Tage · Stärke 4 → 3",
    "1 Tag krank",
  ]);
  // Pain days without a structured complaint keep their count (review on #3766).
  // Gitar on #3806: counted by the server, also when a structured day has no pain text.
  const textOnly = weekReviewCheckinTiles({ ...checkins, pain_text_days: 2 })[1].children[1].children.map(textOf);
  assert.equal(textOnly.at(-2), "2 Tage Schmerz (Text)");
  // Worse is the "down" (red) direction, better the "up" one.
  assert.equal(lines[0].children[1].dataset.direction, "down");
  assert.equal(lines[2].children[1].dataset.direction, "up");
  // Without structured complaints the tile keeps its counts.
  assert.equal(textOf(weekReviewCheckinTiles({ ...checkins, complaints: [] })[1]), "Beschwerden3 Tage Schmerz, 1 Tag krank");
});

test("the calendar day names area, side and severity before the athlete's words", () => {
  const { plannedDayNotes } = extractFunctions("plan-views.js", ["COMPLAINT_AREA_LABELS", "COMPLAINT_SIDE_LABELS", "complaintLabel", "plannedPauseDecorate", "infectionHintText", "infectionHintDismissed", "infectionHintReceipts", "infectionHintNote", "NO_COMPLAINT_ANSWERS", "reportsComplaint", "plannedDayNotes"], {
    document: fakeDocument(), plannedDayNoteSpecs: () => [],
  });
  const note = (checkin) => textOf(plannedDayNotes({ checkin }, [], "2026-11-17"));
  assert.equal(note({ pain: "zieht beim Anlaufen", complaints: [{ area: "achilles", side: "right", severity: 5 }] }), "Beschwerden: Achillessehne rechts 5/10 · zieht beim Anlaufen");
  assert.equal(note({ pain: "Knie" }), "Verletzung/Beschwerden: Knie");
  assert.equal(note({ illness: "Schnupfen", complaints: [{ area: "calf", side: "both", severity: 2 }] }), "Krankheit: Schnupfen · Beschwerden: Wade beidseitig 2/10");
});
