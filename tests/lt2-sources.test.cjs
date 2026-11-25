// LT2 from Garmin beside the local estimates (#3273): rows, adoptable values; the offers of a new LT1 or LT2 (#4135).
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const NAMES = ["zoneValueText", "thresholdMainText", "LT2_METRICS", "LT2_SOURCE_NAMES", "lt2ProviderValue", "lt2Duration", "lt2AdoptValues", "lt2RangeText", "lt2EstimateBasis", "lt2ProviderRow", "lt2EstimateRow", "lt2SourceRows"];
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
    ["Kritische Geschwindigkeit (CS)", "unbekannt", "Keine unabhängigen 3, 7, 12-Minuten-Belastungen in 42 Tagen.", false],
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
    ["Kritische Leistung (CP)", "271 W", true],
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

// #4135: the offers as PR 1's profile read returns them.
const OFFER_LT2 = { key: "lt2:run:heart_rate:dfa_alpha1:172", threshold: "lt2", sport: "run", metric: "heart_rate", source: "dfa_alpha1", value: 172, unit: "bpm", current: 168, sessions: 4, window_days: 60, quality: "good", spread_pct: 2.1, newest: "2026-11-21", adopt: { sport: "run", source: "dfa_alpha1", value: "heart_rate", shown: 172 } };
const OFFER_LT1 = { ...OFFER_LT2, key: "lt1:run:heart_rate:dfa_alpha1:151", threshold: "lt1", value: 151, current: 148, sessions: 6, spread_pct: 3.4, adopt: { percent_lthr: 88 } };
const OFFER_CS = { ...OFFER_LT2, key: "lt2:run:pace:critical_speed:242", metric: "pace", source: "critical_speed", value: 242, unit: "s/km", current: 245, sessions: null, window_days: 42, spread_pct: null };
const OFFER_GARMIN = { ...OFFER_LT2, key: "lt2:ride:power:garmin:270", sport: "ride", metric: "power", source: "garmin", value: 270, unit: "W", current: null, sessions: null, window_days: null, quality: null, spread_pct: null, newest: "2026-11-19" };

function offers() {
  return extractFunctions("settings.js", ["THRESHOLD_OFFER_SOURCES", "THRESHOLD_OFFER_DFA", "THRESHOLD_OFFER_UNITS", "thresholdOfferNumber", "thresholdOfferBasis", "thresholdOffersFor", "thresholdOfferView", "thresholdOfferWhen"], { state: { data: {} }, AppFormat: require("../public/format.js") });
}

test("an offer card names the value, the value in effect, source, basis and quality (#4135)", () => {
  const { thresholdOfferView } = offers();
  const lt2Offer = plain(thresholdOfferView(OFFER_LT2));
  assert.deepEqual([lt2Offer.title, lt2Offer.when, lt2Offer.value, lt2Offer.before], ["Neue LT2", "nach dem Lauf am 21.11.", "172 bpm", "bisher 168"]);
  assert.deepEqual(lt2Offer.facts, [["Quelle", "DFA-α1 0,5 · Brustgurt"], ["Basis", "4 harte Läufe in 60 Tagen"], ["Qualität", "gut · Spanne 2,1 %"]]);
  assert.equal(lt2Offer.detail, "bisher 168 · DFA-α1 0,5 · 4 harte Läufe in 60 Tagen · gut");
  assert.equal(lt2Offer.brief, "bisher 168 · DFA-α1 · 4 Läufe");
  const lt1Offer = plain(thresholdOfferView(OFFER_LT1));
  assert.deepEqual([lt1Offer.title, lt1Offer.value, lt1Offer.before], ["Neue LT1", "151 bpm", "bisher 148"]);
  assert.deepEqual(lt1Offer.facts, [["Quelle", "DFA-α1 0,75 · Brustgurt"], ["Basis", "6 Läufe in 60 Tagen"], ["Qualität", "gut · Spanne 3,4 %"]]);
});

test("CS shows the pace as m:ss, and Garmin has neither basis nor quality (#4135)", () => {
  const { thresholdOfferView } = offers();
  const cs = plain(thresholdOfferView(OFFER_CS));
  assert.deepEqual([cs.value, cs.before], ["4:02 /km", "bisher 4:05"]);
  assert.deepEqual(cs.facts, [["Quelle", "CS"], ["Basis", "Bestwerte in 42 Tagen"], ["Qualität", "gut"]]);
  assert.equal(cs.brief, "bisher 4:05 · CS");
  const garmin = plain(thresholdOfferView(OFFER_GARMIN));
  assert.deepEqual([garmin.value, garmin.before, garmin.when], ["270 W", null, "vom 19.11."]);
  assert.deepEqual(garmin.facts, [["Quelle", "Garmin"]]);
  assert.equal(garmin.detail, "Garmin");
});

test("a sport's offers are its own; none or a failed read is no offer (#4135)", () => {
  const { thresholdOffersFor, thresholdOfferWhen } = offers();
  const all = [OFFER_LT2, OFFER_GARMIN];
  assert.deepEqual(plain(thresholdOffersFor("run", all)).map((offer) => offer.key), [OFFER_LT2.key]);
  assert.deepEqual(plain(thresholdOffersFor("ride", all)).map((offer) => offer.key), [OFFER_GARMIN.key]);
  assert.deepEqual(plain(thresholdOffersFor("run", null)), []);
  assert.equal(thresholdOfferWhen({ ...OFFER_LT2, sport: "ride", source: "critical_power" }), "nach der Fahrt am 21.11.");
  assert.equal(thresholdOfferWhen({ ...OFFER_LT2, newest: null }), "");
});

test("Analyse lists each known local estimate as an estimate", () => {
  const { withLt2Estimates } = extractFunctions("performance-view.js", ["withLt2Estimates"]);
  const values = plain(withLt2Estimates({}, ESTIMATES));
  assert.deepEqual(values.run_lt2_dfa_hr_bpm, { value: 169, unit: "bpm", estimated: true });
  assert.deepEqual(values.cycling_lt2_cp_watts, { value: 271, unit: "W", estimated: true });
  assert.equal(values.run_lt2_cs_pace_seconds_per_km, undefined);
  assert.deepEqual(plain(withLt2Estimates({ a: 1 }, null)), { a: 1 });
});

test("the rows keep their order whatever the server's key order", () => {
  const { lt2SourceRows } = lt2();
  const swapped = { critical_speed: ESTIMATES.run.critical_speed, dfa_alpha1: ESTIMATES.run.dfa_alpha1 };
  assert.deepEqual(plain(lt2SourceRows("run", METRICS, swapped, () => null)).map((row) => row.name), ["Garmin", "DFA-α1", "Kritische Geschwindigkeit (CS)"]);
});
