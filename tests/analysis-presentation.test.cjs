const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions, FakeElement, fakeDocument, textOf } = require("./frontend-harness.cjs");

test("h() applies a style object to the element's own style instead of replacing it (#3596)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const node = h("span", { className: "mark", style: { left: "40%", width: "10%" } });
  // A browser drops an object assigned to `style`; the element's own style must carry the values.
  assert.equal(typeof node.style.setProperty, "function", "the element's own style object stays");
  assert.deepEqual([node.style.left, node.style.width], ["40%", "10%"]);
  // A style text still sets the attribute as before.
  assert.equal(h("span", { style: "color: red" }).style, "color: red");
});

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
  // Hours read as h:mm; a negative value that rounds to 0:00 carries no sign (#3933).
  assert.equal(analysisValue(-0.5, "h"), "−0:30 h");
  assert.equal(analysisValue(-0.004, "h"), "0:00 h");
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
  const history = (series, preferred) => ({ body: { preferred_providers: preferred, windows: { "91d": { metrics: { cycling_w_per_kg: series } } } } });
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
    state, document: fakeDocument(), analysisRefresh() {}, analysisHistoryPeriod: "91d", recoveryHistoryPeriod: "28d",
    ANALYSIS_EMPTY_TITLES: { load: "Noch keine Belastungsdaten", recovery: "Noch keine Erholungsdaten" },
    createEmptyState: (title, cause, action) => ({ title, cause, action }),
  });
  const offline = analysisEmptyCard("load");
  assert.deepEqual([offline.title, offline.cause, offline.action.tag, textOf(offline.action), offline.action.href], ["Noch keine Belastungsdaten", "Garmin oder Wahoo ist nicht verbunden.", "a", "Anbindung einrichten", "#more/connections"]);
  state.data = { garmin: { configured: false }, wahoo: { connected: true } };
  assert.equal(analysisDataSourceConnected(state.data), true);
  const connected = analysisEmptyCard("load");
  assert.deepEqual([connected.cause, connected.action.tag, textOf(connected.action), Object.values(connected.action._listenerCounts).reduce((sum, count) => sum + count, 0)], ["Noch keine Daten abgeglichen.", "button", "Jetzt abgleichen", 1]);
  // An empty 4-week window may hide older data, so it offers the 3-month view instead of a sync.
  const shortWindow = analysisEmptyCard("recovery");
  assert.deepEqual([shortWindow.title, shortWindow.cause, textOf(shortWindow.action)], ["Noch keine Erholungsdaten", "Keine Daten in den letzten 4 Wochen.", "3 Monate anzeigen"]);
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

const BODY_OVERVIEW_FUNCTIONS = ["bodySourceShown", "bodyOverviewLatest", "bodyFoldFat", "bodyOverviewReading", "bodyOverviewRow", "energyGoalText", "energyGoalRow", "renderBodyAnalysis"];

function bodyOverviewHarness(root, reads, profile = { body_fat_pct: "18.5" }) {
  return extractFunctions("analysis.js", BODY_OVERVIEW_FUNCTIONS, {
    state: { data: { profile } }, document: { ...fakeDocument(), getElementById: () => root },
    ENERGY_GOAL_NAMES: { lose: "Abnehmen", hold: "Halten", build: "Aufbauen" }, openEnergyGoalDialog: () => {},
    isPreferredSource: (item, preferred) => item.source_provider === preferred,
    analysisValidPoint: (p) => p.value != null, dateLabel: (v) => v, analysisValue: (v, unit) => `${v} ${unit}`,
    analysisPeriodControls: () => new FakeElement("nav"), bodyHistoryPeriod: "91d", bodyMetricChart: () => null,
    analysisPeriod: (key) => ({ key }), trendHistoryFor: () => null, BASE_PERIOD_DAYS: 91,
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
  assert.equal(textOf(shown[1].children[0]), "KörperfettEingetragen · 2026-11-15");
  assert.equal(textOf(shown[1].children[1]), "15 %");
  assert.equal(shown[0].children[2].children[0].label, "Gewicht eintragen");
  assert.equal(shown[1].children[2].children[0].label, "Körperfett eintragen");
  // The fat-free mass is derived from the two values and has no pencil.
  assert.equal(textOf(shown[2].children[0]), "Fettfreie Masse");
  assert.equal(textOf(shown[2].children[1]), "68 kg");
  assert.equal(shown[2].children[2].children.length, 0);
  // The folds win a shared day and an older entry.
  shown = rows([["2026-11-10", 15]]);
  assert.equal(textOf(shown[1].children[0]), "KörperfettEingetragen (Hautfalten) · 2026-11-10");
  assert.equal(textOf(shown[1].children[1]), "13.9 %");
  // Without any own value the row shows a dash (the profile's 18.5 % is not KFA), and no lean mass.
  reads.skinfolds = { measurements: [] };
  shown = rows([]);
  assert.equal(textOf(shown[1].children[1]), "\u2014");
  // Weight, body fat and the energy goal (#3547), no lean mass.
  assert.equal(shown.length, 3);
  assert.equal(textOf(shown[2].children[0]), "Energieziel");
  assert.equal(textOf(shown[2].children[1]), "nicht gesetzt");
  assert.equal(shown[2].children[2].children[0].label, "Energieziel festlegen");
  const unknown = bodyFoldFat({ measurements: [{ date: "2026-11-15", body_fat_pct: null }] }, null);
  assert.equal(`${unknown.text} ${unknown.note}`, "unbekannt ohne Geburtsjahr");
  assert.equal(bodyFoldFat(null, null), null);
  // A failed skinfold read still shows the entered percentage.
  assert.equal(bodyFoldFat({ failed: true }, { date: "2026-11-15", value: 15 }).value, 15);
  assert.equal(bodyFoldFat({ failed: true }, null), null);
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

test("'Offene Kalorien' head the diary's energy card, else a link to the goal (#3547, #3703)", async () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const opened = [];
  const { energyGoalStrip, energyGoalText } = extractFunctions("analysis.js", ["ENERGY_GOAL_NAMES", "energyGoalText", "energyGoalStrip"], {
    h, formatWhole: (v) => String(v), openEnergyGoalDialog: () => opened.push("dialog"),
    AppRouter: { navigate: async (route) => { opened.push(route); } },
  });
  const goal = { goal: "lose", goal_kcal: 500, source: "katch_mcardle", total_kcal: 2400, target_kcal: 1900, eaten_kcal: 1200, remaining_kcal: 700 };
  const read = { day_partial: true, rows: [], goal };
  const strip = energyGoalStrip(read);
  assert.equal(strip.dataset.energyGoal, "lose");
  assert.equal(textOf(strip.children[0]), "Offene Kalorien700 kcal");
  assert.equal(strip.children[1].max, "1900");
  assert.equal(strip.children[1].value, "1200");
  // Exactly two short items: what was eaten and today's target with the goal's wording.
  assert.deepEqual(strip.children[2].children.map(textOf), ["gegessen 1200", "Ziel heute 1900 (500 Defizit)"]);
  // Above the goal, a surplus goal and hold, which names no deficit or surplus.
  const over = energyGoalStrip({ day_partial: false, rows: [], goal: { ...goal, goal: "build", goal_kcal: 300, source: "device", target_kcal: 2700, eaten_kcal: 2900, remaining_kcal: -200 } });
  assert.equal(textOf(over.children[0]), "Offene Kalorien200 kcal über Ziel");
  assert.deepEqual(over.children[2].children.map(textOf), ["gegessen 2900", "Ziel heute 2700 (300 Überschuss)"]);
  const hold = energyGoalStrip({ ...read, goal: { ...goal, goal: "hold", goal_kcal: 0, target_kcal: 2400, remaining_kcal: 1200 } });
  assert.deepEqual(hold.children[2].children.map(textOf), ["gegessen 1200", "Ziel heute 2400"]);
  assert.equal(energyGoalStrip({ ...read, goal: { ...goal, source: null, total_kcal: null, target_kcal: null, remaining_kcal: null } }).dataset.energyGoal, "pending");
  // No goal: the button opens the goal dialog over the diary (#3930).
  const none = energyGoalStrip({ ...read, goal: null });
  assert.equal(textOf(none), "Ziel festlegen");
  none.children[0].fire("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(opened, ["dialog"]);
  assert.equal(energyGoalText({ energy_goal: "lose", energy_goal_kcal: "500" }), "Abnehmen, 500 kcal/Tag");
  assert.equal(energyGoalText({ energy_goal: "hold" }), "Halten");
});

const DEFICIT_CARD_FUNCTIONS = ["energyFatSharePrior", "energyFatShareRule", "DEFICIT_UNCERTAINTY", "DEFICIT_GOAL_STATUS", "deficitDecimal", "deficitSigned", "deficitDay", "deficitSpan", "deficitGoalBar", "deficitBoxes", "deficitText", "ESTIMATE_MISSING", "deficitIntake", "deficitSpanDays", "deficitFatBox", "ANALYSIS_PERIODS", "analysisPeriod", "verlaufPeriod", "verlaufHead", "energyKcalText", "energyFlaggedText", "energyDeficitCard"];

test("Verlauf's deficit card: hero, goal marker, boxes and text agree, measured and the prior alone (#3548, #3555)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const AppFormat = require("../public/format.js");
  const { energyDeficitCard: drawDeficit } = extractFunctions("analysis.js", ["energyKcalRange", "energyKcalSpan", "energyInfoId", "energyInfoButton", ...DEFICIT_CARD_FUNCTIONS], {
    h, AppFormat, formatWhole: (v) => AppFormat.number(Math.round(v), { digits: 0 }),
    bindAnalysisPopover: (button, info) => { button.dataset.popoverTarget = info.id; },
  });
  // The read holds the card per period (#3704); these tests draw the 4 weeks'.
  const energyDeficitCard = ({ deficit, ...read }) => drawDeficit({ ...read, fit_scale: { "28d": { deficit } } }, "28d");
  const measured = { days: 28, deficit_kcal: 565, band_kcal: 235, method: "measured", span_days: 28, weight_start_kg: 91.2, weight_end_kg: 89.2, weight_change_kg: -2.0, fat_pct_start: 15.5, fat_pct_end: 14.1, fat_date_start: "2026-10-07", fat_date_end: "2026-11-13", weight_last_kg: 89.2, fat_change_kg: -1.6, fat_share_pct: 80, fat_share: { share_pct: 80, prior_pct: 70, measured_pct: 28 }, stored_kcal: -15820, uncertainty: "fat_share", total_kcal: 3800, device_total_kcal: 3100, goal: { goal: "lose", goal_kcal: 500, status: "in" } };
  const rows = [{ source: "device", label: "Garmin" }];
  const card = energyDeficitCard({ rows, deficit: measured });
  assert.equal(card.dataset.energyDeficit, "measured");
  // One title row for every Verlauf card: the title left, the period right (#3704).
  assert.equal(textOf(card.children[0]), "Defizit aus Gewicht und Körperfett4 Wochen");
  assert.equal(card.children[0].className, "verlauf-head");
  assert.match(textOf(card.children[1]), /^≈ 565 kcalpro Tag aus den Reserven330–800/);
  assert.equal(textOf(card.children[3]), "Ziel: 500 kcal Defizit · ✓ im Ziel");
  // #3596: the bar's span and markers carry their positions (330–800 with the goal 500 and the
  // estimate 565 on a 230–900 scale), on each element's own style, as a browser applies them.
  const [band, estimate, target] = card.children[2].children;
  const at = (kcal) => `${((kcal - 230) / (900 - 230)) * 100}%`;
  assert.deepEqual([band.style.left, estimate.style.left, target.style.left], [at(330), at(565), at(500)]);
  assert.equal(band.style.width, `calc(${at(800)} - ${at(330)})`);
  assert.ok(Object.hasOwn(band.style, "setProperty") || typeof band.style.setProperty === "function", "the element keeps its own style object");
  const boxes = card.children[4].children.map(textOf);
  // #3598: an end weight that differs from the last weigh-in at the shown precision is named "aktuell".
  const current = energyDeficitCard({ rows, deficit: { ...measured, weight_last_kg: 89.4 } });
  assert.equal(textOf(current.children[4].children[0]), "Gewicht−2,0 kg91,2 → 89,2 kg (aktuell)");
  // #3598: the readings span the phase, so the Körperfett box names its own dates.
  // #3704: Gewicht and Körperfett are built alike: title, change, from → to, dates.
  assert.deepEqual(boxes, ["Gewicht−2,0 kg91,2 → 89,2 kg", "Körperfett−1,4 %-Pkt.15,5 → 14,1 %07.10. → 13.11.", "davon Fett≈ 1,6 kg≈ 80 % · Faustregel 70 %, zu 28 % gemessen", "pro Tag≈ 565 kcal15.820 kcal ÷ 28 Tage"]);
  const dated = energyDeficitCard({ rows, deficit: { ...measured, weight_start_kg: 92.2, weight_end_kg: 88.8, weight_change_kg: -3.4, weight_last_kg: 88.8, weight_date_start: "2026-10-22", weight_date_end: "2026-11-18" } });
  assert.deepEqual(dated.children[4].children.slice(0, 2).map((box) => box.children.map(textOf)), [["Gewicht", "−3,4 kg", "92,2 → 88,8 kg", "22.10. → 18.11."], ["Körperfett", "−1,4 %-Pkt.", "15,5 → 14,1 %", "07.10. → 13.11."]]);
  // #3739: a long period's ends are its edge means, marked "Ø", with their mean dates and no "aktuell".
  const edges = energyDeficitCard({ rows, deficit: { ...measured, days: 91, edge_weighins: [12, 9], weight_start_kg: 93.5, weight_end_kg: 89.1, weight_change_kg: -4.4, weight_last_kg: 88.6, weight_date_start: "2026-08-27", weight_date_end: "2026-11-13" } });
  assert.deepEqual(edges.children[4].children[0].children.map(textOf), ["Gewicht", "−4,4 kg", "Ø 93,5 → Ø 89,1 kg", "27.08. → 13.11."]);
  // #3618 item 4: the weight box shows the fitted change the fat box takes 80 % of (−2,08 → −2,1 and
  // −1,7), not the difference of the rounded ends (−2,0).
  const fitted = energyDeficitCard({ rows, deficit: { ...measured, weight_change_kg: -2.1, fat_change_kg: -1.7 } });
  assert.deepEqual(fitted.children[4].children.slice(0, 3).map(textOf), ["Gewicht−2,1 kg91,2 → 89,2 kg", "Körperfett−1,4 %-Pkt.15,5 → 14,1 %07.10. → 13.11.", "davon Fett≈ 1,7 kg≈ 80 % · Faustregel 70 %, zu 28 % gemessen"]);
  // #3606: two lines, the facts the boxes lack.
  assert.equal(textOf(card.children[5]), "Echter Verbrauch: ≈ 3.800 kcal am Tag (Garmin: 3.100).");
  assert.equal(textOf(card.children[6]), "Unsicher vor allem: Fettanteil (Faustregel und Messungen).");
  // #3684: the prior follows Forbes at the current fat mass; the box and the sentence name it.
  const forbes = energyDeficitCard({ rows, deficit: { ...measured, fat_share: { share_pct: 63, prior_pct: 57, prior_source: "forbes", measured_pct: 28 } } });
  assert.match(textOf(forbes.children[4].children[2]), /Forbes 57 %, zu 28 % gemessen$/);
  assert.equal(textOf(forbes.children[6]), "Unsicher vor allem: Fettanteil (Forbes und Messungen).");
  // #3606: the read's distance class colours the card; without a goal there is none.
  assert.equal(card.dataset.goalDistance, undefined, "the fixture's goal has no distance class yet");
  const far = energyDeficitCard({ rows, deficit: { ...measured, goal: { ...measured.goal, status: "over", distance: "far" } } });
  assert.equal(far.dataset.goalDistance, "far");
  const unknown = energyDeficitCard({ rows, deficit: { ...measured, device_total_kcal: null } });
  assert.equal(textOf(unknown.children[5]), "Echter Verbrauch: ≈ 3.800 kcal am Tag.");
  // #3603 ("Nur Waage"): without logged meals the card keeps the store; the expenditure says why it is missing.
  const scaleOnly = energyDeficitCard({ rows, deficit: { ...measured, total_kcal: null, expenditure_missing: "intake_days" } });
  assert.match(textOf(scaleOnly.children[1]), /^≈ 565 kcal/);
  assert.equal(textOf(scaleOnly.children[5]), "Echter Verbrauch: braucht erfasste Mahlzeiten.");
  // #3604: the intake from weight and activity, labelled an estimate, with its span and calibration;
  // without one the reason.
  const estimated_intake = { kcal: 2100, band_kcal: 230, calibration_end: "2026-09-19", calibration_age_days: 56, missing: null };
  const estimated = energyDeficitCard({ rows, deficit: { ...measured, total_kcal: null }, estimated_intake });
  assert.equal(textOf(estimated.children[6]), "Geschätzte Aufnahme aus Gewicht und Aktivität: ≈ 2.100 kcal am Tag (1.870–2.330) (erfasst bis 19.09.).");
  const stale = { ...estimated_intake, kcal: null, band_kcal: null, missing: "calibration_age" };
  const none = energyDeficitCard({ rows, deficit: { ...measured, total_kcal: null }, estimated_intake: stale });
  assert.equal(textOf(none.children[6]), "Geschätzte Aufnahme: keine Schätzung, letzte Erfassung zu lange her (erfasst bis 19.09.).");
  assert.equal(none.children[6].dataset.estimatedIntake, "calibration_age");
  // #3614: an unstable window is an estimate and names the weight step.
  const unstable = energyDeficitCard({ rows, deficit: { ...measured, uncertainty: "unstable" } });
  assert.equal(textOf(unstable.children[5]), "Geschätzter Verbrauch: ≈ 3.800 kcal am Tag (Garmin: 3.100).");
  assert.equal(textOf(unstable.children[6]), "Unsicher vor allem: Wasser im Gewicht, die Wochen widersprechen sich.");
  // The prior alone: one measurement, the rule of thumb's share, a surplus, and no goal.
  const prior = { ...measured, method: "prior", deficit_kcal: -300, stored_kcal: 8400, weight_start_kg: 70, weight_end_kg: 71, weight_change_kg: 1, weight_last_kg: 71, fat_pct_start: null, fat_date_start: null, fat_pct_end: 12, fat_change_kg: 0.7, fat_share_pct: 70, fat_share: { share_pct: 70, prior_pct: 70, measured_pct: 0 }, goal: null };
  const surplus = energyDeficitCard({ rows, deficit: prior });
  assert.equal(textOf(surplus.children[0]), "Defizit aus Gewicht und Körperfett4 Wochen");
  assert.match(textOf(surplus.children[1]), /pro Tag in die Speicher/);
  assert.deepEqual(surplus.children[2].children.map(textOf), ["Gewicht+1,0 kg70,0 → 71,0 kg", "Körperfett–nur eine Messungvom 13.11.","davon Fett≈ 0,7 kg≈ 70 % · Faustregel 70 %", "pro Tag≈ 300 kcal8.400 kcal ÷ 28 Tage"]);
  assert.equal(textOf(surplus.children[3]), "Echter Verbrauch: ≈ 3.800 kcal am Tag (Garmin: 3.100).");
  assert.equal(surplus.dataset.goalDistance, undefined, "no goal, no distance class");
  // Two readings that do not weigh yet keep the rule of thumb but are not "missing" (Gitar on #3581).
  const light = energyDeficitCard({ rows, deficit: { ...measured, method: "prior", fat_share: { share_pct: 70, prior_pct: 70, measured_pct: 0 } } });
  assert.equal(textOf(light.children[4].children[1]), "Körperfett−1,4 %-Pkt.15,5 → 14,1 %07.10. → 13.11.");
  // The flagged-meal values of the latest 28 days sit behind the card's "i" (#3704).
  const flaggedRead = { rows, flagged: { days: 2, unflagged: { total_kcal: 2650, total_band_kcal: 130, end: "2026-10-24" }, corrected: null, bias_pct: null } };
  const [, info, tooltip] = energyDeficitCard({ ...flaggedRead, deficit: measured }).children[0].children;
  assert.equal(textOf(info), "i");
  assert.equal(textOf(tooltip), "Verbrauch, Ø 28 Tage, ohne geschätzte Tage (bis 24.10.): 2.650 kcal (2.520–2.780) · mit gelernter Korrektur: noch nicht gelernt");
  assert.equal(energyDeficitCard({ ...flaggedRead, deficit: measured }).children.length, card.children.length, "no table under the card");
  assert.equal(energyDeficitCard({ rows, deficit: measured, flagged: { days: 0 } }).children[0].children.length, 2, "no flagged day, no i");
  // A span reaching past 0 into a surplus is signed, not clamped (Gitar on #3562).
  const small = energyDeficitCard({ rows, deficit: { ...measured, deficit_kcal: 100, band_kcal: 235 } });
  assert.match(textOf(small.children[1]), /−135 bis 335zwischen −135 und 335 kcal$/);
  assert.equal(energyDeficitCard({ rows, deficit: null }), null);
});

const ENERGY_CARD_FUNCTIONS = ["ENERGY_INSUFFICIENT", "ENERGY_STEP_SOURCES", "ENERGY_PARTS", "ENERGY_LEARNED", "energyRowParts", "FIT_SOURCE_NAMES", "energyFitSigned", "energyKcalRange", "energyKcalSpan", "energyKcal", "energyNeatInputs", "energyBodyText", "energyBasisText", "energyBiasText", "energyInfoId", "ENERGY_DAY_NOTE", "energyInfoButton", "energyStandNote", "energyTefNote", "energyInfo", "energySplitValue", "energyStrengthNote", "energyPartText", "energySplitBar", "energySourceLine", "energyGuardNotes", "energyUsage"];
const ENERGY_CARD_GLOBALS = (h) => ({
  bindAnalysisPopover: (button, info) => { button.dataset.popoverTarget = info.id; },
  h, formatWhole: (v) => String(v), dateLabel: (v) => v, analysisValue: (v, unit) => `${v} ${unit}`, AppFormat: require("../public/format.js"),
});

test("the diary's usage card draws the outlined source's parts, its day total and the info behind the small i (#3252, #3703)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const { energyUsage, energyBasisText, energyDeficitCard } = extractFunctions("analysis.js", [...ENERGY_CARD_FUNCTIONS, ...DEFICIT_CARD_FUNCTIONS], ENERGY_CARD_GLOBALS(h));
  const split = { resting_kcal: 1839, neat_kcal: 250, sport_kcal: 350, tef_kcal: 250 };
  const read = {
    end: "2026-11-14", windows: ["2026-11-07", "2026-11-14"], best: "katch_mcardle", outlined: "katch_mcardle", day_partial: true,
    fit: { shared_windows: 4, windows: 12, min_windows: 4, reference_band_kcal: 120 },
    rows: [
      { source: "katch_mcardle", resting_kcal: 1839, resting_band_kcal: 219, total_kcal: 2689, total_band_kcal: 227, errors_kcal: [null, 11], mean_error_kcal: 11, split },
      { source: "cunningham", resting_kcal: 1996, resting_band_kcal: 233, total_kcal: 2846, total_band_kcal: 241, errors_kcal: [null, 146], mean_error_kcal: 146 },
      { source: "device", label: "Synthetic Watch", resting_kcal: 1745, total_kcal: 2300, errors_kcal: [null, null], mean_error_kcal: null, split: { resting_kcal: 1745, neat_kcal: 300, sport_kcal: 255, tef_kcal: null } },
      { source: "own", resting_kcal: 1850, resting_band_kcal: 150, total_kcal: 2700, total_band_kcal: 120 },
    ],
    basis: { days: 28, complete_days: 24, insufficient: null },
    body: { source: "skinfold", label: "Hautfalten", date: "2026-11-04", body_fat_pct: 15, lean_mass_kg: 68 },
  };
  const card = energyUsage(read);
  // #3252: how the values come about sits behind the small "i", beside the heading.
  const [title, info, tooltip] = card.children[0].children;
  assert.equal(textOf(title), "Verbrauch heute · bis jetzt");
  assert.equal(textOf(info), "i");
  assert.equal(info.dataset.popoverTarget, tooltip.id);
  assert.match(textOf(tooltip), /Ruhe aus deinen Daten ist ein Rest/);
  assert.match(textOf(tooltip), /Offene Kalorien zieht das Gegessene/);
  assert.doesNotMatch(textOf(tooltip), /Basis/);
  // #3478: a band shows as the estimate with its low–high span below, spoken as "zwischen … und … kcal".
  const span = (value, low, high) => `${value}${low}–${high}zwischen ${low} und ${high} kcal`;
  const [, bar, parts, source] = card.children;
  assert.deepEqual(parts.children.map(textOf), ["Ruhe1839", "Schritte & Treppen250", "Sport350", "Verdauung250", `Tag gesamt${span(2689, 2462, 2916)}`]);
  assert.deepEqual([...bar.children].map((part) => part.dataset.part), ["resting", "neat", "sport", "tef"]);
  const hidden = parts.children.at(-1).descendants().filter((node) => node.attributes["aria-hidden"] === "true");
  assert.deepEqual(hidden.map(textOf), ["2462–2916"], "the visible span is read once, in words");
  assert.equal(textOf(source), "Quelle: Katch-McArdle");
  // #3917: today's expected total is its own row directly under "Tag gesamt", for today and when known only.
  const expected = energyUsage({ ...read, expected_total_kcal: 3050 });
  assert.deepEqual(expected.children[2].children.map(textOf).slice(-2), [`Tag gesamt${span(2689, 2462, 2916)}`, "Tag erwartet≈ 3050"]);
  assert.equal(expected.children[2].children.at(-1).className, "energy-expected");
  assert.equal(expected.children[2].children.length, parts.children.length + 1);
  for (const unknown of [{ expected_total_kcal: null }, {}, { expected_total_kcal: 3050, day_partial: false }]) {
    assert.equal(energyUsage({ ...read, ...unknown }).children[2].children.length, parts.children.length, JSON.stringify(unknown));
  }
  // #3917: the notes are the card's last lines after the source line, only past their line.
  const availability = { kcal_per_kg_ffm: 27, days: 6, floor_kcal_per_kg_ffm: 30, below: true };
  const loss = { pct_per_week: 1.3, limit_pct_per_week: 1, fast: true };
  const noted = energyUsage({ ...read, energy_availability: availability, loss_rate: loss });
  assert.deepEqual(noted.children.slice(3).map(textOf), ["Quelle: Katch-McArdle", "Energieverfügbarkeit (7 Tage) 27 kcal/kg FFM · unter 30", "Gewicht fällt 1,3 %/Woche · schneller als 1 %"]);
  const calm = energyUsage({ ...read, energy_availability: { ...availability, kcal_per_kg_ffm: 31, below: false }, loss_rate: { ...loss, pct_per_week: 0.8, fast: false } });
  assert.equal(calm.children.length, 4, "above the line the card has no note");
  // The wearable names itself by the read's label (#3323) and has no digestion part.
  const watch = energyUsage({ ...read, outlined: "device", day_partial: false });
  assert.equal(textOf(watch.children[0].children[0]), "Verbrauch");
  assert.deepEqual(watch.children[2].children.map(textOf), ["Ruhe1745", "Schritte & Treppen300", "Sport255", "Verdauung–", "Tag gesamt2300"]);
  assert.equal(textOf(watch.children[3]), "Quelle: Synthetic Watch");
  // No outlined source, or one without parts: the heading and a short line, no table of sources.
  for (const outlined of [null, "cunningham"]) {
    const empty = energyUsage({ ...read, outlined });
    assert.deepEqual(empty.children.map(textOf).slice(1), ["Noch keine Quelle"]);
  }
  const note = energyBasisText(read);
  assert.match(note, /24 von 28 Tagen vollständig .* KFA 15 % \(Hautfalten vom 2026-11-04\), fettfreie Masse 68 kg/);
  assert.match(note, /Bänder nominal 95 %, ohne Fehler beim Erfassen des Essens/, "#3187 item 6");
  assert.match(note, /Ruhe aus deinen Daten ist ein Rest/, "#3187 item 8");
  assert.match(energyBasisText({ ...read, basis: { insufficient: "weighins" }, body: null }), /Datenbasis zu gering \(unter 8 Wiegungen\).*Ohne Körperfettwert/);
  // #3255, #3252: NEAT's inputs join the basis behind the one "i", never a line under the card.
  const neat = { days: 27, steps_per_day: 14200, session_steps_per_day: 3100, floors_per_day: 9.5, step_length_m: 0.78, step_length_source: "height" };
  const withNeat = { ...read, basis: { ...read.basis, neat } };
  assert.doesNotMatch(energyBasisText(withNeat), /Schritten/, "the basis text itself carries no NEAT inputs");
  const [, neatButton, neatInfo] = energyUsage(withNeat).children[0].children;
  assert.match(textOf(neatInfo), /Ruhe aus deinen Daten ist ein Rest .* · Schritte & Treppen aus 14200 Schritten\/Tag, davon 3100 in Einheiten abgezogen, Schrittlänge 0.78 m \(aus Körpergröße\), 9.5 Etagen ohne Einheiten$/);
  assert.equal(neatButton.dataset.popoverTarget, neatInfo.id);
  // A kcal value is never negative, so a band wider than its value starts at 0.
  const wide = { ...read, rows: [{ ...read.rows[0], total_kcal: 120, total_band_kcal: 300 }] };
  assert.equal(textOf(energyUsage(wide).children[2].children.at(-1)), `Tag gesamt${span(120, 0, 420)}`);
  // #3100: with flagged meals, the clean-window total and the learned correction sit under the
  // 28-day deficit card in Verlauf (#3548); the per-entry bias joins the popover.
  const flagged = { days: 2, unflagged: { total_kcal: 2650, total_band_kcal: 130, end: "2026-10-24" }, corrected: { total_kcal: 2760, total_band_kcal: 160 }, bias_pct: 12, bias_band_pct: 9 };
  const deficit = { days: 28, deficit_kcal: 565, band_kcal: 235, method: "measured", span_days: 28, weight_start_kg: 91.2, weight_end_kg: 89.2, weight_change_kg: -2.0, fat_pct_start: 15.5, fat_pct_end: 14.1, fat_change_kg: -1.6, fat_share_pct: 80, fat_share: { share_pct: 80, prior_pct: 70, measured_pct: 28 }, stored_kcal: -15820, uncertainty: "fat_share", total_kcal: 3800, device_total_kcal: 3100, goal: null };
  const flaggedInfoText = (flags) => textOf(energyDeficitCard({ ...read, fit_scale: { "28d": { deficit } }, flagged: flags }, "28d").children[0].children[2]);
  assert.equal(flaggedInfoText(flagged), "Verbrauch, Ø 28 Tage, ohne geschätzte Tage (bis 24.10.): 2650 kcal (2520–2780) · mit gelernter Korrektur: 2760 kcal (2600–2920)");
  assert.match(flaggedInfoText({ ...flagged, unflagged: null, corrected: null }), /: kein sauberer Zeitraum · /);
  // #3616: a learned bias without the latest total shows "–"; until it is learned the row says so.
  assert.match(flaggedInfoText({ ...flagged, corrected: null }), /mit gelernter Korrektur: –$/);
  assert.match(flaggedInfoText({ ...flagged, corrected: null, bias_pct: null, bias_band_pct: null }), /mit gelernter Korrektur: noch nicht gelernt$/);
  // The diary's day card carries no 28-day row (#3531).
  assert.equal(energyUsage({ ...read, flagged }).children[2].children.length, energyUsage(read).children[2].children.length);
  assert.equal(energyDeficitCard(read, "28d"), null, "no store estimate, no card");
  const [, , flaggedInfo] = energyUsage({ ...read, flagged }).children[0].children;
  assert.match(textOf(flaggedInfo), /Geschätzte Mahlzeiten wirken im Mittel 12 % höher als erfasst \(± 9 %\)$/);
});

test("the usage card names each part, its note and what has no data yet, with the learned term only while it applies (#3703)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const { energyUsage, energyStrengthNote } = extractFunctions("analysis.js", [...ENERGY_CARD_FUNCTIONS], ENERGY_CARD_GLOBALS(h));
  const split = (resting, neat, sport, tef, extra = {}) => ({ resting_kcal: resting, neat_kcal: neat, sport_kcal: sport, tef_kcal: tef, ...extra });
  const usage = (row, read = {}) => energyUsage({ day_partial: true, outlined: row.source, rows: [{ total_kcal: 3000, ...row }], basis: { complete_days: 1, days: 1 }, ...read });
  const lines = (card) => card.children[2].children.map(textOf);
  const cunningham = { source: "cunningham", split: split(1839, 220, 350, 250) };
  assert.deepEqual(lines(usage(cunningham)), ["Ruhe1839", "Schritte & Treppen220", "Sport350", "Verdauung250", "Tag gesamt3000"], "no learned term, no Übriger Alltag row");
  // #3577, #3689: the learned term follows Schritte & Treppen, signed, and is its own bar segment and source line.
  const learned = { ...cunningham, split: split(1839, 220, 350, 250, { learned_kcal: 480 }) };
  const drawn = usage(learned);
  assert.deepEqual(lines(drawn), ["Ruhe1839", "Schritte & Treppen220", "Übriger Alltag+480", "Sport350", "Verdauung250", "Tag gesamt3000"]);
  assert.deepEqual([...drawn.children[1].children].map((part) => part.dataset.part), ["resting", "neat", "learned", "sport", "tef"]);
  assert.equal(drawn.children[1].children[0].style.width, `${(1839 / 3139) * 100}%`);
  assert.equal(textOf(drawn.children[3]), "Quelle: Cunningham mit übrigem Alltag");
  const reduced = usage({ ...cunningham, split: split(1839, 220, 350, 250, { learned_kcal: -106 }) });
  assert.equal(lines(reduced)[2], "Übriger Alltag−106");
  assert.deepEqual([...reduced.children[1].children].map((part) => part.dataset.part), ["resting", "neat", "sport", "tef"], "a reduction has no segment");
  // #3784 review: nor does it shrink the spread, so the drawn segments fill the bar exactly.
  const widths = [...reduced.children[1].children].map((part) => Number.parseFloat(part.style.width));
  assert.ok(Math.abs(widths.reduce((sum, width) => sum + width, 0) - 100) < 1e-9, String(widths));
  // #3723: until a qualified term is taken over, the row counts the provisional estimate and says so.
  const provisional = usage({ ...cunningham, split: split(1839, 220, 350, 250, { learned_kcal: 172, learned_provisional: true }) });
  assert.equal(lines(provisional)[2], "Übriger Alltag (vorläufig)+172");
  assert.match(provisional.children[1].attributes["aria-label"], / · Übriger Alltag \(vorläufig\) \+172 · /);
  // #3530, #3719: Garmin's day summary ends at 12:18; the row shows the number only, the "i" names the time
  // and, behind a later session, also that.
  const info = (card) => textOf(card.children[0].children[2]);
  const device = { source: "device", label: "Garmin Connect", split: split(1745, 610, 765, null) };
  const synced = usage(device, { wellness: { as_of: "12:18", behind: false } });
  assert.equal(lines(synced)[1], "Schritte & Treppen610");
  assert.match(info(synced), / · Schritte & Treppen nach dem letzten Garmin-Abruf, Stand 12:18$/);
  const waiting = usage({ ...device, pending_parts: ["neat"] }, { wellness: { as_of: "12:18", behind: true } });
  assert.equal(lines(waiting)[1], "Schritte & Treppennoch keine Daten");
  assert.match(info(waiting), /Stand 12:18, Garmin hängt hinterher/);
  // #3784: the bar draws no segment for a part whose row says "noch keine Daten", and spreads only
  // the counted parts; its spoken label still names the pending one.
  const segments = [...waiting.children[1].children];
  assert.deepEqual(segments.map((part) => part.dataset.part), ["resting", "sport"]);
  assert.equal(segments[0].style.width, `${(1745 / (1745 + 765)) * 100}%`);
  assert.match(waiting.children[1].attributes["aria-label"], /Schritte & Treppen noch keine Daten/);
  assert.deepEqual([...synced.children[1].children].map((part) => part.dataset.part), ["resting", "neat", "sport"]);
  assert.equal(lines(usage(device, { wellness: null }))[1], "Schritte & Treppen610");
  assert.doesNotMatch(info(usage(device, { wellness: null })), /Garmin-Abruf/);
  // #3530: a strength session's RPE and the MET it set follow the Sport part, on the formula rows only.
  const note = energyStrengthNote([{ rpe: 8, met: 5, kcal: 210 }, { rpe: null, met: 3.5, kcal: 131 }]);
  assert.equal(note, "Kraft RPE 8, MET 5; Kraft ohne RPE, MET 3,5");
  assert.equal(lines(usage(cunningham, { strength: [{ rpe: 8, met: 5, kcal: 210 }] }))[2], "Sport350 (Kraft RPE 8, MET 5)");
  assert.equal(lines(usage(device, { strength: [{ rpe: 8, met: 5, kcal: 210 }] }))[2], "Sport765");
  assert.equal(energyStrengthNote(undefined), "");
  // #3617 item 3, #3719: digestion of kcal without macros is estimated; the "i" says so, the row shows the number.
  const estimated = usage(cunningham, { tef_estimated: true });
  assert.equal(lines(estimated)[3], "Verdauung250");
  assert.match(info(estimated), / · Verdauung geschätzt, wo Mahlzeiten keine Makros haben$/);
  assert.doesNotMatch(info(usage(cunningham)), /Verdauung geschätzt/);
  assert.doesNotMatch(info(usage(device, { tef_estimated: true })), /Verdauung geschätzt/, "Garmin has no digestion part");
  // #3494: today a part whose input has not arrived counts 0 and says so; an unknown part stays "–" (Gitar #3495),
  // a missing digestion reads "nicht erfasst", Garmin has none.
  const early = (source, pending, parts) => ({ source, pending_parts: pending, split: split(1800, ...parts) });
  assert.deepEqual(lines(usage(early("katch_mcardle", ["neat", "sport", "tef"], [0, 0, 0]))).slice(0, 4), ["Ruhe1800", "Schritte & Treppennoch keine Daten", "Sportnoch keine Daten", "Verdauungnoch keine Daten"]);
  assert.deepEqual(lines(usage(early("device", ["neat", "sport"], [null, null, null]))).slice(0, 4), ["Ruhe1800", "Schritte & Treppennoch keine Daten", "Sportnoch keine Daten", "Verdauung–"]);
  assert.deepEqual(lines(usage(early("katch_mcardle", [], [null, null, null]))).slice(0, 4), ["Ruhe1800", "Schritte & Treppen–", "Sport–", "Verdauungnicht erfasst"]);
  // A part still waiting for its data shows only that.
  assert.equal(lines(usage({ ...early("katch_mcardle", ["tef"], [0, 0, 0]), source: "katch_mcardle" }, { tef_estimated: true }))[3], "Verdauungnoch keine Daten");
  // The bar names the same parts for a screen reader.
  assert.equal(drawn.children[1].attributes["aria-label"], "Ruhe 1839 · Schritte & Treppen 220 · Übriger Alltag +480 · Sport 350 · Verdauung 250");
});

test("the learned everyday line offers the check, then names its date (#3577)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const { energyLearnedLine } = extractFunctions("analysis.js", ["ENERGY_LEARNED_FORMULAS", "energyLearnedDeferred", "energyKcalRange", "energyFitSigned", "energyLearnedLine"], {
    h, formatWhole: (v) => String(v), dateLabel: (v) => `am-${v}`, todayIso: () => "2026-11-19", saveLearnedCheck: () => {}, renderDiaryEnergy: () => {},
  });
  const fit = (kcal, band, qualified = true) => ({ kcal, raw_kcal: kcal, band_kcal: band, windows: 4, qualified });
  const offered = energyLearnedLine({ learned_everyday: { katch_mcardle: fit(480, 180), cunningham: fit(330, 180), checked: null } });
  assert.equal(textOf(offered.children[0]), "Übriger Alltag aus 4 FensternKatch-McArdle +480 kcal/Tag (300–660) · Cunningham +330 (150–510)");
  assert.deepEqual([...offered.children[1].children].map((button) => textOf(button)), ["Übernehmen", "Noch nicht"]);
  const checked = energyLearnedLine({ learned_everyday: { katch_mcardle: fit(480, 180), cunningham: null, checked: "2026-11-19" } });
  assert.equal(textOf(checked), "Übriger Alltag übernommen am am-2026-11-19aus 4 Fenstern, wird mit jedem Fenster genauer");
  // #3723: without any estimate the term is not learned yet and counts 0.
  assert.equal(textOf(energyLearnedLine({ learned_everyday: { katch_mcardle: null, cunningham: null, checked: null } })), "Übriger Alltag noch nicht gelernt");
  // #3689: an overshooting formula's term is a reduction, signed.
  const reduced = energyLearnedLine({ learned_everyday: { katch_mcardle: fit(-106, 256), cunningham: null, checked: null } });
  assert.match(textOf(reduced.children[0]), /Katch-McArdle −106 kcal\/Tag \(-362–150\)$/);
  // #3689: until it qualifies it is not offered, also after an earlier check; the row already
  // shows it as "vorläufig" (#3723), so the line has nothing to add.
  for (const checkedOn of [null, "2026-11-19"]) {
    const unready = energyLearnedLine({ learned_everyday: { katch_mcardle: fit(172, 439, false), cunningham: fit(90, 439, false), checked: checkedOn } });
    assert.equal(unready, null);
  }
});

test("a reset drops the energy read in flight, so a pre-save answer is never shown", async () => {
  // Gitar #3180: a reused in-flight read used to write the view from before the skinfold save.
  const answers = [];
  let renders = 0;
  const fns = extractFunctions("analysis.js", ["energyRead", "energyRequest", "energyGeneration", "energyReadDay", "energyDay", "resetEnergy", "loadEnergy"], {
    api: () => new Promise((resolve) => answers.push(resolve)),
    renderBodyAnalysis: () => { renders += 1; }, renderDiaryEnergy: () => { renders += 1; }, renderNutritionFit: () => {},
    state: { data: {} },
    createLoadingState: () => ({}), h: () => ({}),
    document: { querySelector: () => null },
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
  assert.deepEqual([label("run_lt1_pace_seconds_per_km"), label("run_lt1_hr_bpm"), label("cycling_lt1_hr_bpm"), label("cycling_lt1_watts")], ["Aerobe Schwelle (LT1) · Pace", "Aerobe Schwelle (LT1) · Herzfrequenz", "Aerobe Schwelle (LT1) · Herzfrequenz", "Aerobe Schwelle (LT1) · Leistung"]);
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
  assert.deepEqual([weeks[1].date, weeks[1].value, weeks[1].weekReadings], ["2026-10-31", 14.9, 3], "the week sits on its start and shows its last reading, not the median 15.2");
  assert.equal(analysisPlottable({ points: weeks, minPoints: 1 }), true, "body charts draw one weekly value");
  assert.equal(analysisPlottable({ points: weeks }), false, "other charts still need two values");
});

test("the seven-day ln(RMSSD) note shows the back-transformed mean, range and spread", () => {
  const { recoveryRollingNote } = extractFunctions("analysis.js", ["analysisRatioUnit", "analysisCountUnit", "analysisNumber", "analysisValue", "recoveryRollingNote"], { AppFormat: require("../public/format.js") });
  assert.equal(
    recoveryRollingNote({ status: "ok", mean_ms: 51.9, lower_ms: 53, upper_ms: 62.2, position: "below", cv_pct: 3, window_nights: 5 }),
    "7-Tage-Mittel 51,9 ms (5 Nächte) · unter deinem Bereich 53 ms–62,2 ms · Schwankung 3,0 %",
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
  assert.deepEqual(cards.map((card) => Array.from(card)), [["Herzfrequenzzonen", "Z2:1200"], ["Leistungszonen", "Z2:600"], ["Pace-Zonen", "Z4:900 Z1:300"]]);
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
  const { appendAnalysisTable } = extractFunctions("analysis.js", ["appendAnalysisTable", "analysisTableValue", "ANALYSIS_TABLE_ROWS", "analysisValidPoint", "analysisAxisCaption"], {
    document,
    makeScrollRegionFocusable,
    dateLabel: (date) => date,
    analysisPointValue: (_item, point) => String(point.value),
    analysisReadingDetails: () => "",
  });
  const section = new FakeElement("section");
  appendAnalysisTable(section, "Ruhepuls", [{ label: "Garmin", points: [{ date: "2026-10-10", value: 48 }] }], "bpm");
  const region = section.children[0].children.find((child) => child.className === "analysis-chart-table analysis-series-table");
  assert.deepEqual([region.tabIndex, region.attributes.role, region.attributes["aria-label"]], [0, "region", "Ruhepuls: Einzelwerte"]);
});

// A-03 (#3928): real columns, the newest week first, the last six rows and the rest on request.
test("a weekly table has the columns Woche ab, Wert, Messungen and Quelle, newest first, and opens its older rows on request", () => {
  const { makeScrollRegionFocusable } = extractFunctions("shared.js", ["makeScrollRegionFocusable"]);
  const { appendAnalysisTable } = extractFunctions("analysis.js", ["appendAnalysisTable", "analysisTableValue", "ANALYSIS_TABLE_ROWS", "analysisValidPoint", "analysisAxisCaption"], {
    document: fakeDocument(),
    makeScrollRegionFocusable,
    dateLabel: (date) => date,
    analysisPointValue: (_item, point) => `${point.value} kg`,
    analysisReadingDetails: (point) => (point.count ? ` · ${point.count} Messungen` : "") + (point.observedDate && point.observedDate !== point.date ? ` · letzter Messwert ${point.observedDate}` : ""),
  });
  const dates = ["2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"];
  const points = dates.map((date, index) => ({ date, value: 70 + index, count: 3, observedDate: "2026-11-01" }));
  const section = new FakeElement("section");
  appendAnalysisTable(section, "Gewicht", [{ label: "Gewicht", source: "Garmin Connect", cadenceDays: 7, points }], "kg");
  const region = section.children[0].children[1];
  const [, head, body] = region.children[0].children;
  assert.deepEqual(head.children[0].children.map((cell) => cell.textContent), ["Woche ab", "Wert", "Messungen", "Quelle"]);
  const rows = body.children.map((row) => row.children.map((cell) => cell.textContent));
  assert.deepEqual(rows[0], [dates[7], "77 kg", "3", "Garmin Connect"], "the newest week is first, its count and source in their own cells");
  assert.deepEqual(body.children.map((row) => Boolean(row.hidden)), [false, false, false, false, false, false, true, true]);
  const more = region.children[1];
  assert.equal(more.textContent, "Alle 8 Wochen anzeigen");
  more.fire("click");
  assert.ok(body.children.every((row) => !row.hidden));
  assert.equal(body.children[6].children[0].focused, true, "the focus moves to the first row that appeared");
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
    { date: "2026-10-05", value: 4.3, observedDate: "2026-10-11" },
    { date: "2026-10-12", value: null },
    { date: "2026-10-19", value: 10, observedDate: "2026-10-21" },
  ]);
});

test("weekly means round to one decimal over the known days of each week", () => {
  assert.deepEqual(plain(weekly().analysisWeeklyMeanPoints(WEEKLY_POINTS, "2026-10-05", "2026-10-21")), [
    { date: "2026-10-05", value: 1.4, observedDate: "2026-10-11", count: 3 },
    { date: "2026-10-12", value: null },
    { date: "2026-10-19", value: 5, observedDate: "2026-10-21", count: 2 },
  ]);
});

test("the weekly state keeps the last known point of each week with its fields", () => {
  const points = WEEKLY_POINTS.map((point) => ({ ...point, source: "garmin" }));
  assert.deepEqual(plain(weekly().analysisWeeklyLastPoints(points, "2026-10-05", "2026-10-21")), [
    { date: "2026-10-05", value: 0.3, source: "garmin", observedDate: "2026-10-11" },
    { date: "2026-10-12", value: null },
    { date: "2026-10-19", value: 3, source: "garmin", observedDate: "2026-10-21" },
  ]);
});

test("weekly performance takes the latest reading by date, or the median with its count", () => {
  const points = [
    { date: "2026-10-09", value: 40 }, { date: "2026-10-05", value: 50 }, { date: "2026-10-07", value: 60 }, { date: "2026-10-06", value: null },
    { date: "2026-10-20", value: 30 }, { date: "2026-10-19", value: 10 },
  ];
  assert.deepEqual(plain(weekly().analysisWeeklyPerformancePoints(points, "2026-10-05", "2026-10-21")), [
    { date: "2026-10-05", value: 40, observedDate: "2026-10-09" },
    { date: "2026-10-12", value: null },
    { date: "2026-10-19", value: 30, observedDate: "2026-10-20" },
  ]);
  assert.deepEqual(plain(weekly().analysisWeeklyPerformancePoints(points, "2026-10-05", "2026-10-21", true)), [
    { date: "2026-10-05", value: 50, observedDate: "2026-10-09", count: 3 },
    { date: "2026-10-12", value: null },
    { date: "2026-10-19", value: 20, observedDate: "2026-10-20", count: 2 },
  ]);
});

test("weekly body points keep the last reading with its fields, the reading count and gap weeks", () => {
  const points = [
    { date: "2026-10-05", value: 70.2, observed_at: "2026-10-05T06:00:00Z" }, { date: "2026-10-08", value: 69.8 }, { date: "2026-10-10", value: null },
    { date: "2026-10-20", value: 69.1, observed_at: "2026-10-20T07:00:00Z" },
  ];
  assert.deepEqual(plain(weekly().bodyWeeklyPoints(points, "2026-10-05", "2026-10-21")), [
    { date: "2026-10-05", value: 69.8, weekReadings: 2, observedDate: "2026-10-08" },
    { date: "2026-10-12", value: null, weekReadings: 0 },
    { date: "2026-10-19", value: 69.1, observed_at: "2026-10-20T07:00:00Z", weekReadings: 1, observedDate: "2026-10-20T07:00:00Z" },
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

test("a body fat fallback names its source in the info (#3362)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const { energyBasisText } = extractFunctions("analysis.js", ENERGY_CARD_FUNCTIONS, ENERGY_CARD_GLOBALS(h));
  const read = {
    rows: [{ source: "katch_mcardle" }], basis: { complete_days: 30, days: 30 }, flagged: null, skinfold_missing: null,
    body: { source: "profile", label: "Profil", date: null, body_fat_pct: 18.5, lean_mass_kg: 61 },
  };
  assert.match(energyBasisText(read), /KFA 18.5 % \(Profil\), fettfreie Masse 61 kg/);
});

test("a diary render with the shown energy read keeps the card and its open popover (#3379)", () => {
  const totals = new FakeElement("div");
  let slot = null;
  let built = 0;
  const context = extractFunctions("analysis.js", ["renderDiaryEnergy"], {
    document: { querySelector: (selector) => (selector === "#nutritionTotals" ? totals : slot) },
    h: (tag, attributes) => Object.assign(new FakeElement(tag), { id: attributes?.id }),
    energyDiaryCards: () => { built += 1; return [new FakeElement("section")]; },
    energyRead: { rows: [] },
    // The same diary day as the read holds (#3395).
    energyReadDay: "2026-11-17",
    energyDay: () => "2026-11-17",
  });
  totals.after = (node) => { slot = node; };
  context.renderDiaryEnergy();
  const table = slot.children[0];
  context.renderDiaryEnergy();
  assert.equal(built, 1, "the same read is not rebuilt");
  assert.equal(slot.children[0], table, "the table node stays, so an open popover stays open");
});

test("a body receipt names the Garmin outcome and puts the day back on undo (#3403)", () => {
  let weightRead = { entries: [{ date: "2026-11-15", kg: 80 }] };
  const { weightReceipt } = extractFunctions("analysis.js", ["bodyReceipt", "weightReceipt"], {
    get weightRead() { return weightRead; },
  });
  const saved = weightReceipt("2026-11-15", "gespeichert");
  assert.equal(saved("sent").text, "Gewicht gespeichert und an Garmin Connect gesendet.");
  assert.equal(saved("pending").text, "Gewicht lokal gespeichert, Garmin-Sync fehlgeschlagen – wird erneut versucht.");
  assert.equal(saved("pending").error, true);
  assert.equal(saved("needs_weight").text, "Gewicht lokal gespeichert, Garmin braucht ein Gewicht am selben Tag.");
  assert.equal(saved("off").text, "Gewicht gespeichert.");
  // Undo restores the day's earlier entry, or removes a day that had none.
  assert.equal(JSON.stringify(saved("sent").undo), JSON.stringify(["/api/body/weight", { date: "2026-11-15", kg: 80 }]));
  assert.equal(JSON.stringify(weightReceipt("2026-11-14", "gespeichert")("sent").undo), JSON.stringify(["/api/body/weight/remove", { date: "2026-11-14", confirmed: true }]));
  assert.equal(weightReceipt("2026-11-15", "gelöscht")("removed").text, "Gewicht gelöscht und in Garmin Connect entfernt.");
});

const FIT_FUNCTIONS = ["ANALYSIS_PERIODS", "analysisPeriod", "verlaufPeriod", "verlaufHead", "FIT_SOURCE_NAMES", "FIT_COLORS", "deficitDay", "energyKcalRange", "energyKcalSpan", "energyFitSources", "energyFitSigned", "energyFitSpan", "energyFitVerdict", "energyFitDomain", "energyFitMark", "energyFitLane", "energyFitAxis", "energyFitOwnRow", "energyFitLabel", "energyFitSourceRow", "energyFitLegend", "energyFitNoteText", "energyFitScale"];

test("the fit scale names the own value, sorts the sources nearest first and signs their distance (#3397)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const { energyFitScale } = extractFunctions("analysis.js", [...FIT_FUNCTIONS], {
    h, formatWhole: (v) => String(v), nutritionHistoryPeriod: "28d",
  });
  const scale = {
    days: 91, own_days: 91, own_kcal: 2726, own_band_kcal: 454, weeks: 13, weeks_with_data: 3, span_start: "2026-08-21", span_end: "2026-11-13",
    sources: [
      { source: "katch_mcardle", total_kcal: 3768 },
      { source: "cunningham", total_kcal: 3930 },
      { source: "device", label: "Garmin Connect", total_kcal: 3157 },
    ],
  };
  const card = energyFitScale({ best: null, outlined: "device", fit_scale: { "91d": scale } }, "91d");
  const [head, verdict, , list, note] = card.children;
  // The shared title row: the title left, the period right (#3704).
  assert.equal(textOf(head), "Welche Quelle trifft dein Gewicht?3 Monate");
  // Not yet distinguishable: the outlined source is named, with its signed distance, and not called
  // certain; the sentence names the weigh-ins' span the sources average over (#3613).
  assert.equal(textOf(verdict), "Vom 21.08. bis 13.11. verbrauchst du rund 2726 kcal am Tag. Am besten bisher: Garmin Connect (+431) – noch nicht sicher.");
  const rows = list.children;
  assert.deepEqual(rows.map((row) => row.dataset.fitSource), ["own", "device", "katch_mcardle", "cunningham"]);
  // #3478: the own value carries its band as the low–high span, with a spoken form.
  // #3704: the row's label left, its lane in the middle and its value right.
  assert.deepEqual(rows.map((row) => row.children.length), [3, 3, 3, 3]);
  assert.deepEqual(rows.map((row) => textOf(row.children[2])), ["27262272–3180zwischen 2272 und 3180 kcal", "3157 (+431)", "3768 (+1042)", "3930 (+1204)"]);
  // +431 lies inside the ±454 band, the formulas outside.
  assert.deepEqual(rows.slice(1).map((row) => row.dataset.inside), ["true", "false", "false"]);
  assert.equal(textOf(note), "kcal pro Tag, Ø 3 Monate. Grünes Band: so genau kennt die App deinen echten Verbrauch – liegt eine Quelle darin, trifft sie. 3 von 13 Wochen mit Daten.");
  // Distinguishable by the 12-week rule: the nearest is named as fitting, with "mehr"/"weniger".
  const sure = energyFitScale({ best: "device", outlined: "device", fit_scale: { "91d": { ...scale, sources: [{ source: "device", label: "Garmin Connect", total_kcal: 2600 }] } } }, "91d");
  assert.equal(textOf(sure.children[1]), "Vom 21.08. bis 13.11. verbrauchst du rund 2726 kcal am Tag. Am besten trifft Garmin Connect dein Gewicht, mit 126 kcal weniger.");
  // One rule (#3613): the sentence names the outlined source even where another dot lies nearer.
  const outlined = energyFitScale({ best: null, outlined: "katch_mcardle", fit_scale: { "91d": scale } }, "91d");
  assert.match(textOf(outlined.children[1]), / Am besten bisher: Katch-McArdle \(\+1042\) – noch nicht sicher\.$/);
  // 4 weeks: without the weigh-ins' span the sentence names the period.
  const short = energyFitScale({ best: null, outlined: "device", fit_scale: { "28d": { ...scale, days: 28, own_days: 28, weeks: 4, weeks_with_data: 1, span_start: null, span_end: null } } }, "28d");
  assert.match(textOf(short.children[1]), /^In den letzten 4 Wochen verbrauchst du rund 2726 kcal.* Am besten bisher: Garmin Connect \(\+431\)/);
  assert.match(textOf(short.children.at(-1)), /^kcal pro Tag, Ø 4 Wochen\. .* 1 von 4 Wochen mit Daten\.$/);
  const half = energyFitScale({ best: null, outlined: "device", fit_scale: { "182d": { ...scale, days: 182, own_days: 182, weeks: 26, span_start: null, span_end: null } } }, "182d");
  assert.match(textOf(half.children[1]), /^In den letzten 6 Monaten verbrauchst du rund 2726 kcal/);
  // Without an own estimate the sources still show, unsigned, with no sentence that denies them and no green band
  // in the note (#3930); the sentence stands only where nothing is drawn.
  const none = energyFitScale({ best: null, fit_scale: { "91d": { ...scale, own_kcal: null, own_band_kcal: null } } }, "91d");
  assert.doesNotMatch(textOf(none), /Für einen Vergleich fehlen/);
  assert.doesNotMatch(textOf(none.children.at(-1)), /Grünes Band/);
  assert.deepEqual(none.children[2].children.map((row) => textOf(row.children[2])), ["3157", "3768", "3930"]);
  const empty = energyFitScale({ best: null, fit_scale: { "91d": { ...scale, own_kcal: null, own_band_kcal: null, sources: [] } } }, "91d");
  assert.equal(textOf(empty.children[1]), "Für einen Vergleich fehlen noch genug vollständig erfasste Tage und Wiegungen.");
  // #3723: a formula row with a learned everyday adds "mit Alltag ≈", ranked and signed on the raw
  // total; the legend says "vorläufig" until every shown term qualifies.
  const withLearned = (qualified) => ({ ...scale, sources: [{ source: "katch_mcardle", total_kcal: 3768, learned_total_kcal: 3948, learned_qualified: qualified }, ...scale.sources.slice(1)] });
  const learned = energyFitScale({ best: null, outlined: "device", fit_scale: { "91d": withLearned(false) } }, "91d");
  const learnedRows = learned.children[3].children;
  assert.deepEqual(learnedRows.map((row) => row.dataset.fitSource), ["own", "device", "katch_mcardle", "cunningham"]);
  assert.equal(textOf(learnedRows[2].children[2]), "3768 (+1042)mit Alltag ≈ 3948 (+1222)");
  assert.equal(textOf(learned.children[4]), "Formel alleinmit Alltag (vorläufig)");
  const qualified = energyFitScale({ best: null, outlined: "device", fit_scale: { "91d": withLearned(true) } }, "91d");
  assert.equal(textOf(qualified.children[4]), "Formel alleinmit Alltag");
  // A term of 0 keeps the line but draws no ring over the dot (review on #3723).
  const marks = (row) => row.children[1].children.map((node) => node.className);
  assert.deepEqual(marks(learnedRows[2]), ["energy-fit-band is-faint", "energy-fit-link", "energy-fit-point", "energy-fit-learned"]);
  const zero = energyFitScale({ best: null, outlined: "device", fit_scale: { "91d": { ...scale, sources: [{ source: "katch_mcardle", total_kcal: 3768, learned_total_kcal: 3768, learned_qualified: false }, ...scale.sources.slice(1)] } } }, "91d");
  const zeroRow = zero.children[3].children[2];
  assert.deepEqual(marks(zeroRow), ["energy-fit-band is-faint", "energy-fit-point"]);
  assert.equal(textOf(zeroRow.children[2]), "3768 (+1042)mit Alltag ≈ 3768 (+1042)");
});

test("the Verlauf sentence names the server's outlined source, in every period (#3613)", () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const { energyFitScale } = extractFunctions("analysis.js", [...FIT_FUNCTIONS], {
    h, formatWhole: (v) => String(v), nutritionHistoryPeriod: "28d",
  });
  // The rows' mean errors would rank Garmin first; the server's one rule names Cunningham.
  const rows = [{ source: "katch_mcardle", mean_error_kcal: 300 }, { source: "cunningham", mean_error_kcal: 250 }, { source: "device", mean_error_kcal: 100 }, { source: "own", mean_error_kcal: 0 }];
  const scale = (days, own) => ({ days, own_days: own, own_kcal: 2700, own_band_kcal: 300, weeks: days / 7, weeks_with_data: 2, span_start: null, span_end: null,
    sources: [{ source: "katch_mcardle", total_kcal: 2690 }, { source: "cunningham", total_kcal: 2900 }, { source: "device", label: "Garmin Connect", total_kcal: 2710 }] });
  for (const best of [null, "cunningham"]) {
    const read = { best, outlined: "cunningham", rows, fit_scale: { "28d": scale(28, 28), "91d": scale(91, 91), "182d": scale(182, 182) } };
    for (const key of ["28d", "91d", "182d"]) {
      const named = energyFitScale(read, key).children[1].children.find((node) => node?.tagName === "B" && textOf(node) !== "2700 kcal am Tag");
      assert.equal(textOf(named), "Cunningham");
    }
  }
});

test("one merged body series names each day's source in the overview and the table (#3464)", () => {
  const { bodyOverviewReading } = extractFunctions("analysis.js", ["bodySourceShown", "bodyOverviewLatest", "bodyFoldFat", "bodyOverviewReading"], {
    isPreferredSource: (item, preferred) => item.source_provider === preferred,
    analysisValidPoint: (p) => p.value != null, dateLabel: (v) => v,
    skinfoldRead: { measurements: [] },
  });
  const merged = (points) => ({ preferred_providers: { weight_kg: "garmin", body_fat_pct: "garmin" }, windows: { "14d": { start: "2026-11-01", end: "2026-11-17", metrics: {
    weight_kg: [{ source: "Garmin Connect", source_provider: "garmin", points }],
    body_fat_pct: [{ source: "Garmin Connect", source_provider: "garmin", points }],
  } } } });
  const preferred = { weight_kg: "garmin", body_fat_pct: "garmin" };
  const entered = merged([{ date: "2026-11-16", value: 80, source_label: "Garmin" }, { date: "2026-11-17", value: 79.5, source_label: "eingetragen" }]);
  assert.equal(JSON.stringify(bodyOverviewReading("weight_kg", entered, preferred)), JSON.stringify({ value: 79.5, note: "Eingetragen · 2026-11-17" }));
  const garmin = merged([{ date: "2026-11-17", value: 80, source_label: "Garmin" }]);
  assert.equal(bodyOverviewReading("weight_kg", garmin, preferred).note, "Garmin Connect · 2026-11-17");
  // KFA reads only the athlete's own days, also out of the merged series.
  assert.equal(bodyOverviewReading("body_fat_pct", entered, preferred).value, 79.5);
  assert.equal(bodyOverviewReading("body_fat_pct", garmin, preferred).text, "—");
  // #3569: a profile body fat is the athlete's own skinfold and says so.
  const fold = merged([{ date: "2026-11-16", value: 14, source_label: "Garmin" }, { date: "2026-11-17", value: 13.5, source_label: "Hautfalten" }]);
  assert.equal(JSON.stringify(bodyOverviewReading("body_fat_pct", fold, preferred)), JSON.stringify({ value: 13.5, note: "Eingetragen (Hautfalten) · 2026-11-17" }));
});

test("one period table serves every Analyse and Ernährung selector (#3839)", () => {
  const { ANALYSIS_PERIODS, analysisPeriod, analysisPeriodWeekly, trainingFocusPeriodLabel } = extractFunctions("analysis.js", ["ANALYSIS_PERIODS", "analysisPeriod", "analysisPeriodWeekly", "trainingFocusPeriodLabel"], {});
  assert.equal(JSON.stringify(ANALYSIS_PERIODS.map((item) => [item.key, item.days, item.label])), JSON.stringify([["28d", 28, "4 Wochen"], ["91d", 91, "3 Monate"], ["182d", 182, "6 Monate"], ["365d", 365, "12 Monate"]]));
  // 4 Wochen draw days, every longer period calendar weeks of 4, 13, 26 and 52.
  assert.equal(JSON.stringify(ANALYSIS_PERIODS.map((item) => [analysisPeriodWeekly(item.key), item.weeks])), JSON.stringify([[false, 4], [true, 13], [true, 26], [true, 52]]));
  assert.equal(analysisPeriod("unknown").key, "28d");
  const focus = (days) => trainingFocusPeriodLabel({ start: "2026-01-01", end: new Date(Date.parse("2026-01-01") + (days - 1) * 86400000).toISOString().slice(0, 10) });
  assert.equal(JSON.stringify([28, 91, 182, 365, 30].map(focus)), JSON.stringify(["Letzte 4 Wochen", "Letzte 3 Monate", "Letzte 6 Monate", "Letzte 12 Monate", "Letzte 30 Tage"]));
});
