const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const context = extractFunctions("plan-views.js", ["externalCalendarDetail"]);

test("the calendar status names the skipped appointments", () => {
  assert.equal(context.externalCalendarDetail({ configured: true, skipped_events: 2 }), "2 Termine übersprungen");
  assert.equal(context.externalCalendarDetail({ configured: true, skipped_events: 0 }), "");
  assert.equal(context.externalCalendarDetail({ configured: true }), "");
});

test("an error outranks the skipped count", () => {
  assert.equal(context.externalCalendarDetail({ configured: true, last_error: "x", skipped_events: 2 }), "Fehler bei letzter Aktualisierung");
});

test("without an address the status names the next step", () => {
  assert.equal(context.externalCalendarDetail({ configured: false }), "Adresse speichern, dann synchronisieren.");
});
