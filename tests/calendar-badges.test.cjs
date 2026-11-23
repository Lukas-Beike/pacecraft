const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions, FakeElement, fakeDocument, textOf } = require("./frontend-harness.cjs");

function helpers() {
  return extractFunctions("plan-views.js", [
    "CALENDAR_MARKERS", "MARKER_WORD_GAP", "calendarMarkerSource", "CALENDAR_MARKER_PATTERN", "PLANNED_RAIN_FLAG_MIN_PERCENT", "PLANNED_UV_FLAG_MIN",
    "PLANNED_AQI_FLAG_MIN", "PLANNED_CAMS_POLLEN_FLAG_MIN", "PLANNED_DAY_STATUS_LABELS",
    "stripCalendarMarkers", "calendarMarkerKeys", "plannedDayStatusLabel", "plannedDayNoteSpecs", "plannedConflictLabels", "plannedAppointmentLabel",
  ]);
}

// vm contexts have their own Array/Object prototypes; normalise before deep comparison.
const plain = (value) => JSON.parse(JSON.stringify(value));

test("raw calendar marker tokens are removed from appointment labels", () => {
  const { stripCalendarMarkers, plannedAppointmentLabel } = helpers();
  assert.equal(stripCalendarMarkers("Arzt [NO_TRAINING]  [short_only] Termin"), "Arzt Termin");
  assert.equal(stripCalendarMarkers("[NO_INTENSITY]"), "");
  assert.equal(
    plannedAppointmentLabel({ name: "Physio [NO_INTENSITY]", start_local: "2026-10-10T08:30:00" }),
    "Physio · 08:30",
  );
  assert.equal(plannedAppointmentLabel({ name: "[SHORT_ONLY]", all_day: true }), "Trainingstermin · ganztägig");
});

test("marker flags and raw tokens both map to marker keys in a stable order", () => {
  const { calendarMarkerKeys } = helpers();
  assert.deepEqual(plain(calendarMarkerKeys({ short_only: true, no_training: true })), ["no_training", "short_only"]);
  assert.deepEqual(plain(calendarMarkerKeys({ name: "Sauna [NO_INTENSITY]" })), ["no_intensity"]);
  assert.deepEqual(plain(calendarMarkerKeys({ name: "Ruhig", no_training: false })), []);
  assert.deepEqual(plain(calendarMarkerKeys(null)), []);
});

test("day notes show competition, confirmed rest status and one calendar line with deduplicated markers", () => {
  const { plannedDayNoteSpecs } = helpers();
  const specs = plannedDayNoteSpecs(
    {
      day_status: "rest",
      no_training: true,
      appointments: [{ name: "Arzt [NO_TRAINING]", no_training: true, training_relevant: false }],
    },
    [
      { event_date: "2026-10-10", name: "Stadtlauf", priority: "A" },
      { event_date: "2026-10-11", name: "Anderer Tag", priority: "C" },
    ],
    "2026-10-10",
  );
  assert.deepEqual(plain(specs), [
    { kind: "competition", icon: "🏁", text: "A-Wettkampf: Stadtlauf" },
    { kind: "day-status", icon: "", text: "Ruhetag geplant" },
    { kind: "calendar", icon: "📅", text: "Arzt · Kein Training" },
  ]);
});

test("pause status and unknown status values are handled explicitly", () => {
  const { plannedDayStatusLabel, plannedDayNoteSpecs } = helpers();
  assert.equal(plannedDayStatusLabel("pause"), "Trainingspause");
  assert.equal(plannedDayStatusLabel("unknown"), null);
  assert.equal(plannedDayStatusLabel("constructor"), null);
  assert.deepEqual(plain(plannedDayNoteSpecs({}, [], "2026-10-10")), []);
});

test("conflicts use the backend labels as text, skip empty entries and drop duplicates", () => {
  const { plannedConflictLabels } = helpers();
  assert.deepEqual(
    plain(plannedConflictLabels([
      { code: "short_only", label: "Nur kurze Einheiten" },
      { code: "competition_ab", label: "" },
      { code: "short_only", label: "Nur kurze Einheiten" },
      null,
    ])),
    ["Nur kurze Einheiten"],
  );
  assert.deepEqual(plain(plannedConflictLabels(undefined)), []);
});

test("tolerant marker spellings are stripped and detected like the backend", () => {
  const { stripCalendarMarkers, calendarMarkerKeys, plannedAppointmentLabel } = helpers();
  assert.equal(stripCalendarMarkers("Arzt (NO TRAINING) Termin"), "Arzt Termin");
  assert.equal(stripCalendarMarkers("Physio [NO-TRAINING]"), "Physio");
  assert.equal(stripCalendarMarkers("Sauna [ no intensity ]"), "Sauna");
  assert.equal(stripCalendarMarkers("Lauf (short-only] Termin"), "Lauf Termin");
  assert.equal(stripCalendarMarkers("Kurs [NOTRAINING]"), "Kurs");
  assert.equal(stripCalendarMarkers("Stadtlauf Termin"), "Stadtlauf Termin");
  assert.equal(stripCalendarMarkers("NO TRAINING ohne Klammern"), "NO TRAINING ohne Klammern");
  assert.deepEqual(plain(calendarMarkerKeys({ name: "Arzt (NO TRAINING)" })), ["no_training"]);
  assert.deepEqual(plain(calendarMarkerKeys({ name: "Lauf [short_only" })), []);
  assert.equal(
    plannedAppointmentLabel({ name: "Physio (No-Intensity)", start_local: "2026-10-10T08:30:00" }),
    "Physio · 08:30",
  );
});

test("each calendar marker is named in German on the day note", () => {
  const { plannedDayNoteSpecs } = helpers();
  const labels = { "[NO_TRAINING]": "Kein Training", "[NO_INTENSITY]": "Keine Intensität", "[SHORT_ONLY]": "Nur kurze Einheiten" };
  for (const [token, label] of Object.entries(labels)) {
    const [note] = plannedDayNoteSpecs({ appointments: [{ name: `Kurs ${token}` }] }, [], "2026-10-10");
    assert.equal(note.text, `Kurs · ${label}`, token);
  }
});

test("a badge shows its text as text and hides its glyph from screen readers", () => {
  const { calendarBadge, calendarBadgeRow } = extractFunctions("plan-views.js", ["calendarBadge", "calendarBadgeRow"], {
    document: { createElement: (tag) => new FakeElement(tag) },
  });
  const badge = calendarBadge({ kind: "calendar", icon: "*", text: "<img src=x onerror=alert(1)>" });
  assert.equal(badge.className, "planned-badge is-calendar");
  assert.equal(badge.children[0].attributes["aria-hidden"], "true");
  assert.equal(badge.children[1], "<img src=x onerror=alert(1)>");
  const row = calendarBadgeRow([{ kind: "calendar", text: "A" }, { kind: "day-status", text: "B" }]);
  assert.equal(row.className, "planned-badges");
  assert.deepEqual(row.children.map((child) => child.className), ["planned-badge is-calendar", "planned-badge is-day-status"]);
});

test("the infection hint names only each unusual value and its usual range, and only on a hint", () => {
  const { infectionHintText } = extractFunctions("plan-views.js", ["calendarMetricNumber", "infectionHintText"], {
    AppFormat: { number: (value, { digits }) => value.toFixed(digits).replace(".", ",") },
  });
  const signals = [
    { label: "Atemfrequenz", value: 16.2, unit: "brpm", lower: 14, upper: 15, source: "Garmin Connect", unusual: true },
    { label: "Ruhepuls", value: 53, unit: "bpm", lower: 48, upper: 50, source: "Garmin Connect", unusual: true },
    // A value inside its usual range is not listed.
    { label: "HRV", value: 64, unit: "ms", lower: 60, upper: 70, source: "Garmin Connect", unusual: false },
  ];
  assert.equal(
    infectionHintText({ status: "hint", signals, limits: "Keine Diagnose." }),
    "Infekt-Hinweis: Atemfrequenz 16,2/min (üblich 14–15) · Ruhepuls 53 bpm (üblich 48–50)",
  );
  assert.equal(infectionHintText({ status: "none", signals }), "");
  assert.equal(infectionHintText(undefined), "");
});

// #3821: "Mir geht's gut" hides the hint for its day, "Ich bin krank" records illness with a receipt and undo.
function hintNote({ fail = null } = {}) {
  const calls = [];
  const announced = [];
  const helpers = extractFunctions("plan-views.js", ["calendarMetricNumber", "infectionHintText", "infectionHintDismissed", "infectionHintReceipts", "INFECTION_SYMPTOMS", "infectionHintNote", "plannedMoveUndo"], {
    document: fakeDocument(),
    AppFormat: { number: (value, { digits }) => value.toFixed(digits).replace(".", ",") },
    api: async (path, options) => {
      calls.push([path, options.method, options.body]);
      if (fail) throw fail;
      return path.endsWith("/ill") ? { status: "ok", change: { id: "c1", after_hash: "h1" } } : { status: "ok" };
    },
    isSessionEndedError: () => false,
    announcePlanChange: (text) => announced.push(text),
    showPlannedDay: () => {},
    load: async () => calls.push(["load"]),
  });
  const hint = { status: "hint", signals: [{ label: "Ruhepuls", value: 53, unit: "bpm", lower: 48, upper: 50, unusual: true }] };
  return { ...helpers, calls, announced, hint };
}
const buttons = (note) => note.descendants().filter((node) => node.tag === "button");

test("the infection hint offers 'Mir geht's gut' (neutral) and 'Ich bin krank' (filled) side by side", () => {
  const { infectionHintNote, hint } = hintNote();
  const note = infectionHintNote(hint, "2026-11-21", false);
  assert.equal(note.dataset.infectionHint, "");
  assert.equal(textOf(note.children[0]), "Infekt-Hinweis: Ruhepuls 53 bpm (üblich 48–50)");
  assert.equal(note.children[1].className, "coach-action-card-actions infection-hint-actions");
  assert.deepEqual(buttons(note).map((button) => [textOf(button), button.className]), [["Mir geht's gut", "secondary-button"], ["Ich bin krank", ""]]);
  // A day that already reports illness keeps the values without the question.
  assert.deepEqual(buttons(infectionHintNote(hint, "2026-11-21", true)), []);
  assert.equal(infectionHintNote({ status: "none", signals: [] }, "2026-11-21", false), null);
});

test("a 'keine' or 'nein' illness answer still asks the infection hint question", () => {
  const { reportsComplaint } = extractFunctions("plan-views.js", ["NO_COMPLAINT_ANSWERS", "reportsComplaint"]);
  for (const answer of ["", "nein", "Keine.", " kein ", "gesund", "n/a"]) assert.equal(reportsComplaint(answer), false, answer);
  for (const answer of ["Halsweh", "krank", "Schnupfen; Fieber"]) assert.equal(reportsComplaint(answer), true, answer);
});

test("'Mir geht's gut' hides the hint for that day only", async () => {
  const { infectionHintNote, hint, calls, announced } = hintNote();
  const [well] = buttons(infectionHintNote(hint, "2026-11-21", false));
  await well.fire("click");
  assert.deepEqual(calls, [["/api/infection-hint/well", "POST", "{\"date\":\"2026-11-21\"}"]]);
  assert.deepEqual(announced, ["Infekt-Hinweis für heute ausgeblendet"]);
  assert.equal(infectionHintNote(hint, "2026-11-21", false), null);
  // A new hint on a later day shows again.
  assert.notEqual(infectionHintNote(hint, "2026-11-22", false), null);
});

const symptomInputs = (note) => note.descendants().filter((node) => node.tag === "input");

test("'Ich bin krank' expands the card into nine symptom chips and 'Bestätigen' without saving yet", async () => {
  const { infectionHintNote, hint, calls } = hintNote();
  const note = infectionHintNote(hint, "2026-11-21", false);
  await buttons(note)[1].fire("click");
  assert.deepEqual(calls, []);
  assert.deepEqual(symptomInputs(note).map((input) => input.value), ["schnupfen", "halsschmerzen", "husten", "kopfschmerzen", "fieber", "gliederschmerzen", "muedigkeit", "brust_atemnot", "magen_darm"]);
  assert.equal(textOf(note.children[1]), "SchnupfenHalsschmerzenHustenKopfschmerzenFieberGliederschmerzenMüdigkeit/AbgeschlagenheitDruck auf der Brust/AtemnotMagen-DarmBestätigen");
  assert.deepEqual(buttons(note).map((button) => textOf(button)), ["Bestätigen"]);
});

test("'Bestätigen' saves the selected symptoms, and none selected saves an empty list", async () => {
  for (const [picked, expected] of [[[1, 4], ["halsschmerzen", "fieber"]], [[], []]]) {
    const { infectionHintNote, hint, calls } = hintNote();
    const note = infectionHintNote(hint, "2026-11-21", false);
    await buttons(note)[1].fire("click");
    for (const index of picked) symptomInputs(note)[index].checked = true;
    await buttons(note)[0].fire("click");
    assert.deepEqual(calls, [["/api/infection-hint/ill", "POST", JSON.stringify({ date: "2026-11-21", symptoms: expected })], ["load"]]);
  }
});

test("'Ich bin krank' shows the receipt with undo, and undo takes it back", async () => {
  const { infectionHintNote, hint, calls } = hintNote();
  const note = infectionHintNote(hint, "2026-11-21", false);
  await buttons(note)[1].fire("click");
  await buttons(note)[0].fire("click");
  assert.deepEqual(calls, [["/api/infection-hint/ill", "POST", "{\"date\":\"2026-11-21\",\"symptoms\":[]}"], ["load"]]);
  // After the reload the day reports illness: the receipt and its undo stay.
  const ill = infectionHintNote(hint, "2026-11-21", true);
  const [undo] = buttons(ill);
  assert.equal(textOf(ill.children[1]), "Krankheit für heute gespeichert. Rückgängig");
  await undo.fire("click");
  assert.deepEqual(calls.slice(2), [["/api/change-history/undo", "POST", JSON.stringify({ change_id: "c1", expected_current_hash: "h1" })], ["load"]]);
  assert.equal(textOf(infectionHintNote(hint, "2026-11-21", false).children[2]), "Zurückgenommen.");
});

test("offline, an answer is not queued: the note stays and shows the network message", async () => {
  const offline = Object.assign(new Error("Keine Verbindung zum Server."), { status: 0, reason: "network_error" });
  const { infectionHintNote, hint, calls } = hintNote({ fail: offline });
  const note = infectionHintNote(hint, "2026-11-21", false);
  const [well, sick] = buttons(note);
  await sick.fire("click");
  await buttons(note)[0].fire("click");
  assert.deepEqual(calls, [["/api/infection-hint/ill", "POST", "{\"date\":\"2026-11-21\",\"symptoms\":[]}"]]);
  assert.equal(textOf(note.children[2]), "Keine Verbindung zum Server.");
  assert.equal(well.disabled || sick.disabled, false);
  await well.fire("click");
  assert.notEqual(infectionHintNote(hint, "2026-11-21", false), null);
});

test("a rest day is planned only while no session, shown or hidden by the filter, stands on the day", () => {
  const { plannedDayNoteSpecs } = helpers();
  const withSession = plannedDayNoteSpecs({ day_status: "rest" }, [], "2026-10-10", true);
  assert.deepEqual(plain(withSession), []);
  assert.deepEqual(plain(plannedDayNoteSpecs({ day_status: "rest" }, [], "2026-10-10", false)), [{ kind: "day-status", icon: "", text: "Ruhetag geplant" }]);
  assert.deepEqual(plain(plannedDayNoteSpecs({ day_status: "pause" }, [], "2026-10-10", true)), [{ kind: "day-status", icon: "", text: "Trainingspause" }]);
});

test("a competition goal is shown as a duration only when it is a time", () => {
  const { competitionGoal } = extractFunctions("plan-views.js", ["competitionGoal"], { AppFormat: require("../public/format.js") });
  assert.equal(competitionGoal("Unter 45 Minuten"), "Unter 45 Minuten");
  assert.equal(competitionGoal(null), null);
  assert.equal(competitionGoal("01:45:00"), competitionGoal("1:45:00"));
  assert.notEqual(competitionGoal("01:45:00"), "01:45:00");
  // Two parts are ambiguous (3:30 is hours and minutes for a marathon), so they stay as written.
  assert.equal(competitionGoal("3:30"), "3:30");
});
