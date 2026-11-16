const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions, fakeDocument, textOf } = require("./frontend-harness.cjs");


const WEEK = ["nutritionChartDays", "nutritionChartRows", "nutritionEmptyRunRow", "nutritionChartDayRow", "nutritionWeekValue", "renderNutritionWeek"];
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
  const status = { textContent: "" };
  const ctx = extractFunctions("nutrition.js", ["nutritionMealTitle", "nutritionCard", "nutritionEstimateChip", "nutritionEntryActions", "markNutritionEstimate"], {
    h, document: { querySelector: () => status },
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
  await ctx.markNutritionEstimate(meal);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [["/api/nutrition/entry/estimate", { id: "m1", estimate_uncertain: true }]]);
  assert.equal(resets, 1, "the energy table reads again");
  assert.deepEqual(JSON.parse(JSON.stringify(receipts)), [["Als geschätzt markiert.", "c1"]]);
});

test("weekly chart averages observed days independently and preserves empty weeks", () => {
  const days = Array.from({ length: 84 }, (_, index) => ({ date: new Date(Date.UTC(2026, 0, index + 1)).toISOString().slice(0, 10), intake_kcal: null, expenditure_kcal: null }));
  days[0].intake_kcal = 0;
  days[1].intake_kcal = 2000;
  days[1].expenditure_kcal = 2400;
  days[83].expenditure_kcal = 2100;
  const weeks = context.nutritionChartDays(days);
  assert.equal(weeks.length, 12);
  assert.equal(weeks[0].intake_kcal, 1000);
  assert.equal(weeks[0].intake_kcal_days, 2);
  assert.equal(weeks[0].expenditure_kcal_days, 1);
  assert.equal(weeks[1].intake_kcal_days, 0);
  assert.equal(weeks[0].expenditure_kcal, 2400);
  assert.equal(weeks[1].intake_kcal, null);
  assert.equal(weeks[1].expenditure_kcal, null);
  assert.equal(weeks[11].expenditure_kcal, 2100);
  assert.equal(weeks[0].date, days[0].date);
  assert.equal(weeks[11].end_date, days[83].date);
  assert.equal(context.nutritionChartDays(days.slice(0, 14)).length, 14);
});

test("history displays daily balance from paired days alongside the total", () => {
  const document = fakeDocument();
  const markValueMissing = (node, text = "nicht erfasst") => { node.textContent = text; node.missing = true; return node; };
  const setValueWithUnit = (node, text) => { node.textContent = text; };
  const ctx = extractFunctions("nutrition.js", WEEK, { document, markValueMissing, setValueWithUnit, nutritionExpenditureDate: (date) => date, AppFormat: { number: (value) => Math.round(value).toLocaleString("de-DE") } });
  for (const [count, balance, paired, expected] of [[14, -4468, 14, "-319 kcal"], [84, -600, 3, "-200 kcal"], [14, 300, 3, "+100 kcal"], [14, 3, 14, "0 kcal"], [14, -5, 14, "0 kcal"], [14, -7, 14, "-1 kcal"], [14, 0, 2, "0 kcal"], [84, null, 0, "nicht erfasst"]]) {
    const averages = Object.fromEntries(["intake_kcal", "expenditure_kcal", "carbs_g", "protein_g", "fat_g", "sugar_g", "salt_g"].map((key) => [key, { value: null, days: 0 }]));
    ctx.renderNutritionWeek({ days: Array(count).fill({ date: "2026-01-01" }), start_date: "start", end_date: "end", balance_kcal: balance, balance_days: paired, averages });
    const tiles = document.nodes.get("#nutritionWeekTotals").children;
    assert.equal(tiles[1].children[0].textContent, expected);
    assert.equal(textOf(tiles[1].children[1]), "Ø Kalorienbilanz pro Tag");
    assert.match(textOf(tiles[1].children[2]), new RegExp(`^${paired} von ${count} Tagen`));
    assert.equal(tiles[0].style.gridColumn, undefined);
  }
});
