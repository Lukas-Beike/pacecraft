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
function unitStepRows(text) {
  const rows = [];
  let group = null;
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    if (!line) { group = null; continue; }
    const step = UNIT_STEP.exec(line);
    if (step) {
      (group ? group.steps : rows).push({ kind: "step", minutes: Number(step[1]), value: step[2].replace(",", ".").replace(/\s+/, " ") });
      continue;
    }
    if (line.startsWith("-")) return null;
    const repeat = UNIT_REPEAT.exec(line);
    if (repeat) { group = { kind: "repeat", name: repeat[1] || "", times: Number(repeat[2]), steps: [] }; rows.push(group); continue; }
    if (group) return null;
    rows.push({ kind: "label", name: line });
  }
  return rows;
}

function unitStepsText(rows) {
  const lines = [];
  const stepLine = (row) => `- ${row.minutes}m ${row.value}`;
  for (const row of rows) {
    if (row.kind === "step") lines.push(stepLine(row));
    else {
      if (lines.length) lines.push("");
      lines.push(row.kind === "label" ? row.name : `${row.name ? `${row.name} ` : ""}${row.times}x`);
      if (row.kind === "repeat") { lines.push(...row.steps.map(stepLine), ""); }
    }
  }
  return lines.join("\n").trim();
}

function unitStepsMinutes(rows) {
  return rows.reduce((sum, row) => sum + (row.kind === "step" ? row.minutes : row.kind === "repeat" ? row.times * unitStepsMinutes(row.steps) : 0), 0);
}

function unitExerciseRow(item = {}) {
  const load = ["kg", "pct_1rm", "rpe", "rir"].find((key) => item[key] != null) || "";
  const base = { name: "", sets: 3, reps: 8, kg: null, pct_1rm: null, rpe: null, rir: null, rest_s: null, warmup: null, note: null, garmin_exercise: null, ...item };
  return { base, load, amount: load ? String(item[load]) : "" };
}

function unitExercises(rows) {
  return rows.filter((row) => row.base.name.trim()).map((row) => {
    const loads = { kg: null, pct_1rm: null, rpe: null, rir: null };
    if (row.load && row.amount !== "") loads[row.load] = Number(row.amount);
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

function unitEditorBody(editor) {
  const strength = editor.sport === "WeightTraining";
  const sportKey = UNIT_EDITOR_SPORT_KEY[editor.sport];
  const body = { name: editor.name.trim(), target: strength ? "AUTO" : editor.target };
  if (!editor.entry || editor.sport !== editor.originalSport) body.sport = editor.sport;
  body.session_focus = editor.focus || null;
  if (strength) {
    body.strength_intensity = editor.intensity || null;
    body.strength_structure = unitExercises(editor.exercises);
    // The server writes the text from the exercises; the lines only name them.
    body.description = body.strength_structure.map((item) => `${item.name}: ${item.sets}x${item.reps}`).join("\n");
    body.duration_minutes = Number(editor.duration);
  } else {
    body.strength_intensity = null;
    const text = editor.textMode ? editor.text.trim() : unitStepsText(editor.rows);
    body.description = text;
    body.duration_minutes = editor.textMode ? Number(editor.duration) : unitStepsMinutes(editor.rows);
  }
  if (!FOCUS_OPTIONS[sportKey]) body.session_focus = null;
  if (editor.kind === "unit") {
    body.date = editor.date;
    if (editor.time) body.starts_at_local = `${editor.date}T${editor.time}:00`;
    if (editor.templateId) body.template_id = editor.templateId;
  }
  return body;
}

function unitEditorTitle(editor) {
  const noun = editor.kind === "unit" ? "Einheit" : "Bibliothekseinheit";
  return `${noun} ${editor.entry ? "bearbeiten" : "hinzufügen"}`;
}

async function saveUnitEditor(dialog, status, save, onSaved) {
  const editor = unitEditor;
  status.textContent = "";
  const body = unitEditorBody(editor);
  if (!body.name) { status.textContent = "Der Name fehlt."; return; }
  if (editor.kind === "unit" && !editor.date) { status.textContent = "Der Tag fehlt."; return; }
  if (!body.description) { status.textContent = editor.sport === "WeightTraining" ? "Mindestens eine Übung fehlt." : "Mindestens ein Schritt fehlt."; return; }
  const entry = editor.entry;
  const path = editor.kind === "library"
    ? (entry ? `/api/library/${encodeURIComponent(entry.id)}/update` : "/api/library")
    : (entry ? `/api/planned-units/${encodeURIComponent(entry.local_id)}/update` : "/api/planned-units");
  if (editor.kind === "unit" && entry) body.expected_payload_hash = entry.payload_hash;
  if (editor.accept) body.accept_plan_findings = ["illness_hard"];
  save.disabled = true;
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(body) });
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
    load.addEventListener("change", () => { row.load = load.value; amount.disabled = !row.load; if (!row.load) amount.value = ""; row.amount = amount.value; });
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
    const date = h("input", { type: "date", required: true, value: editor.date, min: timezoneDateKey(state.data?.profile?.timezone, new Date()) });
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

// `kind` is "unit" or "library"; `entry` the unit or entry to change (none: a new one), `dateKey` the day a new
// unit starts on; `onSaved(result, editor)` shows the receipt and reloads.
function openUnitEditor({ kind, entry = null, dateKey = "", onSaved }) {
  const dialog = document.querySelector("#unitEditDialog");
  const form = dialog?.querySelector("form");
  if (!form) return;
  unitEditor = unitEditorFrom({ kind, entry, dateKey });
  renderUnitEditor(form, onSaved);
  dialog.addEventListener("close", () => { unitEditor = null; restoreDialogFocus(dialog); }, { once: true });
  showAccessibleDialog(dialog);
}
