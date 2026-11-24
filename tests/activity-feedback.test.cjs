const assert = require("node:assert/strict");
const { test } = require("node:test");
const { loadPublicScript } = require("./frontend-harness.cjs");

const { ActivityFeedback } = loadPublicScript(["format.js", "activity-feedback.js"], { document: {}, history: {}, location: {}, addEventListener() {} });

test("the watch's effort marks the step it falls on, none outside 1 to 10", () => {
  assert.equal(ActivityFeedback.watchStep(6), 6);
  assert.equal(ActivityFeedback.watchStep(6.4), 6);
  assert.equal(ActivityFeedback.watchStep(10), 10);
  for (const value of [0, 0.4, 10.6, null, undefined, "x", Number.NaN]) assert.equal(ActivityFeedback.watchStep(value), null, String(value));
});

test("the load line names the value, its source and the minutes, or why it is open", () => {
  const text = ActivityFeedback.sessionLoadText({ value: 372, rpe: 6, rpe_source_label: "Garmin", duration_min: 62 });
  assert.match(text, /^RPE-Belastung: 372 RPE-Punkte · RPE 6 \(Garmin\) × 62 min$/);
  assert.equal(ActivityFeedback.sessionLoadText({ reason: "no_rating" }), "");
  assert.equal(ActivityFeedback.sessionLoadText({ value: null, reason: "invalid_duration" }), "RPE-Belastung: offen · ungültige Dauer");
});
