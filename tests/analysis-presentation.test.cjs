const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions, FakeElement, fakeDocument, textOf } = require("./frontend-harness.cjs");

test("analysis popovers stay inside the viewport on narrow screens and flip above when needed", () => {
  const { analysisPopoverPosition } = extractFunctions("analysis.js", ["analysisPopoverPosition"]);
  const viewport = { width: 360, height: 640 };
  const popover = { width: 340, height: 200 };
  const below = analysisPopoverPosition({ left: 300, top: 80, bottom: 100 }, popover, viewport);
  assert.equal(below.left, 12, "left edge is clamped to the 12px margin");
  assert.equal(below.top, 108, "popover opens below the anchor when it fits");

  const flipped = analysisPopoverPosition({ left: 20, top: 600, bottom: 620 }, popover, viewport);
  assert.equal(flipped.top, 392, "popover opens above the anchor near the bottom edge");

  const short = analysisPopoverPosition({ left: 20, top: 100, bottom: 120 }, { width: 200, height: 280 }, { width: 360, height: 300 });
  assert.ok(short.top >= 12 && short.top + 280 <= 300, "popover that fits neither side is clamped vertically");

  const right = analysisPopoverPosition({ left: 350, top: 80, bottom: 100 }, { width: 200, height: 100 }, viewport);
  assert.equal(right.left, 148, "anchor near the right edge pulls the popover back inside");
});

test("ratio units such as W/bpm keep significant digits and small efficiencies are not rounded to zero", () => {
  const { analysisValue } = extractFunctions("analysis.js", ["analysisRatioUnit", "analysisCountUnit", "analysisNumber", "analysisValue"], { AppFormat: require("../public/format.js") });
  assert.equal(analysisValue(0.0234, "(m/s)/bpm"), "0,0234 (m/s)/bpm");
  assert.equal(analysisValue(1.4237, "W/bpm"), "1,42 W/bpm");
  assert.equal(analysisValue(1.5, "W/bpm"), "1,5 W/bpm");
  assert.equal(analysisValue(12.34, "W"), "12,3 W", "other units keep one decimal");
  assert.equal(analysisValue(10.123456789, ""), "10,1");
  // A precise VO2max keeps its decimal; a whole value stays whole (#2263).
  assert.equal(analysisValue(54.3, "ml/kg/min"), "54,3 ml/kg/min");
  assert.equal(analysisValue(54, "ml/kg/min"), "54 ml/kg/min");
  assert.equal(analysisValue(null, "W/bpm"), "unbekannt");
});

test("general maximum heart rate preserves the server provider selection", () => {
  const { currentPerformanceValues } = extractFunctions("performance-view.js", ["currentPerformanceValues"]);
  const selected = { value: 187, source_provider: "garmin" };
  const values = currentPerformanceValues({
    running_max_hr_bpm: selected,
    cycling_max_hr_bpm: { value: 200, source_provider: "intervals" },
    max_hr_bpm: selected,
  });
  assert.equal(values.max_hr_bpm, selected);
});

test("race prediction rows exclude body weight and empty states give a reason", () => {
  const { racePredictionRows, racePredictionEmptyReason } = extractFunctions("performance-view.js", ["racePredictionRows", "racePredictionEmptyReason"]);
  const rows = racePredictionRows({
    run_5k_seconds: { value: 1200, source: "Garmin Connect" },
    run_marathon_seconds: { value: null },
    weight_kg: { value: 72.4, source: "Garmin Connect" },
  });
  assert.deepEqual(Array.from(rows, (row) => row.label), ["5 km"]);
  assert.equal(racePredictionRows(undefined).length, 0);
  // 5 and 10 km show seconds, the half and full marathon hours and minutes (#2263).
  const { predictionTime } = extractFunctions("performance-view.js", ["predictionTime"], { AppFormat: require("../public/format.js") });
  const shown = Object.fromEntries(racePredictionRows({
    run_5k_seconds: { value: 1307 }, run_10k_seconds: { value: 2741.4 }, run_half_marathon_seconds: { value: 5960 }, run_marathon_seconds: { value: 12_310 },
  }).map(({ label, metric, withSeconds }) => [label, predictionTime(metric.value, withSeconds)]));
  assert.deepEqual(shown, { "5 km": "21:47", "10 km": "45:41", Halbmarathon: "1:39 h", Marathon: "3:25 h" });
  assert.equal(predictionTime(3735, true), "1:02:15 h", "an hour or more carries h like the longer races");

  assert.equal(
    racePredictionEmptyReason({ weight_kg: { value: 72.4 } }),
    "Noch keine Prognosen – dafür fehlen aktuelle Leistungswerte aus Garmin oder Wahoo.",
  );
  assert.match(racePredictionEmptyReason({ run_threshold_pace_seconds_per_km: { value: 290 } }), /^Noch keine Prognosen – Laufwerte sind vorhanden/);
});

test("current W/kg uses the preferred provider's newest point and falls back to the newest of all", () => {
  const { isPreferredSource } = extractFunctions("shared.js", ["isPreferredSource"]);
  const { analysisValidPoint } = extractFunctions("analysis.js", ["analysisValidPoint"]);
  const { currentPerformanceValues } = extractFunctions("performance-view.js", ["currentPerformanceValues"], { isPreferredSource, analysisValidPoint });
  const garmin = (points) => ({ source: "Garmin Connect", source_provider: "garmin", points });
  const intervals = (points) => ({ source: "Intervals.icu eFTP", source_provider: "intervals", points });
  const history = (series, preferred) => ({ body: { preferred_providers: preferred, windows: { "12w": { metrics: { cycling_w_per_kg: series } } } } });
  const wPerKg = (series, preferred) => currentPerformanceValues({}, history(series, preferred)).cycling_w_per_kg;

  const older = wPerKg([garmin([{ date: "2026-10-10", value: 3.1 }]), intervals([{ date: "2026-10-14", value: 3.3 }, { date: "2026-10-15", value: 3.4 }])], { cycling_w_per_kg: "garmin" });
  assert.equal(older.value, 3.1, "an older preferred Garmin point wins over newer Intervals points");
  assert.equal(older.source, "Garmin Connect", "the value keeps the Garmin source label");
  assert.equal(older.unit, "W/kg");

  const invalid = wPerKg([garmin([{ date: "2026-10-12", value: null }]), intervals([{ date: "2026-10-14", value: 3.3 }, { date: "2026-10-15", value: 3.4 }])], { cycling_w_per_kg: "garmin" });
  assert.equal(invalid.value, 3.4, "without a valid Garmin point the newest Intervals point is used");
  assert.equal(invalid.source, "Intervals.icu eFTP");
  assert.equal(wPerKg([garmin([]), intervals([{ date: "2026-10-14", value: 3.3 }])], { cycling_w_per_kg: "garmin" }).value, 3.3, "a Garmin series without points falls back too");

  const tie = wPerKg([intervals([{ date: "2026-10-15", value: 3.4 }]), garmin([{ date: "2026-10-15", value: 3.2 }])], { cycling_w_per_kg: "garmin" });
  assert.equal(tie.value, 3.2, "the preferred Garmin point wins a same-date tie");
  const unlabelled = (series) => series.map(({ source_provider, ...item }) => item);
  const byLabel = wPerKg(unlabelled([garmin([{ date: "2026-10-10", value: 3.1 }]), intervals([{ date: "2026-10-15", value: 3.4 }])]), { cycling_w_per_kg: "garmin" });
  assert.equal(byLabel.value, 3.4, "display labels are never compared, so a series without a provider id is not preferred");

  const none = wPerKg([garmin([{ date: "2026-10-10", value: 3.1 }]), intervals([{ date: "2026-10-15", value: 3.4 }])], {});
  assert.equal(none.value, 3.4, "a metric without a preference keeps the newest point");
  assert.equal(wPerKg([], { cycling_w_per_kg: "garmin" }), undefined, "no points at all stay empty");
});

test("analysisHasData is true only when a series holds a valid finite point", () => {
  const { analysisValidPoint, analysisHasData } = extractFunctions("analysis.js", ["analysisValidPoint", "analysisHasData"]);
  const has = (...seriesPoints) => analysisHasData(seriesPoints.map((points) => ({ points })));
  assert.equal(has(), false, "no series at all");
  assert.equal(has([]), false, "a series without points");
  assert.equal(has([{ date: "2026-10-10", value: null }, { date: "2026-10-11", value: "x" }]), false, "only invalid points");
  assert.equal(has([{ date: "2026-10-10", value: null }], [{ date: "2026-10-11", value: 0 }]), true, "partially filled: a zero is a valid measurement");
  assert.equal(has([{ date: "2026-10-10", value: 3.1 }, { date: "2026-10-11", value: 3.4 }]), true, "filled");
  assert.equal(analysisValidPoint({ value: Infinity }), false);
});

test("an analysis chart without data is omitted and its title is collected once as missing", () => {
  const { analysisChart, collectAnalysisChart } = extractFunctions(
    "analysis.js",
    ["analysisValidPoint", "analysisHasData", "analysisAxisCaption", "analysisChart", "collectAnalysisChart"],
    {
      h: (tag, className, text) => ({ tag, text, className }),
    },
  );
  const empty = [{ label: "Garmin", points: [{ date: "2026-10-10", value: null }] }];
  assert.equal(analysisChart("Ruhepuls", empty, "bpm", "2026-10-01", "2026-10-17", ""), null);

  const missing = [];
  assert.equal(collectAnalysisChart(missing, "Ruhepuls", empty, "bpm", "2026-10-01", "2026-10-17", ""), null);
  assert.equal(collectAnalysisChart(missing, "HRV", [], "ms", "2026-10-01", "2026-10-17", ""), null);
  assert.deepEqual(Array.from(missing), ["Ruhepuls", "HRV"]);
});

test("an analysis segment is empty only when every content container reports no data", () => {
  const markers = {};
  const { analysisSegmentEmpty } = extractFunctions("analysis.js", ["analysisSegmentEmpty"], {
    ANALYSIS_SEGMENT_CONTENT: { load: [["focus"], ["charts"]], recovery: [["recovery"]], running: [["shared", "analysisEmptyRunning"]], cycling: [["shared", "analysisEmptyCycling"]] },
    document: { getElementById: (id) => (id in markers ? { dataset: typeof markers[id] === "object" ? markers[id] : { analysisEmpty: markers[id] } } : { dataset: {} }) },
  });
  assert.equal(analysisSegmentEmpty("load"), false, "unrendered containers are not empty");
  Object.assign(markers, { focus: "true", charts: "loading" });
  assert.equal(analysisSegmentEmpty("load"), false, "a loading container keeps the segment");
  markers.charts = "failed";
  assert.equal(analysisSegmentEmpty("load"), false, "a failed container shows its retry");
  markers.charts = "true";
  assert.equal(analysisSegmentEmpty("load"), true);
  markers.recovery = "false";
  assert.equal(analysisSegmentEmpty("recovery"), false);
  // Laufen and Radfahren share a container and read their own sport's marker (#2265).
  markers.shared = { analysisEmpty: "false", analysisEmptyRunning: "true", analysisEmptyCycling: "false" };
  assert.deepEqual([analysisSegmentEmpty("running"), analysisSegmentEmpty("cycling")], [true, false]);
});

test("the analysis empty card offers one action that depends on the connected sources", () => {
  const state = { data: {} };
  const { analysisEmptyCard, analysisDataSourceConnected } = extractFunctions("analysis.js", ["analysisDataSourceConnected", "analysisSegmentPeriod", "analysisEmptyCard"], {
    state, document: fakeDocument(), analysisRefresh() {}, analysisHistoryPeriod: "twelveWeeks", recoveryHistoryPeriod: "fortnight",
    ANALYSIS_EMPTY_TITLES: { load: "Noch keine Belastungsdaten", recovery: "Noch keine Erholungsdaten" },
    createEmptyState: (title, cause, action) => ({ title, cause, action }),
  });
  const offline = analysisEmptyCard("load");
  assert.deepEqual([offline.title, offline.cause, offline.action.tag, textOf(offline.action), offline.action.href], ["Noch keine Belastungsdaten", "Garmin oder Wahoo ist nicht verbunden.", "a", "Anbindung einrichten", "#more/connections"]);
  state.data = { garmin: { configured: false }, wahoo: { connected: true } };
  assert.equal(analysisDataSourceConnected(state.data), true);
  const connected = analysisEmptyCard("load");
  assert.deepEqual([connected.cause, connected.action.tag, textOf(connected.action), Object.values(connected.action._listenerCounts).reduce((sum, count) => sum + count, 0)], ["Noch keine Daten abgeglichen.", "button", "Jetzt abgleichen", 1]);
  // An empty 14-day window may hide older data, so it offers the 12-week view instead of a sync.
  const shortWindow = analysisEmptyCard("recovery");
  assert.deepEqual([shortWindow.title, shortWindow.cause, textOf(shortWindow.action)], ["Noch keine Erholungsdaten", "Keine Daten in den letzten 14 Tagen.", "12 Wochen anzeigen"]);
  state.data = {};
  assert.equal(textOf(analysisEmptyCard("recovery").action), "Anbindung einrichten", "without a source the period does not matter");
  assert.equal(analysisDataSourceConnected({ garmin: { configured: true } }), true);
  assert.equal(analysisDataSourceConnected({ garmin: { configured: false }, wahoo: { connected: false } }), false);
});

test("the power profile counts only measured durations as values", () => {
  const { cyclingPowerProfileHasValues } = extractFunctions("analysis.js", ["cyclingPowerProfilePoint", "cyclingPowerProfileHasValues"], {
    CYCLING_POWER_PROFILE_DURATIONS: [[5, "5 Sekunden"], [60, "1 Minute"]],
  });
  assert.equal(cyclingPowerProfileHasValues({}), false);
  assert.equal(cyclingPowerProfileHasValues({ all_time: { power: [{ duration_seconds: 5, status: "missing", watts: null }] } }), false);
  assert.equal(cyclingPowerProfileHasValues({ all_time: { power: [{ duration_seconds: 60, status: "ok", watts: 300 }] } }), true);
});

test("a filled analysis chart is kept and is not listed as missing", () => {
  const chart = { title: "Gewicht" };
  const { collectAnalysisChart } = extractFunctions("analysis.js", ["collectAnalysisChart"], {
    analysisChart: (title) => (title === "Gewicht" ? chart : null),
  });
  const missing = [];
  assert.equal(collectAnalysisChart(missing, "Gewicht", [{ points: [{ value: 72 }] }]), chart);
  assert.equal(collectAnalysisChart(missing, "Körperfett", []), null);
  assert.deepEqual(Array.from(missing), ["Körperfett"], "only the empty chart is listed");
});

const BODY_OVERVIEW_FUNCTIONS = ["bodySourceShown", "bodyOverviewLatest", "bodyFoldFat", "bodyOverviewReading", "bodyOverviewRow", "renderBodyAnalysis"];

function bodyOverviewHarness(root, reads) {
  return extractFunctions("analysis.js", BODY_OVERVIEW_FUNCTIONS, {
    state: { data: { profile: { body_fat_pct: "18.5" } } }, document: { ...fakeDocument(), getElementById: () => root },
    isPreferredSource: (item, preferred) => item.source_provider === preferred,
    analysisValidPoint: (p) => p.value != null, dateLabel: (v) => v, analysisValue: (v, unit) => `${v} ${unit}`,
    analysisPeriodControls: () => new FakeElement("nav"), bodyHistoryPeriod: "twelveWeeks", bodyMetricChart: () => null,
    energyBodyCards: () => [], loadSkinfolds: () => {}, openBodyFatDialog: () => {}, openWeightDialog: () => {},
    bodyEditButton: (label) => Object.assign(new FakeElement("button"), { label }),
    get skinfoldRead() { return reads.skinfolds; },
  });
}

test("body overview: the newest own body fat with its source, weight with its source, lean mass derived (#3402)", () => {
  const root = Object.assign(new FakeElement("div"), { querySelectorAll: () => [] });
  const reads = { skinfolds: { measurements: [{ date: "2026-11-10", body_fat_pct: 13.9 }] } };
  const { renderBodyAnalysis, bodyFoldFat } = bodyOverviewHarness(root, reads);
  const series = (provider, source, points) => ({ source_provider: provider, source, points: points.map(([date, value]) => ({ date, value })) });
  const body = (fat) => ({ preferred_providers: { weight_kg: "garmin" }, windows: { "12w": { start: "2026-10-01", end: "2026-11-17", metrics: {
    weight_kg: [series("garmin", "Garmin Connect", [["2026-11-16", 80]])],
    body_fat_pct: [series("garmin", "Garmin Connect", [["2026-11-17", 22]]), series(null, "Lokal", fat)],
  } } } });
  const rows = (fat) => { renderBodyAnalysis(body(fat)); return root.children[0].children[0].children[1].children; };
  // An entered percentage newer than the folds is the KFA; the scale's newer 22 % is not KFA (#3336).
  let shown = rows([["2026-11-15", 15]]);
  assert.equal(textOf(shown[0].children[0]), "GewichtGarmin Connect · 2026-11-16");
  assert.equal(textOf(shown[1].children[0]), "KörperfettLokal · 2026-11-15");
  assert.equal(textOf(shown[1].children[1]), "15 %");
  assert.equal(shown[0].children[2].children[0].label, "Gewicht eintragen");
  assert.equal(shown[1].children[2].children[0].label, "Körperfett eintragen");
  // The fat-free mass is derived from the two values and has no pencil.
  assert.equal(textOf(shown[2].children[0]), "Fettfreie Masse");
  assert.equal(textOf(shown[2].children[1]), "68 kg");
  assert.equal(shown[2].children[2].children.length, 0);
  // The folds win a shared day and an older entry.
  shown = rows([["2026-11-10", 15]]);
  assert.equal(textOf(shown[1].children[0]), "KörperfettLokal (Hautfalten) · 2026-11-10");
  assert.equal(textOf(shown[1].children[1]), "13.9 %");
  // Without any own value the row shows a dash (the profile's 18.5 % is not KFA), and no lean mass.
  reads.skinfolds = { measurements: [] };
  shown = rows([]);
  assert.equal(textOf(shown[1].children[1]), "\u2014");
  assert.equal(shown.length, 2);
  const unknown = bodyFoldFat({ measurements: [{ date: "2026-11-15", body_fat_pct: null }] }, null);
  assert.equal(`${unknown.text} ${unknown.note}`, "unbekannt ohne Geburtsjahr");
  assert.equal(bodyFoldFat(null, null), null);
});

test("the skinfold preview gives Jackson-Pollock body fat as the server does (#3402)", () => {
  const { skinfoldBodyFat } = extractFunctions("analysis.js", ["SKINFOLD_DENSITY", "skinfoldBodyFat"]);
  // The server's worked value (e2e/skinfolds.spec.js): men, 16/20/17 mm at 40 years give 17.1 %.
  assert.equal(skinfoldBodyFat("male", [16, 20, 17], 40), 17.1);
  assert.equal(skinfoldBodyFat("male", [16, 20, 0], 40), null, "an empty fold gives no value");
  assert.equal(skinfoldBodyFat("female", [16, 20, 17], 17), null, "an age outside 18-80 gives none");
});

test("an entered weight shows beside the preferred source and wins its own day", () => {
  const { bodyOverviewReading } = extractFunctions("analysis.js", ["bodySourceShown", "bodyOverviewLatest", "bodyFoldFat", "bodyOverviewReading"], {
    isPreferredSource: (item, preferred) => item.source_provider === preferred,
    analysisValidPoint: (p) => p.value != null, dateLabel: (v) => v, skinfoldRead: null,
  });
  const series = (provider, points) => ({ source_provider: provider, points: points.map(([date, value]) => ({ date, value, observed_at: date })) });
  const body = (local) => ({ windows: { "14d": { start: "2026-11-01", end: "2026-11-14", metrics: { weight_kg: [
    series("garmin", [["2026-11-12", 71.2]]), series("wahoo", [["2026-11-14", 90]]), series(null, local),
  ] } } } });
  const shown = (local) => `${bodyOverviewReading("weight_kg", body(local), { weight_kg: "garmin" }).value} kg`;
  // Wahoo is not the preferred source; a newer local entry is the latest weight.
  assert.equal(shown([["2026-11-13", 72.5]]), "72.5 kg");
  // On its own day the entry wins over the scale's measurement.
  assert.equal(shown([["2026-11-12", 72.5]]), "72.5 kg");
  // A measurement of a later day is the newest.
  assert.equal(shown([["2026-11-11", 72.5]]), "71.2 kg");
});

test("the energy table shows bands, the badge only with a fit, and the fit basis", () => {
  const charts = [];
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const { energyTable, energyBasisText, energyFitNote, energyFitCharts } = extractFunctions("analysis.js", ["ENERGY_SOURCES", "ENERGY_INSUFFICIENT", "ENERGY_STEP_SOURCES", "ENERGY_IMPLAUSIBLE", "ENERGY_IMPLAUSIBLE_REASON", "ENERGY_PARTS", "ENERGY_FORMULAS", "ENERGY_BODY_MISSING", "energyMissingCell", "energyKcal", "energyNeatInputs", "energyBodyText", "energyBasisText", "energyBiasText", "energyInfoId", "energyInfo", "energyFitNote", "energySplitRow", "energyFlaggedRows", "energyTable", "energyFitCharts"], {
    bindAnalysisPopover: (button, info) => { button.dataset.popoverTarget = info.id; },
    h, formatWhole: (v) => String(v), dateLabel: (v) => v, analysisValue: (v, unit) => `${v} ${unit}`,
    createStatusChip: (label) => h("span", null, label),
    analysisChart: (title, series) => { charts.push({ title, series }); return new FakeElement("section"); },
  });
  const read = {
    end: "2026-11-14", windows: ["2026-11-07", "2026-11-14"], best: "katch_mcardle",
    fit: { shared_windows: 4, windows: 12, min_windows: 4, reference_band_kcal: 120 },
    rows: [
      { source: "katch_mcardle", resting_kcal: 1839, resting_band_kcal: 219, total_kcal: 2689, total_band_kcal: 227, errors_kcal: [null, 11], mean_error_kcal: 11 },
      { source: "cunningham", resting_kcal: 1996, resting_band_kcal: 233, total_kcal: 2846, total_band_kcal: 241, errors_kcal: [null, 146], mean_error_kcal: 146 },
      { source: "device", label: "Synthetic Watch", resting_kcal: null, total_kcal: null, errors_kcal: [null, null], mean_error_kcal: null },
      { source: "own", resting_kcal: 1850, resting_band_kcal: 150, total_kcal: 2700, total_band_kcal: 120 },
    ],
    basis: { days: 28, complete_days: 24, insufficient: null },
    body: { source: "skinfold", label: "Hautfalten", date: "2026-11-04", body_fat_pct: 15, lean_mass_kg: 68 },
  };
  const table = energyTable(read);
  // #3252: how the values come about sits behind the small "i", not under the table.
  const [captionText, info, tooltip] = table.children[0].children;
  assert.equal(captionText, "Energieverbrauch ");
  assert.equal(textOf(info), "i");
  assert.equal(info.dataset.popoverTarget, tooltip.id);
  assert.match(textOf(tooltip), /Ruhe aus deinen Daten ist ein Rest/);
  const rows = table.children[2].children;
  assert.equal(textOf(rows[0].children[0]), "Katch-McArdle passt am bestenKFA: Hautfalten");
  assert.equal(textOf(rows[0].children[1]), "1839 ± 219", "a formula row carries its band");
  assert.equal(textOf(rows[1].children[0]), "CunninghamKFA: Hautfalten", "only the best source carries the badge");
  assert.equal(textOf(rows[2].children[0]), "Synthetic Watch", "#3323: the device row's name comes from the read");
  assert.equal(textOf(rows[2].children[1]), "–", "a source without a value stays unknown");
  assert.equal(textOf(rows[3].children[2]), "2700 ± 120");
  assert.equal(textOf(energyTable({ ...read, best: null }).children[2].children[0].children[0]), "Katch-McArdleKFA: Hautfalten", "no best, no badge");
  assert.equal(textOf(energyFitNote(read)), "Vergleich · 4 von 12 Fenstern · Band aus deinen Daten ± 120 kcal");
  assert.equal(textOf(energyFitNote({ ...read, best: null, fit: { ...read.fit, shared_windows: 3 } })), "Welche Quelle passt: noch nicht unterscheidbar · 3 von 12 Fenstern · Band aus deinen Daten ± 120 kcal");
  assert.equal(textOf(energyFitNote({ ...read, best: null, fit: { ...read.fit, shared_windows: 0, reference_band_kcal: null } })), "Welche Quelle passt: noch nicht unterscheidbar · 0 von 12 Fenstern");
  const note = energyBasisText(read);
  assert.match(note, /24 von 28 Tagen vollständig .* KFA 15 % \(Hautfalten vom 2026-11-04\), fettfreie Masse 68 kg/);
  assert.match(note, /Bänder nominal 95 %, ohne Fehler beim Erfassen des Essens/, "#3187 item 6");
  assert.match(note, /Ruhe aus deinen Daten ist ein Rest/, "#3187 item 8");
  assert.match(energyBasisText({ ...read, basis: { insufficient: "weighins" }, body: null }), /Datenbasis zu gering \(unter 8 Wiegungen\).*Ohne Körperfettwert/);
  // #3255, #3252: NEAT's inputs join the basis behind the one "i", never a line under the table.
  const neat = { days: 27, steps_per_day: 14200, session_steps_per_day: 3100, floors_per_day: 9.5, step_length_m: 0.78, step_length_source: "height" };
  const withNeat = { ...read, basis: { ...read.basis, neat } };
  assert.doesNotMatch(energyBasisText(withNeat), /Schritten/, "the basis text itself carries no NEAT inputs");
  const [, neatButton, neatInfo] = energyTable(withNeat).children[0].children;
  assert.match(textOf(neatInfo), /Ruhe aus deinen Daten ist ein Rest .* · Alltag aus 14200 Schritten\/Tag, davon 3100 in Einheiten abgezogen, Schrittlänge 0.78 m \(aus Körpergröße\), 9.5 Etagen ohne Einheiten$/);
  assert.equal(neatButton.dataset.popoverTarget, neatInfo.id);
  // #3255: a residual below the resting reference is not shown as a value.
  const implausible = { ...read, rows: [...read.rows.slice(0, 3), { source: "own", resting_kcal: null, resting_band_kcal: null, resting_implausible: true, total_kcal: 2832, total_band_kcal: 455 }] };
  const gated = energyTable(implausible).children[2].children;
  assert.equal(textOf(gated[3].children[1]), "nicht plausibel");
  assert.equal(textOf(gated[3].children[2]), "2832 ± 455", "the day total stays shown");
  // #3100: with flagged meals, the clean-window total and the learned correction follow the own row;
  // the per-entry bias joins the popover.
  const flagged = { days: 2, unflagged: { total_kcal: 2650, total_band_kcal: 130, end: "2026-10-24" }, corrected: { total_kcal: 2760, total_band_kcal: 160 }, bias_pct: 12, bias_band_pct: 9 };
  const flaggedRows = energyTable({ ...read, flagged }).children[2].children.slice(-2);
  assert.deepEqual(flaggedRows.map((row) => [textOf(row.children[0]), textOf(row.children[1]), textOf(row.children[2])]), [["ohne geschätzte Tage", "–", "2650 ± 130"], ["mit gelernter Korrektur", "–", "2760 ± 160"]]);
  const noClean = energyTable({ ...read, flagged: { ...flagged, unflagged: null, corrected: null } }).children[2].children.at(-1);
  assert.equal(textOf(noClean.children[2]), "kein sauberer Zeitraum");
  assert.equal(energyTable({ ...read, flagged: { ...flagged, days: 0 } }).children[2].children.length, energyTable(read).children[2].children.length, "no flagged day, no extra row");
  const [, , flaggedInfo] = energyTable({ ...read, flagged }).children[0].children;
  assert.match(textOf(flaggedInfo), /Geschätzte Mahlzeiten wirken im Mittel 12 % höher als erfasst \(± 9 %\)$/);
  assert.equal(energyFitCharts(read).length, 1, "the chart alone; the ±100 hit-rate bars are gone");
  assert.deepEqual([...charts[0].series].map((item) => item.label), ["Katch-McArdle", "Cunningham"], "Garmin without a fit is left out");
  assert.deepEqual(JSON.parse(JSON.stringify(charts[0].series[0].points)), [{ date: "2026-11-07", value: null }, { date: "2026-11-14", value: 11 }]);
  assert.match(charts[0].series[0].explanation, /innerhalb von ± 120 kcal sind nicht unterscheidbar/);
  assert.equal(energyFitCharts({ ...read, rows: read.rows.slice(2) }).length, 0, "no fit, no chart");
  // Gitar #3180: without shared windows every mean is null, yet each window's distance is still drawn.
  const unshared = read.rows.map((row) => ({ ...row, mean_error_kcal: null }));
  assert.equal(energyFitCharts({ ...read, rows: unshared }).length, 1);
  const fittedDevice = read.rows.map((row) => (row.source === "device" ? { ...row, errors_kcal: [null, 40] } : row));
  energyFitCharts({ ...read, rows: fittedDevice });
  assert.equal(charts.at(-1).series[2].label, "Synthetic Watch", "#3323: the fit legend names the device from the read");
});

test("split A draws one stacked bar per row and hatches only the parts the backend marks shared", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const { energySplitRow, energySplitLegend } = extractFunctions("analysis.js", ["ENERGY_IMPLAUSIBLE", "ENERGY_IMPLAUSIBLE_REASON", "ENERGY_PARTS", "energySplitRow", "energySplitLegend"], { h, formatWhole: (v) => String(v) });
  const split = (resting, neat, sport, tef, shared) => ({ resting_kcal: resting, neat_kcal: neat, sport_kcal: sport, tef_kcal: tef, shared });
  const device = { source: "device", resting_band_kcal: null, split: split(1745, 610, 765, null, { neat: false, sport: false, tef: false }) };
  // Calibrated (k != 1): only digestion is computed alike for every row.
  const own = { source: "own", resting_band_kcal: 150, split: split(1690, 385, 507, 258, { neat: false, sport: false, tef: true }) };
  const deviceRow = energySplitRow(device);
  const [bar, numbers] = deviceRow.children[0].children;
  assert.equal(textOf(numbers), "Ruhe 1745 · Alltag 610 · Sport 765 · Verdauung –", "Garmin has no digestion part");
  assert.deepEqual([...bar.children].map((part) => part.dataset.part), ["resting", "neat", "sport"]);
  assert.equal(bar.children[0].style.width, `${(1745 / 3120) * 100}%`);
  assert.ok([...bar.children].every((part) => part.dataset.shared === undefined), "the device computes its own parts");
  const ownRow = energySplitRow(own);
  assert.equal(textOf(ownRow.children[0].children[1]), "Ruhe (Rest) 1690 ± 150 · Alltag 385 · Sport 507 · Verdauung 258");
  assert.deepEqual([...ownRow.children[0].children[0].children].map((part) => part.dataset.shared ?? null), [null, null, null, ""]);
  assert.equal(energySplitRow({ source: "katch_mcardle", split: null }), null, "no split, no bar");
  const gated = energySplitRow({ source: "own", resting_band_kcal: null, resting_implausible: true, split: split(null, 942, 417, 368, { neat: true, sport: true, tef: true }) });
  assert.equal(textOf(gated.children[0].children[1]), "Ruhe (Rest) nicht plausibel (unter dem Ruheumsatz: Essen unvollständig erfasst oder Alltag überschätzt) · Alltag 942 · Sport 417 · Verdauung 368");
  assert.deepEqual([...gated.children[0].children[0].children].map((part) => part.dataset.part), ["neat", "sport", "tef"]);
  const legend = energySplitLegend({ rows: [device, own] });
  assert.deepEqual([...legend.children].map((item) => textOf(item)), ["Ruhe", "Alltag", "Sport", "Verdauung", "gleich berechnet"]);
  assert.equal(energySplitLegend({ rows: [{ split: null }] }), null);
});

test("a reset drops the energy read in flight, so a pre-save answer is never shown", async () => {
  // Gitar #3180: a reused in-flight read used to write the view from before the skinfold save.
  const answers = [];
  let renders = 0;
  const fns = extractFunctions("analysis.js", ["energyRead", "energyRequest", "energyGeneration", "resetEnergy", "loadEnergy"], {
    api: () => new Promise((resolve) => answers.push(resolve)),
    renderBodyAnalysis: () => { renders += 1; }, renderDiaryEnergy: () => { renders += 1; },
    state: { data: {} },
    createLoadingState: () => ({}), h: () => ({}),
  });
  fns.loadEnergy();
  fns.resetEnergy(); // a skinfold save while the first read waits on the gate
  fns.loadEnergy();
  assert.equal(answers.length, 2, "the reset starts a new read instead of reusing the old one");
  answers[0]({ rows: [], stale: true });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(renders, 0, "the stale answer neither renders nor is kept");
  answers[1]({ rows: [], stale: false });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(renders, 2, "the fresh answer renders the body view and the diary (#3252)");
});

test("a failed skinfold read re-renders the card instead of loading forever", async () => {
  // Gitar #3120: only a successful read used to re-render the body view.
  let renders = 0;
  let reads = 0;
  const { loadSkinfolds } = extractFunctions("analysis.js", ["skinfoldRead", "skinfoldRequest", "loadSkinfolds"], {
    api: () => { reads += 1; return Promise.reject(new Error("offline")); },
    renderBodyAnalysis: () => { renders += 1; },
    state: { data: {} },
  });
  loadSkinfolds();
  loadSkinfolds(); // one read at a time
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(reads, 1);
  assert.equal(renders, 1);
  loadSkinfolds(); // the finished request no longer blocks a new read
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(reads, 2);
});


test("performance status shows only the server's status text", () => {
  const { performanceDataStatus } = extractFunctions("performance-view.js", ["performanceDataStatus"]);
  assert.deepEqual(Array.from(performanceDataStatus({ available: false, reason: "canonical_pending", status: "Daten werden vorbereitet" })), ["Daten werden vorbereitet"]);
  assert.deepEqual(Array.from(performanceDataStatus({ available: true, status: "Daten werden aktualisiert" })), ["Daten werden aktualisiert"]);
  assert.deepEqual(Array.from(performanceDataStatus({ available: false, reason: "canonical_pending" })), []);
  assert.deepEqual(Array.from(performanceDataStatus({ available: true })), []);
  assert.deepEqual(Array.from(performanceDataStatus({ status: "" })), []);
  assert.deepEqual(Array.from(performanceDataStatus(undefined)), []);
});

test("session-RPE charts name the provider that rated, never a fixed provider", () => {
  const { sessionRpeSourceLabel } = extractFunctions("analysis.js", ["sessionRpeSourceLabel"]);
  const day = (athlete, provider, labels) => ({ rpe_sources: { athlete_feedback: athlete, provider }, rpe_provider_labels: labels });
  for (const [days, rated, expected] of [
    [[day(0, 2, ["Garmin Connect"])], 2, "Garmin Connect"],
    [[day(1, 0, []), day(0, 1, ["Garmin Connect"])], 2, "Athletenfeedback + Garmin Connect"],
    [[{ rpe_sources: { athlete_feedback: 2, provider: 0 } }], 2, "Athletenfeedback"],
    [[day(0, 0, [])], 0, "Athletenfeedback"],
  ]) assert.equal(sessionRpeSourceLabel(days, rated), expected);
});

test("current performance shows the status lines below its heading", () => {
  const root = new FakeElement("section");
  const { renderCurrentPerformance } = extractFunctions("performance-view.js", ["currentPerformanceValues", "currentPerformanceRows", "withFirstThresholds", "withLt2Estimates", "renderCurrentPerformance"], {
    document: { ...fakeDocument(), getElementById: () => root },
    rememberAnalysisSectionState() {},
    makeAnalysisSectionCollapsible() {},
    analysisValidPoint: () => true,
    isPreferredSource: () => false,
    analysisValue: (value, unit) => `${value} ${unit}`,
  });
  renderCurrentPerformance({}, undefined, ["Daten werden vorbereitet"]);
  assert.deepEqual(root.children.slice(0, 2).map((node) => [node.tag, textOf(node)]), [["h3", "Aktuelle Werte"], ["p", "Daten werden vorbereitet"]]);
  assert.equal(root.dataset.analysisEmpty, "false", "a status line keeps the section visible");
  assert.deepEqual([root.dataset.analysisEmptyRunning, root.dataset.analysisEmptyCycling], ["false", "false"]);
  renderCurrentPerformance({}, undefined);
  assert.equal(root.children.filter((node) => textOf(node) === "Daten werden vorbereitet").length, 0);
  assert.equal(root.dataset.analysisEmpty, "true");
  // A sport with values keeps its own segment while the other one is empty (#2265).
  renderCurrentPerformance({ cycling_ftp_watts: { value: 260, unit: "W" } }, undefined);
  assert.deepEqual([root.dataset.analysisEmptyRunning, root.dataset.analysisEmptyCycling, root.dataset.analysisEmpty], ["true", "false", "false"]);
  // LT1 rows come from the thresholds without any zones; no row carries an estimate chip any more (#3297).
  const thresholds = { first_thresholds: { run: { hr_bpm: 128, hr_origin: "zone_2", pace_seconds_per_km: 307, pace_origin: "fixed_share" }, ride: { hr_bpm: 138, hr_origin: "measured", watts: 187, power_origin: "own_sessions" } } };
  renderCurrentPerformance({}, undefined, [], thresholds);
  const rows = [];
  const walk = (node) => { if (node?.dataset?.metric) rows.push(node); (node?.children || []).forEach(walk); };
  root.children.forEach(walk);
  const label = (key) => textOf(rows.find((row) => row.dataset.metric === key).children[0]);
  assert.deepEqual([label("run_lt1_pace_seconds_per_km"), label("run_lt1_hr_bpm"), label("cycling_lt1_hr_bpm"), label("cycling_lt1_watts")], ["Aerobe Schwelle (LT1) · Pace", "Aerobe Schwelle (LT1) · Puls", "Aerobe Schwelle (LT1) · Puls", "Aerobe Schwelle (LT1) · Leistung"]);
  assert.equal(root.dataset.analysisEmptyRunning, "false");
});

test("weekly daily-activity mean averages known days and keeps empty weeks as gaps", () => {
  const { analysisValidPoint, analysisWeeklyMeanPoints } = extractFunctions("analysis.js", ["analysisValidPoint", "analysisWeeklyPoints", "analysisWeeklyMeanPoints"], {
    addDateKey: (date, days) => new Date(Date.parse(`${date}T12:00:00Z`) + days * 86400000).toISOString().slice(0, 10),
  });
  const points = [
    { date: "2026-10-05", value: 1000 }, { date: "2026-10-06", value: 0 }, { date: "2026-10-07", value: null },
    { date: "2026-10-12", value: null },
  ];
  const weeks = analysisWeeklyMeanPoints(points, "2026-10-05", "2026-10-18");
  assert.equal(analysisValidPoint(weeks[0]) && weeks[0].value, 500, "unknown days are not zero, a real zero counts");
  assert.equal(weeks[1].value, null);
});

test("12-week body charts show the latest reading of each week and draw a single week as a point", () => {
  const { bodyWeeklyPoints, analysisPlottable } = extractFunctions("analysis.js", ["analysisValidPoint", "analysisPlottable", "analysisWeeklyPoints", "bodyWeeklyPoints"], {
    addDateKey: (key, days) => new Date(Date.parse(key) + days * 86400000).toISOString().slice(0, 10),
  });
  const points = [
    { date: "2026-10-31", value: null },
    { date: "2026-11-02", value: 15.6 },
    { date: "2026-11-04", value: 15.2 },
    { date: "2026-11-06", value: 14.9 },
  ];
  const weeks = bodyWeeklyPoints(points, "2026-10-24", "2026-11-06");
  assert.equal(weeks.length, 2);
  assert.deepEqual([weeks[0].value, weeks[0].weekReadings], [null, 0], "a week without readings stays a gap");
  assert.deepEqual([weeks[1].date, weeks[1].value, weeks[1].weekReadings], ["2026-11-06", 14.9, 3], "the week shows its last reading, not the median 15.2");
  assert.equal(analysisPlottable({ points: weeks, minPoints: 1 }), true, "body charts draw one weekly value");
  assert.equal(analysisPlottable({ points: weeks }), false, "other charts still need two values");
});

test("the seven-day ln(RMSSD) note shows the back-transformed mean, range and spread", () => {
  const { recoveryRollingNote } = extractFunctions("analysis.js", ["analysisRatioUnit", "analysisCountUnit", "analysisNumber", "analysisValue", "recoveryRollingNote"], { AppFormat: require("../public/format.js") });
  assert.equal(
    recoveryRollingNote({ status: "ok", mean_ms: 51.9, lower_ms: 53, upper_ms: 62.2, position: "below", cv_pct: 3, window_nights: 5 }),
    "7-Tage-Mittel 51,9 ms (5 Nächte) · unter deinem Bereich 53 ms–62,2 ms · Schwankung 3 %",
  );
  assert.equal(
    recoveryRollingNote({ status: "provisional", mean_ms: 57, lower_ms: 53, upper_ms: 62.2, position: "within", cv_pct: null, window_nights: 3 }),
    "7-Tage-Mittel 57 ms (3 Nächte) · in deinem Bereich 53 ms–62,2 ms · vorläufig",
  );
  assert.equal(
    recoveryRollingNote({ status: "insufficient_data", reason: "Mindestens 3 Nächte in den letzten 7 Tagen erforderlich." }),
    "7-Tage-Mittel: Mindestens 3 Nächte in den letzten 7 Tagen erforderlich",
  );
});

test("steps and floors carry their own per-day unit, a named basis and whole numbers", () => {
  const fns = extractFunctions("analysis.js", ["dailyActivityMetrics", "analysisBasisCaption", "analysisRatioUnit", "analysisCountUnit", "analysisNumber", "analysisValue", "analysisValidPoint", "dailyActivityTitle"], { AppFormat: require("../public/format.js") });
  const metrics = fns.dailyActivityMetrics();
  assert.deepEqual(Array.from(metrics, ({ key, unit }) => [key, unit]), [["steps", "Schritte/Tag"], ["floors", "Stockwerke/Tag"]]);
  assert.equal(fns.analysisBasisCaption("Schritte/Tag", true, "mean"), "Schritte/Tag · Wochenmittel");
  assert.equal(fns.analysisBasisCaption("Stockwerke/Tag", false, "mean"), "Stockwerke/Tag");
  assert.equal(fns.analysisBasisCaption("Belastungspunkte", true, "state"), "Belastungspunkte · Wochenstand");
  assert.equal(fns.analysisBasisCaption("h", false, "sum"), "h · Tagessumme");
  for (const { unit } of metrics) assert.doesNotMatch(fns.analysisBasisCaption(unit, true, "mean"), /Belastungspunkte/);
  assert.equal(fns.analysisValue(8171.4, "Schritte/Tag"), "8.171 Schritte/Tag");
  assert.equal(fns.analysisValue(7.5, "Stockwerke/Tag"), "8 Stockwerke/Tag");
  const days = [{ date: "2026-10-01", value: 6500 }, { date: "2026-10-02", value: 9843 }, { date: "2026-10-03", value: null }];
  assert.equal(fns.dailyActivityTitle("Schritte", "Schritte/Tag", days), "Schritte · Ø 8.172 pro Tag", "the header averages the same days the chart plots");
});

test("zone distributions list the highest zone first and draw pace zones after power", () => {
  const { trainingFocusZoneSeconds, appendTrainingFocusZones } = extractFunctions("analysis.js", ["trainingFocusZoneSeconds", "appendTrainingFocusZones"], {
    trainingFocusDistribution: (title, readings) => [title, Array.from(readings, (item) => `${item.label}:${item.value}`).join(" ")],
    trainingFocusZoneInfo: () => "",
  });
  const readings = trainingFocusZoneSeconds([{ seconds: { Z1: 60, Z3: 30 } }, { seconds: { Z2: 10, Z5: 5, Z1: 15 } }]);
  assert.deepEqual(Array.from(readings, (item) => [item.label, item.value]), [["Z5", 5], ["Z3", 30], ["Z2", 10], ["Z1", 75]]);
  // The stored run pace-zone seconds are drawn as their own card after power (#2808).
  const zone = (sensor, seconds, zone_model = "local") => ({ sensor, zone_model, seconds, measured_seconds: Object.values(seconds).reduce((a, b) => a + b, 0) });
  const cards = [];
  appendTrainingFocusZones({ zones: [zone("pace", { Z1: 300, Z4: 900 }), zone("power", { Z2: 600 }), zone("heart_rate", { Z2: 1200 }), zone("pace", { Z0: 0 }, "provider")] }, { append: (card) => cards.push(card) });
  assert.deepEqual(cards.map((card) => Array.from(card)), [["HF-Zonen", "Z2:1200"], ["Power-Zonen", "Z2:600"], ["Pace-Zonen", "Z4:900 Z1:300"]]);
});

test("provider metrics carry German labels and the unit points where they are scores", () => {
  const { providerMetricLabel, providerMetricUnit } = extractFunctions("analysis.js", ["providerMetricLabel", "providerMetricUnit"]);
  assert.equal(providerMetricLabel("running_tolerance"), "Lauf-Belastbarkeit");
  assert.equal(providerMetricLabel("endurance_score"), "Ausdauer-Score");
  assert.equal(providerMetricLabel("unknown_metric"), "unknown_metric");
  assert.equal(providerMetricUnit("training_readiness_history"), "Punkte");
  assert.equal(providerMetricUnit("endurance_score"), "Punkte");
  assert.equal(providerMetricUnit("running_tolerance"), "");
});

test("an equipment sport shows its German label, an indoor ride is named, an unknown sport stays as stored", () => {
  const { equipmentSportLabel } = extractFunctions("analysis.js", ["SPORT_LABELS", "equipmentSportLabel"]);
  assert.equal(equipmentSportLabel({ sport: "run" }), "Laufen");
  assert.equal(equipmentSportLabel({ sport: "ride", environment: "outdoor" }), "Rad");
  assert.equal(equipmentSportLabel({ sport: "ride", environment: "virtual" }), "Indoor-Rad");
  assert.equal(equipmentSportLabel({ sport: "kitesurf" }), "kitesurf");
});

test("the coverage line counts the days or weeks that have a value", () => {
  const { analysisCoverageText } = extractFunctions("analysis.js", ["analysisCoverageText"]);
  assert.equal(analysisCoverageText(5, 7, "Tagen"), "Daten für 5 von 7 Tagen vorhanden.");
  assert.equal(analysisCoverageText(3, 12, "Wochen"), "Daten für 3 von 12 Wochen vorhanden.");
});

test("the table behind a chart scrolls in a named region that the keyboard can reach", () => {
  const { makeScrollRegionFocusable } = extractFunctions("shared.js", ["makeScrollRegionFocusable"]);
  const document = fakeDocument();
  const { appendAnalysisTable } = extractFunctions("analysis.js", ["appendAnalysisTable", "analysisValidPoint", "analysisAxisCaption"], {
    document,
    makeScrollRegionFocusable,
    dateLabel: (date) => date,
    analysisPointValue: (_item, point) => String(point.value),
    analysisReadingDetails: () => "",
  });
  const section = new FakeElement("section");
  appendAnalysisTable(section, "Ruhepuls", [{ label: "Garmin", points: [{ date: "2026-10-10", value: 48 }] }], "bpm");
  const region = section.children[0].children.find((child) => child.className === "analysis-chart-table");
  assert.deepEqual([region.tabIndex, region.attributes.role, region.attributes["aria-label"]], [0, "region", "Ruhepuls: Einzelwerte"]);
});

test("the element helpers keep values as text and map class, properties, attributes and data", () => {
  const document = { ...fakeDocument(), createElementNS: (namespace, tag) => Object.assign(new FakeElement(tag), { namespace }) };
  const { h, table, svgNode } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode", "table"], { document });
  const plain = h("p", "muted", "<b>", 0, null, false, ["a", h("i")]);
  assert.deepEqual([plain.className, plain.children.length, plain.children[0], plain.children[1]], ["muted", 4, "<b>", "0"]);
  const button = h("button", { type: "button", className: "x", "aria-expanded": "false", dataset: { zone: "z2" }, title: undefined }, "Mehr");
  assert.deepEqual([button.type, button.className, button.attributes["aria-expanded"], button.dataset.zone, button.title], ["button", "x", "false", "z2", ""]);
  const chip = h("span", "status-chip");
  const grid = table(["Tag", "Wert"], [[h("th", { scope: "row" }, "Mo"), chip], ["Di", 0]], { caption: "Werte" });
  const [caption, head, body] = grid.children;
  assert.deepEqual([caption.tag, head.children[0].children.map((cell) => cell.scope), body.children[0].children.map((cell) => cell.tag)], ["caption", ["col", "col"], ["th", "td"]]);
  assert.equal(body.children[0].children[1].children[0], chip, "a non-cell node is wrapped in a td");
  assert.deepEqual(body.children[1].children.map((cell) => cell.children[0]), ["Di", "0"]);
  const label = svgNode("text", { x: 4 }, 0);
  assert.deepEqual([label.namespace, label.attributes.x, label.textContent], ["http://www.w3.org/2000/svg", "4", 0]);
});

// Runs the real weekly aggregations; results are copied out of the sandbox for literal comparison.
const WEEKLY = ["analysisValidPoint", "analysisWeeklyPoints", "analysisWeeklyPerformancePoints", "analysisWeeklySumPoints", "analysisWeeklyMeanPoints", "analysisWeeklyLastPoints", "bodyWeeklyPoints"];
const weekly = () => extractFunctions("analysis.js", WEEKLY, {
  addDateKey: (key, days) => new Date(Date.parse(`${key}T12:00:00Z`) + days * 86400000).toISOString().slice(0, 10),
});
const plain = (value) => JSON.parse(JSON.stringify(value));

// Mondays 2026-10-05, 10-12 and 10-19; the third week is cut at 10-21.
const WEEKLY_POINTS = [
  { date: "2026-10-05", value: 1.5 }, { date: "2026-10-07", value: 2.5 }, { date: "2026-10-09", value: null }, { date: "2026-10-11", value: 0.3 },
  { date: "2026-10-14", value: "n/a" },
  { date: "2026-10-19", value: 7 }, { date: "2026-10-21", value: 3 },
];

test("weekly sums add the known days per week, keep empty weeks as gaps and cut the last week at the end", () => {
  assert.deepEqual(plain(weekly().analysisWeeklySumPoints(WEEKLY_POINTS, "2026-10-05", "2026-10-21")), [
    { date: "2026-10-11", value: 4.3, observedDate: "2026-10-11" },
    { date: "2026-10-18", value: null },
    { date: "2026-10-21", value: 10, observedDate: "2026-10-21" },
  ]);
});

test("weekly means round to one decimal over the known days of each week", () => {
  assert.deepEqual(plain(weekly().analysisWeeklyMeanPoints(WEEKLY_POINTS, "2026-10-05", "2026-10-21")), [
    { date: "2026-10-11", value: 1.4, observedDate: "2026-10-11" },
    { date: "2026-10-18", value: null },
    { date: "2026-10-21", value: 5, observedDate: "2026-10-21" },
  ]);
});

test("the weekly state keeps the last known point of each week with its fields", () => {
  const points = WEEKLY_POINTS.map((point) => ({ ...point, source: "garmin" }));
  assert.deepEqual(plain(weekly().analysisWeeklyLastPoints(points, "2026-10-05", "2026-10-21")), [
    { date: "2026-10-11", value: 0.3, source: "garmin", observedDate: "2026-10-11" },
    { date: "2026-10-18", value: null },
    { date: "2026-10-21", value: 3, source: "garmin", observedDate: "2026-10-21" },
  ]);
});

test("weekly performance takes the latest reading by date, or the median with its count", () => {
  const points = [
    { date: "2026-10-09", value: 40 }, { date: "2026-10-05", value: 50 }, { date: "2026-10-07", value: 60 }, { date: "2026-10-06", value: null },
    { date: "2026-10-20", value: 30 }, { date: "2026-10-19", value: 10 },
  ];
  assert.deepEqual(plain(weekly().analysisWeeklyPerformancePoints(points, "2026-10-05", "2026-10-21")), [
    { date: "2026-10-11", value: 40, observedDate: "2026-10-09" },
    { date: "2026-10-18", value: null },
    { date: "2026-10-21", value: 30, observedDate: "2026-10-20" },
  ]);
  assert.deepEqual(plain(weekly().analysisWeeklyPerformancePoints(points, "2026-10-05", "2026-10-21", true)), [
    { date: "2026-10-11", value: 50, observedDate: "2026-10-09", count: 3 },
    { date: "2026-10-18", value: null },
    { date: "2026-10-21", value: 20, observedDate: "2026-10-20", count: 2 },
  ]);
});

test("weekly body points keep the last reading with its fields, the reading count and gap weeks", () => {
  const points = [
    { date: "2026-10-05", value: 70.2, observed_at: "2026-10-05T06:00:00Z" }, { date: "2026-10-08", value: 69.8 }, { date: "2026-10-10", value: null },
    { date: "2026-10-20", value: 69.1, observed_at: "2026-10-20T07:00:00Z" },
  ];
  assert.deepEqual(plain(weekly().bodyWeeklyPoints(points, "2026-10-05", "2026-10-21")), [
    { date: "2026-10-11", value: 69.8, weekReadings: 2, observedDate: "2026-10-08" },
    { date: "2026-10-18", value: null, weekReadings: 0 },
    { date: "2026-10-21", value: 69.1, observed_at: "2026-10-20T07:00:00Z", weekReadings: 1, observedDate: "2026-10-20T07:00:00Z" },
  ]);
});

test("the equipment notice names exceeded targets first and never archived items or items without a target", () => {
  const { equipmentStatusItem } = extractFunctions("analysis.js", ["analysisNumber", "analysisPercent", "analysisRatioUnit", "analysisCountUnit", "analysisValue", "equipmentLifetime", "equipmentState", "equipmentStatusItem"], { AppFormat: require("../public/format.js") });
  const local = (name, distance, target, extra = {}) => ({ name, usage: { distance_km: distance, maintenance_due: false }, lifetime_target_km: target, ...extra });
  const quiet = {
    items: [local("Alt", 900, 800, { status: "archived" }), local("Ohne Ziel", 900, null), local("Neu", 100, 800)],
    garmin_items: [{ name: "Garmin ohne Ziel", distance_km: 5000 }, { name: "Garmin alt", distance_km: 900, goal_km: 800, status: "archived" }],
  };
  assert.equal(equipmentStatusItem(quiet), null);
  assert.equal(equipmentStatusItem({}), null);
  const due = {
    items: [local("Kette", 100, 800, { usage: { distance_km: 100, maintenance_due: true } }), ...quiet.items],
    garmin_items: [{ name: "Pegasus 40", distance_km: 900, goal_km: 800 }],
  };
  // Exceeded targets lead; maintenance of a local item follows as "+1" (#2809).
  assert.deepEqual(JSON.parse(JSON.stringify(equipmentStatusItem(due))), { level: "warning", title: "Pegasus 40: Ziel überschritten", detail: "+1", action: { href: "#more/equipment", label: "Ansehen" } });
  assert.equal(equipmentStatusItem({ items: due.items }).title, "Kette: Wartung fällig");
});

test("the watch-upload sentence appears from 24 hours on and never without a known time", () => {
  const { deviceSyncSentence } = extractFunctions("performance-view.js", ["deviceSyncSentence"]);
  const now = Date.parse("2026-11-14T08:00:00Z");
  // #2807: the athlete chose 24 hours; a fresh upload needs no sentence.
  assert.equal(deviceSyncSentence("2026-11-13T08:00:01Z", now), "");
  assert.equal(deviceSyncSentence("2026-11-13T08:00:00Z", now), "Die Uhr hat seit einem Tag nichts übertragen.");
  assert.equal(deviceSyncSentence("2026-11-12T07:00:00Z", now), "Die Uhr hat seit 2 Tagen nichts übertragen.");
  assert.equal(deviceSyncSentence(null, now), "");
  assert.equal(deviceSyncSentence("kein Datum", now), "");
});

test("an empty formula row names its reason and opens the dialog that fills it (#3336)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const opened = [];
  const { energyTable } = extractFunctions("analysis.js", ["ENERGY_SOURCES", "ENERGY_INSUFFICIENT", "ENERGY_STEP_SOURCES", "ENERGY_IMPLAUSIBLE", "ENERGY_IMPLAUSIBLE_REASON", "ENERGY_PARTS", "ENERGY_FORMULAS", "ENERGY_BODY_MISSING", "energyMissingCell", "energyKcal", "energyNeatInputs", "energyBodyText", "energyBasisText", "energyBiasText", "energyInfoId", "energyInfo", "energySplitRow", "energyFlaggedRows", "energyTable"], {
    bindAnalysisPopover: () => {}, h, formatWhole: (v) => String(v), dateLabel: (v) => v,
    analysisValue: (v, unit) => `${v} ${unit}`, createStatusChip: (label) => h("span", null, label),
    openBodyFatDialog: () => opened.push("body fat"), openWeightDialog: () => opened.push("weight"),
  });
  const read = (reason) => ({
    rows: [{ source: "katch_mcardle" }, { source: "cunningham" }, { source: "garmin", resting_kcal: 1700, total_kcal: 2500 }, { source: "own" }],
    basis: { complete_days: 30, days: 30 }, body: null, skinfold_missing: reason, flagged: null,
  });
  const table = energyTable(read("no_skinfold"));
  const rows = table.children[2].children;
  const cell = rows[0].children[1];
  assert.equal(cell.colSpan, 2);
  assert.equal(textOf(cell), "Körperfett fehlt");
  cell.children[0].fire("click");
  assert.deepEqual([...opened], ["body fat"]);
  // Garmin keeps its numbers; the info icon is the chart groups' one, not the yellow badge.
  assert.equal(rows[2].children.length, 3);
  assert.equal(table.children[0].children[1].className, "analysis-legend-info analysis-info-icon");
  const weight = energyTable(read("no_weight")).children[2].children[0].children[1];
  assert.equal(textOf(weight), "Gewicht fehlt");
  weight.children[0].fire("click");
  assert.deepEqual([...opened], ["body fat", "weight"]);
});

test("a body fat fallback names its source on both formula rows (#3362)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const { energyTable, energyBasisText } = extractFunctions("analysis.js", ["ENERGY_SOURCES", "ENERGY_INSUFFICIENT", "ENERGY_STEP_SOURCES", "ENERGY_IMPLAUSIBLE", "ENERGY_IMPLAUSIBLE_REASON", "ENERGY_PARTS", "ENERGY_FORMULAS", "ENERGY_BODY_MISSING", "energyMissingCell", "energyKcal", "energyNeatInputs", "energyBodyText", "energyBasisText", "energyBiasText", "energyInfoId", "energyInfo", "energySplitRow", "energyFlaggedRows", "energyTable"], {
    bindAnalysisPopover: () => {}, h, formatWhole: (v) => String(v), dateLabel: (v) => v,
    analysisValue: (v, unit) => `${v} ${unit}`, createStatusChip: (label) => h("span", null, label),
  });
  const read = {
    rows: [{ source: "katch_mcardle", resting_kcal: 1700, resting_band_kcal: 200, total_kcal: 2500, total_band_kcal: 220 }, { source: "cunningham", resting_kcal: 1850, total_kcal: 2650 }, { source: "device", label: "Garmin Connect" }, { source: "own" }],
    basis: { complete_days: 30, days: 30 }, flagged: null, skinfold_missing: null,
    body: { source: "profile", label: "Profil", date: null, body_fat_pct: 18.5, lean_mass_kg: 61 },
  };
  const rows = energyTable(read).children[2].children;
  assert.equal(textOf(rows[0].children[0]), "Katch-McArdleKFA: Profil");
  assert.equal(textOf(rows[1].children[0]), "CunninghamKFA: Profil");
  assert.equal(textOf(rows[2].children[0]), "Garmin Connect", "the device row names no body fat");
  assert.match(energyBasisText(read), /KFA 18.5 % \(Profil\), fettfreie Masse 61 kg/);
});

test("a diary render with the shown energy read keeps the table and its open popover (#3379)", () => {
  const totals = new FakeElement("div");
  let slot = null;
  let built = 0;
  const context = extractFunctions("analysis.js", ["renderDiaryEnergy"], {
    document: { querySelector: (selector) => (selector === "#nutritionTotals" ? totals : slot) },
    h: (tag, attributes) => Object.assign(new FakeElement(tag), { id: attributes?.id }),
    energyDiaryCards: () => { built += 1; return [new FakeElement("section")]; },
    energyRead: { rows: [] },
  });
  totals.after = (node) => { slot = node; };
  context.renderDiaryEnergy();
  const table = slot.children[0];
  context.renderDiaryEnergy();
  assert.equal(built, 1, "the same read is not rebuilt");
  assert.equal(slot.children[0], table, "the table node stays, so an open popover stays open");
});
