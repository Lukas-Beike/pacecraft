const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const { duplicateStamina } = extractFunctions("activity-analysis.js", ["duplicateStamina"]);

const streams = (second) => ({ channels: { stamina_137_pct: { values: [90, 80, 70] }, stamina_138_pct: { values: second } } });
const shown = [{ key: "stamina_137_pct" }, { key: "stamina_138_pct" }];

test("the second stamina channel is dropped only when it repeats the first", () => {
  assert.equal(duplicateStamina(shown[1], shown, streams([90, 80, 70])), true);
  assert.equal(duplicateStamina(shown[1], shown, streams([95, 85, 75])), false);
  assert.equal(duplicateStamina(shown[1], shown, streams([90, 80])), false);
  assert.equal(duplicateStamina(shown[0], shown, streams([90, 80, 70])), false);
  assert.equal(duplicateStamina(shown[1], [shown[1]], streams([90, 80, 70])), false);
});
