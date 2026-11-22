const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

function nodes() {
  const make = () => ({ hidden: true, textContent: "", dataset: {} });
  return {
    firstThresholdEstimate: make(),
    firstThresholdEstimateText: make(),
    firstThresholdStoredLabel: { textContent: "Puls (bpm)" },
    firstThresholdPercent: { textContent: "" },
  };
}

function render(estimate, profile) {
  const elements = nodes();
  const context = extractFunctions("views.js", ["FIRST_THRESHOLD_QUALITY_LABELS", "firstThresholdEstimateText", "renderFirstThresholdEstimate", "firstThresholdPercentText"], {
    document: { getElementById: (id) => elements[id] },
    state: { data: { performance: { metrics: { run_threshold_hr_bpm: { value: 170 } } } } },
  });
  context.renderFirstThresholdEstimate(estimate, profile);
  return elements;
}

test("the estimate names bpm, percent of the LTHR, sessions and quality and is labelled as an estimate", () => {
  const elements = render({ percent_lthr: 90.5, bpm: 153, sessions: 4, quality: "low" }, {});
  assert.equal(elements.firstThresholdEstimate.hidden, false);
  assert.equal(elements.firstThresholdEstimate.dataset.percent, "90.5");
  assert.equal(elements.firstThresholdEstimateText.textContent, "DFA-α1 · 153 bpm · 90,5 % LSHF · 4 Einheiten · Qualität niedrig");
});

test("without a current threshold the bpm is left out and one session is singular", () => {
  const elements = render({ percent_lthr: 88, bpm: null, sessions: 1, quality: "good" }, {});
  assert.equal(elements.firstThresholdEstimateText.textContent, "DFA-α1 · 88 % LSHF · 1 Einheit · Qualität gut");
});

test("no estimate hides the block", () => {
  assert.equal(render(null, {}).firstThresholdEstimate.hidden, true);
});

test("a stored adopted estimate is marked on the stored bpm, the percent is display only (#3915)", () => {
  const adopted = render(null, { first_threshold_hr: "153", first_threshold_origin: "estimated" });
  assert.equal(adopted.firstThresholdStoredLabel.textContent, "Puls (bpm, DFA-α1)");
  // 153 of today's 170 bpm threshold HR.
  assert.equal(adopted.firstThresholdPercent.textContent, "90 % LSHF");
  const measured = render(null, { first_threshold_hr: "144", first_threshold_origin: "measured" });
  assert.equal(measured.firstThresholdStoredLabel.textContent, "Puls (bpm)");
  assert.equal(measured.firstThresholdPercent.textContent, "84,7 % LSHF");
  assert.equal(render(null, {}).firstThresholdPercent.textContent, "");
});

test("the plan card names the method of an estimated cap source", () => {
  const plan = fs.readFileSync(path.join(__dirname, "../public/plan-views.js"), "utf8");
  assert.match(plan, /estimated: "DFA-α1"/);
});

function adoptContext({ dirty, other = "Ada" }) {
  const elements = { ...nodes(), profileForm: { elements: { first_threshold_hr: { value: "145" } } } };
  const rendered = [];
  const dirtyFlags = [];
  const context = extractFunctions("settings.js", ["applyAdoptedFirstThreshold"], {
    $: (selector) => elements[selector.slice(1)],
    state: {
      profileDirty: dirty,
      profileBaseline: { first_threshold_hr: "145", name: "" },
      data: { profile: { first_threshold_hr: "145", name: "" }, first_threshold_estimate: null },
    },
    profileFieldText: (value) => (typeof value === "string" ? value : ""),
    profileFormValues: (form) => ({ first_threshold_hr: form.elements.first_threshold_hr.value, name: other }),
    determineChangedProfileFields: (values, baseline) => Object.fromEntries(Object.entries(values).filter(([key, value]) => value !== baseline[key])),
    renderFirstThresholdEstimate: (estimate, profile) => rendered.push([estimate, profile.first_threshold_hr]),
    setDirtyIndicator: (id, flag) => dirtyFlags.push([id, flag]),
  });
  return { context, elements, rendered, dirtyFlags };
}

test("an adopted estimate reaches the input and the estimate block while the form holds other edits", () => {
  const { context, elements, rendered, dirtyFlags } = adoptContext({ dirty: true });
  const estimate = { percent_lthr: 90, sessions: 4, quality: "low" };
  context.applyAdoptedFirstThreshold({ profile: { first_threshold_hr: "153", first_threshold_origin: "estimated", name: "" }, first_threshold_estimate: estimate });
  assert.equal(elements.profileForm.elements.first_threshold_hr.value, "153");
  assert.equal(context.state.profileBaseline.first_threshold_hr, "153");
  assert.equal(context.state.data.profile.first_threshold_origin, "estimated");
  assert.deepEqual(rendered, [[estimate, "153"]]);
  // The unsaved name edit stays, so the form is still dirty.
  assert.equal(context.state.profileDirty, true);
  assert.deepEqual(dirtyFlags, [["profileDirtyIndicator", true]]);
});

test("the form is clean again when the adopted value was its only edit", () => {
  const { context, dirtyFlags } = adoptContext({ dirty: true, other: "" });
  context.applyAdoptedFirstThreshold({ profile: { first_threshold_hr: "153", first_threshold_origin: "estimated" }, first_threshold_estimate: null });
  assert.equal(context.state.profileDirty, false);
  assert.deepEqual(dirtyFlags, [["profileDirtyIndicator", false]]);
});

test("a step test's LT1 is adopted with the same refresh as the profile's estimate (#3833)", async () => {
  const calls = [];
  const result = { adopted: true, profile: { first_threshold_hr: "153" }, first_threshold_estimate: null };
  const context = extractFunctions("plan-views.js", ["adoptStepTestValue"], {
    state: { data: { profile: {} } },
    api: async (url, options) => { calls.push(["api", url, JSON.parse(options.body)]); return result; },
    applyAdoptedFirstThreshold: (value) => calls.push(["apply", value]),
    invalidateContextPreview: () => calls.push(["invalidate"]),
    toast: (text) => calls.push(["toast", text]),
    load: async () => { calls.push(["load"]); },
    requestConfirmation: async () => true,
  });
  await context.adoptStepTestValue("act_1", { key: "lt1", adopt: { heart_rate_bpm: 152 } }, { disabled: false });
  assert.deepEqual(calls.map(([name]) => name), ["api", "apply", "invalidate", "toast", "load"]);
  assert.deepEqual(calls[0][2], { step_test: "act_1", heart_rate_bpm: 152 });
  assert.equal(calls[1][1], result);
});
