// Saved meals and composed meals (#3952): the list under Produkte, "Eintragen", "Bearbeiten", "Löschen",
// "Als Mahlzeit speichern" on a diary card and "Mahlzeit zusammenstellen". The server calculates every
// value; the client sends names, amounts and ids only. Each change shows its receipt with "Rückgängig".
let nutritionTemplates = [];
let nutritionTemplatesOpen = false;
let nutritionTemplateReads = 0;

// The products tab hides the diary's status line and has its own.
const nutritionTemplateStatus = () => document.querySelector(state.route === "nutrition/products" ? "#nutritionProductStatus" : "#nutritionStatus");
// The day the diary shows: new entries land there.
const nutritionSelectedDay = () => document.querySelector("#nutritionDate").value || todayIso();

async function loadNutritionTemplates() {
  const host = document.querySelector("#nutritionTemplates");
  if (!host) return;
  const read = ++nutritionTemplateReads;
  try {
    const { templates = [] } = await api("/api/nutrition/templates");
    // A newer read replaces this one.
    if (read !== nutritionTemplateReads) return;
    nutritionTemplates = templates;
  } catch (error) {
    if (read === nutritionTemplateReads && !isSessionEndedError(error)) host.replaceChildren(h("p", "tab-sync-detail", `Gespeicherte Mahlzeiten konnten nicht geladen werden: ${error.message}`));
    return;
  }
  renderNutritionTemplates();
}

function renderNutritionTemplates() {
  const host = document.querySelector("#nutritionTemplates");
  if (!host) return;
  const section = h("details", { className: "nutrition-product-section", open: nutritionTemplatesOpen },
    h("summary", null, `Gespeicherte Mahlzeiten (${nutritionTemplates.length})`),
    h("div", "nutrition-list", nutritionTemplates.map(nutritionTemplateCard)),
    h("button", { type: "button", className: "secondary-button", dataset: { nutritionCompose: "" } }, "Mahlzeit zusammenstellen"));
  section.addEventListener("toggle", () => { if (section.isConnected !== false) nutritionTemplatesOpen = section.open; });
  section.querySelector("[data-nutrition-compose]").addEventListener("click", () => openNutritionCompose());
  host.replaceChildren(section);
}

function nutritionTemplateCard(template) {
  const log = h("button", { type: "button" }, "Eintragen");
  log.addEventListener("click", () => openNutritionTemplateLog(template));
  const edit = messageActionButton("edit", `Gespeicherte Mahlzeit bearbeiten: ${template.name}`);
  edit.addEventListener("click", () => openNutritionTemplateEdit(template));
  const remove = messageActionButton("trash", `Gespeicherte Mahlzeit löschen: ${template.name}`);
  remove.addEventListener("click", () => void deleteNutritionTemplate(template, remove));
  return h("article", { className: "nutrition-card", dataset: { templateId: template.id } },
    h("h3", null, template.name),
    h("p", null, `${nutritionNumber(template.kcal)} kcal · KH ${nutritionNumber(template.carbs_g, "g")} g · Protein ${nutritionNumber(template.protein_g, "g")} g · Fett ${nutritionNumber(template.fat_g, "g")} g`),
    h("div", "nutrition-template-actions", log, edit, remove));
}

// After a change: the list and the diary read again, the receipt names what happened.
async function nutritionTemplateChanged(message, change) {
  // The line of the tab the change was made on, whatever tab shows when the reads end.
  const status = nutritionTemplateStatus();
  const reload = async () => { await loadNutritionTemplates(); resetEnergy(); await loadNutrition({ preserve: true }); };
  await reload().catch(() => {});
  showNutritionReceipt(status, message, change, reload);
}

// One small dialog: a title, fields, and the buttons of the action; the dialog removes itself on close.
function nutritionTemplateDialog(title, fields, ...buttons) {
  const cancel = h("button", { type: "button", className: "secondary-button" }, "Abbrechen");
  const status = h("output", { className: "tab-sync-detail", "aria-live": "polite" });
  const form = h("form", { className: "confirmation-dialog nutrition-template-form" }, h("h2", { id: "nutritionTemplateDialogTitle" }, title), status, ...fields, h("div", "dialog-actions", cancel, ...buttons));
  const dialog = h("dialog", { id: "nutritionTemplateDialog", "aria-labelledby": "nutritionTemplateDialogTitle" }, form);
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { restoreDialogFocus(dialog); dialog.remove(); });
  document.querySelector("#nutritionTemplateDialog")?.remove();
  document.body.append(dialog);
  return { dialog, form, status };
}

const nutritionMealTypeSelect = (value = "") => h("select", { name: "meal_type" }, [h("option", { value: "" }, "Automatisch"), ...Object.entries(NUTRITION_MEAL_LABELS).filter(([key]) => !key.includes("session")).map(([key, label]) => h("option", { value: key, selected: key === value }, label))]);

// What the form shows as the day, time and type of a logged meal; empty fields are left out.
function nutritionTemplateMealFields(form) {
  const field = (name) => form.elements[name].value.trim();
  return Object.fromEntries([["meal_date", field("meal_date")], ["meal_time", field("meal_time")], ["meal_type", field("meal_type")]].filter(([, value]) => value));
}

const nutritionTemplateDayFields = (date = todayIso()) => [
  h("label", null, "Tag", h("input", { name: "meal_date", type: "date", required: true, value: date })),
  h("label", null, "Uhrzeit", h("input", { name: "meal_time", type: "time" })),
  h("label", null, "Mahlzeit", nutritionMealTypeSelect()),
];

// Sends one request from a dialog: its button waits, a refusal shows in the dialog and keeps the entries.
async function nutritionTemplateSend(ui, path, body, done, pressed = ui.form.querySelector("button[type=submit]")) {
  // One write at a time: every button of the dialog waits, the pressed one with its spinner.
  const buttons = [...ui.form.querySelectorAll("button")];
  for (const button of buttons) button.disabled = true;
  setSaving(pressed, true);
  let result;
  try {
    result = await api(path, { method: "POST", body: JSON.stringify(body) });
  } catch (error) {
    for (const button of buttons) button.disabled = false;
    setSaving(pressed, false);
    if (!isSessionEndedError(error)) ui.status.textContent = error.message;
    return;
  }
  ui.dialog.close();
  await nutritionTemplateChanged(done, result.change);
}

function openNutritionTemplateLog(template) {
  const portions = h("label", null, "Portionen", h("input", { name: "portions", type: "number", min: 0.01, max: 20, step: "any", required: true, value: 1, inputMode: "decimal" }));
  const save = h("button", { type: "submit" }, "Eintragen");
  const ui = nutritionTemplateDialog(template.name, [portions, ...nutritionTemplateDayFields(nutritionSelectedDay())], save);
  ui.form.addEventListener("submit", (event) => {
    event.preventDefault();
    void nutritionTemplateSend(ui, "/api/nutrition/templates/log", { id: template.id, portions: Number(ui.form.elements.portions.value), ...nutritionTemplateMealFields(ui.form) }, "Mahlzeit eingetragen.");
  });
  showAccessibleDialog(ui.dialog);
}

function openNutritionTemplateEdit(template) {
  const name = h("label", null, "Name", h("input", { name: "name", maxLength: 120, required: true, autocomplete: "off", value: template.name }));
  const description = h("label", null, "Beschreibung", h("textarea", { name: "description", rows: 2, maxLength: 500, required: true, value: template.description || "" }));
  const save = h("button", { type: "submit", className: "push-button" }, "Speichern");
  const ui = nutritionTemplateDialog("Gespeicherte Mahlzeit", [name, description], save);
  ui.form.addEventListener("submit", (event) => {
    event.preventDefault();
    void nutritionTemplateSend(ui, "/api/nutrition/templates", { id: template.id, name: ui.form.elements.name.value.trim(), description: ui.form.elements.description.value.trim() }, "Gespeicherte Mahlzeit geändert.");
  });
  showAccessibleDialog(ui.dialog);
}

// Acts at once, like the diary's delete (#3934): "Rückgängig" in the receipt takes the question's place.
async function deleteNutritionTemplate(template, button) {
  setSaving(button, true);
  try {
    const result = await api("/api/nutrition/templates/delete", { method: "POST", body: JSON.stringify({ id: template.id, confirmed: true }) });
    await nutritionTemplateChanged("Gespeicherte Mahlzeit gelöscht.", result.change);
  } catch (error) {
    setSaving(button, false);
    if (!isSessionEndedError(error)) toast(error.message, true);
  }
}

// "Als Mahlzeit speichern" on a diary card: the server copies the meal's values; only the name is asked.
function openNutritionTemplateFromEntry(item) {
  const name = h("label", null, "Name", h("input", { name: "name", maxLength: 120, required: true, autocomplete: "off", value: String(item.description || "").slice(0, 120) }));
  const save = h("button", { type: "submit", className: "push-button" }, "Speichern");
  const ui = nutritionTemplateDialog("Als Mahlzeit speichern", [name], save);
  ui.form.addEventListener("submit", (event) => {
    event.preventDefault();
    void nutritionTemplateSend(ui, "/api/nutrition/templates/from-entry", { id: item.id, name: ui.form.elements.name.value.trim() }, "Mahlzeit gespeichert.");
  });
  showAccessibleDialog(ui.dialog);
}

// The components of a composed meal, as the server takes them; never a nutrient value.
function nutritionComposeComponents(rows) {
  return rows.map((row) => ({ kind: row.key === "food_id" ? "database" : "local_product", [row.key]: row.id, amount: Number(row.amount), unit: row.unit }));
}

function nutritionComposeRow(rows, row, redraw) {
  const amount = h("input", { type: "number", value: row.amount, min: 0, max: 10000, step: "any", required: true, inputMode: "decimal", "aria-label": `Menge ${row.name}` });
  amount.addEventListener("input", () => { row.amount = amount.value; });
  const remove = messageActionButton("trash", `${row.name} entfernen`);
  remove.addEventListener("click", () => { rows.splice(rows.indexOf(row), 1); redraw(); });
  return h("li", "edit-row", h("div", null, h("strong", null, row.name)), h("div", "edit-controls", h("label", "edit-amount", amount, h("span", null, row.unit)), h("div", "push", remove)));
}

async function searchNutritionCompose(ui, rows, redraw, results) {
  const query = ui.form.elements.query.value.trim();
  if (query.length < 2) { ui.status.textContent = "Mindestens 2 Zeichen."; return; }
  ui.status.textContent = "Suche läuft …";
  results.replaceChildren();
  try {
    const { foods = [] } = await api("/api/nutrition/products/lookup", { method: "POST", body: JSON.stringify({ query }) });
    const found = foods.map((food) => ({ ...normalizeNutritionProduct(food), id: food.id })).filter((food) => food.id && NUTRITION_MEAL_UNITS.has(food.basis_unit));
    ui.status.textContent = found.length ? "" : "Keine Treffer.";
    results.replaceChildren(...found.map((food) => {
      const prefix = NUTRITION_FOOD_ID.exec(String(food.id))?.[1];
      const name = [food.brand, food.name].filter(Boolean).join(" · ");
      const pick = h("button", { type: "button", className: "swap-result" }, h("strong", null, name), h("span", "origin", prefix ? NUTRITION_FOOD_ORIGINS[prefix] : nutritionDatabaseLabel(food) || "Eigenes Produkt"));
      pick.addEventListener("click", () => {
        rows.push({ key: prefix ? "food_id" : "product_id", id: food.id, name, unit: food.basis_unit, amount: String(food.basis_unit === "portion" ? 1 : food.basis_amount || 100) });
        results.replaceChildren();
        redraw();
      });
      return h("li", null, pick);
    }));
  } catch (error) {
    if (!isSessionEndedError(error)) ui.status.textContent = `Suche fehlgeschlagen: ${error.message}`;
  }
}

// "Mahlzeit zusammenstellen": foods with amounts, then logged in the diary or kept as a saved meal.
function openNutritionCompose() {
  const rows = [];
  const list = h("ul", "edit-list");
  const results = h("ul", "swap-results");
  const log = h("button", { type: "button" }, "Eintragen");
  const keep = h("button", { type: "button", className: "secondary-button" }, "Als Mahlzeit speichern");
  const search = h("button", { type: "button", className: "secondary-button" }, "Suchen");
  const fields = [
    h("label", null, "Name", h("input", { name: "name", maxLength: 120, required: true, autocomplete: "off" })),
    list,
    h("label", null, "Zutat suchen", h("input", { name: "query", type: "search", autocomplete: "off", placeholder: "Name, Marke oder EAN" })),
    search, results,
    ...nutritionTemplateDayFields(nutritionSelectedDay()),
  ];
  const ui = nutritionTemplateDialog("Mahlzeit zusammenstellen", fields, keep, log);
  const redraw = () => list.replaceChildren(...rows.map((row) => nutritionComposeRow(rows, row, redraw)));
  const body = () => ({ name: ui.form.elements.name.value.trim(), components: nutritionComposeComponents(rows) });
  const ready = () => {
    if (!rows.length) { ui.status.textContent = "Mindestens eine Zutat."; return false; }
    return ui.form.reportValidity();
  };
  search.addEventListener("click", () => void searchNutritionCompose(ui, rows, redraw, results));
  ui.form.elements.query.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); search.click(); } });
  // Enter in a field writes nothing: the athlete presses "Eintragen" or "Als Mahlzeit speichern".
  ui.form.addEventListener("submit", (event) => event.preventDefault());
  log.addEventListener("click", () => {
    if (!ready()) return;
    const { name, components } = body();
    void nutritionTemplateSend(ui, "/api/nutrition/entry", { description: name, components, ...nutritionTemplateMealFields(ui.form) }, "Mahlzeit eingetragen.", log);
  });
  keep.addEventListener("click", () => {
    if (!ready()) return;
    const { name, components } = body();
    void nutritionTemplateSend(ui, "/api/nutrition/templates", { name, description: rows.map((row) => row.name).join(", ").slice(0, 500), components }, "Mahlzeit gespeichert.", keep);
  });
  showAccessibleDialog(ui.dialog);
}

// "+ Mahlzeit" under the diary day (#4304): a meal type, the search over products and foods, and the
// saved meals. A picked food asks for its amount and becomes a new meal of the shown day (a product
// through the outbox, whose edit knows products only); a saved meal is logged at once.
const NUTRITION_ADD_TYPES = ["breakfast", "lunch", "dinner", "snack"];

// Today the type the server gives a meal without one at this hour (backend/nutrition/models.py
// meal_type_from_hour, in the profile's zone); another day starts as a snack, like "Neuer Snack" (#4271).
function nutritionAddMealType() {
  if (nutritionSelectedDay() !== todayIso()) return "snack";
  const parts = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", hourCycle: "h23", timeZone: state.data?.profile?.timezone || undefined }).formatToParts(new Date());
  const hour = Number(parts.find((part) => part.type === "hour")?.value);
  if (hour >= 5 && hour < 11) return "breakfast";
  if (hour >= 11 && hour < 15) return "lunch";
  return hour >= 18 && hour < 23 ? "dinner" : "snack";
}

async function logNutritionAddedMeal(ui, row, log) {
  const mealType = ui.form.elements.meal_type.value;
  const label = NUTRITION_MEAL_LABELS[mealType];
  const base = { amount: Number(row.amount), unit: row.unit, meal_type: mealType, meal_date: nutritionSelectedDay(), source: "manual" };
  const payload = row.key === "product_id"
    ? { product_id: row.id, ...base }
    : { description: row.name, components: [{ kind: "database", food_id: row.id, amount: base.amount, unit: base.unit }], meal_type: mealType, meal_date: base.meal_date, source: "manual" };
  // One write at a time: every button of the dialog waits.
  const buttons = [...ui.form.querySelectorAll("button")];
  for (const button of buttons) button.disabled = true;
  setSaving(log, true);
  const generation = state.sessionGeneration;
  try {
    const result = row.key === "product_id"
      ? await saveOwnEntry({ kind: "meal", method: "POST", path: "/api/nutrition/entry", payload, queuedPayload: queuedMealPayload(payload), label: `„${row.name}“ als ${label}` })
      : await api("/api/nutrition/entry", { method: "POST", body: JSON.stringify(payload) });
    if (generation !== state.sessionGeneration) return;
    ui.dialog.close();
    if (result.outboxEntry) { nutritionTemplateStatus().textContent = `„${row.name}“ wartet, noch nicht gesendet.`; return; }
    await nutritionTemplateChanged(`„${row.name}“ als ${label} erfasst.`, result.change);
  } catch (error) {
    for (const button of buttons) button.disabled = false;
    setSaving(log, false);
    if (!isSessionEndedError(error)) ui.status.textContent = error.message;
  }
}

async function openNutritionAddMeal() {
  const rows = [];
  const list = h("ul", "edit-list");
  const results = h("ul", "swap-results");
  const saved = h("div", "nutrition-add-saved");
  const log = h("button", { type: "button", className: "push-button", hidden: true }, "Eintragen");
  const search = h("button", { type: "button", className: "secondary-button" }, "Suchen");
  const chosen = nutritionAddMealType();
  const types = h("fieldset", "chip-field", h("legend", null, "Mahlzeit"), h("div", "chip-group", NUTRITION_ADD_TYPES.map((type) =>
    h("label", "chip", h("input", { type: "radio", name: "meal_type", value: type, checked: type === chosen }), h("span", null, NUTRITION_MEAL_LABELS[type])))));
  const fields = [
    types,
    h("label", null, "Produkt oder Lebensmittel", h("input", { name: "query", type: "search", autocomplete: "off" })),
    search, results, list, saved,
  ];
  const ui = nutritionTemplateDialog("Mahlzeit hinzufügen", fields, log);
  // One food per meal here: a new pick replaces the earlier one.
  const redraw = () => {
    rows.splice(0, rows.length - 1);
    list.replaceChildren(...rows.map((row) => nutritionComposeRow(rows, row, redraw)));
    log.hidden = !rows.length;
    list.querySelector("input")?.focus();
  };
  search.addEventListener("click", () => void searchNutritionCompose(ui, rows, redraw, results));
  ui.form.elements.query.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); search.click(); } });
  ui.form.addEventListener("submit", (event) => event.preventDefault());
  log.addEventListener("click", () => { if (rows.length && ui.form.reportValidity()) void logNutritionAddedMeal(ui, rows[0], log); });
  showAccessibleDialog(ui.dialog);
  try {
    const { templates = [] } = await api("/api/nutrition/templates");
    if (!ui.dialog.isConnected || !templates.length) return;
    saved.replaceChildren(h("h3", null, "Gespeicherte Mahlzeiten"), h("div", "chip-group", templates.map((template) => {
      const pick = h("button", { type: "button", className: "secondary-button" }, template.name);
      pick.addEventListener("click", () => void nutritionTemplateSend(ui, "/api/nutrition/templates/log", { id: template.id, portions: 1, meal_date: nutritionSelectedDay(), meal_type: ui.form.elements.meal_type.value }, `„${template.name}“ eingetragen.`, pick));
      return pick;
    })));
  } catch (error) {
    if (!isSessionEndedError(error)) saved.replaceChildren(h("p", "tab-sync-detail", `Gespeicherte Mahlzeiten konnten nicht geladen werden: ${error.message}`));
  }
}

document.querySelector("#nutritionAddMeal")?.addEventListener("click", () => void openNutritionAddMeal());
document.addEventListener("app:route-rendered", () => { if (state.route === "nutrition/products") void loadNutritionTemplates(); });
