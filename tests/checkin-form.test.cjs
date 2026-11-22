const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

// checkinChanges sends only what differs from the stored day (#3953), so an omitted field keeps its value.
const { checkinChanges } = extractFunctions("checkin-form.js", ["CHECKIN_SCORES", "CHECKIN_TAGS", "checkinComplaintKey", "checkinSorted", "checkinFilled", "checkinChanges"], {});

// Objects built inside the harness context compare by value after a JSON round trip.
const plain = (value) => JSON.parse(JSON.stringify(value));

const shown = (texts = {}, rest = {}) => ({
  texts: { soreness: "", stress: "", motivation: "", available_minutes: "", illness: "", notes: "", ...texts },
  day_status: "unknown", tags: { travel: false, late_meal: false, high_stress: false }, complaints: [], ...rest,
});

test("a form that shows the stored day changes nothing", () => {
  const stored = { stress: 3, notes: "ok", day_status: "rest", tag_answers: { travel: true, late_meal: false }, complaints: [{ area: "knee", side: "left", severity: 2 }] };
  const values = shown({ stress: "3", notes: "ok" }, { day_status: "rest", tags: { travel: true, late_meal: false, high_stress: false }, complaints: [{ area: "knee", side: "left", severity: 2 }] });
  assert.equal(checkinChanges(values, stored, "2026-11-20"), null);
  assert.equal(checkinChanges(shown(), {}, "2026-11-20"), null);
});

test("only the changed fields are sent, as numbers and trimmed text", () => {
  const body = checkinChanges(shown({ soreness: "4", stress: "3", illness: "Husten" }), { stress: 3 }, "2026-11-20");
  assert.deepEqual(plain(body), { checkin_date: "2026-11-20", soreness: 4, illness: "Husten" });
});

test("an emptied field is cleared by name and an empty stored field is not", () => {
  const body = checkinChanges(shown({ stress: "", motivation: "" }), { stress: 3, motivation: null, notes: "" }, "2026-11-20");
  assert.deepEqual(plain(body), { checkin_date: "2026-11-20", clear_fields: ["stress"] });
});

test("a tag counts as answered once checked or stored, so an unchecked stored tag is a no", () => {
  const checked = checkinChanges(shown({}, { tags: { travel: true, late_meal: false, high_stress: false } }), {}, "2026-11-20");
  assert.deepEqual(plain(checked.tag_answers), { travel: true });
  const unchecked = checkinChanges(shown(), { tag_answers: { travel: true } }, "2026-11-20");
  assert.deepEqual(plain(unchecked.tag_answers), { travel: false });
});

test("complaints are sent whole, also when the last one is removed", () => {
  const added = [{ area: "achilles", side: "left", severity: 3 }];
  assert.deepEqual(plain(checkinChanges(shown({}, { complaints: added }), {}, "2026-11-20").complaints), added);
  assert.deepEqual(plain(checkinChanges(shown(), { complaints: added }, "2026-11-20").complaints), []);
});

test("a day status that changed is sent and the default is not", () => {
  assert.equal(checkinChanges(shown({}, { day_status: "pause" }), {}, "2026-11-20").day_status, "pause");
  assert.equal(checkinChanges(shown({}, { day_status: "unknown" }), {}, "2026-11-20"), null);
});
