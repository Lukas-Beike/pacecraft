const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const context = extractFunctions("nutrition.js", ["nutritionSessionLabel", "nutritionMealTitle", "NUTRITION_MEAL_LABELS"], {
  NUTRITION_SPORTS: { ride: "Rad", run: "Lauf" },
});

test("the session chip names sport, start and duration", () => {
  // 11400 s are 3 h 10 min.
  assert.equal(
    context.nutritionSessionLabel({ sport: "ride", started_at_local: "2026-11-14T09:15:00", elapsed_duration_s: 11400 }),
    "Rad · 09:15 · 3 h 10 min",
  );
  assert.equal(context.nutritionSessionLabel({ sport: "yoga", started_at_local: "2026-11-14T07:05:00" }), "Einheit · 07:05");
});

test("the session slots have their own card titles", () => {
  const entry = (meal_type) => ({ meal_type, logged_at: "2026-11-14T10:30:00", logged_time_known: true });
  assert.equal(context.nutritionMealTitle(entry("pre_session")), "10:30 · Vor der Einheit");
  assert.equal(context.nutritionMealTitle(entry("during_session")), "10:30 · Während der Einheit");
});
