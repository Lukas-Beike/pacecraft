// One editor for a planned unit and a library entry (#3950, #3951): sport, name, day, focus and target, the
// steps of an endurance unit or the exercises of a strength unit as rows. The server stores it through the
// Coach's use cases and checks it the same way; nothing goes to a provider.
const UNIT_SPORTS = [["Run", "Lauf"], ["Ride", "Rad"], ["VirtualRide", "Rad indoor"], ["WeightTraining", "Kraft"]];
const UNIT_TARGETS = [["POWER", "Leistung"], ["HR", "Puls"], ["PACE", "Tempo"], ["AUTO", "Auto"]];
const UNIT_LOADS = [["", "Ohne Last"], ["kg", "kg"], ["pct_1rm", "% 1RM"], ["rpe", "RPE"], ["rir", "RIR"]];
const UNIT_STEP = /^-\s*(\d+)m\s+(\d+(?:[.,]\d+)?%|Z[1-7]\s+(?:HR|Pace))$/;
const UNIT_REPEAT = /^(?:(.*\S)\s+)?(\d+)x$/;
const UNIT_NEW_STEP = { POWER: "60%", AUTO: "60%", HR: "Z2 HR", PACE: "Z2 Pace" };
const UNIT_EDITOR_SPORT_KEY = { Run: "run", Ride: "ride", VirtualRide: "ride", WeightTraining: "strength" };
let unitEditor = null;

function unitSportOf(entry) {
  const sport = String(entry?.sport || "").toLowerCase();
  if (sport === "run") return "Run";
  if (sport === "strength") return "WeightTraining";
  if (sport === "ride") return ["indoor", "virtual"].includes(entry.environment) ? "VirtualRide" : "Ride";
  return entry?.type || "Ride";
}

// The workout text as rows ({ kind: "step"|"label"|"repeat" }); null when a line is not one of them, so the
// editor shows the text itself and nothing the Coach wrote is lost.
// One line as a row: a step, the head of a repeat or a label; null when the rows cannot show it.
function unitParsedLine(line, inGroup) {
  const step = UNIT_STEP.exec(line);
  if (step) return { kind: "step", minutes: Number(step[1]), value: step[2].replace(",", ".").replace(/\s+/, " ") };
  if (line.startsWith("-")) return null;
  const repeat = UNIT_REPEAT.exec(line);
  if (repeat) return { kind: "repeat", name: repeat[1] || "", times: Number(repeat[2]), steps: [] };
  return inGroup ? null : { kind: "label", name: line };
}

function unitStepRows(text) {
  const rows = [];
  let group = null;
  for (const line of String(text || "").split("\n").map((raw) => raw.trim())) {
    if (!line) { group = null; continue; }
    const row = unitParsedLine(line, Boolean(group));
    if (!row) return null;
    if (row.kind === "step" && group) group.steps.push(row);
    else rows.push(row);
    if (row.kind === "repeat") group = row;
  }
  // Prose without a step line stays text: rows would read it as a duration of 0.
  return rows.length && rows.every((row) => row.kind === "label") ? null : rows;
}

function unitRowHead(row) {
  if (row.kind === "label") return row.name;
  return [row.name, `${row.times}x`].filter(Boolean).join(" ");
}

function unitStepsText(rows) {
  const lines = [];
  const stepLine = (row) => `- ${row.minutes}m ${row.value}`;
  for (const row of rows) {
    if (row.kind === "step") { lines.push(stepLine(row)); continue; }
    if (lines.length && lines.at(-1)) lines.push("");
    lines.push(unitRowHead(row));
    if (row.kind === "repeat") lines.push(...row.steps.map(stepLine), "");
  }
  return lines.join("\n").trim();
}

function unitRowMinutes(row) {
  if (row.kind === "step") return row.minutes;
  return row.kind === "repeat" ? row.times * unitStepsMinutes(row.steps) : 0;
}

function unitStepsMinutes(rows) {
  return rows.reduce((sum, row) => sum + unitRowMinutes(row), 0);
}

function unitExerciseRow(item = {}) {
  const load = ["kg", "pct_1rm", "rpe", "rir"].find((key) => item[key] != null) || "";
  const base = { name: "", sets: 3, reps: 8, kg: null, pct_1rm: null, rpe: null, rir: null, rest_s: null, warmup: null, note: null, garmin_exercise: null, ...item };
  return { base, load, shown: load, amount: load ? String(item[load]) : "" };
}

// The row edits one load; a further stored one (kg beside an RPE) stays as it is.
function unitExercises(rows) {
  return rows.filter((row) => row.base.name.trim()).map((row) => {
    const loads = { kg: row.base.kg, pct_1rm: row.base.pct_1rm, rpe: row.base.rpe, rir: row.base.rir };
    if (row.shown) loads[row.shown] = null;
    if (row.load) {
      // A relative load replaces the stored one; kg may stay beside it.
      if (row.load !== "kg") for (const key of ["pct_1rm", "rpe", "rir"]) loads[key] = null;
      loads[row.load] = row.amount === "" ? null : Number(row.amount);
    }
    return { ...row.base, name: row.base.name.trim(), ...loads };
  });
}

function unitEditorFrom({ kind, entry, dateKey }) {
  const sport = entry ? unitSportOf(entry) : "Ride";
  const parsed = entry && sport !== "WeightTraining" ? unitStepRows(entry.description) : [];
  const start = String(entry?.starts_at_local || "");
  // Midnight is the stored form of a unit without a time.
  const time = /T(\d\d:\d\d)/.exec(start)?.[1].replace("00:00", "") || "";
  return {
    kind, entry, sport, originalSport: sport,
    name: entry?.name || "", date: entry?.date ? String(entry.date).slice(0, 10) : dateKey || "", time, originalTime: time,
    // A derived focus or intensity is offered by the calendar card, never saved as the athlete's own.
    focus: entry?.focus_origin === "derived" ? "" : entry?.session_focus || "",
    intensity: entry?.intensity_origin === "derived" ? "" : entry?.strength_intensity || "",
    target: entry?.target || "POWER", text: entry?.description || "", rows: parsed || [], textMode: parsed === null,
    exercises: (entry?.strength_structure || []).map(unitExerciseRow),
    duration: entry ? String(Math.round(Number(entry.duration_minutes) || (entry.planned_duration_s || 0) / 60) || "") : "45",
    accept: false, templateId: "",
  };
}

// The content a strength or an endurance unit sends: exercises, or the steps as text with their duration.
function unitContentFields(editor) {
  if (editor.sport === "WeightTraining") {
    const structure = unitExercises(editor.exercises);
    // The server writes the text from the exercises; the lines only name them.
    return { strength_intensity: editor.intensity || null, strength_structure: structure, description: structure.map((item) => `${item.name}: ${item.sets}x${item.reps}`).join("\n"), duration_minutes: Number(editor.duration) };
  }
  const text = editor.textMode ? editor.text.trim() : unitStepsText(editor.rows);
  return { strength_intensity: null, description: text, duration_minutes: editor.textMode ? Number(editor.duration) : unitStepsMinutes(editor.rows) };
}

// Where and when a unit stands: its day, a time (midnight is the stored form of none), its library entry and plan.
function unitPlacementFields(editor) {
  const body = { date: editor.date };
  if (editor.time || editor.originalTime) body.starts_at_local = `${editor.date}T${editor.time || "00:00"}:00`;
  if (editor.templateId) body.template_id = editor.templateId;
  return body;
}

function unitEditorBody(editor) {
  const strength = editor.sport === "WeightTraining";
  const focus = FOCUS_OPTIONS[UNIT_EDITOR_SPORT_KEY[editor.sport]] ? editor.focus || null : null;
  const body = { name: editor.name.trim(), target: strength ? "AUTO" : editor.target, session_focus: focus, ...unitContentFields(editor) };
  if (!editor.entry || editor.sport !== editor.originalSport) body.sport = editor.sport;
  return editor.kind === "unit" ? { ...body, ...unitPlacementFields(editor) } : body;
}

function unitEditorTitle(editor) {
  const noun = editor.kind === "unit" ? "Einheit" : "Bibliothekseinheit";
  return `${noun} ${editor.entry ? "bearbeiten" : "hinzufügen"}`;
}

function unitEditorPath({ kind, entry }) {
  const base = kind === "library" ? "/api/library" : "/api/planned-units";
  if (!entry) return base;
  return `${base}/${encodeURIComponent(kind === "library" ? entry.id : entry.local_id)}/update`;
}

function unitEditorProblem(editor, body) {
  if (!body.name) return "Der Name fehlt.";
  if (editor.kind === "unit" && !editor.date) return "Der Tag fehlt.";
  if (body.description) return "";
  return editor.sport === "WeightTraining" ? "Mindestens eine Übung fehlt." : "Mindestens ein Schritt fehlt.";
}

async function saveUnitEditor(dialog, status, save, onSaved) {
  const editor = unitEditor;
  const body = unitEditorBody(editor);
  status.textContent = unitEditorProblem(editor, body);
  if (status.textContent) return;
  if (editor.collect) {
    // A unit of a period replacement is kept by the caller and saved with the whole replacement.
    dialog.close();
    await onSaved(body, editor);
    return;
  }
  if (editor.kind === "unit" && editor.entry) body.expected_payload_hash = editor.entry.payload_hash;
  if (editor.accept) body.accept_plan_findings = ["illness_hard"];
  save.disabled = true;
  try {
    const result = await api(unitEditorPath(editor), { method: "POST", body: JSON.stringify(body) });
    dialog.close();
    await onSaved(result, editor);
  } catch (error) {
    status.textContent = error.message;
    if (error.reason === "illness_hard") { editor.accept = true; save.textContent = "Trotzdem speichern"; }
    save.disabled = false;
  }
}

function unitField(label, control) {
  return h("label", null, label, control);
}

function unitChipField(label, control) {
  return h("fieldset", "chip-field", h("legend", null, label), control);
}

function unitChips(name, options, current, onPick) {
  const group = h("div", { className: "chip-group", role: "radiogroup", "aria-label": name });
  for (const [value, label] of options) {
    const input = h("input", { type: "radio", name: `unitEditor-${name}`, value, checked: value === current });
    input.addEventListener("change", () => onPick(value));
    group.append(h("label", "chip", input, h("span", null, label)));
  }
  return group;
}

function unitStepValue(row, onChange) {
  if (row.value.endsWith("%")) {
    const input = h("input", { type: "number", min: 1, max: 300, step: "any", inputmode: "decimal", value: row.value.slice(0, -1), "aria-label": "Prozent der Schwelle" });
    input.addEventListener("input", () => { row.value = `${input.value}%`; onChange(); });
    return h("span", "unit-step-value", input, h("span", null, "%"));
  }
  const kind = row.value.endsWith("Pace") ? "Pace" : "HR";
  const select = h("select", { "aria-label": "Zone" }, [1, 2, 3, 4, 5, 6, 7].map((zone) => h("option", { value: `Z${zone} ${kind}`, selected: row.value === `Z${zone} ${kind}` }, `Zone ${zone} · ${kind === "HR" ? "Puls" : "Tempo"}`)));
  select.addEventListener("change", () => { row.value = select.value; onChange(); });
  return select;
}

function unitStepRow(row, rows, redraw, onChange) {
  const minutes = h("input", { type: "number", min: 1, max: 600, inputmode: "numeric", value: row.minutes, "aria-label": "Minuten" });
  minutes.addEventListener("input", () => { row.minutes = Number(minutes.value) || 0; onChange(); });
  const remove = h("button", { type: "button", className: "secondary-button", "aria-label": "Schritt entfernen" }, "✕");
  remove.addEventListener("click", () => { rows.splice(rows.indexOf(row), 1); redraw(); });
  return h("li", "unit-row", minutes, unitStepValue(row, onChange), remove);
}

function unitRowsList(rows, redraw, onChange) {
  const list = h("ul", "unit-rows");
  for (const row of rows) {
    if (row.kind === "step") { list.append(unitStepRow(row, rows, redraw, onChange)); continue; }
    const name = h("input", { type: "text", maxLength: 60, value: row.name, "aria-label": row.kind === "label" ? "Abschnitt" : "Name der Wiederholung" });
    name.addEventListener("input", () => { row.name = name.value; });
    const remove = h("button", { type: "button", className: "secondary-button", "aria-label": "Abschnitt entfernen" }, "✕");
    remove.addEventListener("click", () => { rows.splice(rows.indexOf(row), 1); redraw(); });
    if (row.kind === "label") { list.append(h("li", "unit-row is-label", name, remove)); continue; }
    const times = h("input", { type: "number", min: 2, max: 50, inputmode: "numeric", value: row.times, "aria-label": "Wiederholungen" });
    times.addEventListener("input", () => { row.times = Number(times.value) || 2; onChange(); });
    const add = h("button", { type: "button", className: "secondary-button" }, "+ Schritt");
    add.addEventListener("click", () => { row.steps.push({ kind: "step", minutes: 5, value: UNIT_NEW_STEP[unitEditor.target] }); redraw(); });
    list.append(h("li", "unit-group", h("div", "unit-row", name, h("span", null, "×"), times, remove), unitRowsList(row.steps, redraw, onChange), add));
  }
  return list;
}

function unitExerciseList(editor, redraw) {
  const list = h("ul", "unit-rows");
  for (const row of editor.exercises) {
    const name = h("input", { type: "text", maxLength: 80, value: row.base.name, placeholder: "Übung", "aria-label": "Übung" });
    name.addEventListener("input", () => { row.base.name = name.value; });
    const numbers = [["sets", "Sätze", 1, 10], ["reps", "Wiederholungen", 1, 100]].map(([key, label, min, max]) => {
      const input = h("input", { type: "number", min, max, inputmode: "numeric", value: row.base[key], "aria-label": label });
      input.addEventListener("input", () => { row.base[key] = Number(input.value) || min; });
      return input;
    });
    const load = h("select", { "aria-label": "Last" }, UNIT_LOADS.map(([value, label]) => h("option", { value, selected: value === row.load }, label)));
    const amount = h("input", { type: "number", min: 0, step: "any", inputmode: "decimal", value: row.amount, disabled: !row.load, "aria-label": "Wert der Last" });
    load.addEventListener("change", () => { row.load = load.value; amount.disabled = !row.load; if (!row.load) { amount.value = ""; } row.amount = amount.value; });
    amount.addEventListener("input", () => { row.amount = amount.value; });
    const remove = h("button", { type: "button", className: "secondary-button", "aria-label": "Übung entfernen" }, "✕");
    remove.addEventListener("click", () => { editor.exercises.splice(editor.exercises.indexOf(row), 1); redraw(); });
    list.append(h("li", "unit-exercise", name, h("div", "unit-row", ...numbers, remove), h("div", "unit-row", load, amount)));
  }
  return list;
}

function unitDurationLabel(minutes) {
  return minutes >= 60 ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")} h` : `${minutes} min`;
}

function renderUnitEditor(form, onSaved) {
  const editor = unitEditor;
  const dialog = form.closest("dialog");
  const strength = editor.sport === "WeightTraining";
  const sportKey = UNIT_EDITOR_SPORT_KEY[editor.sport];
  const redraw = () => renderUnitEditor(form, onSaved);
  const status = h("output", { className: "tab-sync-detail", "aria-live": "polite" });
  const save = h("button", { type: "submit", className: "push-button" }, "Speichern");
  const cancel = h("button", { type: "button", className: "secondary-button" }, "Abbrechen");
  cancel.addEventListener("click", () => dialog.close());
  const name = h("input", { type: "text", maxLength: 200, required: true, value: editor.name });
  name.addEventListener("input", () => { editor.name = name.value; });
  const parts = [
    h("h2", { id: "unitEditTitle" }, unitEditorTitle(editor)),
    // A unit of another sport keeps it as a further choice.
    unitChips("Sportart", UNIT_SPORTS.some(([value]) => value === editor.sport) ? UNIT_SPORTS : [...UNIT_SPORTS, [editor.sport, activitySportLabel({ type: editor.sport })]], editor.sport, (value) => { editor.sport = value; editor.focus = ""; redraw(); }),
    unitField("Name", name),
  ];
  const templates = (state.data?.library || []).filter((item) => item.id && !item.archived && !item.date);
  if (editor.kind === "unit" && !editor.entry && templates.length) {
    const pick = h("select", null, h("option", { value: "" }, "Neu"), templates.map((item) => h("option", { value: item.id, selected: item.id === editor.templateId }, item.name || "Bibliothekseinheit")));
    pick.addEventListener("change", () => {
      const template = templates.find((item) => item.id === pick.value);
      const { date, time } = editor;
      Object.assign(editor, unitEditorFrom({ kind: "unit", entry: template || null, dateKey: date }), { entry: null, date, time, templateId: template?.id || "" });
      redraw();
    });
    parts.push(unitField("Aus Bibliothek", pick));
  }
  if (editor.kind === "unit") {
    // A past unit keeps its own day as the earliest choice, so a rename saves.
    const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
    const own = String(editor.entry?.date || "").slice(0, 10);
    const date = h("input", { type: "date", required: true, value: editor.date, min: editor.range?.[0] || (own && own < today ? own : today), max: editor.range?.[1] });
    date.addEventListener("input", () => { editor.date = date.value; });
    const time = h("input", { type: "time", value: editor.time });
    time.addEventListener("input", () => { editor.time = time.value; });
    parts.push(h("div", "edit-meal", unitField("Datum", date), unitField("Uhrzeit", time)));
  }
  if (FOCUS_OPTIONS[sportKey]) {
    const focus = h("select", null, h("option", { value: "" }, "Offen"), FOCUS_OPTIONS[sportKey].map((value) => h("option", { value, selected: value === editor.focus }, FOCUS_LABELS[value])));
    focus.addEventListener("change", () => { editor.focus = focus.value; });
    parts.push(unitField("Art der Einheit", focus));
  }
  if (strength) {
    parts.push(unitChipField("Intensität", unitChips("Intensität", Object.entries(STRENGTH_INTENSITY_LABELS), editor.intensity, (value) => { editor.intensity = value; })));
    const add = h("button", { type: "button", className: "secondary-button" }, "+ Übung");
    add.addEventListener("click", () => { editor.exercises.push(unitExerciseRow()); redraw(); });
    const duration = h("input", { type: "number", min: 5, max: 1440, inputmode: "numeric", value: editor.duration, required: true });
    duration.addEventListener("input", () => { editor.duration = duration.value; });
    parts.push(h("strong", null, "Übungen"), unitExerciseList(editor, redraw), h("div", "dialog-actions", add), unitField("Dauer (min)", duration));
  } else {
    parts.push(unitChipField("Ziel", unitChips("Ziel", UNIT_TARGETS, editor.target, (value) => { editor.target = value; })));
    if (editor.textMode) {
      const text = h("textarea", { rows: 6, value: editor.text });
      text.addEventListener("input", () => { editor.text = text.value; });
      const duration = h("input", { type: "number", min: 5, max: 1440, inputmode: "numeric", value: editor.duration, required: true });
      duration.addEventListener("input", () => { editor.duration = duration.value; });
      parts.push(unitField("Workout-Text", text), unitField("Dauer (min)", duration));
    } else {
      const total = h("p", { className: "unit-total" });
      const onChange = () => { total.textContent = `Dauer ${unitDurationLabel(unitStepsMinutes(editor.rows))}`; };
      onChange();
      const addStep = h("button", { type: "button", className: "secondary-button" }, "+ Schritt");
      addStep.addEventListener("click", () => { editor.rows.push({ kind: "step", minutes: 10, value: UNIT_NEW_STEP[editor.target] }); redraw(); });
      const addRepeat = h("button", { type: "button", className: "secondary-button" }, "+ Wiederholung");
      addRepeat.addEventListener("click", () => { editor.rows.push({ kind: "repeat", name: "", times: 3, steps: [{ kind: "step", minutes: 5, value: UNIT_NEW_STEP[editor.target] }, { kind: "step", minutes: 3, value: "50%" }] }); redraw(); });
      parts.push(h("strong", null, "Schritte"), unitRowsList(editor.rows, redraw, onChange), h("div", "dialog-actions", addStep, addRepeat), total);
    }
  }
  parts.push(status, h("div", "dialog-actions", cancel, save));
  form.replaceChildren(...parts);
  form.onsubmit = (event) => { event.preventDefault(); void saveUnitEditor(dialog, status, save, onSaved); };
}

// `kind` is "unit" (a planned unit, always with its `entry`: the calendar plans no new unit) or "library" (an entry to
// change, or none for a new one); `onSaved(result, editor)` shows the receipt and reloads.
// A `collect` editor (a new unit of a plan's replaced period) hands the unit's fields to `onSaved` instead of saving it,
// inside `range` ([first, last] day); `onClose` runs when the dialog closes either way.
function openUnitEditor({ kind, entry = null, dateKey = "", onSaved, collect = false, range = null, onClose = null }) {
  const dialog = document.querySelector("#unitEditDialog");
  const form = dialog?.querySelector("form");
  if (!form) return;
  unitEditor = { ...unitEditorFrom({ kind, entry, dateKey }), collect, range };
  renderUnitEditor(form, onSaved);
  dialog.addEventListener("close", () => { unitEditor = null; restoreDialogFocus(dialog); onClose?.(); }, { once: true });
  showAccessibleDialog(dialog);
}

// The form of a training plan (#3950): name, goal, period and status. The server keeps the plan's period at
// the days of its units.
const PLAN_STATUSES = [["planned", "Geplant"], ["active", "Aktiv"], ["paused", "Pausiert"], ["completed", "Beendet"], ["cancelled", "Abgebrochen"]];

function openPlanEditor({ plan = null, onSaved }) {
  const dialog = document.querySelector("#unitEditDialog");
  const form = dialog?.querySelector("form");
  if (!form) return;
  const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
  const values = { name: plan?.name || "", goal: plan?.goal || "", start: plan?.start_date || today, end: plan?.end_date || addDateKey(today, 27), status: plan?.status === "archived" ? "planned" : plan?.status || "planned" };
  const field = (label, control, key, event = "input") => {
    control.addEventListener(event, () => { values[key] = control.value; });
    return unitField(label, control);
  };
  const status = h("output", { className: "tab-sync-detail", "aria-live": "polite" });
  const save = h("button", { type: "submit", className: "push-button" }, "Speichern");
  const cancel = h("button", { type: "button", className: "secondary-button" }, "Abbrechen");
  cancel.addEventListener("click", () => dialog.close());
  form.replaceChildren(
    h("h2", { id: "unitEditTitle" }, plan ? "Plan bearbeiten" : "Plan hinzufügen"),
    field("Name", h("input", { type: "text", maxLength: 200, required: true, value: values.name }), "name"),
    field("Ziel", h("textarea", { rows: 2, maxLength: 2000, value: values.goal }), "goal"),
    h("div", "edit-meal", field("Von", h("input", { type: "date", required: true, value: values.start }), "start"), field("Bis", h("input", { type: "date", required: true, value: values.end }), "end")),
    unitChipField("Status", unitChips("Status", PLAN_STATUSES, values.status, (value) => { values.status = value; })),
    status, h("div", "dialog-actions", cancel, save));
  form.onsubmit = async (event) => {
    event.preventDefault();
    status.textContent = "";
    save.disabled = true;
    try {
      const body = { name: values.name.trim(), goal: values.goal.trim(), start_date: values.start, end_date: values.end, status: values.status };
      const result = await api(plan ? `/api/training-plans/${encodeURIComponent(plan.id)}/update` : "/api/training-plans", { method: "POST", body: JSON.stringify(body) });
      dialog.close();
      // Saved: a failing reload is the caller's to show, never a second save.
      void Promise.resolve(onSaved(result, body)).catch((error) => globalThis.toast?.(error.message, true));
    } catch (error) {
      status.textContent = error.message;
      save.disabled = false;
    }
  };
  dialog.addEventListener("close", () => restoreDialogFocus(dialog), { once: true });
  showAccessibleDialog(dialog);
}

// "Zeitraum ersetzen" (#4024): the plan's units in a period are archived and the new ones, made with the unit editor,
// take their place in one change; the dialog lists both before "Ersetzen".
function planPeriodUnits(plan, start, end) {
  return (state.data?.training_calendar || []).filter((item) => item.local_id && item.plan_id === plan.id && !item.archived && !item.local_deleted
    && String(item.date).slice(0, 10) >= start && String(item.date).slice(0, 10) <= end);
}

function planReplaceList(title, rows) {
  return [h("strong", null, title), h("ul", "replan-rows", rows.map(([text, day, remove]) => h("li", "plan-replace-row", h("span", null, text), h("span", null, day), remove)))];
}

function openPlanReplace({ plan, onReplaced }) {
  const dialog = document.querySelector("#unitEditDialog");
  const form = dialog?.querySelector("form");
  if (!form) return;
  const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
  const start = [plan.start_date, today].sort((a, b) => a.localeCompare(b)).at(-1);
  const period = { start, end: plan.end_date >= start ? plan.end_date : addDateKey(start, 13), workouts: [] };
  const dayText = (key) => MOVE_DAY_FORMAT.format(dateFromKey(key));
  const render = () => {
    const old = planPeriodUnits(plan, period.start, period.end);
    const status = h("output", { className: "tab-sync-detail", "aria-live": "polite" });
    const save = h("button", { type: "submit", className: "push-button", disabled: !period.workouts.length }, "Ersetzen");
    const cancel = h("button", { type: "button", className: "secondary-button" }, "Abbrechen");
    cancel.addEventListener("click", () => dialog.close());
    const add = h("button", { type: "button", className: "secondary-button" }, "+ Einheit");
    add.addEventListener("click", () => {
      // The editor opens once this dialog's close event ran, so that event never closes the editor's own handler.
      dialog.addEventListener("close", () => openUnitEditor({ kind: "unit", dateKey: period.start, range: [period.start, period.end], collect: true,
        onSaved: (body) => { period.workouts.push(body); }, onClose: () => { render(); showAccessibleDialog(dialog); } }), { once: true });
      dialog.close();
    });
    const date = (label, key) => {
      const input = h("input", { type: "date", required: true, value: period[key], min: today });
      input.addEventListener("change", () => { period[key] = input.value; period.workouts = period.workouts.filter((item) => item.date >= period.start && item.date <= period.end); render(); });
      return unitField(label, input);
    };
    const fresh = period.workouts.map((item, index) => {
      const remove = h("button", { type: "button", className: "secondary-button", "aria-label": "Einheit entfernen" }, "✕");
      remove.addEventListener("click", () => { period.workouts.splice(index, 1); render(); });
      return [item.name, dayText(item.date), remove];
    });
    form.replaceChildren(
      h("h2", { id: "unitEditTitle" }, "Zeitraum ersetzen"), h("p", null, plan.name),
      h("div", "edit-meal", date("Von", "start"), date("Bis", "end")),
      ...planReplaceList(`Wird archiviert · ${old.length} Einheit${old.length === 1 ? "" : "en"}`, old.map((item) => [item.name || "Einheit", dayText(String(item.date).slice(0, 10)), null])),
      ...planReplaceList(`Neu · ${fresh.length} Einheit${fresh.length === 1 ? "" : "en"}`, fresh),
      h("div", "dialog-actions", add), status, h("div", "dialog-actions", cancel, save));
    form.onsubmit = async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        const body = { start: period.start, end: period.end, replaced_ids: old.map((item) => item.local_id), workouts: period.workouts, confirmed: true };
        const result = await api(`/api/training-plans/${encodeURIComponent(plan.id)}/replace`, { method: "POST", body: JSON.stringify(body) });
        dialog.close();
        await onReplaced(result, { replaced: old.length, created: period.workouts.length });
      } catch (error) {
        status.textContent = error.message;
        save.disabled = false;
      }
    };
  };
  render();
  dialog.addEventListener("close", () => restoreDialogFocus(dialog), { once: true });
  showAccessibleDialog(dialog);
}
