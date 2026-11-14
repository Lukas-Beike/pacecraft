const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const TRAINING_RECORDS = "/api/analysis/training-records";

function createContext(calls) {
  return extractFunctions("analysis.js", ["analysisReportCache", "analysisReportVersionKey", "requestAnalysisReport", "invalidateAnalysisReport", "clearAnalysisReportCache"], {
    state: { data: { state_versions: { training: 1 } } },
    DATA_LOAD_TIMEOUT_MS: 15_000,
    api: async (requestPath, options) => {
      // Analysis reports time out into a retry state instead of a permanent loading line (#1783).
      assert.equal(options?.timeoutMs, 15_000);
      calls.push(requestPath);
      return { path: requestPath, call: calls.length };
    },
  });
}

test("training records are reused for the same state version until invalidated", async () => {
  const calls = [];
  const context = createContext(calls);

  const first = await context.requestAnalysisReport(TRAINING_RECORDS);
  const second = await context.requestAnalysisReport(TRAINING_RECORDS);
  assert.equal(calls.length, 1);
  assert.equal(second, first);

  context.invalidateAnalysisReport(TRAINING_RECORDS);
  const afterMaintenance = await context.requestAnalysisReport(TRAINING_RECORDS);
  assert.equal(calls.length, 2);
  assert.notEqual(afterMaintenance, first);
});

test("invalidation drops only the named report", async () => {
  const calls = [];
  const context = createContext(calls);

  await context.requestAnalysisReport(TRAINING_RECORDS);
  await context.requestAnalysisReport("/api/analysis/endurance");
  context.invalidateAnalysisReport(TRAINING_RECORDS);
  await context.requestAnalysisReport("/api/analysis/endurance");

  assert.deepEqual(calls, [TRAINING_RECORDS, "/api/analysis/endurance"]);
});

test("the equipment notice reads once per activity state when idle and never retries a failed read", async () => {
  const calls = [];
  const updates = [];
  const idle = [];
  const state = { data: { state_versions: { training: 1 } }, sessionGeneration: 1, replaceDataOnLoad: true };
  const page = { visibilityState: "hidden" };
  const context = extractFunctions("analysis.js", ["analysisReportCache", "analysisReportVersionKey", "requestAnalysisReport", "equipmentNoticeKey", "refreshEquipmentNotice"], {
    state,
    DATA_LOAD_TIMEOUT_MS: 15_000,
    requestIdleCallback: (callback) => idle.push(callback),
    document: page,
    equipmentStatusItem: (equipment) => equipment?.notice ?? null,
    updateStatusSources: (changes) => updates.push(changes),
    api: async (requestPath) => {
      calls.push(requestPath);
      if (calls.length === 1) throw new Error("offline");
      return { equipment: { notice: "due" } };
    },
  });
  // The device snapshot shown before the fresh state reads nothing.
  context.refreshEquipmentNotice();
  assert.equal(idle.length, 0);
  state.replaceDataOnLoad = false;
  // A hidden page reads nothing and leaves the read to the next status render.
  context.refreshEquipmentNotice();
  idle.shift()();
  page.visibilityState = "visible";
  context.refreshEquipmentNotice();
  context.refreshEquipmentNotice();
  assert.equal(calls.length, 0, "nothing is read before the browser is idle");
  assert.equal(idle.length, 1);
  idle.shift()();
  await new Promise((resolve) => setImmediate(resolve));
  context.refreshEquipmentNotice();
  assert.deepEqual([calls.length, idle.length, updates.length], [1, 0, 0], "a failed read waits for the next state version");
  // A chat turn changes other versions only and reads nothing again.
  state.data.state_versions.chat = 2;
  context.refreshEquipmentNotice();
  assert.equal(idle.length, 0);
  state.data.state_versions.activities = 2;
  context.refreshEquipmentNotice();
  idle.shift()();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(JSON.parse(JSON.stringify(updates)), [{ equipment: "due" }]);
});
