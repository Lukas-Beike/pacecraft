// A diary meal's components: remove, change the amount or swap for another food (#3948).
// The server recalculates; the dialog sends only the index and the change, never nutrient values.
let nutritionMealEdit = null;

const NUTRITION_MEAL_UNITS = new Set(["g", "ml", "portion"]);
const NUTRITION_FOOD_ID = /^(bls|off|yazio):/;
const NUTRITION_FOOD_ORIGINS = { bls: "BLS 4.0", off: "Open Food Facts", yazio: "Yazio" };

function nutritionMealEditNodes() {
  const root = document.querySelector("#nutritionMealEditDialog");
  const pick = (id) => root.querySelector(`#nutritionMealEdit${id}`);
  return { root, form: pick("Main"), main: pick("Main"), swap: pick("Swap"), title: pick("Title"), subtitle: pick("Subtitle"), list: pick("List"), save: pick("Save"), status: pick("Status"), query: pick("Query"), results: pick("Results"), swapStatus: pick("SwapStatus") };
}

// "BLS 4.0", "Open Food Facts", "Yazio", "Eigenes Produkt" or "Schätzung" for a stored component.
function nutritionComponentOrigin(component) {
  const basis = component.nutrition_basis || {};
  if (basis.kind === "database") return NUTRITION_FOOD_ORIGINS[NUTRITION_FOOD_ID.exec(String(basis.food?.id))?.[1]] || "Datenbank";
  if (basis.kind === "local_product") return nutritionDatabaseLabel(basis.product) || "Eigenes Produkt";
  return component.kind === "estimate" ? "Schätzung" : "Eingegeben";
}

async function openNutritionMealEdit(item) {
  const status = document.querySelector("#nutritionStatus");
  try {
    const { components, revision } = await api(`/api/nutrition/entry/components?id=${encodeURIComponent(item.id)}`);
    nutritionMealEdit = {
      item,
      revision,
      generation: state.sessionGeneration,
      rows: components.map((component) => ({ index: component.index, name: component.name, unit: component.unit, originalUnit: component.unit, original: String(component.amount), amount: String(component.amount), kcal: component.kcal, origin: nutritionComponentOrigin(component), removed: false, swap: null })),
      swapRow: null,
      searchSequence: 0,
    };
  } catch (error) {
    status.textContent = `Mahlzeit konnte nicht geöffnet werden: ${error.message}`;
    return;
  }
  const nodes = nutritionMealEditNodes();
  nodes.subtitle.textContent = [nutritionMealTitle(item), item.description].filter(Boolean).join(" · ");
  nodes.status.textContent = "";
  renderNutritionMealEdit();
  showAccessibleDialog(nodes.root);
}

function nutritionMealEditChanges() {
  const edits = [];
  for (const row of nutritionMealEdit.rows) {
    const resized = row.amount !== row.original || row.unit !== row.originalUnit;
    // A food in another unit than the estimate's portion names its unit with the amount.
    const sized = { ...(resized ? { amount: Number(row.amount) } : {}), ...(row.unit === row.originalUnit ? {} : { unit: row.unit }) };
    if (row.removed) edits.push({ index: row.index, remove: true });
    else if (row.swap) edits.push({ index: row.index, [row.swap.key]: row.swap.id, ...sized });
    else if (resized) edits.push({ index: row.index, ...sized });
  }
  return edits;
}

function renderNutritionMealEdit() {
  const nodes = nutritionMealEditNodes();
  const { rows, swapRow } = nutritionMealEdit;
  nodes.main.hidden = Boolean(swapRow);
  nodes.subtitle.hidden = Boolean(swapRow);
  nodes.swap.hidden = !swapRow;
  if (swapRow) {
    nodes.title.textContent = `Tauschen: ${swapRow.name}`;
    return;
  }
  nodes.title.textContent = "Mahlzeit bearbeiten";
  nodes.list.replaceChildren(...rows.map(nutritionMealEditRow));
  updateNutritionMealEditSave();
}

function updateNutritionMealEditSave() {
  const nodes = nutritionMealEditNodes();
  const offline = !navigator.onLine;
  nodes.save.disabled = offline || nutritionMealEditChanges().length === 0;
  if (offline) nodes.status.textContent = "Offline: Speichern erst mit Verbindung möglich.";
  else if (nodes.status.textContent.startsWith("Offline")) nodes.status.textContent = "";
}

function nutritionMealEditRow(row) {
  const shown = row.swap ? row.swap.name : row.name;
  // The stored kcal fits only the stored amount.
  const detail = () => row.swap
    ? `${row.swap.origin} · statt ${row.name}`
    : [row.origin, row.amount === row.original && row.kcal != null ? `${nutritionNumber(row.kcal)} kcal` : ""].filter(Boolean).join(" · ");
  const small = h("small", null, detail());
  const head = h("div", null, h("strong", null, shown), h("br"), small);
  const item = h("li", { className: `edit-row${row.removed ? " is-removed" : ""}` }, head);
  if (row.removed) {
    const restore = h("button", { type: "button", className: "secondary-button" }, "Wiederherstellen");
    restore.addEventListener("click", () => { row.removed = false; renderNutritionMealEdit(); });
    item.append(h("div", "edit-controls", h("span", null, "Wird entfernt"), h("div", "push", restore)));
    return item;
  }
  const amount = h("input", { type: "number", value: row.amount, min: "0", max: "10000", step: "any", required: true, inputMode: "decimal", "aria-label": `Menge ${shown}` });
  amount.addEventListener("input", () => { row.amount = amount.value; small.textContent = detail(); updateNutritionMealEditSave(); });
  const swap = h("button", { type: "button", className: "secondary-button", "aria-label": `${shown} tauschen` }, "Tauschen");
  swap.addEventListener("click", () => openNutritionMealSwap(row));
  const remove = messageActionButton("trash", `${shown} entfernen`);
  remove.addEventListener("click", () => { row.removed = true; renderNutritionMealEdit(); });
  item.append(h("div", "edit-controls", h("label", "edit-amount", amount, h("span", null, row.unit)), h("div", "push", swap, remove)));
  return item;
}

function openNutritionMealSwap(row) {
  const nodes = nutritionMealEditNodes();
  nutritionMealEdit.swapRow = row;
  nodes.query.value = "";
  nodes.results.replaceChildren();
  nodes.swapStatus.textContent = "";
  renderNutritionMealEdit();
  nodes.query.focus();
}

function closeNutritionMealSwap() {
  nutritionMealEdit.swapRow = null;
  nutritionMealEdit.searchSequence += 1;
  renderNutritionMealEdit();
}

async function searchNutritionMealSwap() {
  const nodes = nutritionMealEditNodes();
  const { swapRow: row, generation } = nutritionMealEdit;
  const sequence = ++nutritionMealEdit.searchSequence;
  const query = nodes.query.value.trim();
  if (query.length < 2) { nodes.swapStatus.textContent = "Mindestens 2 Zeichen."; return; }
  nodes.swapStatus.textContent = "Suche läuft …";
  nodes.results.replaceChildren();
  try {
    const { foods = [] } = await api("/api/nutrition/products/lookup", { method: "POST", body: JSON.stringify({ query }) });
    if (generation !== state.sessionGeneration || sequence !== nutritionMealEdit?.searchSequence) return;
    // A food counted in another unit cannot take the amount (no density conversion); only an
    // estimate in portions has no weight to keep, so its swap asks for the amount.
    const fitting = foods.map((food) => ({ ...normalizeNutritionProduct(food), id: food.id })).filter((food) => food.id && NUTRITION_MEAL_UNITS.has(food.basis_unit) && (food.basis_unit === row.unit || row.originalUnit === "portion"));
    const inUnit = row.originalUnit === "portion" ? "" : ` in ${row.unit}`;
    nodes.swapStatus.textContent = fitting.length ? "" : `Keine Treffer${inUnit}.`;
    nodes.results.replaceChildren(...fitting.map((food) => nutritionMealSwapResult(row, food)));
  } catch (error) {
    if (sequence === nutritionMealEdit?.searchSequence) nodes.swapStatus.textContent = `Suche fehlgeschlagen: ${error.message}`;
  }
}

function nutritionMealSwapResult(row, food) {
  const prefix = NUTRITION_FOOD_ID.exec(String(food.id))?.[1];
  const origin = prefix ? NUTRITION_FOOD_ORIGINS[prefix] : nutritionDatabaseLabel(food) || "Eigenes Produkt";
  const name = [food.brand, food.name].filter(Boolean).join(" · ");
  const facts = `pro ${nutritionNumber(food.basis_amount ?? 100)} ${food.basis_unit} · ${nutritionNumber(food.kcal)} kcal · KH ${nutritionNumber(food.carbs_g)} g · Protein ${nutritionNumber(food.protein_g)} g · Fett ${nutritionNumber(food.fat_g)} g`;
  const button = h("button", { type: "button", className: "swap-result" }, h("strong", null, name), h("span", "origin", origin), h("small", null, facts));
  button.addEventListener("click", () => {
    row.swap = { key: prefix ? "food_id" : "product_id", id: food.id, name, origin };
    if (food.basis_unit !== row.unit) { row.unit = food.basis_unit; row.amount = ""; }
    closeNutritionMealSwap();
  });
  return h("li", null, button);
}

async function saveNutritionMealEdit() {
  const nodes = nutritionMealEditNodes();
  const edit = nutritionMealEdit;
  const { item, rows, revision, generation } = edit;
  if (!nodes.form.reportValidity()) return;
  // Without a component the meal goes: "Speichern" is no delete cue, so this path asks first.
  if (rows.every((row) => row.removed)) {
    const confirmed = await requestConfirmation(`Die Mahlzeit „${item.description}“ wird gelöscht, weil keine Zutat übrig ist.`, { title: "Mahlzeit löschen?", danger: true, confirmLabel: "Löschen" });
    if (confirmed && await deleteNutritionEntry(item)) nodes.root.close();
    return;
  }
  nodes.save.disabled = true;
  nodes.status.textContent = "";
  let result;
  try {
    result = await api("/api/nutrition/entry/components", { method: "POST", body: JSON.stringify({ id: item.id, revision, edits: nutritionMealEditChanges() }) });
  } catch (error) {
    if (generation !== state.sessionGeneration) return;
    const message = `Mahlzeit konnte nicht gespeichert werden: ${error.message}`;
    // The dialog was closed while the request ran: a toast carries the error.
    if (!nodes.root.open || nutritionMealEdit !== edit) { toast(message, true); return; }
    nodes.status.textContent = message;
    updateNutritionMealEditSave();
    return;
  }
  if (generation !== state.sessionGeneration) return;
  nodes.root.close();
  // The change is saved; a failed reload still shows its receipt, and the diary reads again on the next refresh.
  const reload = () => { resetEnergy(); return loadNutrition({ preserve: true }); };
  await reload().catch(() => {});
  showNutritionReceipt(document.querySelector("#nutritionStatus"), "Mahlzeit geändert.", result.change, reload);
}

document.querySelector("#nutritionMealEditCancel")?.addEventListener("click", () => document.querySelector("#nutritionMealEditDialog").close());
document.querySelector("#nutritionMealEditBack")?.addEventListener("click", closeNutritionMealSwap);
document.querySelector("#nutritionMealEditMain")?.addEventListener("submit", (event) => { event.preventDefault(); void saveNutritionMealEdit(); });
document.querySelector("#nutritionMealEditSearch")?.addEventListener("submit", (event) => { event.preventDefault(); void searchNutritionMealSwap(); });
document.querySelector("#nutritionMealEditDialog")?.addEventListener("close", () => { nutritionMealEdit = null; });
for (const type of ["online", "offline"]) globalThis.addEventListener(type, () => { if (nutritionMealEdit && !nutritionMealEdit.swapRow) updateNutritionMealEditSave(); });
