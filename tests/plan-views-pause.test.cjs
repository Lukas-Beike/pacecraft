const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const addDateKey = (key, days) => new Date(Date.parse(`${key}T12:00:00Z`) + days * 86400000).toISOString().slice(0, 10);
const { plannedPauseEnd } = extractFunctions("plan-views.js", ["plannedPauseEnd"], { addDateKey });

const pause = (date) => ({ date, day_status: "pause" });

test("a pause runs to the last of its consecutive pause days", () => {
  const contexts = [pause("2026-11-17"), pause("2026-11-18"), pause("2026-11-19"), { date: "2026-11-20", day_status: "rest" }, pause("2026-11-21")];
  assert.equal(plannedPauseEnd("2026-11-17", contexts, null), "2026-11-19");
  assert.equal(plannedPauseEnd("2026-11-19", contexts, null), "2026-11-19");
  assert.equal(plannedPauseEnd("2026-11-21", contexts, null), "2026-11-21");
});

test("an approved illness pause that covers the day carries the end further", () => {
  const illness = { approved: true, start_date: "2026-11-17", end_date: "2026-11-24" };
  assert.equal(plannedPauseEnd("2026-11-17", [pause("2026-11-17")], illness), "2026-11-24");
  assert.equal(plannedPauseEnd("2026-11-17", [pause("2026-11-17")], { ...illness, approved: false }), "2026-11-17");
  assert.equal(plannedPauseEnd("2026-11-16", [pause("2026-11-16")], illness), "2026-11-16");
  assert.equal(plannedPauseEnd("2026-11-25", [pause("2026-11-25")], illness), "2026-11-25");
});
