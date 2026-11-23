const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

// The workout text as rows and back, and the loads of a strength row (#3950, #3951).
const NAMES = ["UNIT_STEP", "UNIT_REPEAT", "unitParsedLine", "unitStepRows", "unitRowHead", "unitStepsText", "unitRowMinutes", "unitStepsMinutes", "unitExerciseRow", "unitExercises"];
const editor = () => extractFunctions("unit-editor.js", NAMES);

test("a Coach layout with labels and a repeat becomes rows and keeps its minutes", () => {
  const { unitStepRows, unitStepsMinutes, unitStepsText } = editor();
  const text = "Warmup\n- 15m 60%\n\nMain set 2x\n- 15m 90%\n- 6m 55%\n\nCooldown\n- 10m Z1 HR";
  const rows = unitStepRows(text);
  assert.equal(unitStepsMinutes(rows), 15 + 2 * (15 + 6) + 10);
  assert.equal(unitStepsText(rows), text);
});

test("text the rows cannot show stays text", () => {
  const { unitStepRows } = editor();
  for (const text of ["- 30s 120%", "- 5m 105% 90rpm", "Lockerer Dauerlauf, Puls unten halten", "Main 3x\nHinweis"]) {
    assert.equal(unitStepRows(text), null, text);
  }
  assert.deepEqual([...unitStepRows("")], []);
});

test("an exercise keeps the load its row does not edit and clears only what the athlete changed", () => {
  const { unitExerciseRow, unitExercises } = editor();
  const both = unitExerciseRow({ name: "Kniebeuge", sets: 3, reps: 8, kg: 60, rpe: 8 });
  assert.deepEqual({ ...unitExercises([both])[0] }, { ...both.base, kg: 60, rpe: 8 });

  both.amount = "65";
  assert.deepEqual([unitExercises([both])[0].kg, unitExercises([both])[0].rpe], [65, 8]);

  both.load = "pct_1rm";
  both.amount = "80";
  const changed = unitExercises([both])[0];
  assert.deepEqual([changed.kg, changed.pct_1rm, changed.rpe], [null, 80, null]);
});
