const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

function loadTickLabels() {
  const context = extractFunctions("analysis.js", ["analysisWeekTickLabel", "analysisDayTickLabel"]);
  return { weekTickLabel: context.analysisWeekTickLabel, dayTickLabel: context.analysisDayTickLabel };
}

test("weekly ticks show a date from the displayed week", () => {
  const { weekTickLabel } = loadTickLabels();
  assert.equal(weekTickLabel("2026-10-05"), "05.10");
  assert.equal(weekTickLabel("2026-10-11"), "11.10");
  assert.equal(weekTickLabel("2025-12-29"), "29.12");
  assert.equal(weekTickLabel("2027-01-01"), "01.01");
  assert.equal(weekTickLabel("2026-12-31"), "31.12");
});

test("the label step keeps the labels within what fits, up to a year of daily ticks (#3928)", () => {
  const { analysisTickStep } = extractFunctions("analysis.js", ["analysisTickStep"]);
  const labels = (ticks, max) => Math.floor((ticks - 1) / analysisTickStep(ticks, max)) + 1;
  for (const [ticks, max] of [[28, 4], [28, 7], [91, 4], [91, 7], [182, 4], [182, 7], [364, 4], [364, 7], [53, 4], [400, 7]]) {
    assert.ok(labels(ticks, max) <= max, `${ticks} ticks, ${max} labels`);
    assert.ok(labels(ticks, max) >= Math.min(ticks, 3), `${ticks} ticks, ${max} labels are not too few`);
  }
  // A week of days keeps a round step; a short axis labels every tick.
  assert.equal(analysisTickStep(7, 7), 1);
  assert.equal(analysisTickStep(28, 4), 7);
  assert.equal(analysisTickStep(364, 7), 56);
});

test("rolling seven-day buckets keep day-month date labels", () => {
  const { dayTickLabel } = loadTickLabels();
  assert.equal(dayTickLabel("2026-10-07"), "07.10");
  assert.equal(dayTickLabel("2026-10-10"), "10.10");
});

