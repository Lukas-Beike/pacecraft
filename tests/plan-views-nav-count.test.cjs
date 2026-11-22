const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

// #3384: the Kalender badge adds the running week's units to sync to the undecided events, by the
// week sync button's own rule (plannedWeekJobs).
const hash = "a".repeat(64);
const ids = { intervals: "intervals", garmin: "garmin", wahoo: "wahoo" };
const addDateKey = (key, days) => {
  const date = new Date(`${key}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
};

function navCount(calendar, undecided) {
  const link = { attributes: {}, badge: null,
    querySelector() { return this.badge; },
    append(...nodes) { this.badge = nodes.at(-1); },
    setAttribute(name, value) { this.attributes[name] = value; } };
  const state = { data: {
    profile: { timezone: "Europe/Berlin" },
    configured: { intervals: true },
    garmin: { configured: true, can_write_workouts: true },
    external_calendar: { undecided_upcoming: undecided },
  } };
  const context = extractFunctions("plan-views.js", ["runningWeekSyncCount", "weekSyncNoticeText", "renderUndecidedNavCount", "undecidedPlannedCount", "undecidedNoticeText", "plannedWeekJobs", "plannedWeekSyncJobs", "plannedPushTargets", "plannedGarminSdkRequired", "plannedGarminBlockedText", "leftoverEntry", "PLAN_PUSH_MAX_ENTRIES", "plannedLeftoversByWeek", "plannedLeftoverItems", "plannedLeftoverWeek", "plannedProviderConfiguration"], {
    state,
    renderedCalendar: calendar,
    document: { querySelectorAll: () => [link] },
    h: (tag, attributes) => ({ tag, attributes, textContent: "" }),
    timezoneDateKey: () => "2026-11-18",
    planWeekStart: (day) => addDateKey(day, -((new Date(`${day}T00:00:00Z`).getUTCDay() + 6) % 7)),
    addDateKey,
    dateFromKey: (key) => new Date(`${key}T00:00:00Z`),
    UNDECIDED_NOTICE_DAYS: 14,
    CALENDAR_WEEKS: { past: 1, future: 4 },
    plannedEventDate: (entry) => entry.date,
    calendarActualActivity: () => null,
    providerIds: () => ids,
    focusSport: (entry) => String(entry?.sport || "").toLowerCase() || null,
  });
  context.renderUndecidedNavCount();
  return { count: context.runningWeekSyncCount(calendar), badge: link.badge.textContent, label: link.attributes["aria-label"] };
}

const pending = (id, date, push_pending) => ({ local_id: id, date, payload_hash: hash, push_pending });

test("the running week's units to sync count once each, by the week sync button's rule", () => {
  const calendar = [
    pending("new", "2026-11-19", { intervals: true }),
    // Waiting for two providers is still one unit.
    pending("both", "2026-11-20", { intervals: true, garmin: true }),
    // Synced, completed, archived and next week's units do not count.
    pending("synced", "2026-11-20", { intervals: false, garmin: false }),
    { ...pending("done", "2026-11-19", { intervals: true }), is_completed_activity: true },
    { ...pending("archived", "2026-11-21", { intervals: true }), archived: true },
    pending("next", "2026-11-24", { intervals: true }),
    // A leftover the week sync removes from Garmin counts as well.
    { local_id: "old", date: "2026-11-17", push_cleanup: { garmin: [{ id: "g1", date: "2026-11-17" }] } },
  ];
  const { count, badge, label } = navCount(calendar, 2);
  assert.equal(count, 3);
  assert.equal(badge, "5", "one sum with the undecided events");
  assert.match(label, /^Kalender 5, 2 neue Termine bis \d\d\.\d\d\., 3 Einheiten zu synchronisieren$/);
});

test("the label names only the non-zero parts, and nothing shows without either", () => {
  const one = navCount([pending("new", "2026-11-19", { intervals: true })], 0);
  assert.equal(one.badge, "1");
  assert.equal(one.label, "Kalender 1, 1 Einheit zu synchronisieren");
  const none = navCount([pending("synced", "2026-11-19", { intervals: false })], 0);
  assert.equal(none.badge, "");
  assert.equal(none.label, "Kalender");
  const undecided = navCount([], 1);
  assert.match(undecided.label, /^Kalender 1, 1 neuer Termin bis /);
});
