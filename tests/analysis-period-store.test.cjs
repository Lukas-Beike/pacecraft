const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

function store(initial = null, { broken = false } = {}) {
  const data = new Map(initial == null ? [] : [["analysisPeriods", initial]]);
  const localStorage = broken
    ? { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } }
    : { getItem: (key) => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) };
  const functions = extractFunctions("shared.js", ["ANALYSIS_PERIOD_KEYS", "ANALYSIS_PERIOD_STORE", "storedAnalysisPeriod", "rememberAnalysisPeriod"], { localStorage });
  return { ...functions, data };
}

test("a tab keeps its default until a choice, and the choice is remembered per tab (A-11)", () => {
  const { storedAnalysisPeriod, rememberAnalysisPeriod, data } = store();
  assert.equal(storedAnalysisPeriod("recovery", "28d"), "28d");
  rememberAnalysisPeriod("recovery", "365d");
  rememberAnalysisPeriod("load", "182d");
  assert.equal(storedAnalysisPeriod("recovery", "28d"), "365d");
  assert.equal(storedAnalysisPeriod("load", "91d"), "182d");
  assert.equal(storedAnalysisPeriod("body", "91d"), "91d");
  assert.deepEqual(JSON.parse(data.get("analysisPeriods")), { recovery: "365d", load: "182d" });
});

test("a stored value that is no period, or blocked storage, falls back to the default", () => {
  assert.equal(store('{"load":"7d"}').storedAnalysisPeriod("load", "91d"), "91d");
  assert.equal(store("not json").storedAnalysisPeriod("load", "91d"), "91d");
  const blocked = store(null, { broken: true });
  assert.equal(blocked.storedAnalysisPeriod("load", "91d"), "91d");
  assert.doesNotThrow(() => blocked.rememberAnalysisPeriod("load", "28d"));
});
