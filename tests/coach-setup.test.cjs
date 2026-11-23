const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const { coachSetupSteps } = extractFunctions("coach.js", ["coachSetupSteps"]);
const done = (data) => Array.from(coachSetupSteps(data), (step) => [step.key, step.done]);

test("a fresh start has three open steps: profile, data source and model", () => {
  assert.deepEqual(done({ profile: {}, configured: {}, garmin: {}, wahoo: {} }), [["profile", false], ["source", false], ["model", false]]);
});

test("each step is done by its own fact", () => {
  assert.deepEqual(done({ profile: { name: "Ada" }, configured: { coach: true }, garmin: { configured: true, available: true }, wahoo: {} }), [["profile", true], ["source", true], ["model", true]]);
  assert.deepEqual(done({ profile: { goals: " " }, configured: { openai: true }, garmin: {}, wahoo: { connected: true } }), [["profile", false], ["source", true], ["model", true]]);
  assert.equal(done({ profile: {}, configured: {}, garmin: { configured: true, available: false }, wahoo: {} })[1][1], false, "an unavailable Garmin is no source");
});

test("the model counts as set while the state is not loaded", () => {
  assert.equal(done(null)[2][1], true);
});
