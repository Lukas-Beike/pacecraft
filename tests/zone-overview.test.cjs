const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const NAMES = ["zoneMetricValue", "ZONE_METRIC_LABELS", "ZONE_SPORTS", "POWER_ZONE_NAMES", "zoneConfigurationFor", "zoneLimitsOf", "zoneLimitsText", "zoneLimitValue", "zoneValueText", "zoneRows", "zoneRange", "zoneSourceText", "zoneEditorValues", "defaultZoneItem", "OPEN_ZONE_LIMIT", "THRESHOLD_ORIGINS", "lt2Origin", "thresholdRows", "thresholdMainText", "thresholdEffortNote", "recordedMaxHrNote"];

// Garmin's heart-rate zones carry no sport: the server names their key for every sport (#2262).
const GARMIN_HR = { key: "garmin|heart_rate|||2026-11-09", source: "garmin", label: "Garmin", sport: null, metric: "heart_rate", environment: null, threshold: 163, maximum: 186, limits: [130, 145, 155, 166, 186], percent_limits: null };
const LOCAL_POWER = { key: "|power|ride||2026-11-01", source: "local", label: "Lokal", sport: "ride", metric: "power", environment: null, threshold: 285, maximum: null, limits: null, percent_limits: [55, 75, 90, 105, 150], percent_basis: "threshold" };
const EFFECTIVE = [
  { sport: "ride", metric: "heart_rate", environment: null, key: GARMIN_HR.key },
  { sport: "run", metric: "heart_rate", environment: null, key: GARMIN_HR.key },
  { sport: "ride", metric: "power", environment: null, key: LOCAL_POWER.key },
];

// Values built inside the script context have that context's prototypes.
const plain = (value) => JSON.parse(JSON.stringify(value));

function zones(globals = {}) {
  return extractFunctions("settings.js", NAMES, {
    zoneConfigurations: [GARMIN_HR, LOCAL_POWER],
    zoneEffective: EFFECTIVE,
    zoneDefaults: {},
    state: { data: {} },
    formatPace: (seconds) => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")} /km`,
    AppFormat: require("../public/format.js"),
    ...globals,
  });
}

test("an all-sports Garmin zone set is shown for every sport and prefills the editor", () => {
  const { zoneConfigurationFor, zoneRows, zoneSourceText, zoneEditorValues } = zones();
  for (const sport of ["ride", "run"]) {
    const shown = zoneConfigurationFor(sport, "heart_rate");
    assert.equal(shown?.key, GARMIN_HR.key, sport);
    assert.equal(zoneSourceText(shown), "Garmin, gilt für alle Sportarten");
    assert.deepEqual(plain(zoneEditorValues(sport, "heart_rate", shown)), { threshold: 163, maximum: 186, limits: "130, 145, 155, 166, 186" });
  }
  assert.deepEqual(plain(zoneRows(GARMIN_HR).map((row) => row.range)), ["bis 130", "131–145", "146–155", "156–166", "ab 167 · max 186"]);
  assert.equal(zoneConfigurationFor("swim", "power"), null, "a kind without zones shows the empty state");
});

test("the cycling FTP prefills the power editor of a sport without power zones", () => {
  const metrics = { cycling_ftp_watts: { value: 265 }, bike_threshold_hr_bpm: { value: 165 }, cycling_max_hr_bpm: { value: 190 }, run_threshold_watts: { value: 300 } };
  const { zoneEditorValues } = zones();
  const read = (key) => (key && metrics[key] ? metrics[key].value : null);
  assert.deepEqual(plain(zoneEditorValues("ride", "power", null, read)), { threshold: 265, maximum: "", limits: "" });
  assert.deepEqual(plain(zoneEditorValues("run", "power", null, read)), { threshold: 300, maximum: "", limits: "" });
  assert.deepEqual(plain(zoneEditorValues("ride", "heart_rate", null, read)), { threshold: 165, maximum: 190, limits: "" });
});

test("the standard zones show read-only once their threshold is known and prefill the editor (#3936 M-01)", () => {
  const defaults = { power: [55, 75, 90, 105, 120, 150], heart_rate: [68, 83, 94, 105], pace: [80, 90, 100] };
  const metrics = { cycling_ftp_watts: { value: 200 }, bike_threshold_hr_bpm: { value: 160 }, run_threshold_pace_seconds_per_km: { value: 240 } };
  const read = (key) => (key && metrics[key] ? metrics[key].value : null);
  const { defaultZoneItem, zoneRows, zoneSourceText, zoneEditorValues } = zones();
  const power = defaultZoneItem("ride", "power", defaults, read);
  assert.equal(zoneSourceText(power), "Standardzonen");
  assert.deepEqual(plain(zoneRows(power).map((row) => row.range)).slice(0, 2), ["bis 110 W · 55 %", "111–150 W · 75 %"]);
  // The open top zone stays: six limits are seven zones, four heart-rate limits five.
  assert.deepEqual([zoneRows(power).length, zoneRows(defaultZoneItem("ride", "heart_rate", defaults, read)).length], [7, 5]);
  assert.equal(zoneRows(power).at(-1).range, "ab 301 W");
  assert.equal(defaultZoneItem("run", "power", defaults, read), null, "no threshold, no read-only zones");
  assert.deepEqual(plain(zoneEditorValues("ride", "power", null, read, defaults)).limits, "55, 75, 90, 105, 120, 150, 999");
  assert.deepEqual(plain(zoneEditorValues("ride", "heart_rate", null, read, defaults)).limits, "109, 133, 150, 168, 176");
  assert.equal(zoneRows(defaultZoneItem("run", "pace", defaults, read), 240)[0].range, "langsamer als 5:00 · 80 %");
});

test("power zones show names, watts and the percent of the FTP", () => {
  const { zoneRows } = zones();
  assert.deepEqual(plain(zoneRows(LOCAL_POWER).map((row) => `${row.name}: ${row.range}`)), [
    "Z1 Regeneration: bis 157 W · 55 %",
    "Z2 Grundlage: 158–214 W · 75 %",
    "Z3 Tempo: 215–257 W · 90 %",
    "Z4 Schwelle: 258–299 W · 105 %",
    "Z5 VO2max: ab 300 W",
  ]);
});

test("pace zones read the percent of the threshold speed as paces", () => {
  // The stored limits end with the open top zone's cap.
  const pace = { metric: "pace", percent_limits: [80, 90, 100, 150], threshold: null };
  const { zoneRows } = zones();
  // 4:00 /km threshold: 80 % of its speed is 5:00, 90 % 4:27, 100 % 4:00.
  assert.deepEqual(plain(zoneRows(pace, 240).map((row) => row.range)), ["langsamer als 5:00 · 80 %", "5:00–4:27 · 90 %", "4:27–4:00 · 100 %", "schneller als 4:00"]);
  // Without a threshold pace (swimming) the limits stay percents.
  assert.deepEqual(plain(zoneRows(pace, null).map((row) => row.range)), ["langsamer als 80 %", "80 %–90 %", "90 %–100 %", "schneller als 100 %"]);
});

test("a threshold no recorded effort supports gets one line, nothing otherwise (#3920 C5)", () => {
  const { thresholdEffortNote } = zones();
  const thresholds = { unsupported_by_recent_effort: {
    ride: { threshold_w: 250, best_w: 241, duration_seconds: 1200, window_days: 90, date: "2026-09-10" },
    run: { threshold_pace_seconds_per_km: 270, best_pace_seconds_per_km: 278, distance_meters: 5000, window_days: 90, date: "2026-09-11" },
  } };
  assert.equal(thresholdEffortNote("ride", thresholds), "20-min-Bestwert 241 W (90 T.) · Test empfohlen");
  assert.equal(thresholdEffortNote("run", thresholds), "5-km-Bestwert 4:38 /km (90 T.) · Test empfohlen");
  // No recorded 20-min or 5 km effort, or a supported value: the server names none.
  assert.equal(thresholdEffortNote("ride", { unsupported_by_recent_effort: {} }), null);
  assert.equal(thresholdEffortNote("run", { unsupported_by_recent_effort: { ride: thresholds.unsupported_by_recent_effort.ride } }), null);
  assert.equal(thresholdEffortNote("ride", {}), null);
  assert.equal(thresholdEffortNote("ride", undefined), null);
});

test("a chest-strap maximum above the stored one gets one line under the heart-rate zones (#3920 C6)", () => {
  const { recordedMaxHrNote } = zones({ dateLabel: (day) => day.split("-").reverse().join(".") });
  assert.equal(recordedMaxHrNote({ recorded_max_hr_above: { value: 191, date: "2026-09-14", stored_max_hr: 188, window_days: 365 } }), "Brustgurt 191 bpm am 14.09.2026");
  assert.equal(recordedMaxHrNote({ recorded_max_hr_above: null }), null);
  assert.equal(recordedMaxHrNote({}), null);
});

test("LT2 comes from the performance values, LT1 from the thresholds", () => {
  const metrics = { cycling_ftp_watts: { value: 285, source: "Garmin Connect", observed_at: "2026-11-09T08:00:00Z" }, bike_threshold_hr_bpm: { value: 163, source: "Garmin Connect", observed_at: "2026-11-08" } };
  const { thresholdRows } = zones();
  const ride = { hr_bpm: 142, hr_origin: "estimated", watts: 214, power_origin: "own_sessions" };
  const rows = thresholdRows("ride", metrics, { first_thresholds: { ride } });
  assert.deepEqual([rows.lt2.main, rows.lt2.hr, rows.lt1.hr, rows.lt1.main], [285, 163, 142, 214]);
  assert.equal(rows.validFrom, "2026-11-09T08:00:00Z");
  // The yellow "i" of each row names its origin.
  assert.deepEqual(plain(rows.origins), { lt1: "Herzfrequenz aus der Herzfrequenzvariabilität · Leistung aus eigenen Einheiten", lt2: "Von Garmin Connect" });
  // Providers that differ per value are named per value.
  const mixed = thresholdRows("ride", { ...metrics, bike_threshold_hr_bpm: { value: 160, source: "Wahoo" } }, {});
  assert.equal(mixed.origins.lt2, "Leistung von Garmin Connect · Herzfrequenz von Wahoo");
  const empty = thresholdRows("run", {}, {});
  assert.deepEqual([empty.lt1.main, empty.lt1.hr, empty.lt2.main, empty.lt2.hr, empty.origins.lt1, empty.origins.lt2], [null, null, null, null, null, null]);
});
