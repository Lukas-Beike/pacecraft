const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

function loadTickLabels() {
  const context = extractFunctions("analysis.js", ["analysisWeekTickLabel", "analysisDayTickLabel", "ANALYSIS_MONTH_TICK_FORMAT", "ANALYSIS_MONTH_FORMAT", "analysisMonthTickLabel", "analysisMonthLabel", "analysisMonthDates"]);
  return { weekTickLabel: context.analysisWeekTickLabel, dayTickLabel: context.analysisDayTickLabel, monthTickLabel: context.analysisMonthTickLabel, monthLabel: context.analysisMonthLabel, monthDates: context.analysisMonthDates };
}

test("weekly ticks show a date from the displayed week", () => {
  const { weekTickLabel } = loadTickLabels();
  assert.equal(weekTickLabel("2026-10-05"), "05.10");
  assert.equal(weekTickLabel("2026-10-11"), "11.10");
  assert.equal(weekTickLabel("2025-12-29"), "29.12");
  assert.equal(weekTickLabel("2027-01-01"), "01.01");
  assert.equal(weekTickLabel("2026-12-31"), "31.12");
});

test("rolling seven-day buckets keep day-month date labels", () => {
  const { dayTickLabel } = loadTickLabels();
  assert.equal(dayTickLabel("2026-10-07"), "07.10");
  assert.equal(dayTickLabel("2026-10-10"), "10.10");
});

// Monthly points (#2922): short month names on the axis, the month and year elsewhere, one tick per month.
test("monthly charts name their months and tick each month start", () => {
  const { monthTickLabel, monthLabel, monthDates } = loadTickLabels();
  assert.deepEqual(["2026-03-01", "2026-05-01", "2026-11-01"].map(monthTickLabel), ["Mär", "Mai", "Nov"]);
  assert.equal(monthLabel("2026-11-01"), "November 2026");
  assert.deepEqual([...monthDates("2025-11-01", "2026-02-01")], ["2025-11-01", "2025-12-01", "2026-01-01", "2026-02-01"]);
  // A start after the 1st begins with the next month, inside the plotted range.
  assert.deepEqual([...monthDates("2026-06-15", "2026-08-01")], ["2026-07-01", "2026-08-01"]);
});
