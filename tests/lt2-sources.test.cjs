// LT2 from Garmin beside the local estimates (#3273): rows, adoptable values, the change notice.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const NAMES = ["zoneValueText", "thresholdMainText", "LT2_METRICS", "LT2_SOURCE_NAMES", "lt2ProviderValue", "lt2Duration", "lt2AdoptValues", "lt2RangeText", "lt2EstimateBasis", "lt2ProviderRow", "lt2EstimateRow", "lt2SourceRows", "lt2NoticeValues", "LT2_NOTICE_MIN", "LT2_NOTICE_UNITS", "LT2_NOTICE_LABELS", "lt2NoticeChanges", "lt2NoticeValueText", "lt2NoticeDetail"];
const plain = (value) => JSON.parse(JSON.stringify(value));

const METRICS = {
  run_threshold_pace_seconds_per_km: { value: 252, source: "Garmin", source_provider: "garmin", observed_at: "2026-11-03" },
  run_threshold_hr_bpm: { value: 171, source: "Garmin", source_provider: "garmin" },
  cycling_ftp_watts: { value: 280, source: "Lokal", source_provider: null, alternatives: [{ value: 265, source: "Garmin", source_provider: "garmin" }] },
  bike_threshold_hr_bpm: { value: 158, source: "Garmin", source_provider: "garmin" },
};
const ESTIMATES = {
  run: {
    dfa_alpha1: { status: "ok", heart_rate_bpm: 169, pace_seconds_per_km: 256, sessions: 6, window_days: 60, quality: "good" },
    critical_speed: { status: "unknown", reason: "Keine unabhängigen 3, 7, 12-Minuten-Belastungen in 42 Tagen.", sessions: 0 },
  },
  ride: {
    dfa_alpha1: { status: "unknown", reason: "nur aus Läufen", sessions: 0 },
    critical_power: { status: "ok", power_w: 271, durations_s: [180, 420, 720, 1200], window_days: 42, quality: "low" },
  },
};

function lt2(globals = {}) {
  return extractFunctions("settings.js", NAMES, {
    state: { data: {} },
    formatPace: (seconds) => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")} /km`,
    dateLabel: (day) => day.split("-").reverse().join("."),
    AppFormat: require("../public/format.js"),
    ...globals,
  });
}

test("a run shows Garmin, the DFA-α1 values and CS with its reason", () => {
  const { lt2SourceRows } = lt2();
  const rows = plain(lt2SourceRows("run", METRICS, ESTIMATES.run, () => null));
  assert.deepEqual(rows.map((row) => [row.name, row.values, row.basis, row.inEffect]), [
    ["Garmin", "4:12 /km · 171 bpm", "vom 03.11.2026", true],
    ["DFA-α1", "169 bpm · 4:16 /km", "aus 6 Läufen der letzten 60 Tage", false],
    ["CS", "unbekannt", "Keine unabhängigen 3, 7, 12-Minuten-Belastungen in 42 Tagen.", false],
  ]);
  // F8: pulse and pace are adoptable one by one; an unknown estimate offers nothing.
  assert.deepEqual(rows[1].adopt.map((item) => [item.value, item.shown]), [["heart_rate", 169], ["pace", 256]]);
  assert.deepEqual(rows[2].adopt, []);
});

test("an adopted estimate is the one in effect and Garmin's value comes from its alternative", () => {
  const { lt2SourceRows } = lt2();
  const effective = (metric) => (metric === "power" ? { threshold_origin: "critical_power" } : null);
  const rows = plain(lt2SourceRows("ride", METRICS, ESTIMATES.ride, effective));
  assert.deepEqual(rows.map((row) => [row.name, row.values, row.inEffect]), [
    ["Garmin", "265 W · 158 bpm", false],
    ["DFA-α1", "unbekannt", false],
    ["CP", "271 W", true],
  ]);
  // B2 (#3919): CS/CP name that they usually lie above the sustainable threshold.
  assert.equal(rows[2].basis, "Bestwerte 3, 7, 12 und 20 min aus 42 Tagen · meist einige Prozent über der Dauerschwelle");
  assert.equal(rows[2].quality, "low");
});

test("the DFA-α1 heart rate shows its cited limits of agreement, other values and sources none (#3920 C7)", () => {
  const { lt2SourceRows } = lt2();
  const range = { heart_rate_bpm: [-24, 16], basis: "Limits of Agreement gegen VT2, Rogers et al. 2021 (R76)" };
  const withRange = { ...ESTIMATES.run, dfa_alpha1: { ...ESTIMATES.run.dfa_alpha1, range } };
  const rows = plain(lt2SourceRows("run", METRICS, withRange, () => null));
  // The range belongs to the heart rate only, not to the pace read from it; the adopt labels stay plain.
  assert.equal(rows[1].values, "169 bpm (−24/+16) · 4:16 /km");
  assert.deepEqual(rows[1].adopt.map((item) => item.label), ["Herzfrequenz 169 bpm", "Pace 4:16 /km"]);
  // Without a range in the estimate, and for the Garmin and CP rows, nothing is added.
  assert.equal(plain(lt2SourceRows("run", METRICS, ESTIMATES.run, () => null))[1].values, "169 bpm · 4:16 /km");
  assert.equal(rows[0].values, "4:12 /km · 171 bpm");
  assert.equal(plain(lt2SourceRows("ride", METRICS, ESTIMATES.ride, () => null))[2].values, "271 W");
  const { lt2RangeText } = lt2();
  assert.equal(lt2RangeText({ value: "heart_rate" }, { range: { heart_rate_bpm: [-5, 0] } }), " (−5/+0)");
});

test("the notice needs 2 bpm, 3 s/km or 5 W, or a switch to or from unbekannt", () => {
  const { lt2NoticeChanges, lt2NoticeDetail } = lt2();
  const seen = { provider_hr: 171, provider_pace: 252, dfa_hr: 169, dfa_pace: 256, cs_pace: null };
  const wobble = { provider_hr: 172, provider_pace: 254, dfa_hr: 170, dfa_pace: 258, cs_pace: null };
  assert.deepEqual(plain(lt2NoticeChanges(seen, wobble)), []);
  const moved = { provider_hr: 173, provider_pace: 249, dfa_hr: 169, dfa_pace: 256, cs_pace: 249 };
  const changes = lt2NoticeChanges(seen, moved);
  assert.deepEqual(plain(changes).map((change) => change.key), ["provider_hr", "provider_pace", "cs_pace"]);
  assert.equal(lt2NoticeDetail(changes, "Garmin"), "Garmin Herzfrequenz 171 → 173 bpm, Garmin Pace 4:12 → 4:09 /km, CS unbekannt → 4:09 /km");
  assert.deepEqual(plain(lt2NoticeChanges({ cp_power: 271 }, { cp_power: 266 })).length, 1);
  assert.deepEqual(plain(lt2NoticeChanges({ cp_power: 271 }, { cp_power: 267 })), []);
});

test("the notice compares the Garmin and estimate values of each sport", () => {
  const { lt2NoticeValues } = lt2();
  assert.deepEqual(plain(lt2NoticeValues("run", METRICS, ESTIMATES.run)), { provider_hr: 171, provider_pace: 252, dfa_hr: 169, dfa_pace: 256, cs_pace: null });
  assert.deepEqual(plain(lt2NoticeValues("ride", METRICS, ESTIMATES.ride)), { provider_power: 265, provider_hr: 158, cp_power: 271 });
});

test("Analyse lists each known local estimate as an estimate", () => {
  const { withLt2Estimates } = extractFunctions("performance-view.js", ["withLt2Estimates"]);
  const values = plain(withLt2Estimates({}, ESTIMATES));
  assert.deepEqual(values.run_lt2_dfa_hr_bpm, { value: 169, unit: "bpm", estimated: true });
  assert.deepEqual(values.cycling_lt2_cp_watts, { value: 271, unit: "W", estimated: true });
  assert.equal(values.run_lt2_cs_pace_seconds_per_km, undefined);
  assert.deepEqual(plain(withLt2Estimates({ a: 1 }, null)), { a: 1 });
});

test("a failed baseline save is not retried by itself, and one save per sport runs at a time", async () => {
  // Gitar on #3303: a failing first save must not loop through refreshLt2Notice.
  let calls = 0;
  let refreshes = 0;
  const toasts = [];
  const { seeLt2Values } = extractFunctions("settings.js", ["lt2BaselineSaving", "seeLt2Values"], {
    state: { data: {} },
    api: async () => { calls += 1; throw new Error("Server nicht erreichbar"); },
    toast: (message) => toasts.push(message),
    refreshLt2Notice: () => { refreshes += 1; },
  });
  await Promise.all([seeLt2Values("run", { provider_hr: 171 }), seeLt2Values("run", { provider_hr: 171 })]);
  assert.equal(calls, 1);
  assert.equal(refreshes, 0);
  assert.deepEqual(toasts, ["Server nicht erreichbar"]);
});

test("the rows keep their order whatever the server's key order", () => {
  const { lt2SourceRows } = lt2();
  const swapped = { critical_speed: ESTIMATES.run.critical_speed, dfa_alpha1: ESTIMATES.run.dfa_alpha1 };
  assert.deepEqual(plain(lt2SourceRows("run", METRICS, swapped, () => null)).map((row) => row.name), ["Garmin", "DFA-α1", "CS"]);
});
