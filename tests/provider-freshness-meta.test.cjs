const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const { freshnessMeta } = extractFunctions("sync-status.js", ["freshnessMeta"], { providerIds: () => ({ intervals: "intervals" }) });
const format = (value) => `<${value}>`;

test("a card names a retry only where one is due", () => {
  assert.equal(freshnessMeta({ provider: "garmin", state: "fresh", last_attempt_at: "a", last_success_at: "b" }, format), "Letzter Versuch: <a> · Letzter Erfolg: <b>");
  assert.equal(freshnessMeta({ provider: "garmin", state: "error", last_attempt_at: "a" }, format), "Letzter Versuch: <a> · Noch kein erfolgreicher Abruf · Keine automatische Wiederholung geplant");
  assert.equal(freshnessMeta({ provider: "garmin", state: "error", last_attempt_at: "a", last_success_at: "b", next_retry_at: "c" }, format), "Letzter Versuch: <a> · Letzter Erfolg: <b> · Nächster Versuch ab: <c>");
});

test("a card that never ran says nothing, and the output target reads in sending words", () => {
  assert.equal(freshnessMeta({ provider: "garmin", state: "never_loaded" }, format), "");
  assert.equal(freshnessMeta({ provider: "intervals", state: "fresh", last_attempt_at: "a" }, format), "Letzter Sendeversuch: <a> · Noch nichts gesendet");
  assert.equal(freshnessMeta({ provider: "intervals", state: "fresh", last_attempt_at: "a", last_success_at: "b" }, format), "Letzter Sendeversuch: <a> · Zuletzt gesendet: <b>");
});
