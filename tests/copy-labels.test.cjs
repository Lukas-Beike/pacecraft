const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions, fakeDocument } = require("./frontend-harness.cjs");

test("equipment count labels use singular for exactly one and German thousands separators", () => {
  const context = extractFunctions("analysis.js", ["analysisCountLabel"]);
  assert.equal(context.analysisCountLabel(1, "zugeordnete Einheit", "zugeordnete Einheiten"), "1 zugeordnete Einheit");
  assert.equal(context.analysisCountLabel(0, "zugeordnete Einheit", "zugeordnete Einheiten"), "0 zugeordnete Einheiten");
  assert.equal(context.analysisCountLabel(2, "Einheit", "Einheiten"), "2 Einheiten");
  assert.equal(context.analysisCountLabel(undefined, "Nachweis", "Nachweise"), "0 Nachweise");
  assert.equal(context.analysisCountLabel(1234, "Einheit", "Einheiten"), "1.234 Einheiten");
});

test("equipment percentages render with a German decimal comma and a space before % like every other percentage (#3933)", () => {
  const context = extractFunctions("analysis.js", ["analysisPercent"]);
  assert.equal(context.analysisPercent(42.5), "42,5 %");
  assert.equal(context.analysisPercent(25), "25 %");
  assert.equal(context.analysisPercent(125), "125 %");
});

test("profile sports are shown in German while stored identifiers stay unchanged", () => {
  const context = extractFunctions("views.js", ["PROFILE_SPORT_LABELS", "profileSportsLabel"]);
  assert.equal(context.profileSportsLabel("Cycling,Running,Swimming"), "Radfahren, Laufen, Schwimmen");
  assert.equal(context.profileSportsLabel("Strength"), "Kraft");
  assert.equal(context.profileSportsLabel("Rowing"), "Rowing");
  assert.equal(context.profileSportsLabel(""), "");
});

test("a nutrition entry without a recorded basis is labelled by its source and leaves the sync state to the card", () => {
  const { nutritionBasisLabel } = extractFunctions("nutrition.js", ["nutritionBasisLabel"]);
  assert.equal(nutritionBasisLabel(undefined, "Manuelle Angabe"), "Manuelle Angabe");
  assert.equal(nutritionBasisLabel({ kind: "packaging_label" }, "Erfasst"), "Verpackungsangabe");
  assert.equal(nutritionBasisLabel({ kind: "composite" }, "Erfasst"), "");
});

function contextValueNodeFor() {
  return extractFunctions("coach.js", ["CONTEXT_PREVIEW_MAX_DEPTH", "CONTEXT_PREVIEW_MAX_ITEMS", "CONTEXT_HIDDEN_KEYS", "CONTEXT_KEY_LABELS", "CONTEXT_ENVIRONMENTS", "contextWallTime", "contextScalarText", "contextPreNode", "contextTextNode", "contextListNode", "contextObjectNode", "contextValueNode"], {
    document: fakeDocument(),
    formatTime: (value) => value,
    profileSportsLabel: extractFunctions("views.js", ["PROFILE_SPORT_LABELS", "profileSportsLabel"]).profileSportsLabel,
  }).contextValueNode;
}

function descendants(node) {
  return node.children.filter((child) => typeof child !== "string").flatMap((child) => [child, ...descendants(child)]);
}

test("the context preview shows readable values and keeps deeply nested provider data as compact text", () => {
  const contextValueNode = contextValueNodeFor();
  const readable = contextValueNode({ unlabelled_key: "ok", flag: true, empty: [], missing: null });
  const text = readable.children.map((node) => node.textContent);
  assert.deepEqual(text, ["unlabelled key", "ok", "flag", "Ja", "empty", "Keine Angaben", "missing", "–"]);
  const nested = descendants(contextValueNode({ a: { b: { c: { d: 1 } } } }));
  assert.deepEqual(nested.filter((node) => node.tag === "pre").map((node) => node.textContent), ['{\n  "d": 1\n}']);
  // The profile's own keys and sports read in German (#3933).
  const profile = contextValueNode({ goals: "Marathon", training_background: "Lauf", availability: "Abends", sports: "Cycling, Running" });
  assert.deepEqual(profile.children.map((node) => node.textContent), ["Ziele", "Marathon", "Trainingshintergrund", "Lauf", "Verfügbarkeit", "Abends", "Sportarten", "Radfahren, Laufen"]);
  // A competition row: no ids or sync flags, German keys, readable dates and categories (#3981).
  const race = contextValueNode({ id: 7, local_id: "x", sync_dirty: 1, name: "Rennen", event_date: "2026-11-21", category: "RACE_A", sport: "ride", environment: "virtual", starts_at_local: "2026-11-21T10:00" });
  assert.deepEqual(race.children.map((node) => node.textContent), ["Name", "Rennen", "Datum", "21.11.2026", "Kategorie", "Wettkampf A", "Sportart", "Radfahren", "Umgebung", "Indoor", "Beginn", "21.11.2026, 10:00"]);
  // A local wall time is shown as stored, never converted between zones (Gitar, #3981).
  assert.equal(contextValueNode({ starts_at_local: "2026-11-21T23:30:00" }).children[1].textContent, "21.11.2026, 23:30");
  assert.equal(contextValueNode("Langer Text", "content").tag, "pre");
  const longList = contextValueNode(Array.from({ length: 30 }, (_, index) => index));
  assert.equal(longList.children.length, 26);
  assert.equal(longList.children[25].textContent, "… 5 weitere Einträge");
});

test("release changes are grouped as Neu, Verbessert, Behoben and the rest, without type and pull request number", () => {
  const { releaseChangeGroups } = extractFunctions("views.js", ["releaseChangeGroups"]);
  const groups = releaseChangeGroups([
    "feat(calendar): sport filter for the week and month (#2853)",
    "fix: a restart closes the refreshes left running",
    "feat!: breaking change",
    "docs(spec): unlinking a race (#2860)",
    "Revert \"feat(x): something\"",
    "perf(analysis): keep the session curves across restarts (#2821) (#3035)",
    "perf: faster start",
    "perf(hooks): the guard scans long commands in linear time (#3129)",
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(groups)), {
    feat: ["calendar: sport filter for the week and month", "breaking change"],
    // A performance change to the app is noticeable; one to the tooling stays Technik.
    perf: ["analysis: keep the session curves across restarts (#2821)", "faster start"],
    fix: ["a restart closes the refreshes left running"],
    other: ["spec: unlinking a race", "Revert \"feat(x): something\"", "hooks: the guard scans long commands in linear time"],
  });
});

test("a library card shows its stored step lines in German words (#3933)", () => {
  const { libraryDescriptionText } = extractFunctions("plan-views.js", ["LIBRARY_STEP_UNITS", "libraryStepLine", "LIBRARY_SECTION_NAMES", "libraryHeaderLine", "libraryDescriptionText"]);
  assert.equal(libraryDescriptionText("Locker\n- 20m 55%\n- 20m Z1 HR\n- 400mtr"), "Locker\n20 min · 55 %\n20 min · Z1 Herzfrequenz\n400 m");
  // Section and repeat headers and watt targets, as the Intervals.icu workout text writes them.
  assert.equal(libraryDescriptionText("Warmup\n- 10m 55-65%\nMain Set 5x\n- 5m 250W\n- 3m 55%\n3x\nWarmup | Cooldown"), "Einlaufen\n10 min · 55–65 %\nHauptteil · 5×\n5 min · 250 W\n3 min · 55 %\n3×\nEinlaufen und Auslaufen");
  assert.equal(libraryDescriptionText(""), "");
  assert.equal(libraryDescriptionText("- Locker einlaufen"), "- Locker einlaufen", "a prose bullet stays as written");
  assert.equal(libraryDescriptionText("Bergsprints 6x"), "Bergsprints 6x", "a prose line ending in a count is no header");
});

test("a change that rounds to zero carries no sign (#3933)", () => {
  const AppFormat = require("../public/format.js");
  const { analysisChange } = extractFunctions("analysis.js", ["analysisChange"], {
    analysisValue: (value, unit) => `${AppFormat.number(value, { digits: 1, minDigits: 1 })} ${unit}`,
    formatPace: AppFormat.pace,
  });
  assert.equal(analysisChange(0.4, "kg"), "+0,4 kg");
  assert.equal(analysisChange(-0.4, "kg"), "−0,4 kg");
  assert.equal(analysisChange(0.04, "kg"), "0,0 kg");
  assert.equal(analysisChange(-4, "s/km"), "−0:04 /km");
  assert.equal(analysisChange(0.2, "s/km"), "0:00 /km");
});

// Weight and body fat name their change by the edge means, never last minus first (#4037).
test("the body charts name the change by the edge means", () => {
  const AppFormat = require("../public/format.js");
  const { analysisEdgeChangeText } = extractFunctions("analysis.js", ["analysisEdgeChangeText", "analysisChange"], {
    analysisValue: (value, unit) => `${AppFormat.number(value, { digits: 1, minDigits: 1 })} ${unit}`,
    formatPace: AppFormat.pace,
  });
  assert.equal(analysisEdgeChangeText({ span_days: 7, first_mean: 73.83, last_mean: 72.43, change: -1.4 }, "kg"), "Änderung (Wochenmittel): −1,4 kg · Ø 73,8 kg → Ø 72,4 kg");
  assert.equal(analysisEdgeChangeText({ span_days: 14, first_mean: 20, last_mean: 20.04, change: 0.04 }, "%"), "Änderung (Mittel von 2 Wochen): 0,0 % · Ø 20,0 % → Ø 20,0 %");
});

// The term list of docs/view-catalogue.md: one word per thing, none of the others in the client source (#3933).
test("the client source holds one word per thing", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const dir = path.join(__dirname, "../public");
  const sources = fs.readdirSync(dir).filter((name) => /\.(js|html)$/.test(name)).map((name) => [name, fs.readFileSync(path.join(dir, name), "utf8")]);
  const other = ["Ø Puls", "Pulszonen", "HF-Zone", "Ø Tempo", "Kadenz", "Power-Zonen", "Kurzzeitbelastung", "Langzeitbelastung", "Session-RPE-Last", "Athletenkontext", "Mit Coach lösen", "Mit Coach strukturieren", "Mit dem Coach", "Nicht gesetzt", "nicht konfiguriert", "Noch nicht getestet", "\"Krafttraining\"", ">Krafttraining<", "Trainingsbelastung", "Akute Belastung", "akute Belastung", "Training Effect", "Load-Focus", ">Thinking<", ": Thinking", ">Personal API<", "SDK-Login"];
  const found = sources.flatMap(([name, text]) => other.filter((word) => text.includes(word)).map((word) => `${name}: ${word}`));
  assert.deepEqual(found, []);
});

// A connection or freshness line shows a server timestamp in words, never as the ISO text (#3981).
test("connection and freshness lines never render a raw ISO timestamp", () => {
  const AppFormat = require("../public/format.js");
  const { formatTime } = extractFunctions("shared.js", ["formatTime"], { AppFormat, state: { data: {} } });
  const { freshnessMeta } = extractFunctions("sync-status.js", ["freshnessMeta"], { providerIds: () => ({ intervals: "intervals" }) });
  const iso = /\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;
  for (const stamp of ["2026-11-21T10:00:00.123456+00:00", "2026-11-21 10:00:00.123456+00:00", "2026-11-21T10:00:00.123456", "not a time"]) {
    const line = freshnessMeta({ provider: "garmin", state: "ok", last_attempt_at: stamp, last_success_at: stamp, next_retry_at: stamp }, formatTime);
    assert.doesNotMatch(line, iso, stamp);
  }
  assert.equal(AppFormat.dateTime("2026-11-21 10:00:00.123456+00:00"), AppFormat.dateTime("2026-11-21T10:00:00.123+00:00"));
});
