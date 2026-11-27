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

// #4304: "+ Mahlzeit" preselects today's type as the server types a meal without one
// (backend/nutrition/models.py meal_type_from_hour), and another day as a snack.
test("the add-meal sheet preselects the server's type for today's hour and a snack on another day", () => {
  const at = (iso, day = "2026-11-27") => extractFunctions("nutrition-templates.js", ["nutritionSelectedDay", "nutritionAddMealType"], {
    state: { data: { profile: { timezone: "UTC" } } },
    document: { querySelector: () => ({ value: day }) },
    todayIso: () => "2026-11-27",
    Date: class extends Date { constructor(...args) { super(...(args.length ? args : [iso])); } },
  }).nutritionAddMealType();
  const cases = [["04:30", "snack"], ["05:00", "breakfast"], ["10:59", "breakfast"], ["11:00", "lunch"], ["15:00", "snack"], ["18:00", "dinner"], ["22:59", "dinner"], ["23:00", "snack"]];
  for (const [clock, type] of cases) assert.equal(at(`2026-11-27T${clock}:00Z`), type, clock);
  assert.equal(at("2026-11-27T08:00:00Z", "2026-11-26"), "snack");
});
