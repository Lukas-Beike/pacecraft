const assert = require("node:assert/strict");
const { test } = require("node:test");

const AppFormat = require("../public/format.js");

test("format module exposes a single frozen global object", () => {
  assert.equal(globalThis.AppFormat, AppFormat);
  assert.ok(Object.isFrozen(AppFormat));
});

test("number uses de-DE separators and honours digits", () => {
  assert.equal(AppFormat.number(12.345), "12,3");
  assert.equal(AppFormat.number(12), "12");
  assert.equal(AppFormat.number("7.25", { digits: 2 }), "7,25");
  // #2831: a minimum keeps trailing zeros, such as a training effect of 0,0.
  assert.equal(AppFormat.number(0, { digits: 1, minDigits: 1 }), "0,0");
  assert.equal(AppFormat.number(3.45, { digits: 1, minDigits: 1 }), "3,5");
  assert.equal(AppFormat.number(1234.56, { digits: 0 }), "1.235");
  assert.equal(AppFormat.number(NaN), null);
  assert.equal(AppFormat.number(Infinity), null);
  assert.equal(AppFormat.number("abc"), null);
});

test("distance formats kilometres with one decimal in de-DE", () => {
  assert.equal(AppFormat.distance(12300), "12,3 km");
  assert.equal(AppFormat.distance(42000), "42,0 km");
  assert.equal(AppFormat.distance(500), "0,5 km");
});

test("distance returns null for missing or non-positive values", () => {
  assert.equal(AppFormat.distance(null), null);
  assert.equal(AppFormat.distance(undefined), null);
  assert.equal(AppFormat.distance(""), null);
  assert.equal(AppFormat.distance(0), null);
  assert.equal(AppFormat.distance(-5), null);
  assert.equal(AppFormat.distance("abc"), null);
});

test("duration is written in words: minutes under one hour, then hours and minutes", () => {
  assert.equal(AppFormat.duration(2700), "45 min");
  assert.equal(AppFormat.duration(3570), "1 h");
  assert.equal(AppFormat.duration(3600), "1 h");
  assert.equal(AppFormat.duration(3900), "1 h 5 min");
  assert.equal(AppFormat.duration(0), "0 min");
  assert.equal(AppFormat.duration(null), null);
  assert.equal(AppFormat.duration(undefined), null);
  assert.equal(AppFormat.duration(-60), null);
});

test("clock keeps the stopwatch style for pace and lap values", () => {
  assert.equal(AppFormat.clock(65), "1:05");
  assert.equal(AppFormat.clock(3661), "1:01:01");
  assert.equal(AppFormat.clock(299.6), "5:00");
  assert.equal(AppFormat.clock(null), null);
  assert.equal(AppFormat.clock("abc"), null);
});

test("relativeDay names neighbouring days and counts whole days", () => {
  assert.equal(AppFormat.relativeDay("2026-10-10", "2026-10-10"), "Heute");
  assert.equal(AppFormat.relativeDay("2026-10-11", "2026-10-10"), "Morgen");
  assert.equal(AppFormat.relativeDay("2026-10-09", "2026-10-10"), "Gestern");
  assert.equal(AppFormat.relativeDay("2026-10-13", "2026-10-10"), "in 3 Tagen");
  assert.equal(AppFormat.relativeDay("2026-09-30", "2026-10-10"), "vor 10 Tagen");
});

test("relativeDay counts whole days across the daylight-saving change", () => {
  assert.equal(AppFormat.relativeDay("2026-03-30", "2026-03-28"), "in 2 Tagen");
  assert.equal(AppFormat.relativeDay("2026-10-26", "2026-10-24"), "in 2 Tagen");
});

test("relativeDay returns null for invalid date keys", () => {
  assert.equal(AppFormat.relativeDay("bad", "2026-10-10"), null);
  assert.equal(AppFormat.relativeDay("2026-10-10", null), null);
  assert.equal(AppFormat.relativeDay("2026-02-30", "2026-10-10"), null);
});

test("date formats date keys in de-DE without shifting the calendar day", () => {
  assert.equal(AppFormat.date("2026-09-08"), "08.09.2026");
  assert.equal(AppFormat.date("2026-01-01", { timeZone: "America/Los_Angeles" }), "01.01.2026");
  assert.equal(AppFormat.date(null), null);
  assert.equal(AppFormat.date("not a date"), null);
});

test("date and dateTime honour a valid timezone and fall back for an invalid one", () => {
  assert.equal(AppFormat.dateTime("2026-09-08T12:00:00Z", { timeZone: "Europe/Berlin" }), "08.09.2026, 14:00");
  assert.equal(AppFormat.dateTime("2026-09-08T12:00:00Z", { timeZone: "Not/AZone" }), AppFormat.dateTime("2026-09-08T12:00:00Z"));
  assert.equal(AppFormat.date("2026-09-08T12:00:00Z", { timeZone: "Not/AZone" }), AppFormat.date("2026-09-08T12:00:00Z"));
});

test("dateTime returns null for missing or invalid values", () => {
  assert.equal(AppFormat.dateTime(null), null);
  assert.equal(AppFormat.dateTime(""), null);
  assert.equal(AppFormat.dateTime("garbage"), null);
});

test("a completed session's duration leads with the total time for strength only", () => {
  // #2317: rest between sets is part of a strength session's work.
  const durations = { elapsed_duration_s: 3480, moving_duration_s: 1860 };
  assert.equal(AppFormat.sessionSeconds({ ...durations, sport: "strength" }), 3480);
  assert.equal(AppFormat.sessionSeconds({ ...durations, sport: "run" }), 1860);
  assert.equal(AppFormat.sessionSeconds(durations, "strength"), 3480, "the sport may come from the session");
  assert.equal(AppFormat.sessionSeconds({ sport: "strength", moving_duration_s: 1860 }), 1860, "without a total time the moving time stands in");
  assert.equal(AppFormat.sessionSeconds({ sport: "run", elapsed_duration_s: 3000 }), 3000, "without a moving time the total time stands in");
});

test("negative values take the minus sign and a value that rounds to zero carries none (#3933)", () => {
  assert.equal(AppFormat.number(-1.2), "−1,2");
  assert.equal(AppFormat.number(-0.04), "0");
  assert.equal(AppFormat.number(-0.04, { digits: 1, minDigits: 1 }), "0,0");
  assert.equal(AppFormat.signed(1.24), "+1,2");
  assert.equal(AppFormat.signed(-0.4), "−0,4");
  assert.equal(AppFormat.signed(0.04), "0");
  assert.equal(AppFormat.signed("x"), null);
});

test("percent, speed and pace have one spelling each (#3933)", () => {
  assert.equal(AppFormat.percent(42.5), "42,5 %");
  assert.equal(AppFormat.percent(125, { digits: 0 }), "125 %");
  assert.equal(AppFormat.percent(-0.04), "0 %");
  assert.equal(AppFormat.speed(25), "25,0 km/h");
  assert.equal(AppFormat.speed(32.94), "32,9 km/h");
  assert.equal(AppFormat.pace(400), "6:40 /km");
  assert.equal(AppFormat.pace(274), "4:34 /km");
  assert.equal(AppFormat.pace(null), null);
});

test("plural names exactly one in the singular, a fraction in the plural (#3933)", () => {
  assert.equal(AppFormat.plural(1, "Messung", "Messungen"), "1 Messung");
  assert.equal(AppFormat.plural(0, "Messung", "Messungen"), "0 Messungen");
  assert.equal(AppFormat.plural(2, "Einheit", "Einheiten"), "2 Einheiten");
  assert.equal(AppFormat.plural(0.5, "Portion", "Portionen"), "0,5 Portionen");
  assert.equal(AppFormat.plural(1234, "Einheit", "Einheiten"), "1.234 Einheiten");
  assert.equal(AppFormat.plural(undefined, "Einheit", "Einheiten"), "0 Einheiten");
});
