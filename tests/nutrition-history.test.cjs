const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions, fakeDocument, textOf } = require("./frontend-harness.cjs");


const WEEK = ["nutritionChartDays", "nutritionChartRows", "nutritionEmptyRunRow", "nutritionWeekValue"];
const context = extractFunctions("nutrition.js", WEEK);

// Gitar #3269: the energy table lives in the diary since #3252.
test("logout clears the diary's energy table and a diary refresh reads it again", () => {
  const cleared = [];
  const nodes = new Map();
  const node = (selector) => nodes.get(selector) || nodes.set(selector, { value: "", hidden: false, close() {}, replaceChildren() { cleared.push(selector); } }).get(selector);
  const document = { getElementById: (id) => node(id), querySelector: (selector) => node(selector) };
  let resets = 0;
  const loads = [];
  const ctx = extractFunctions("nutrition.js", ["resetNutritionView", "nutritionRefresh"], {
    document, resetSkinfolds() {}, stopNutritionBarcode() {}, resetEnergy: () => { resets += 1; },
    loadNutrition: (options) => loads.push(options),
    nutritionLoadSequence: 0, nutritionProductLoadSequence: 0, nutritionLookupResultShown: false,
    nutritionProductSectionsOpen: {}, nutritionProductSearchShown: "", nutritionProductSectionsBeforeSearch: null, nutritionProductForUse: null,
    AppViewRefresh: { create: (config) => config }, api() {}, waitForSyncJob() {}, state: {}, todayIso: () => "2026-11-16",
  });
  ctx.resetNutritionView();
  assert.ok(cleared.includes("nutritionEnergy"), "the previous athlete's table does not stay in the diary");
  ctx.nutritionRefresh.reload("nutrition/diary");
  assert.equal(resets, 1, "the diary refresh forgets the energy read");
  assert.equal(loads.length, 1);
});

// #3100 part 2: a meal flagged as estimated says so and its button flips the flag with undo.
test("a diary card marks an estimated meal and its button flips the flag", async () => {
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const calls = [];
  let resets = 0;
  const receipts = [];
  const opened = [];
  const status = { textContent: "" };
  const ctx = extractFunctions("nutrition.js", ["nutritionMealTitle", "NUTRITION_MEAL_LABELS", "NUTRITION_SOURCE_LABELS", "nutritionCard", "nutritionEstimateChip", "nutritionEntryActions", "nutritionEditButton", "markNutritionEstimate"], {
    h, document: { querySelector: () => status },
    messageActionButton: (icon, label) => h("button", { "aria-label": label }, icon), openNutritionMealEdit: (item) => opened.push(item.id),
    createStatusChip: (label) => h("span", null, label),
    nutritionBasisLabel: () => "", nutritionSessionBlock: () => null, nutritionComponentDetails: () => null,
    nutritionNumber: (value) => String(value), AppFormat: { number: String },
    deleteNutritionEntry() {}, requestConfirmation: async () => true,
    api: async (path, options) => { calls.push([path, JSON.parse(options.body)]); return { change: { id: "c1" } }; },
    resetEnergy: () => { resets += 1; }, loadNutrition: async () => {},
    showNutritionReceipt: (_status, message, change) => receipts.push([message, change.id]),
  });
  const meal = { id: "m1", meal_type: "dinner", logged_at: "2026-11-16T19:30:00", description: "Pizza", kcal: 950, source: "manual", sync_state: "local" };
  const flagged = ctx.nutritionCard({ ...meal, estimate_uncertain: true }, false);
  assert.match(textOf(flagged.children[0]), /Menge geschätzt/);
  const buttons = (card) => card.children.filter((child) => child.tagName === "BUTTON").map(textOf);
  assert.deepEqual(buttons(flagged), ["Nicht geschätzt", "Löschen"]);
  const plain = ctx.nutritionCard(meal, false);
  assert.doesNotMatch(textOf(plain.children[0]), /geschätzt/);
  assert.deepEqual(buttons(plain), ["Als geschätzt markieren", "Löschen"]);
  // The pencil in the head opens the meal's components; a template has none.
  const pencil = plain.children[0].children[1];
  assert.equal(pencil.attributes["aria-label"], "Mahlzeit bearbeiten: Pizza");
  pencil.fire("click");
  assert.deepEqual(opened, ["m1"]);
  assert.equal(ctx.nutritionCard({ ...meal, name: "Vorlage" }, true).children[0].tagName, "H3");
  await ctx.markNutritionEstimate(meal);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [["/api/nutrition/entry/estimate", { id: "m1", estimate_uncertain: true }]]);
  assert.equal(resets, 1, "the energy table reads again");
  assert.deepEqual(JSON.parse(JSON.stringify(receipts)), [["Als geschätzt markiert.", "c1"]]);
});

test("weekly chart averages observed days independently and preserves empty weeks", () => {
  const days = Array.from({ length: 91 }, (_, index) => ({ date: new Date(Date.UTC(2026, 0, index + 1)).toISOString().slice(0, 10), intake_kcal: null, expenditure_kcal: null, balance_kcal: null }));
  days[0].intake_kcal = 0;
  days[1].intake_kcal = 2000;
  days[1].expenditure_kcal = 2400;
  days[1].balance_kcal = -400;
  days[90].expenditure_kcal = 2100;
  const weeks = context.nutritionChartDays(days);
  // 3 months are 13 weeks and 6 months 26; 4 weeks keep their days (#3704).
  assert.equal(weeks.length, 13);
  assert.equal(context.nutritionChartDays(Array.from({ length: 182 }, () => ({ date: "2026-01-01" }))).length, 26);
  assert.equal(weeks[0].balance_kcal_days, 1);
  assert.equal(weeks[0].balance_kcal, -400);
  assert.equal(weeks[1].balance_kcal, null);
  assert.equal(weeks[0].intake_kcal, 1000);
  assert.equal(weeks[0].intake_kcal_days, 2);
  assert.equal(weeks[0].expenditure_kcal_days, 1);
  assert.equal(weeks[1].intake_kcal_days, 0);
  assert.equal(weeks[0].expenditure_kcal, 2400);
  assert.equal(weeks[1].intake_kcal, null);
  assert.equal(weeks[1].expenditure_kcal, null);
  assert.equal(weeks[12].expenditure_kcal, 2100);
  assert.equal(weeks[0].date, days[0].date);
  assert.equal(weeks[12].end_date, days[90].date);
  assert.equal(context.nutritionChartDays(days.slice(0, 28)).length, 28);
});

const BALANCE = ["NUTRITION_WEEK_MACROS", "nutritionStatTile", "nutritionMacroCard", "nutritionGoalText", "nutritionBalanceCard", "nutritionPeriodText", "nutritionDayLabel", "nutritionWeekShown", "nutritionWeekValue", "nutritionChartSeries", "nutritionChartBalance", "nutritionChartDayRow"];

// #3704: the Bilanz card's hero is the mean balance per day, coloured by its distance to the goal.
test("the Bilanz and Makros cards: hero by goal distance, three tiles, no empty Zucker or Salz tile", () => {
  const markValueMissing = (node, text = "nicht erfasst") => { node.textContent = text; node.missing = true; return node; };
  const setValueWithUnit = (node, text) => { node.textContent = text; };
  const { h } = extractFunctions("components.js", ["h", "SVG_NS", "svgNode"], { document: fakeDocument() });
  const verlauf = extractFunctions("analysis.js", ["ANALYSIS_PERIODS", "analysisPeriod", "verlaufPeriod", "verlaufHead", "verlaufGoalDistance"], { h, nutritionHistoryPeriod: "28d" });
  const ctx = extractFunctions("nutrition.js", BALANCE, {
    h, markValueMissing, setValueWithUnit, nutritionExpenditureDate: (date) => date.split("-").reverse().join("."),
    AppFormat: { number: (value) => Math.round(value).toLocaleString("de-DE") }, formatWhole: (value) => String(value),
    verlaufPeriod: verlauf.verlaufPeriod, verlaufHead: verlauf.verlaufHead, verlaufGoalDistance: verlauf.verlaufGoalDistance,
  });
  const averages = (days) => ({ intake_kcal: { value: 2200, days }, expenditure_kcal: { value: 2500, days }, carbs_g: { value: 250, days }, protein_g: { value: 120, days }, fat_g: { value: 70, days }, sugar_g: { value: null, days: 0 }, salt_g: { value: 5.5, days: 3 } });
  const week = (balance, paired) => ({ days: Array(28).fill({ date: "2026-01-01" }), start_date: "2026-10-22", end_date: "2026-11-18", balance_kcal: balance, balance_days: paired, averages: averages(paired), expenditure_source: "own" });
  const lose = { goal: "lose", goal_kcal: 300 };
  // −8,400 over 28 days is −300 a day: the goal itself, "near".
  const card = ctx.nutritionBalanceCard(week(-8400, 28), lose);
  assert.equal(textOf(card.children[0]), "Bilanz22.10.–18.11.2026 · 28/28 Tage erfasst");
  const hero = card.children[1];
  assert.equal(hero.dataset.goalDistance, "near");
  assert.equal(textOf(hero), "-300 kcalØ pro TagZiel −300");
  assert.deepEqual(card.children[2].children.map(textOf), ["Aufnahme2.200 kcalØ pro Tag", "Verbrauch2.500 kcalØ pro Tag · aus deinen Daten", "Summe-8.400 kcal"]);
  // 200 off the goal is "mid", more than 350 "far"; a surplus against a loss goal is far.
  assert.equal(ctx.nutritionBalanceCard(week(-2800, 28), lose).children[1].dataset.goalDistance, "mid");
  assert.equal(ctx.nutritionBalanceCard(week(11200, 28), lose).children[1].dataset.goalDistance, "far");
  assert.equal(textOf(ctx.nutritionBalanceCard(week(11200, 28), { goal: "build", goal_kcal: 400 }).children[1]), "+400 kcalØ pro TagZiel +400");
  // Without a goal the hero keeps its own colour and names none; a partial period names its days.
  const free = ctx.nutritionBalanceCard(week(-600, 3), null);
  assert.deepEqual(free.children[1].dataset, {});
  assert.equal(textOf(free.children[1]), "-200 kcalØ pro Tag");
  assert.match(textOf(free.children[0]), /3\/28 Tage erfasst$/);
  assert.equal(ctx.nutritionBalanceCard(week(null, 0), lose).children[1].children[0].missing, true);
  // Zucker and Salz show only when logged on any day of the period: no empty tile.
  const macros = ctx.nutritionMacroCard(week(-8400, 28));
  assert.equal(textOf(macros.children[0]), "Makros4 Wochen · Ø pro Tag");
  assert.deepEqual(macros.children[1].children.map((tile) => textOf(tile.children[0])), ["Kohlenhydrate", "Protein", "Fett", "Salz"]);
  assert.equal(macros.children[1].children.some((tile) => tile.children[1].missing), false);
  // A Tagesverlauf row: its two bars and the balance on the right, coloured by the goal distance.
  const day = { date: "2026-10-22", intake_kcal: 2000, expenditure_kcal: 2300, balance_kcal: -300 };
  const row = ctx.nutritionChartDayRow(day, 2300, lose);
  assert.deepEqual(row.children[1].children.map((bars) => bars.className), ["nutrition-week-chart-series is-intake", "nutrition-week-chart-series is-expenditure"]);
  assert.equal(row.children[2].dataset.goalDistance, "near");
  assert.equal(textOf(row.children[2]), "-300");
  assert.deepEqual(ctx.nutritionChartDayRow({ ...day, balance_kcal: null }, 2300, lose).children[2].dataset, {});
});

// #3852: the energy read follows every change of the day's entries the read consumes, not only kcal.
test("the diary's intake key changes with kcal, macros or the estimate flag of an entry", () => {
  const { nutritionIntakeKey } = extractFunctions("nutrition.js", ["nutritionIntakeKey"]);
  const meal = { id: "a", kcal: 640, protein_g: 20, carbs_g: 90, fat_g: 15, estimate_uncertain: false };
  const day = (entry) => ({ date: "2026-11-21", total_kcal: entry.kcal, entries: [entry] });
  const shown = nutritionIntakeKey(day(meal));
  assert.equal(nutritionIntakeKey(day({ ...meal })), shown);
  for (const change of [{ kcal: 700 }, { protein_g: 40 }, { estimate_uncertain: true }]) {
    assert.notEqual(nutritionIntakeKey(day({ ...meal, ...change })), shown, JSON.stringify(change));
  }
});

// #3930 N-01: the start of a period across a year keeps its year, and the days of a long period name theirs.
test("a Verlauf period keeps the year of its start, and a long period labels its days with the year", () => {
  const vm = require("node:vm");
  const ctx = extractFunctions("nutrition.js", ["nutritionExpenditureDate", "nutritionDayLabel", "nutritionPeriodText", "nutritionWeekShown"]);
  assert.equal(ctx.nutritionPeriodText("2025-11-22", "2026-11-21"), "22.11.2025–21.11.2026");
  assert.equal(ctx.nutritionPeriodText("2026-10-22", "2026-11-18"), "22.10.–18.11.2026");
  assert.equal(ctx.nutritionDayLabel("2026-11-18"), "18.11", "no year without a week on screen");
  vm.runInContext("nutritionWeekShown = { days: new Array(28) }", ctx);
  assert.equal(ctx.nutritionDayLabel("2026-11-18"), "18.11");
  vm.runInContext("nutritionWeekShown = { days: new Array(365) }", ctx);
  assert.equal(ctx.nutritionDayLabel("2026-11-18"), "18.11.2026");
});

// #3930 N-06: only a missing confidence hides the line; a photo read of 0 is the lowest reading and keeps its warning.
test("the confidence line is hidden for a manual product and shown, with its warning, for a photo read of 0", () => {
  const line = { hidden: false, textContent: "" };
  const checklist = { hidden: false, replaceChildren() {} };
  const form = { querySelector: (selector) => (selector === "[data-extraction-confidence]" ? line : checklist), elements: { namedItem: () => null } };
  const { setNutritionProductConfidence } = extractFunctions("nutrition.js", ["setNutritionProductConfidence"], { h: () => ({}) });
  setNutritionProductConfidence(form, { confidence: null });
  assert.deepEqual([line.hidden, line.textContent], [true, ""]);
  setNutritionProductConfidence(form, { confidence: 0 });
  assert.equal(line.hidden, false);
  assert.match(line.textContent, /^Erkennungssicherheit: 0 %\. Geringe Erkennungssicherheit/);
  setNutritionProductConfidence(form, { confidence: 0.9 });
  assert.match(line.textContent, /^Erkennungssicherheit: 90 %\. Erkannte Werte/);
});
