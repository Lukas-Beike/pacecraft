// Read-only nutrition views; every write remains an explicit Coach request.
let nutritionLoadSequence = 0;
let nutritionHistoryPeriod = "28d";

const MAX_NUTRITION_IMAGE_EDGE = 1600;
const MAX_NUTRITION_IMAGE_BYTES = 1_200_000;

async function nutritionImageDataUrl(blob, mime) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCodePoint(...bytes.subarray(offset, Math.min(offset + 0x8000, bytes.length)));
  }
  return `data:${mime};base64,${btoa(binary)}`;
}

async function resizeNutritionImage(bitmap, maxEdge, maxBytes) {
  const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const context = canvas.getContext("2d", { alpha: false });
  if (!context) throw new Error("Die Bildverarbeitung ist auf diesem Gerät nicht verfügbar.");
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  let blob;
  for (const quality of [0.84, 0.72, 0.6, 0.48]) {
    blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    if (blob && blob.size <= maxBytes) break;
  }
  if (!blob || blob.size > maxBytes) throw new Error("Das Foto ließ sich nicht ausreichend verkleinern. Bitte ein kleineres Bild wählen.");
  return { dataUrl: await nutritionImageDataUrl(blob, "image/jpeg"), blob, width: canvas.width, height: canvas.height, mime: "image/jpeg" };
}

async function prepareNutritionImage(file, { maxEdge = MAX_NUTRITION_IMAGE_EDGE, maxBytes = MAX_NUTRITION_IMAGE_BYTES } = {}) {
  if (!file || !/^image\/(png|jpe?g|webp)$/i.test(file.type)) throw new Error("Bitte ein JPEG-, PNG- oder WebP-Bild auswählen.");
  if (!file.size || file.size > 15_000_000) throw new Error("Das Foto darf höchstens 15 MB groß sein.");
  const normalizedMime = String(file.type || "").toLowerCase() === "image/jpg" ? "image/jpeg" : String(file.type || "").toLowerCase();
  let bitmap;
  let objectUrl;
  try {
    if (typeof createImageBitmap === "function") bitmap = await createImageBitmap(file, { imageOrientation: "from-image", resizeWidth: maxEdge, resizeQuality: "high" });
    else {
      objectUrl = URL.createObjectURL(file);
      bitmap = await new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error("Das Foto konnte nicht dekodiert werden."));
        image.src = objectUrl;
      });
    }
    if (bitmap.width <= maxEdge && bitmap.height <= maxEdge && file.size <= maxBytes) {
      return { dataUrl: await nutritionImageDataUrl(file, normalizedMime), blob: file, width: bitmap.width, height: bitmap.height, mime: normalizedMime };
    }
    return await resizeNutritionImage(bitmap, maxEdge, maxBytes);
  } finally {
    bitmap?.close?.();
    if (objectUrl) URL.revokeObjectURL(objectUrl);
  }
}

function renderNutritionSegments(route) {
  const week = route === "nutrition/week";
  const products = route === "nutrition/products";
  const body = route === "nutrition/body";
  const names = { "nutrition/diary": "Tagebuch", "nutrition/week": "Verlauf", "nutrition/body": "Körper", "nutrition/products": "Produkte" };
  document.querySelector("#nutritionRefreshButton").setAttribute("aria-label", `Ernährung aktualisieren: ${names[route] || "Tagebuch"}`);
  document.querySelector("#nutritionDiary").hidden = week || products || body;
  document.querySelector("#bodyAnalysisCharts").hidden = !body;
  document.querySelector("#nutritionStatus").hidden = week || products || body;
  document.querySelector("#nutritionWeek").hidden = !week;
  document.querySelector("#nutritionProductsPanel").hidden = !products;
  document.querySelectorAll("[data-nutrition-segment]").forEach((link) => {
    let activeSegment = "diary";
    if (week) activeSegment = "week";
    if (body) activeSegment = "body";
    if (products) activeSegment = "products";
    const active = link.dataset.nutritionSegment === activeSegment;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function nutritionNumber(value, unit = "") {
  if (value == null || value === "") return "–";
  const number = Number(value);
  return Number.isFinite(number) ? AppFormat.number(number, { digits: 1, minDigits: unit === "g" ? 1 : 0 }) : String(value);
}

// A diary write reports its text with "Rückgängig" while the change can still be undone.
function showNutritionReceipt(status, message, change, afterUndo) {
  const text = h("span", null, `${message} `);
  status.replaceChildren(text);
  if (!change?.id) return;
  const undo = h("button", { type: "button", className: "message-undo" }, "Rückgängig");
  undo.addEventListener("click", async () => {
    undo.disabled = true;
    try {
      await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: change.id, expected_current_hash: change.after_hash }) });
      await afterUndo?.();
      status.textContent = "Zurückgenommen.";
    } catch (error) {
      undo.disabled = false;
      text.textContent = `${error.message} `;
    }
  });
  status.append(undo);
}

async function deleteNutritionEntry(item) {
  // Acts at once (athlete decision, #3934 G-03): "Rückgängig" in the receipt takes the question's place.
  const status = document.querySelector("#nutritionStatus");
  try {
    const result = await api("/api/nutrition/entry/delete", { method: "POST", body: JSON.stringify({ id: item.id }) });
    await loadNutrition({ preserve: true });
    showNutritionReceipt(status, "Mahlzeit gelöscht.", result.change, () => loadNutrition({ preserve: true }));
    return true;
  } catch (error) { status.textContent = `Mahlzeit konnte nicht gelöscht werden: ${error.message}`; return false; }
}

// Flags or clears a meal as estimated; the logged values stay, the energy table reads again (#3100).
async function markNutritionEstimate(item) {
  const status = document.querySelector("#nutritionStatus");
  const flag = !item.estimate_uncertain;
  const reload = () => { resetEnergy(); return loadNutrition({ preserve: true }); };
  try {
    const result = await api("/api/nutrition/entry/estimate", { method: "POST", body: JSON.stringify({ id: item.id, estimate_uncertain: flag }) });
    await reload();
    showNutritionReceipt(status, flag ? "Als geschätzt markiert." : "Markierung entfernt.", result.change, reload);
  } catch (error) { status.textContent = `Markierung konnte nicht gespeichert werden: ${error.message}`; }
}

// "Open Food Facts" or "Yazio" for a product from a database; "" for an own product.
function nutritionDatabaseLabel(product) {
  if (!product?.origin || product.origin === "own") return "";
  return { open_food_facts: "Open Food Facts", yazio: "Yazio" }[product.origin] || "Datenbank";
}

// Who logged a meal; an estimated component names the same source (#3297).
const NUTRITION_SOURCE_LABELS = { coach: "Coach", photo: "Foto", voice: "Sprache", manual: "Manuelle Angabe" };

function nutritionComponentSource(basis, sourceLabel = "Erfasst") {
  const kind = basis?.kind;
  if (kind === "local_product" || kind === "product") {
    const product = basis.product || basis;
    const database = nutritionDatabaseLabel(product);
    const source = product.source === "packaging_label" ? "Verpackungsangabe" : product.source;
    const label = product.product_name || product.name || (database ? "Datenbankprodukt" : "Gespeichertes Produkt");
    if (database) return `Datenbankprodukt: ${label} · ${database}`;
    return ["Gespeichertes Produkt: " + label, source].filter(Boolean).join(" · ");
  }
  if (kind === "database") {
    const food = basis.food || basis;
    return [food.source || "Datenbank", food.name].filter(Boolean).join(": ");
  }
  if (kind === "packaging_label") return "Verpackungsangabe";
  if (kind === "manual") return "Manuelle Angabe";
  if (kind === "estimate") return sourceLabel;
  return "Quelle unbekannt";
}

// "1890 kcal" keeps its text; the unit is set smaller than the value (#1789).
function setValueWithUnit(element, text) {
  const split = text.lastIndexOf(" ");
  if (split < 1) { element.textContent = text; return element; }
  element.replaceChildren(text.slice(0, split), " ", h("span", "nutrition-unit", text.slice(split + 1)));
  return element;
}

function nutritionBasisLabel(basis, item, sourceLabel) {
  if (basis?.kind === "composite") return "";
  if (basis?.kind === "database") {
    return "Datenbankberechnung · " + basis.ingredients.map((food) => `${food.source}: ${food.name}, ${food.amount} ${food.unit} (Basis 100 ${food.basis_unit})`).join("; ");
  }
  if (basis?.kind === "product" || basis?.kind === "local_product") {
    const product = basis.product || basis;
    const name = product.product_name || product.name || "Produkt";
    const database = nutritionDatabaseLabel(product);
    const source = database || product.source ? ` (${database || product.source})` : "";
    const amount = basis.amount != null ? ` · ${basis.amount} ${basis.unit || "g"}` : "";
    return `${database ? "Datenbankprodukt" : "Lokales Produkt"} · ${name}${source}${amount}`;
  }
  if (basis?.kind === "manual_correction") return "Manuell korrigierte Nährwerte";
  if (basis?.kind === "packaging_label") return "Verpackungsangabe";
  return sourceLabel;
}

function nutritionComponentDetails(item) {
  const components = item.nutrition_basis?.kind === "composite" ? item.nutrition_basis.components : [];
  if (!Array.isArray(components) || !components.length) return null;
  return h("details", "nutrition-components",
    h("summary", null, `Zutaten anzeigen (${components.length})`),
    h("ul", "nutrition-component-list", components.map((component) => h("li", null,
      h("strong", null, component.name || "Unbenannte Zutat"),
      h("span", null, ` · ${component.amount ?? "–"} ${component.unit || ""}`.trim()),
      h("span", null, ` · ${nutritionNumber(component.kcal)} kcal · KH ${nutritionNumber(component.carbs_g, "g")} g · Protein ${nutritionNumber(component.protein_g, "g")} g · Fett ${nutritionNumber(component.fat_g, "g")} g`),
      h("small", null, nutritionComponentSource(component.nutrition_basis, NUTRITION_SOURCE_LABELS[item.source]))))));
}

const NUTRITION_SESSION_SLOTS = new Set(["pre_session", "during_session"]);
const NUTRITION_SPORTS = { ride: "Rad", run: "Lauf", swim: "Schwimmen", strength: "Kraft", walk: "Gehen", hike: "Wandern" };

// "Rad · 09:15 · 3:10 h"
function nutritionSessionLabel(session) {
  const time = String(session.started_at_local || "").slice(11, 16);
  const minutes = Math.round((Number(session.elapsed_duration_s) || 0) / 60);
  const duration = minutes ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")} h` : "";
  return [NUTRITION_SPORTS[session.sport] || "Einheit", time, duration].filter(Boolean).join(" · ");
}

async function chooseNutritionSession(item, session) {
  const status = document.querySelector("#nutritionStatus");
  try {
    const result = await api("/api/nutrition/entry/session", { method: "POST", body: JSON.stringify({ id: item.id, session_id: session.id }) });
    await loadNutrition({ preserve: true });
    showNutritionReceipt(status, "Einheit zugeordnet.", result.change, () => loadNutrition({ preserve: true }));
  } catch (error) { status.textContent = `Einheit konnte nicht zugeordnet werden: ${error.message}`; }
}

// The session chip of a "Vor/Während der Einheit" entry; an open one offers the day's sessions to choose from.
function nutritionSessionBlock(item, sessions) {
  if (!NUTRITION_SESSION_SLOTS.has(item.meal_type)) return null;
  if (item.session) return h("p", "nutrition-session", nutritionSessionLabel(item.session));
  const block = h("div", "nutrition-session-open", h("p", "nutrition-session nutrition-session-pending", "Einheit noch nicht zugeordnet"));
  if (sessions.length) {
    block.append(h("p", null, "Zu welcher Einheit gehört das?"), h("div", "nutrition-session-choices", sessions.map((session) => {
      const choice = h("button", { type: "button", className: "secondary-button" }, nutritionSessionLabel(session));
      choice.addEventListener("click", () => void chooseNutritionSession(item, session));
      return choice;
    })));
  }
  return block;
}

const NUTRITION_MEAL_LABELS = { breakfast: "Frühstück", lunch: "Mittagessen", dinner: "Abendessen", snack: "Snack", pre_session: "Vor der Einheit", during_session: "Während der Einheit" };

function nutritionMealTitle(item) {
  const mealLabel = NUTRITION_MEAL_LABELS[item.meal_type] || "Mahlzeit";
  const mealTime = item.logged_time_known === false ? "" : String(item.logged_at).split("T")[1]?.slice(0, 5) || "";
  return mealTime ? `${mealTime} · ${mealLabel}` : mealLabel;
}

function nutritionCard(item, template, sessions = []) {
  const sourceLabel = NUTRITION_SOURCE_LABELS[item.source] || "Erfasst";
  const syncLabel = item.sync_state === "synced" ? " · Synchronisiert" : " · Lokal";
  const basisLabel = nutritionBasisLabel(item.nutrition_basis, { template, syncLabel }, sourceLabel);
  // The default (typed in by hand, stored locally) is not repeated under every meal; any other source or state is shown.
  const isDefaultSource = !template && item.source === "manual" && item.sync_state !== "synced" && basisLabel === sourceLabel;
  const sourceSuffix = template ? "" : syncLabel;
  const sourceText = basisLabel && !isDefaultSource ? basisLabel + sourceSuffix : "";
  const volume = item.volume_ml > 0 ? ` · ${AppFormat.number(item.volume_ml, { digits: 0 })} ml` : "";
  const title = h("h3", null, template ? item.name : nutritionMealTitle(item), ...(template ? [] : nutritionEstimateChip(item)));
  const card = h("article", "nutrition-card",
    template ? title : h("div", "nutrition-card-head", title, nutritionEditButton(item)),
    template ? null : nutritionSessionBlock(item, sessions),
    h("p", null, item.description),
    h("p", null, `${nutritionNumber(item.kcal)} kcal · KH ${nutritionNumber(item.carbs_g, "g")} g · Protein ${nutritionNumber(item.protein_g, "g")} g · Fett ${nutritionNumber(item.fat_g, "g")} g${template ? " / Portion" : ""}${volume}`),
    sourceText ? h("small", null, sourceText) : null);
  if (!template) card.append(...nutritionEntryActions(item));
  return card;
}

// The pencil in a meal card's head opens its components (#3948).
function nutritionEditButton(item) {
  const button = messageActionButton("edit", `Mahlzeit bearbeiten: ${item.description}`);
  button.addEventListener("click", () => void openNutritionMealEdit(item));
  return button;
}

// A meal whose amounts were estimated (restaurant, friends) says so in its title (#3100).
function nutritionEstimateChip(item) {
  if (!item.estimate_uncertain) return [];
  const chip = createStatusChip("Menge geschätzt", "estimate");
  chip.classList.add("estimate-chip");
  return [" ", chip];
}

// A logged meal's ingredients, its estimate mark and its removal.
function nutritionEntryActions(item) {
  const estimate = h("button", { type: "button", className: "secondary-button" }, item.estimate_uncertain ? "Markierung entfernen" : "Als geschätzt markieren");
  estimate.addEventListener("click", () => void markNutritionEstimate(item));
  const remove = h("button", { type: "button", className: "secondary-button danger-button", "aria-label": `Mahlzeit löschen: ${item.description}` }, "Löschen");
  remove.addEventListener("click", async () => {
    // One tap, one delete: the button waits for the request, as the removed dialog did.
    remove.disabled = true;
    try { await deleteNutritionEntry(item); } finally { remove.disabled = false; }
  });
  return [nutritionComponentDetails(item), estimate, remove].filter(Boolean);
}

function nutritionExpenditureDate(value) {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return "Datum unbekannt";
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return "Datum unbekannt";
  return new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" }).format(parsed);
}

function appendNutritionTotals(totals, day) {
  // Empty days report zero totals, which are unknown, so the tiles stay "nicht erfasst".
  const hasEntries = Number(day.entry_count) > 0;
  for (const [label, key, unit] of [["Kalorien", "kcal", "kcal"], ["Kohlenhydrate", "carbs_g", "g"], ["Protein", "protein_g", "g"], ["Fett", "fat_g", "g"], ["Zucker", "sugar_g", "g"], ["Salz", "salt_g", "g"]]) {
    const tile = nutritionTotalTile(label, key, unit, day, hasEntries);
    if (key === "kcal") tile.classList.add("is-hero");
    if (key === "sugar_g" || key === "salt_g") tile.classList.add("is-minor");
    totals.append(tile);
  }
}

function nutritionTotalTile(label, key, unit, day, hasEntries) {
  const tile = h("div", "nutrition-total");
  const value = h("strong");
  const complete = hasEntries ? day[`total_${key}`] : null;
  const range = day.macro_ranges?.[key];
  if (complete != null && Array.isArray(range) && range.length === 2) {
    if (complete < range[0] && day.date < todayIso()) tile.classList.add("nutrition-below-range");
    else if (complete > range[1]) tile.classList.add("nutrition-above-range");
  }
  // A partial sum of known values is shown only with an explicit incompleteness label.
  const total = hasEntries ? (complete ?? day.known_macro_totals?.[key] ?? day.known_intake_totals?.[key]) : null;
  if (total == null) markValueMissing(value);
  else setValueWithUnit(value, `${nutritionNumber(total, unit)} ${unit}`);
  tile.append(value, h("span", null, label + (complete == null && total != null ? " · unvollständig" : "")));
  return tile;
}

function appendNutritionMacroNote(totals, entriesWithoutMacros) {
  if (entriesWithoutMacros > 0) {
    totals.append(h("p", "nutrition-total-note", entriesWithoutMacros === 1 ? "1 Eintrag ohne vollständige Makros" : `${entriesWithoutMacros} Einträge ohne vollständige Makros`));
  }
}

function nutritionWeekValue(value, unit, signed = false) {
  if (value == null) return "Nicht verfügbar";
  const prefix = signed && value > 0 ? "+" : "";
  const suffix = unit ? ` ${unit}` : "";
  return `${prefix}${AppFormat.number(value, { digits: unit === "g" ? 1 : 0 })}${suffix}`;
}

function renderNutritionHistoryPeriod() {
  const root = document.querySelector("#nutritionHistoryPeriod");
  if (!root.childElementCount) {
    root.append(analysisPeriodControls("Zeitraum für Ernährung", nutritionHistoryPeriod, (period) => {
      nutritionHistoryPeriod = period;
      void loadNutrition();
    }));
  }
  root.querySelectorAll("button").forEach((button, index) => {
    button.setAttribute("aria-pressed", String(ANALYSIS_PERIODS[index].key === nutritionHistoryPeriod));
  });
  document.querySelector("#nutritionRefreshButton").setAttribute("aria-label", `Ernährung aktualisieren: Verlauf, ${verlaufPeriod().label}`);
}

// The Monday of a date: a week of the Verlauf is a calendar week, as in Körper (#3930).
function nutritionWeekStart(date) {
  const day = new Date(`${date}T12:00:00Z`);
  return new Date(day.getTime() - ((day.getUTCDay() + 6) % 7) * 86400000).toISOString().slice(0, 10);
}

// Up to 4 weeks show a row per day; 3, 6 and 12 months a row per calendar week, so the list stays short (#3704).
function nutritionChartDays(days) {
  if (days.length <= 28) return days;
  const weeks = [];
  for (const day of days) {
    const start = nutritionWeekStart(day.date);
    if (weeks.at(-1)?.start === start) weeks.at(-1).days.push(day);
    else weeks.push({ start, days: [day] });
  }
  return weeks.map(({ start, days: week }) => {
    const point = { date: week[0].date, end_date: week.at(-1).date, week_start: start, week_days: week.length };
    for (const key of ["intake_kcal", "expenditure_kcal", "balance_kcal"]) {
      const values = week.map((day) => day[key]).filter(Number.isFinite);
      point[`${key}_days`] = values.length;
      point[key] = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
    }
    return point;
  });
}

// A day label of the Verlauf: day and month, with the year once the period is longer than three months (#3930).
function nutritionDayLabel(value) {
  const full = nutritionExpenditureDate(value);
  return nutritionWeekShown?.days.length > 91 ? full : full.slice(0, 5);
}

// The period of the Verlauf: the start keeps its year when it is not the end's year.
function nutritionPeriodText(startDate, endDate) {
  const start = nutritionExpenditureDate(startDate);
  return `${startDate?.slice(0, 4) === endDate?.slice(0, 4) ? start.slice(0, 6) : start}–${nutritionExpenditureDate(endDate)}`;
}

// Days without intake and expenditure are shown as one row per run (#1789).
function nutritionChartRows(chartDays) {
  const rows = [];
  for (const day of chartDays) {
    if (Number.isFinite(day.intake_kcal) || Number.isFinite(day.expenditure_kcal)) { rows.push({ day }); continue; }
    const last = rows.at(-1);
    if (last?.empty) last.end = day.end_date || day.date;
    else rows.push({ empty: true, start: day.date, end: day.end_date || day.date });
  }
  return rows;
}

// "22.11.–13.11." for a run of days, one date for a run of one.
function nutritionRunLabel({ start, end }) {
  const first = nutritionDayLabel(start);
  const last = nutritionDayLabel(end);
  return first === last ? first : `${first}–${last}`;
}

function nutritionEmptyRunRow(run) {
  return h("div", "nutrition-week-chart-day is-empty", h("strong", null, nutritionRunLabel(run)), h("span", null, "keine Daten"));
}

// One bar of a row: Aufnahme blue, Verbrauch green, with its amount.
function nutritionChartSeries(day, [key, label, kind], scale) {
  const value = day[key];
  const track = h("span", "nutrition-week-chart-track");
  if (Number.isFinite(value) && value >= 0) {
    const bar = h("span", "nutrition-week-chart-bar");
    bar.style.width = `${value / scale * 100}%`;
    track.append(bar);
  } else track.classList.add("is-missing");
  const amountText = value == null ? "–" : nutritionWeekValue(value, "kcal");
  const coverage = day.end_date ? `${day[key + "_days"]} von ${day.week_days} Tagen` : "";
  let ariaLabel = `${label}: ${amountText}${value == null ? " (keine Daten)" : ""}`;
  if (coverage) ariaLabel = `${label}: ${amountText}, ${coverage}`;
  return h("div", { className: `nutrition-week-chart-series is-${kind}`, "aria-label": ariaLabel },
    track, h("span", null, amountText, coverage ? [h("br"), h("small", null, coverage)] : null));
}

// The day's balance, coloured by its distance to the energy goal (#3704).
function nutritionChartBalance(balance, goal) {
  const distance = verlaufGoalDistance(balance, goal);
  const text = balance == null ? "keine Daten" : nutritionWeekValue(balance, "kcal", true);
  return h("span", { className: "nutrition-week-chart-balance", "aria-label": `Bilanz: ${text}`, dataset: distance ? { goalDistance: distance } : {} },
    balance == null ? "–" : nutritionWeekValue(balance, "", true));
}

// "Woche ab 16.11." for a week row, "Diese Woche" for the one that holds today, else the day.
function nutritionRowLabel(day) {
  if (!day.end_date) return nutritionDayLabel(day.date);
  // The Verlauf ends yesterday, so the week of today is told by its Monday, not by the last day it holds.
  if (day.week_start === nutritionWeekStart(todayIso())) return "Diese Woche";
  const start = nutritionDayLabel(day.week_start);
  return "Woche ab " + (start.length === 5 ? start + "." : start);
}

// The names of a row's three columns, drawn once above the rows.
function nutritionChartHead(weekly) {
  return h("div", "nutrition-week-chart-head", h("strong", null, weekly ? "Woche" : "Tag"), h("span", null, "Aufnahme · Verbrauch"), h("span", null, "Bilanz"));
}

// A row of the Tagesverlauf: the day, its two bars and, right, its balance.
function nutritionChartDayRow(day, scale, goal = null) {
  const bars = h("div", "nutrition-week-chart-bars",
    ...[["intake_kcal", "Aufnahme", "intake"], ["expenditure_kcal", "Verbrauch", "expenditure"]].map((series) => nutritionChartSeries(day, series, scale)));
  return h("div", "nutrition-week-chart-day",
    h("strong", { "aria-label": (day.end_date && `${nutritionExpenditureDate(day.date)} bis ${nutritionExpenditureDate(day.end_date)}`) || null }, nutritionRowLabel(day)),
    bars, nutritionChartBalance(day.balance_kcal, goal));
}

const NUTRITION_WEEK_MACROS = [["Kohlenhydrate", "carbs_g"], ["Protein", "protein_g"], ["Fett", "fat_g"], ["Zucker", "sugar_g"], ["Salz", "salt_g"]];

// One stat tile of Verlauf's cards: label, value, a small line; a missing value says so.
function nutritionStatTile(label, value, unit, note, signed = false) {
  const number = h("strong");
  if (value == null) markValueMissing(number); else setValueWithUnit(number, nutritionWeekValue(value, unit, signed));
  return h("div", "stat-tile", h("span", null, label), number, note ? h("small", null, note) : null);
}

// "Makros" as tiles; Zucker and Salz only when logged on any day of the period (#3704).
function nutritionMacroCard(week) {
  const tiles = NUTRITION_WEEK_MACROS
    .filter(([, key]) => !["sugar_g", "salt_g"].includes(key) || week.averages[key].days > 0)
    .map(([label, key]) => nutritionStatTile(label, week.averages[key].value, "g", `an ${week.averages[key].days} von ${week.days.length} Tagen`));
  return h("section", { className: "analysis-chart-card", dataset: { verlaufCard: "macros" } }, verlaufHead("Makros", `${verlaufPeriod().label} · Ø pro Tag`), h("div", "stat-tiles", tiles));
}

// The goal as a balance per day: "Ziel −300" for a deficit, "Ziel +300" for a surplus, "Ziel 0" to hold.
function nutritionGoalText(goal) {
  if (!goal) return null;
  if (goal.goal === "hold") return "Ziel 0";
  return `Ziel ${goal.goal === "build" ? "+" : "−"}${formatWhole(goal.goal_kcal)}`;
}

// "Bilanz": the mean balance per day as the hero, coloured by its distance to the goal (#3606),
// with the goal beside it, then Aufnahme, Verbrauch and Summe; the title row names the dates and the
// days with a balance (#3704).
function nutritionBalanceCard(week, goal) {
  const dayCount = week.days.length;
  const balance = week.balance_kcal;
  const average = week.balance_days > 0 && balance != null ? Math.sign(balance) * Math.round(Math.abs(balance / week.balance_days)) || 0 : null;
  const distance = verlaufGoalDistance(average, goal);
  const number = h("strong");
  if (average == null) markValueMissing(number); else setValueWithUnit(number, nutritionWeekValue(average, "kcal", true));
  const aim = nutritionGoalText(goal);
  const hero = h("p", { className: "verlauf-hero", dataset: distance ? { goalDistance: distance } : {} }, number, h("small", null, "Ø pro Tag"), aim ? h("small", "verlauf-goal", aim) : null);
  const period = `${nutritionPeriodText(week.start_date, week.end_date)} · Bilanz an ${week.balance_days} von ${dayCount} Tagen`;
  // The period balance's expenditure is the own day total (athlete decision, #3600).
  const tiles = [nutritionStatTile("Aufnahme", week.averages.intake_kcal.value, "kcal", `Ø pro Tag · an ${week.averages.intake_kcal.days} Tagen`),
    nutritionStatTile("Verbrauch", week.averages.expenditure_kcal.value, "kcal", week.expenditure_source === "own" ? "Ø pro Tag · aus deinen Daten" : "Ø pro Tag"),
    nutritionStatTile("Summe", balance, "kcal", null, true)];
  return h("section", { className: "analysis-chart-card", dataset: { verlaufCard: "balance" } }, verlaufHead("Bilanz", period), hero, h("div", "stat-tiles is-three", tiles));
}

// The parts that depend on the energy goal, drawn again when the read that holds it arrives.
function renderNutritionGoalParts(week) {
  const goal = energyRead?.goal || null;
  document.querySelector("#nutritionWeekBalance").replaceChildren(nutritionBalanceCard(week, goal));
  const chartDays = nutritionChartDays(week.days);
  const scale = Math.max(1, ...chartDays.flatMap((day) => [day.intake_kcal, day.expenditure_kcal].filter((value) => Number.isFinite(value) && value >= 0)));
  document.querySelector("#nutritionWeekChart").replaceChildren(nutritionChartHead(week.days.length > 28),
    ...nutritionChartRows(chartDays).map((entry) => (entry.empty ? nutritionEmptyRunRow(entry) : nutritionChartDayRow(entry.day, scale, goal))));
}

let nutritionWeekShown = null;

function renderNutritionWeek(week) {
  nutritionWeekShown = week;
  document.querySelector("#nutritionWeekStatus").textContent = "";
  document.querySelector("#nutritionWeekMacros").replaceChildren(nutritionMacroCard(week));
  document.querySelector("#nutritionWeekChartHeading").textContent = week.days.length > 28 ? "Wochendurchschnitte" : "Tagesverlauf";
  document.querySelector("#nutritionWeekChartPeriod").textContent = verlaufPeriod().label;
  document.querySelector("#nutritionWeekChartSection").hidden = false;
  // The fit cards, the Bilanz and the rows, which take the goal from the same read.
  renderNutritionFit();
  // Days without data are one row per run, as in the chart: a year is not 365 rows of "–".
  document.querySelector("#nutritionWeekDays").replaceChildren(...nutritionChartRows(week.days).map((entry) => {
    if (entry.empty) return h("tr", null, h("th", { scope: "row" }, nutritionRunLabel(entry)), h("td", { colSpan: 3 }, "keine Daten"));
    const { day } = entry;
    return h("tr", null, h("th", { scope: "row" }, nutritionDayLabel(day.date)),
      ...["intake_kcal", "expenditure_kcal", "balance_kcal"].map((key) => h("td", null, day[key] == null ? "–" : nutritionWeekValue(day[key], "", key === "balance_kcal"))));
  }));
  document.querySelector("#nutritionWeekDetails").hidden = false;
}

// Renders the last loaded response at once; the fresh response replaces it.
function showCachedNutritionView(path, renderView) {
  const cached = readViewCache(path);
  if (!cached) return false;
  try {
    renderView(cached);
    return true;
  } catch { return false; }
}

function isCurrentNutritionWeekLoad(sequence, generation, cacheEpoch, expectedRoute, expectedView) {
  return sequence === nutritionLoadSequence
    && generation === state.sessionGeneration
    && cacheEpoch === viewCacheEpoch
    && (!expectedRoute || state.route === expectedRoute)
    && (!expectedView || nutritionHistoryPeriod === expectedView);
}

async function loadNutritionWeek(sequence, generation, preserve = false, expectedRoute = null, expectedView = null, throwOnError = false) {
  const path = `/api/nutrition/week?days=${verlaufPeriod().days}`;
  renderNutritionHistoryPeriod();
  const cacheEpoch = viewCacheEpoch;
  const status = document.querySelector("#nutritionWeekStatus");
  const cached = showCachedNutritionView(path, renderNutritionWeek);
  if (!cached && !preserve) {
    status.textContent = "Wochenübersicht wird geladen…";
    nutritionWeekShown = null;
    document.querySelector("#nutritionWeekBalance").replaceChildren();
    document.querySelector("#nutritionWeekMacros").replaceChildren();
    document.querySelector("#nutritionWeekDays").replaceChildren();
    document.querySelector("#nutritionWeekChart").replaceChildren();
    document.querySelector("#nutritionWeekChartSection").hidden = true;
    document.querySelector("#nutritionWeekDetails").hidden = true;
  }
  try {
    const week = await api(path);
    if (!isCurrentNutritionWeekLoad(sequence, generation, cacheEpoch, expectedRoute, expectedView)) return;
    renderNutritionWeek(week);
    rememberViewCache(path, week, cacheEpoch);
  } catch (error) {
    if (isCurrentNutritionWeekLoad(sequence, generation, cacheEpoch, expectedRoute, expectedView)) {
      status.textContent = cached
        ? `Gespeicherter Stand. Aktualisierung fehlgeschlagen: ${error.message}`
        : `Wochenübersicht konnte nicht geladen werden: ${error.message}`;
    }
    if (throwOnError) throw error;
  }
}

function isCurrentNutritionDayLoad(sequence, generation, cacheEpoch, dateInput, expectedRoute, expectedDate) {
  return sequence === nutritionLoadSequence
    && generation === state.sessionGeneration
    && cacheEpoch === viewCacheEpoch
    && (!expectedRoute || state.route === expectedRoute)
    && (!expectedDate || dateInput.value === expectedDate);
}

// The energy card's "gegessen" and its digestion come from its own read: once the day's entries
// differ from the ones that read was drawn beside (kcal, macros, flags), it reads again, so no
// card shows the old intake (#3852).
let energyIntakeKey = null;

function nutritionIntakeKey(day) {
  return JSON.stringify([day.date, day.total_kcal, day.entries || []]);
}

function renderNutritionDay(day) {
  document.querySelector("#nutritionDate").value = day.date;
  const intake = nutritionIntakeKey(day);
  // Another day is read again by the energy card itself (#3395); only a changed intake of the
  // same day drops the read here, so a day switch starts one read, not two.
  if (energyIntakeKey !== null && energyIntakeKey !== intake && JSON.parse(energyIntakeKey)[0] === day.date) resetEnergy();
  energyIntakeKey = intake;
  renderDiaryEnergy();
  document.querySelector("#nutritionStatus").textContent = day.entry_count ? "" : "Noch keine Mahlzeiten erfasst.";
  const totals = document.querySelector("#nutritionTotals");
  totals.replaceChildren();
  appendNutritionTotals(totals, day);
  appendNutritionMacroNote(totals, Number(day.entries_without_macros) || 0);
  document.querySelector("#nutritionEntries").replaceChildren(...day.entries.map((item) => nutritionCard(item, false, day.sessions || [])));
}

function showNutritionDayLoadError(status, cached, error, dateInput, requestedDate) {
  if (cached) {
    status.textContent = `Gespeicherter Stand. Aktualisierung fehlgeschlagen: ${error.message}`;
    return;
  }
  // The requested day stays in the date field so ← and → still have a day to move from.
  dateInput.value = requestedDate;
  const retry = h("button", { type: "button", className: "secondary-button" }, "Erneut laden");
  retry.addEventListener("click", () => void loadNutrition());
  status.textContent = `Ernährung konnte nicht geladen werden: ${error.message}`;
  status.append(h("br"), retry);
}

async function loadNutrition({ preserve = false, expectedRoute = null, expectedDate = null, throwOnError = false } = {}) {
  const sequence = ++nutritionLoadSequence;
  const generation = state.sessionGeneration;
  if (state.route === "nutrition/body") return;
  if (state.route === "nutrition/week") return loadNutritionWeek(sequence, generation, preserve, expectedRoute, expectedDate, throwOnError);
  const dateInput = document.querySelector("#nutritionDate");
  const status = document.querySelector("#nutritionStatus");
  const path = dateInput.value ? `/api/nutrition/day?date=${encodeURIComponent(dateInput.value)}` : "/api/nutrition/day";
  // A start without a date asks for today, so its cached view is keyed by today.
  const cacheEpoch = viewCacheEpoch;
  const requestedDate = dateInput.value || todayIso();
  const cacheKey = `/api/nutrition/day?date=${encodeURIComponent(requestedDate)}`;
  const cached = showCachedNutritionView(cacheKey, renderNutritionDay);
  if (!cached && !preserve) {
    status.textContent = "Ernährung wird geladen…";
    document.querySelector("#nutritionEntries").replaceChildren();
    document.querySelector("#nutritionTotals").replaceChildren();
  }
  try {
    const day = await api(path);
    if (!isCurrentNutritionDayLoad(sequence, generation, cacheEpoch, dateInput, expectedRoute, expectedDate)) return;
    renderNutritionDay(day);
    rememberViewCache(`/api/nutrition/day?date=${encodeURIComponent(day.date)}`, day, cacheEpoch);
  } catch (error) {
    if (isCurrentNutritionDayLoad(sequence, generation, cacheEpoch, dateInput, expectedRoute, expectedDate)) {
      showNutritionDayLoadError(status, cached, error, dateInput, requestedDate);
      if (throwOnError) throw error;
    }
  }
}

document.querySelector("#nutritionDate").addEventListener("change", () => void loadNutrition());
for (const [id, offset] of [["nutritionPrevious", -1], ["nutritionNext", 1]]) {
  document.getElementById(id).addEventListener("click", () => {
    const input = document.querySelector("#nutritionDate");
    if (!input.value) return;
    const day = new Date(`${input.value}T12:00:00Z`);
    day.setUTCDate(day.getUTCDate() + offset);
    input.value = day.toISOString().slice(0, 10);
    void loadNutrition();
  });
}
document.querySelector("#nutritionToday").addEventListener("click", () => { document.querySelector("#nutritionDate").value = ""; void loadNutrition(); });

const nutritionRefresh = AppViewRefresh.create({
  api,
  waitForJob: (...args) => waitForSyncJob(...args),
  getRoute: () => state.route,
  getRequest: (route) => {
    const path = "/api/nutrition/refresh";
    if (route === "nutrition/products") return { path, body: { area: "products" } };
    if (route === "nutrition/body") return { path, body: { area: "body" } };
    if (route === "nutrition/week") return { path, body: { area: "week", days: verlaufPeriod().days } };
    return { path, body: { area: "diary", date: document.querySelector("#nutritionDate").value || todayIso() } };
  },
  getViewKey: (route) => {
    if (route === "nutrition/week") return nutritionHistoryPeriod;
    if (route === "nutrition/diary") return document.querySelector("#nutritionDate").value;
    if (route === "nutrition/products") return document.querySelector("#nutritionProductQuery").value.trim();
    return null;
  },
  reload: (route, request) => {
    if (route === "nutrition/products") {
      const query = document.querySelector("#nutritionProductQuery").value.trim();
      return loadNutritionProducts(query, { background: true, throwOnError: true, refresh: true, expectedQuery: query });
    }
    if (route === "nutrition/body") resetSkinfolds();
    if (route === "nutrition/body") return api("/api/performance").then((result) => {
      if (state.route !== route) return;
      state.data.performance = result.performance || result;
      renderBodyAnalysis(state.data.performance?.history?.body);
    });
    if (route === "nutrition/week") return loadNutrition({ preserve: true, expectedRoute: route, expectedDate: nutritionHistoryPeriod, throwOnError: true });
    // The diary holds the energy table since #3252, so its refresh reads it again too.
    resetEnergy();
    return loadNutrition({ preserve: true, expectedRoute: route, expectedDate: document.querySelector("#nutritionDate").value, throwOnError: true });
  },
  button: document.querySelector("#nutritionRefreshButton"),
  status: document.querySelector("#nutritionRefreshStatus"),
});
document.querySelector("#nutritionRefreshButton").addEventListener("click", () => void nutritionRefresh());

function resetNutritionView() {
  resetSkinfolds();
  ++nutritionLoadSequence;
  ++nutritionProductLoadSequence;
  nutritionLookupResultShown = false;
  nutritionProductSectionsOpen.favorites = nutritionProductSectionsOpen.all = false;
  nutritionProductSearchShown = "";
  nutritionProductSectionsBeforeSearch = null;
  stopNutritionBarcode();
  document.querySelector("#nutritionBarcodeDialog")?.close();
  document.querySelector("#nutritionProductUseDialog")?.close();
  nutritionProductForUse = null;
  document.querySelector("#nutritionDate").value = "";
  nutritionWeekShown = null;
  for (const id of ["nutritionWeekBalance", "nutritionWeekStatus", "nutritionWeekMacros", "nutritionWeekDays", "nutritionWeekChart", "nutritionEntries", "nutritionTotals", "nutritionEnergy", "nutritionStatus", "nutritionProducts", "nutritionLookupResult", "nutritionProductStatus"]) document.getElementById(id)?.replaceChildren();
  document.querySelector("#nutritionLookupResult").hidden = true;
  document.querySelector("#nutritionWeekChartSection").hidden = true;
  const editor = document.querySelector("#nutritionExtraction"); if (editor) { editor.replaceChildren(); editor.hidden = true; }
  const query = document.querySelector("#nutritionProductQuery"); if (query) query.value = "";
}

let nutritionBarcodeStream = null;
let nutritionBarcodeFrame = null;
let nutritionBarcodeDetector = null;
let nutritionBarcodeSession = 0;
let nutritionProductForUse = null;
let nutritionProductUseSequence = 0;
let nutritionProductLoadSequence = 0;
let nutritionLookupResultShown = false;

function nutritionProductValue(value) {
  return value == null || value === "" ? "" : String(value);
}

function nutritionDatabaseSource(source) {
  if (source === "Open Food Facts" || source === "open_food_facts") return "open_food_facts";
  if (source === "Yazio" || source === "yazio") return "yazio";
  return source || "packaging_label";
}

function normalizeNutritionProduct(product = {}) {
  const per100 = product.per_100 || {};
  const source = nutritionDatabaseSource(product.source);
  const normalized = {
    ...product,
    brand: product.brand || product.brands || "",
    kcal: product.kcal ?? per100.kcal ?? null,
    carbs_g: product.carbs_g ?? per100.carbs_g ?? null,
    protein_g: product.protein_g ?? per100.protein_g ?? null,
    fat_g: product.fat_g ?? per100.fat_g ?? null,
    sugar_g: product.sugar_g ?? per100.sugar_g ?? null,
    fiber_g: product.fiber_g ?? per100.fiber_g ?? null,
    salt_g: product.salt_g ?? per100.salt_g ?? null,
    confidence: product.confidence ?? product.extraction_confidence ?? null,
    source_url: product.source_url || "",
    source,
  };
  // A stored database entry is a product with an id of its own; only a live lookup hit has none.
  if (product.origin) return normalized;
  if (source === "open_food_facts" && String(product.id || "").startsWith("off:")) {
    normalized.external_id = normalized.external_id || product.id;
    const barcode = /^off:(\d{8,14})$/.exec(String(product.id));
    if (barcode && !normalized.barcode) normalized.barcode = barcode[1];
    delete normalized.id;
  }
  if (source === "yazio" && String(product.id || "").startsWith("yazio:")) {
    normalized.external_id = normalized.external_id || product.id;
    delete normalized.id;
  }
  return normalized;
}

function nutritionProductForm(product = {}) {
  const form = h("form", "nutrition-product-form");
  form.innerHTML = `<input name="id" type="hidden"><input name="source" type="hidden"><input name="source_url" type="hidden"><input name="external_id" type="hidden"><input name="provenance" type="hidden"><input name="warnings" type="hidden"><label>Marke<input name="brand" maxlength="120"></label><label>EAN/GTIN<input name="barcode" inputmode="numeric" pattern="[0-9]{8,14}" maxlength="14"></label><div class="nutrition-product-grid"><label>Name<input name="name" required maxlength="160"></label><label>kcal<input name="kcal" type="number" min="0" step="any"></label><label>Kohlenhydrate (g)<input name="carbs_g" type="number" min="0" step="any"></label><label>Protein (g)<input name="protein_g" type="number" min="0" step="any"></label><label>Fett (g)<input name="fat_g" type="number" min="0" step="any"></label><label>Bezugsmenge<input name="basis_amount" type="number" min="0.1" step="0.1" value="100"></label><label>Einheit<select name="basis_unit"><option value="g">pro 100 g</option><option value="ml">pro 100 ml</option><option value="portion">pro Portion</option></select></label><label data-product-size="portion"><span data-size-label="Portion">Portion (g)</span><input name="portion_amount" type="number" min="0.1" max="10000" step="any"></label><label data-product-size="package"><span data-size-label="Packung">Packung (g)</span><input name="package_amount" type="number" min="0.1" max="10000" step="any"></label></div><p class="fine-print" data-extraction-confidence></p><ul class="nutrition-warning-checklist" data-extraction-warnings aria-label="Hinweise zur Prüfung" hidden></ul><div class="nutrition-actions"><button type="submit" class="push-button">Produkt lokal speichern</button><button type="button" class="secondary-button" data-product-cancel>Abbrechen</button></div>`;
  populateNutritionProductFields(form, product);
  form.elements.source.value = product.source || "packaging_label";
  form.append(h("input", { name: "confidence", type: "hidden", value: nutritionProductValue(product.confidence) }));
  form.elements.basis_amount.required = true;
  form.elements.basis_unit.required = true;
  setNutritionProductBasis(form, product);
  form.elements.basis_unit.addEventListener("change", () => {
    for (const name of ["portion_amount", "package_amount"]) form.elements[name].value = "";
    syncNutritionProductSizes(form);
  });
  syncNutritionProductSizes(form);
  setNutritionProductMetadata(form, product);
  setNutritionProductConfidence(form, product);
  if (product.origin && nutritionIsDatabaseProduct(product)) lockNutritionDatabaseFields(form);
  return form;
}

// A database owns name, brand, EAN and basis of its entry: shown, not editable.
function lockNutritionDatabaseFields(form) {
  form.dataset.database = "true";
  for (const name of ["name", "brand", "barcode", "basis_amount", "basis_unit"]) form.elements[name].disabled = true;
  // Portion and package sizes belong to own products only (#3452).
  for (const label of form.querySelectorAll("[data-product-size]")) label.hidden = true;
  // The values come from the database, not from a label photo.
  for (const hint of form.querySelectorAll("p.fine-print")) hint.hidden = true;
  form.querySelector('button[type="submit"]').textContent = "Nährwerte speichern";
}

function populateNutritionProductFields(form, product) {
  for (const [key, value] of Object.entries(product)) {
    const field = form.elements.namedItem(key);
    if (field) field.value = nutritionProductValue(value);
  }
  const grid = form.querySelector(".nutrition-product-grid");
  for (const [key, label] of [["sugar_g", "Zucker (g)"], ["fiber_g", "Ballaststoffe (g)"], ["salt_g", "Salz (g)"]]) {
    grid.append(h("label", null, label, h("input", { name: key, type: "number", min: "0", step: "any", value: nutritionProductValue(product[key]) })));
  }
}

function setNutritionProductBasis(form, product) {
  if (!product.basis_amount) form.elements.basis_amount.value = "";
  if (product.basis_unit) return;
  form.elements.basis_unit.prepend(h("option", { value: "", selected: true, disabled: true }, "Bezugsmenge auswählen"));
  form.elements.basis_unit.value = "";
}

// A size counts in the basis unit; a product counted per portion takes none (#3452).
function syncNutritionProductSizes(form) {
  const unit = form.elements.basis_unit.value;
  for (const label of form.querySelectorAll("[data-product-size]")) {
    label.hidden = form.dataset.database === "true" || !["g", "ml"].includes(unit);
    // One text node: a label that is a grid would put the name, the unit and the bracket on three lines.
    const name = label.querySelector("[data-size-label]");
    name.textContent = `${name.dataset.sizeLabel} (${unit})`;
  }
}

function setNutritionProductMetadata(form, product) {
  for (const key of ["source_url", "external_id"]) form.elements[key].value = nutritionProductValue(product[key]);
  const provenance = product.provenance && typeof product.provenance === "object" ? product.provenance : { kind: product.source || product.provenance || "packaging_label", requires_confirmation: true };
  form.elements.provenance.value = JSON.stringify(provenance);
  form.elements.warnings.value = JSON.stringify(product.warnings || []);
}

function setNutritionProductConfidence(form, product) {
  const confidence = Number(product.confidence);
  const warning = Number.isFinite(confidence) && confidence < 0.65 ? "Geringe Erkennungssicherheit. Werte besonders sorgfältig prüfen." : "Erkannte Werte bitte mit dem Etikett abgleichen.";
  const confidenceLine = form.querySelector("[data-extraction-confidence]");
  // Only values read from a photo carry a confidence; a manual product or an edit has none. 0 is a real, lowest reading (#3930).
  const extracted = product.confidence != null && product.confidence !== "" && Number.isFinite(confidence);
  confidenceLine.hidden = !extracted;
  confidenceLine.textContent = extracted ? `Erkennungssicherheit: ${Math.round(confidence * 100)} %. ${warning}` : "";
  const checklist = form.querySelector("[data-extraction-warnings]");
  const warnings = Array.isArray(product.warnings) ? product.warnings.map((text) => String(text ?? "").trim()).filter(Boolean) : [];
  checklist.replaceChildren(...warnings.map((text) => h("li", null, text)));
  checklist.hidden = warnings.length === 0;
  for (const [field, score] of Object.entries(product.field_confidence || {})) {
    const input = form.elements.namedItem(field);
    if (input && Number(score) < 0.65) { input.classList.add("nutrition-low-confidence"); input.title = "Unsichere Fotoerkennung; bitte am Etikett prüfen."; }
  }
}

function nutritionProductPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  const numbers = Object.keys(NUTRITION_FIELD_NAMES);
  if (form.dataset.database === "true") return Object.fromEntries([["id", data.id], ...numbers.map((key) => [key, data[key] === "" ? null : Number(data[key])])]);
  const source = data.source || "packaging_label";
  let provenance;
  try { provenance = JSON.parse(data.provenance || "{}"); } catch { provenance = {}; }
  const payload = { ...data, source, provenance: { ...(provenance && typeof provenance === "object" ? provenance : {}), kind: source, confirmed: true } };
  try { payload.warnings = JSON.parse(data.warnings || "[]"); } catch { payload.warnings = []; }
  for (const key of [...numbers, "basis_amount", "confidence"]) payload[key] = data[key] === "" ? null : Number(data[key]);
  for (const size of ["portion", "package"]) {
    const amount = ["g", "ml"].includes(data.basis_unit) && data[`${size}_amount`] ? Number(data[`${size}_amount`]) : null;
    payload[`${size}_amount`] = amount;
    payload[`${size}_unit`] = amount === null ? null : data.basis_unit;
  }
  return payload;
}

const NUTRITION_FIELD_NAMES = { kcal: "Energie", carbs_g: "Kohlenhydrate", protein_g: "Eiweiß", fat_g: "Fett", sugar_g: "Zucker", fiber_g: "Ballaststoffe", salt_g: "Salz" };
const NUTRITION_PRODUCT_SECTIONS = [["favorites", "Favoriten"], ["all", "Alle Produkte"]];
// Both sections start collapsed whenever the page is opened (#2267).
const nutritionProductSectionsOpen = { favorites: false, all: false };

function nutritionIsDatabaseProduct(product) {
  return product.origin ? product.origin !== "own" : product.local === false;
}

function nutritionMissingFields(product) {
  const keys = Array.isArray(product.missing_fields) ? product.missing_fields : Object.keys(NUTRITION_FIELD_NAMES).filter((key) => product[key] == null);
  return keys.map((key) => NUTRITION_FIELD_NAMES[key] || key);
}

// A fetch time as the athlete's local date, "01.10.2026".
function nutritionProductDate(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" }) : "";
}

const NUTRITION_SOURCE_NAMES = { packaging_label: "Verpackungsangabe", manual: "manuell", open_food_facts: "Open Food Facts", yazio: "Yazio", bls: "BLS", fddb_export: "FDDB" };

function nutritionOriginText(product) {
  if (!nutritionIsDatabaseProduct(product)) return ["eigen", NUTRITION_SOURCE_NAMES[product.source]].filter(Boolean).join(" · ");
  const date = nutritionProductDate(product.fetched_at || product.stored_at);
  return date ? `Datenbank · Stand ${date}` : "Datenbank";
}

function nutritionIncompleteMark(label, text) {
  const mark = h("span", { className: "nutrition-incomplete", "aria-label": label }, text);
  mark.setAttribute("role", "img");
  return mark;
}

function reloadNutritionProducts(options = {}) {
  return loadNutritionProducts(document.querySelector("#nutritionProductQuery").value.trim(), options);
}

function showNutritionProductError(prefix, error) {
  document.querySelector("#nutritionProductStatus").textContent = `${prefix}: ${error.message}`;
}

async function refreshNutritionProduct(product, button) {
  button.disabled = true;
  const status = document.querySelector("#nutritionProductStatus");
  const generation = state.sessionGeneration;
  try {
    const result = await api("/api/nutrition/products/database/refresh", { method: "POST", body: JSON.stringify({ id: product.id }) });
    if (generation !== state.sessionGeneration) return;
    await reloadNutritionProducts({ refresh: true });
    if (generation !== state.sessionGeneration) return;
    showNutritionReceipt(status, [`„${product.name}“ aktualisiert.`, result.notice || result.food?.notice].filter(Boolean).join(" "), result.change, () => reloadNutritionProducts({ refresh: true }));
  } catch (error) {
    if (generation !== state.sessionGeneration) return;
    status.textContent = error.reason === "nutrition_product_barcode_missing"
      ? "Für Aktualisieren wird eine EAN gebraucht. Unter Bearbeiten eintragen."
      : `Eintrag konnte nicht aktualisiert werden: ${error.message}`;
  }
  button.disabled = false;
}

// #3299: one background run re-checks every product with the single "Aktualisieren" rule; the
// button shows its progress and the end its counts.
// "Aufräumen" (#3444): the server names the products not used for the chosen days;
// the athlete sees the number and the list first, then one run removes them, with one undo.
let nutritionCleanupSequence = 0;

async function openNutritionCleanup() {
  document.querySelector("#nutritionCleanupDays").value = "90";
  document.querySelector("#nutritionCleanupDialog").showModal();
  await loadNutritionCleanup();
}

async function loadNutritionCleanup() {
  const sequence = ++nutritionCleanupSequence;
  const days = document.querySelector("#nutritionCleanupDays").value;
  const count = document.querySelector("#nutritionCleanupCount");
  const list = document.querySelector("#nutritionCleanupList");
  const confirm = document.querySelector("#nutritionCleanupConfirm");
  const status = document.querySelector("#nutritionCleanupStatus");
  confirm.disabled = true; status.textContent = ""; count.textContent = "Wird gezählt…";
  try {
    const preview = await api(`/api/nutrition/products/cleanup?days=${encodeURIComponent(days)}`);
    if (sequence !== nutritionCleanupSequence) return;
    const today = todayIso();
    const noun = preview.count === 1 ? "Produkt wird" : "Produkte werden";
    count.textContent = preview.count ? `${preview.count} ${noun} gelöscht.` : "Nichts zu löschen.";
    list.hidden = !preview.count;
    list.querySelector("ul").replaceChildren(...preview.products.map((product) => h("li", null,
      [product.brand, product.name].filter(Boolean).join(" · "), " ",
      h("small", null, nutritionLastUseText({ ...product, usage_count: product.last_used ? 1 : 0 }, today)))));
    confirm.textContent = `${preview.count} löschen`;
    confirm.disabled = !preview.count;
  } catch (error) {
    if (sequence === nutritionCleanupSequence) { count.textContent = ""; status.textContent = `Konnte nicht gezählt werden: ${error.message}`; }
  }
}

async function runNutritionCleanup() {
  const confirm = document.querySelector("#nutritionCleanupConfirm");
  const days = Number(document.querySelector("#nutritionCleanupDays").value);
  confirm.disabled = true;
  try {
    const result = await api("/api/nutrition/products/cleanup", { method: "POST", body: JSON.stringify({ days, confirmed: true }) });
    document.querySelector("#nutritionCleanupDialog").close();
    await reloadNutritionProducts();
    showNutritionReceipt(document.querySelector("#nutritionProductStatus"), `${result.count} ${result.count === 1 ? "Produkt" : "Produkte"} gelöscht.`, result.change, () => reloadNutritionProducts());
  } catch (error) {
    document.querySelector("#nutritionCleanupStatus").textContent = error.message;
    confirm.disabled = false;
  }
}

let nutritionRefreshAllTimer = null;

function nutritionRefreshAllReceipt(run) {
  const counts = `${run.filled} ergänzt, ${run.unchanged} unverändert, ${run.skipped} ohne EAN übersprungen, ${run.failed} fehlgeschlagen`;
  // #3475: a run stops after repeated Open Food Facts timeouts; the rest keeps its values.
  if (run.stopped === "food_database_timeout") return `Open Food Facts antwortet gerade nicht. Angehalten nach ${run.done} / ${run.total}: ${counts}. Die übrigen Produkte bleiben unverändert.`;
  return `Alle aktualisiert: ${counts}.`;
}

async function showNutritionRefreshAll(run, announce) {
  const button = document.querySelector("#nutritionRefreshAllButton");
  clearTimeout(nutritionRefreshAllTimer);
  button.disabled = Boolean(run?.running);
  button.textContent = run?.running ? `Aktualisiere ${run.done} / ${run.total}` : "Alle aktualisieren";
  if (run?.running) {
    nutritionRefreshAllTimer = setTimeout(() => void pollNutritionRefreshAll(true), 1500);
  } else if (announce && run?.finished_at) {
    const generation = state.sessionGeneration;
    await reloadNutritionProducts({ refresh: true });
    if (generation === state.sessionGeneration) document.querySelector("#nutritionProductStatus").textContent = nutritionRefreshAllReceipt(run);
  }
}

async function pollNutritionRefreshAll(announce = false, start = false) {
  const generation = state.sessionGeneration;
  try {
    const run = await api("/api/nutrition/products/refresh-all", start ? { method: "POST", body: "{}" } : undefined);
    if (generation === state.sessionGeneration) await showNutritionRefreshAll(run, announce);
  } catch (error) {
    if (generation !== state.sessionGeneration) return;
    await showNutritionRefreshAll(null, false);
    showNutritionProductError("Aktualisieren", error);
  }
}

async function deleteNutritionProduct(product) {
  const database = nutritionIsDatabaseProduct(product);
  // Only the irreversible removal from the database asks; an own product acts at once with its undo line.
  if (database && !await requestConfirmation(`Der Eintrag „${product.name}“ wird aus der Datenbank entfernt. Eigene Produkte bleiben erhalten.`, { title: "Datenbankeintrag entfernen?", danger: true, confirmLabel: "Entfernen" })) return;
  const generation = state.sessionGeneration;
  try {
    const result = await api(database ? "/api/nutrition/products/database/delete" : "/api/nutrition/products/archive", { method: "POST", body: JSON.stringify({ id: product.id, confirmed: true }) });
    if (generation !== state.sessionGeneration) return;
    await reloadNutritionProducts();
    if (generation !== state.sessionGeneration) return;
    showNutritionReceipt(document.querySelector("#nutritionProductStatus"), database ? `Eintrag „${product.name}“ aus der Datenbank entfernt.` : `Produkt „${product.name}“ gelöscht.`, result.change, () => reloadNutritionProducts());
  } catch (error) { if (generation === state.sessionGeneration) showNutritionProductError(database ? "Eintrag konnte nicht entfernt werden" : "Produkt konnte nicht gelöscht werden", error); }
}

function appendNutritionProductActions(card, product, local) {
  if (!local || product.local === false) {
    const button = h("button", { type: "button", className: "secondary-button" }, "Lokal speichern");
    button.addEventListener("click", () => saveNutritionProduct(product)); card.append(button);
  } else if (product.id) {
    const actions = h("div", "nutrition-actions");
    for (const [label, primary, run] of [
      ["Verwenden", true, () => useNutritionProduct(product)],
      ["Bearbeiten", false, () => saveNutritionProduct(product)],
      ["Aktualisieren", false, (button) => refreshNutritionProduct(product, button)],
      ["Löschen", false, () => deleteNutritionProduct(product)],
    ]) {
      let buttonClass = primary ? "push-button" : "secondary-button";
      if (label === "Löschen") buttonClass += " danger-button";
      const button = h("button", { type: "button", className: buttonClass }, label);
      button.addEventListener("click", async () => {
        if (label !== "Löschen") { run(button); return; }
        // One tap, one delete: the button waits for the request, as the removed dialog did.
        button.disabled = true;
        try { await run(button); } finally { button.disabled = false; }
      });
      actions.append(button);
      // Without a known unit an amount cannot be counted against the values.
      if (label === "Verwenden" && !product.basis_unit) { button.disabled = true; button.title = "Bezugsmenge unbekannt"; }
    }
    card.append(actions);
  }
}

function nutritionFavoriteButton(product) {
  const favorite = Boolean(product.favorite);
  const button = h("button", { type: "button", className: "nutrition-favorite", "aria-pressed": String(favorite), "aria-label": `${favorite ? "Favorit entfernen" : "Als Favorit markieren"}: ${product.name}` }, favorite ? "★" : "☆");
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api("/api/nutrition/products/favorite", { method: "POST", body: JSON.stringify({ id: product.id, favorite: !favorite }) });
      await reloadNutritionProducts({ refresh: true });
    } catch (error) {
      showNutritionProductError("Favorit konnte nicht geändert werden", error);
      button.disabled = false;
    }
  });
  return button;
}

function nutritionProductSection(key, title, products, searching) {
  const section = h("details", { className: "nutrition-product-section", dataset: { section: key } });
  // A new search opens the sections with hits once; later toggles are kept.
  if (searching) nutritionProductSectionsOpen[key] = products.length > 0;
  section.open = nutritionProductSectionsOpen[key];
  // A replaced section's late toggle must not overwrite the shown one's state (#3735).
  section.addEventListener("toggle", () => { if (section.isConnected !== false) nutritionProductSectionsOpen[key] = section.open; });
  const incomplete = products.filter((product) => nutritionMissingFields(product).length).length;
  section.append(
    h("summary", null, `${title} (${products.length})`, incomplete ? [" ", nutritionIncompleteMark(`${incomplete} unvollständig`, `⚠ ${incomplete}`)] : null),
    h("div", "nutrition-list", products.map((product) => nutritionProductCard(product, true))));
  return section;
}

// How many logged meals used the product (#3435) and when it was last used (#3444),
// from the merged list's read.
function nutritionUsageCount(product) {
  if (!Number.isInteger(product.usage_count)) return null;
  const count = product.usage_count;
  return h("span", { className: `nutrition-usage${count ? "" : " is-zero"}`, title: `${count}-mal erfasst` }, `${count}×`);
}

// "14.11." for a day of this year, "14.11.2025" for an older one.
function nutritionShortDay(day, today) {
  const [year, month, date] = String(day).slice(0, 10).split("-");
  return `${date}.${month}.${year === today.slice(0, 4) ? "" : year}`;
}

function nutritionLastUseText(product, day = null) {
  if (!Number.isInteger(product.usage_count)) return "";
  const today = day || todayIso();
  if (product.last_used) return product.last_used === today ? "zuletzt heute" : `zuletzt ${nutritionShortDay(product.last_used, today)}`;
  const created = product.created_at ? timezoneDateKey(state.data?.profile?.timezone, new Date(product.created_at)) : "";
  return created ? `nie genutzt · angelegt ${nutritionShortDay(created, today)}` : "nie genutzt";
}

function nutritionProductCard(product, local) {
  const kcalLabel = product.kcal == null ? "kcal unbekannt" : `${product.kcal} kcal`;
  const missing = nutritionMissingFields(product);
  const card = h("article", "nutrition-card nutrition-product-card",
    h("div", "nutrition-product-head",
      h("h3", null, [product.brand, product.name].filter(Boolean).join(" · ") || "Unbenanntes Produkt"),
      nutritionUsageCount(product),
      local && product.id && product.local !== false ? nutritionFavoriteButton(product) : null),
    h("p", null, `${kcalLabel} · KH ${nutritionNumber(product.carbs_g, "g")} g · Protein ${nutritionNumber(product.protein_g, "g")} g · Fett ${nutritionNumber(product.fat_g, "g")} g`),
    h("div", "nutrition-product-marks",
      h("span", "nutrition-origin", nutritionOriginText(product)),
      nutritionLastUseText(product) ? h("span", "nutrition-last-use", nutritionLastUseText(product)) : null,
      missing.length ? nutritionIncompleteMark(`Unvollständig, es fehlen: ${missing.join(", ")}`, `⚠ fehlt: ${missing.join(", ")}`) : null),
    h("small", null, [product.barcode ? `EAN ${product.barcode}` : "", product.basis_amount && product.basis_unit ? `Basis: ${product.basis_amount} ${product.basis_unit}` : "Bezugsmenge unbekannt", ...nutritionProductSizes(product).map(({ label, amount, unit }) => `${label} ${nutritionNumber(amount)} ${unit}`)].filter(Boolean).join(" · ")));
  appendNutritionProductActions(card, product, local);
  return card;
}

let nutritionProductSearchShown = "";
// What was open before a search; clearing the search restores it.
let nutritionProductSectionsBeforeSearch = null;

function renderNutritionProducts(products, { local = true, targetId = "#nutritionProducts", search = "" } = {}) {
  const target = document.querySelector(targetId);
  if (!target) return;
  // A section opened just before this render keeps its state even before its toggle event ran (#3735).
  if (targetId === "#nutritionProducts") {
    for (const node of target.children) if (Object.hasOwn(nutritionProductSectionsOpen, node.dataset?.section ?? "")) nutritionProductSectionsOpen[node.dataset.section] = node.open;
  }
  target.replaceChildren();
  if (!products?.length) { target.textContent = "Keine Produkte gefunden."; return; }
  const items = products.map(normalizeNutritionProduct);
  if (!local || targetId !== "#nutritionProducts") { target.append(...items.map((product) => nutritionProductCard(product, local))); return; }
  const newSearch = Boolean(search) && search !== nutritionProductSearchShown;
  if (newSearch && !nutritionProductSearchShown) nutritionProductSectionsBeforeSearch = { ...nutritionProductSectionsOpen };
  if (!search && nutritionProductSectionsBeforeSearch) { Object.assign(nutritionProductSectionsOpen, nutritionProductSectionsBeforeSearch); nutritionProductSectionsBeforeSearch = null; }
  nutritionProductSearchShown = search || "";
  target.append(...NUTRITION_PRODUCT_SECTIONS.map(([key, title]) => nutritionProductSection(key, title, key === "favorites" ? items.filter((product) => product.favorite) : items, newSearch)));
}

// The queued meal the open dialog replaces (an edit from the outbox, #3817), else null.
let nutritionOutboxEdit = null;

// A rejected queued meal opens in the product dialog with its product and amount.
async function editQueuedMeal(edit) {
  try {
    const { products } = await api("/api/nutrition/products?scope=all");
    const product = products.map(normalizeNutritionProduct).find((item) => item.id === edit.payload.product_id);
    if (!product) throw new Error("Das Produkt ist nicht mehr vorhanden.");
    await useNutritionProduct(product, edit);
  } catch (error) {
    toast(`Eintrag nicht bearbeitbar: ${error.message}`, true);
  }
}

async function useNutritionProduct(product, edit = null) {
  const unit = nutritionProductUnit(product);
  nutritionProductForUse = product;
  nutritionOutboxEdit = edit;
  const useSequence = ++nutritionProductUseSequence;
  const dialog = document.querySelector("#nutritionProductUseDialog");
  document.querySelector("#nutritionProductUseName").textContent = product.name || "Lokales Produkt";
  document.querySelector("#nutritionProductUseUnit").textContent = `(in ${unit})`;
  const amount = document.querySelector("#nutritionProductUseAmount");
  amount.value = renderNutritionProductUseSizes(product, unit) ?? (product.basis_amount || 100);
  const select = document.querySelector("#nutritionProductUseMeal");
  const addButton = document.querySelector("#nutritionProductUseAdd");
  const status = document.querySelector("#nutritionProductUseStatus");
  select.replaceChildren(h("option", { value: "" }, "Neuer Snack")); addButton.disabled = true; status.textContent = "";
  // A queued meal stays a new snack: no choice of an existing meal, no Coach draft.
  select.closest("label").hidden = Boolean(edit);
  document.querySelector("#nutritionProductUseCoach").hidden = Boolean(edit);
  if (edit) {
    amount.value = edit.payload.amount;
    for (const chip of document.querySelectorAll("#nutritionProductUseSizes input")) chip.checked = Number(chip.value) === Number(amount.value);
    addButton.disabled = false;
    dialog.showModal();
    return;
  }
  dialog.showModal();
  const generation = state.sessionGeneration;
  try {
    const day = await api(`/api/nutrition/day?date=${encodeURIComponent(document.querySelector("#nutritionDate").value || todayIso())}`);
    if (generation !== state.sessionGeneration || useSequence !== nutritionProductUseSequence) return;
    for (const meal of day.entries || []) {
      select.append(h("option", { value: meal.id }, [nutritionMealTitle(meal), String(meal.description || "").slice(0, 40)].filter(Boolean).join(" · ")));
    }
  } catch (error) {
    if (generation === state.sessionGeneration && useSequence === nutritionProductUseSequence) status.textContent = `Mahlzeiten konnten nicht geladen werden: ${error.message}`;
  } finally {
    if (generation === state.sessionGeneration && useSequence === nutritionProductUseSequence) addButton.disabled = false;
  }
}

// The stored portion and package of a product, in its basis unit (#3452).
function nutritionProductSizes(product = {}) {
  return [["portion", "Portion"], ["package", "Packung"]]
    .filter(([size]) => Number(product[`${size}_amount`]) > 0 && product[`${size}_unit`] === product.basis_unit)
    .map(([size, label]) => ({ size, label, amount: Number(product[`${size}_amount`]), unit: product[`${size}_unit`] }));
}

// Chips for 1 Portion, ½ Packung and 1 Packung; returns the portion to start with.
function renderNutritionProductUseSizes(product, unit) {
  const field = document.querySelector("#nutritionProductUseSizes");
  const group = field.querySelector(".chip-group");
  const choices = nutritionProductSizes(product).flatMap(({ size, amount }) => size === "portion"
    ? [["1 Portion", amount]]
    : [["½ Packung", amount / 2], ["1 Packung", amount]])
    // The amount field takes steps of 0.1, so a chip fills a value it accepts.
    .map(([text, value]) => [text, Math.round(value * 10) / 10]);
  const portion = choices.find(([text]) => text === "1 Portion")?.[1];
  group.replaceChildren(...choices.map(([text, value]) => h("label", "chip",
    h("input", { type: "radio", name: "nutritionProductUseSize", value: String(value), checked: text === "1 Portion" }),
    h("span", null, `${text} · ${nutritionNumber(value)} ${unit}`))));
  field.hidden = choices.length === 0;
  return portion;
}

function selectNutritionProductUseSize(event) {
  const amount = document.querySelector("#nutritionProductUseAmount");
  if (event.target.name === "nutritionProductUseSize") { amount.value = event.target.value; return; }
  // A typed amount keeps a chip only while it is that chip's amount.
  for (const chip of document.querySelectorAll("#nutritionProductUseSizes input")) chip.checked = Number(chip.value) === Number(amount.value) && chip.checked;
}

function nutritionProductUnit(product = {}) {
  const unit = String(product.basis_unit || "g").toLowerCase();
  return ["g", "ml", "portion"].includes(unit) ? unit : "g";
}

function nutritionProductUseAmount() {
  const product = nutritionProductForUse;
  const form = document.querySelector("#nutritionProductUseForm");
  if (!product || !form.reportValidity()) return null;
  return { product, unit: nutritionProductUnit(product), amount: document.querySelector("#nutritionProductUseAmount").value };
}

function closeNutritionProductUse() {
  document.querySelector("#nutritionProductUseDialog")?.close();
  nutritionProductForUse = null;
}

function addNutritionProductToCoachDraft() {
  const use = nutritionProductUseAmount();
  const input = document.querySelector("#messageInput");
  if (!use || !input) return;
  const { product, unit, amount } = use;
  const draft = input.value.trimEnd();
  input.value = draft
    ? `${draft}\nDazu ${amount} ${unit} (Produkt-ID: ${product.id}).`
    : `Erfasse ${amount} ${unit} als Mahlzeit (Produkt-ID: ${product.id}).`;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  state.chatDraftDirty = true;
  closeNutritionProductUse();
  void AppRouter.navigate("coach", { historyMode: "push" });
  jumpToLatestMessages();
}

// A meal of today that waits in the outbox keeps the time it was entered, not the later
// time it is sent (review on #3761).
function queuedMealPayload(body) {
  if (body.meal_date !== todayIso()) return body;
  const time = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: state.data?.profile?.timezone || undefined }).format(new Date());
  return { ...body, meal_time: time };
}

async function addNutritionProductToMeal() {
  const use = nutritionProductUseAmount();
  const edit = nutritionOutboxEdit;
  const mealId = edit ? "" : document.querySelector("#nutritionProductUseMeal").value;
  if (!use) return;
  const generation = state.sessionGeneration;
  const addButton = document.querySelector("#nutritionProductUseAdd");
  addButton.disabled = true;
  try {
    const body = mealId
      ? { id: mealId, product_id: use.product.id, amount: Number(use.amount), unit: use.unit }
      : { product_id: use.product.id, amount: Number(use.amount), unit: use.unit, meal_type: "snack", meal_date: edit?.payload.meal_date || document.querySelector("#nutritionDate").value || todayIso(), source: "manual" };
    // An edited meal keeps the day and time it was entered.
    const queued = edit?.payload.meal_time ? { ...body, meal_time: edit.payload.meal_time } : queuedMealPayload(body);
    // A new meal waits in the outbox when the server cannot be reached; adding to a meal does not.
    const result = mealId
      ? await api("/api/nutrition/entry/add-product", { method: "POST", body: JSON.stringify(body) })
      : await saveOwnEntry({ kind: "meal", method: "POST", path: "/api/nutrition/entry", payload: body, queuedPayload: queued, label: `„${use.product.name}“ als Snack`, replaces: edit });
    if (generation !== state.sessionGeneration) return;
    closeNutritionProductUse();
    if (result.outboxEntry) {
      document.querySelector("#nutritionProductStatus").textContent = `„${use.product.name}“ wartet, noch nicht gesendet.`;
      return;
    }
    // The list shows the new use in the product's count (#3435); its reload
    // clears the status line, so the receipt follows it.
    await reloadNutritionProducts();
    if (generation !== state.sessionGeneration) return;
    showNutritionReceipt(document.querySelector("#nutritionProductStatus"), `„${use.product.name}“ ${mealId ? "zur Mahlzeit ergänzt" : "als Snack erfasst"}.`, result.change, () => reloadNutritionProducts());
  } catch (error) {
    if (generation !== state.sessionGeneration) return;
    // The queued entry is gone meanwhile: the next save goes out as a new snack (Gitar on #3830).
    if (error?.reason === "outbox_entry_gone") nutritionOutboxEdit = null;
    document.querySelector("#nutritionProductUseStatus").textContent = `Produkt konnte nicht erfasst werden: ${error.message}`;
    addButton.disabled = false;
  }
}

function isCurrentNutritionProductLoad(sequence, generation, expectedQuery) {
  return sequence === nutritionProductLoadSequence
    && generation === state.sessionGeneration
    && (expectedQuery === null || document.querySelector("#nutritionProductQuery").value.trim() === expectedQuery);
}

function prepareNutritionProductLookup(preserveLookup) {
  if (preserveLookup) return;
  nutritionLookupResultShown = false;
  const lookup = document.querySelector("#nutritionLookupResult");
  if (lookup) { lookup.hidden = true; lookup.replaceChildren(); }
}

function showNutritionProductLoadError(preserveLookup, sequence, generation, expectedQuery, error) {
  if (!preserveLookup && isCurrentNutritionProductLoad(sequence, generation, expectedQuery)) {
    document.querySelector("#nutritionProductStatus").textContent = `Produktsuche fehlgeschlagen: ${error.message}`;
  }
}

async function loadNutritionProducts(query = "", { background = false, throwOnError = false, refresh = false, expectedQuery = null } = {}) {
  if (background && nutritionLookupResultShown && !refresh) return;
  const preserveLookup = refresh && nutritionLookupResultShown;
  prepareNutritionProductLookup(preserveLookup);
  const sequence = ++nutritionProductLoadSequence;
  const generation = state.sessionGeneration;
  const status = document.querySelector("#nutritionProductStatus");
  try {
    if (!preserveLookup) status.textContent = "Produkte werden gesucht …";
    const search = query ? "&q=" + encodeURIComponent(query) : "";
    const result = await api(`/api/nutrition/products?scope=all${search}`);
    if (!isCurrentNutritionProductLoad(sequence, generation, expectedQuery)) return;
    renderNutritionProducts(result.products || [], { search: query });
    if (!preserveLookup) status.textContent = result.products?.length ? "" : "Keine Produkte gefunden.";
  } catch (error) {
    showNutritionProductLoadError(preserveLookup, sequence, generation, expectedQuery, error);
    if (throwOnError) throw error;
  }
}

async function submitNutritionProductForm(form) {
  const generation = state.sessionGeneration;
  const editor = document.querySelector("#nutritionExtraction");
  const status = document.querySelector("#nutritionProductStatus");
  try {
    const payload = nutritionProductPayload(form);
    const saved = await api("/api/nutrition/products", { method: payload.id ? "PUT" : "POST", body: JSON.stringify({ ...payload, confirmed: true }) });
    if (generation !== state.sessionGeneration) return;
    // The list is read again with the search the athlete is in.
    editor.hidden = true; editor.replaceChildren(); await reloadNutritionProducts();
    const name = saved.product?.name || "Produkt";
    const message = form.dataset.database === "true" ? `Nährwerte von „${name}“ gespeichert.` : `Produkt „${name}“ lokal gespeichert.`;
    if (generation !== state.sessionGeneration) return;
    showNutritionReceipt(status, message, saved.change, () => reloadNutritionProducts());
    // A new product is selected for a meal at once, no search needed (#3435).
    if (!payload.id && saved.product?.id && saved.product.basis_unit) void useNutritionProduct(normalizeNutritionProduct(saved.product));
  } catch (error) { if (generation === state.sessionGeneration) status.textContent = `Produkt konnte nicht gespeichert werden: ${error.message}`; }
}

async function saveNutritionProduct(product) {
  if (product instanceof HTMLFormElement) return submitNutritionProductForm(product);
  const editor = document.querySelector("#nutritionExtraction");
  const form = nutritionProductForm(normalizeNutritionProduct(product));
  editor.replaceChildren(); editor.append(form); editor.hidden = false;
  form.scrollIntoView({ block: "nearest" });
  form.addEventListener("submit", (event) => { event.preventDefault(); void saveNutritionProduct(form); });
  form.querySelector("[data-product-cancel]").addEventListener("click", () => { editor.hidden = true; editor.replaceChildren(); });
}

async function extractNutritionProduct(file) {
  const generation = state.sessionGeneration;
  const status = document.querySelector("#nutritionProductStatus");
  try {
    status.textContent = "Nährwerttabelle wird gelesen …";
    const prepared = await prepareNutritionImage(file);
    if (generation !== state.sessionGeneration) return;
    const result = await api("/api/nutrition/products/extract", { method: "POST", body: JSON.stringify({ image_data_url: prepared.dataUrl }) });
    if (generation !== state.sessionGeneration) return;
    const extraction = {};
    const candidate = result.candidate || result.extraction;
    if (candidate && typeof candidate === "object") Object.assign(extraction, candidate);
    extraction.provenance = result.provenance || "packaging_label";
    extraction.source = "packaging_label";
    await saveNutritionProduct(extraction);
    status.textContent = "Bitte erkannte Werte prüfen und ausdrücklich speichern.";
  } catch (error) { if (generation === state.sessionGeneration) status.textContent = `Foto konnte nicht verarbeitet werden: ${error.message}`; }
}

async function lookupNutritionProduct(barcode) {
  const generation = state.sessionGeneration;
  const clean = String(barcode || "").replace(/\D/g, "");
  if (!/^\d{8,14}$/.test(clean)) { document.querySelector("#nutritionBarcodeStatus").textContent = "Bitte eine gültige EAN mit 8 bis 14 Ziffern eingeben."; return; }
  try {
    document.querySelector("#nutritionBarcodeStatus").textContent = "Produkt wird gesucht …";
    const result = await api("/api/nutrition/products/lookup", { method: "POST", body: JSON.stringify({ barcode: clean }) });
    if (generation !== state.sessionGeneration) return;
    document.querySelector("#nutritionBarcodeDialog")?.close();
    ++nutritionProductLoadSequence;
    nutritionLookupResultShown = true;
    const product = result.product || result.foods?.[0] || result.products?.[0] || null;
    const isLocal = result.local !== false && result.source !== "open_food_facts";
    const editor = document.querySelector("#nutritionExtraction");
    if (editor) { editor.replaceChildren(); editor.hidden = true; }
    const lookup = document.querySelector("#nutritionLookupResult");
    if (lookup) lookup.hidden = false;
    renderNutritionProducts(product ? [{ ...product, local: isLocal }] : [], { local: isLocal, targetId: "#nutritionLookupResult" });
    document.querySelector("#nutritionProductStatus").textContent = isLocal ? "Lokales Produkt gefunden." : "Online-Treffer gefunden. Prüfe ihn und speichere ihn lokal.";
    if (product && !isLocal) await saveNutritionProduct(normalizeNutritionProduct({ ...product, barcode: product.barcode || clean, source: result.source || product.source || "open_food_facts" }));
  } catch (error) { if (generation === state.sessionGeneration) document.querySelector("#nutritionBarcodeStatus").textContent = `Produktsuche fehlgeschlagen: ${error.message}`; }
}

function stopNutritionBarcode() {
  nutritionBarcodeSession += 1;
  if (nutritionBarcodeFrame) cancelAnimationFrame(nutritionBarcodeFrame);
  nutritionBarcodeFrame = null;
  nutritionBarcodeStream?.getTracks().forEach((track) => track.stop());
  nutritionBarcodeStream = null;
  const video = document.querySelector("#nutritionBarcodeVideo"); if (video) video.srcObject = null;
}

async function scanNutritionBarcode() {
  const dialog = document.querySelector("#nutritionBarcodeDialog");
  const video = document.querySelector("#nutritionBarcodeVideo");
  const status = document.querySelector("#nutritionBarcodeStatus");
  dialog.showModal();
  const session = ++nutritionBarcodeSession;
  const generation = state.sessionGeneration;
  if (!("BarcodeDetector" in globalThis) || !navigator.mediaDevices?.getUserMedia) { status.textContent = "Automatisches Scannen wird hier nicht unterstützt. Gib die EAN manuell ein."; return; }
  try {
    nutritionBarcodeDetector = new BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e"] });
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
    if (session !== nutritionBarcodeSession || !dialog.open || generation !== state.sessionGeneration) { stream.getTracks().forEach((track) => track.stop()); return; }
    nutritionBarcodeStream = stream;
    video.srcObject = nutritionBarcodeStream; await video.play(); status.textContent = "Barcode vor die Kamera halten …";
    const scan = async () => {
      if (!dialog.open || session !== nutritionBarcodeSession || generation !== state.sessionGeneration) return;
      try { const codes = await nutritionBarcodeDetector.detect(video); if (codes[0]?.rawValue) { document.querySelector("#nutritionBarcodeManual").value = codes[0].rawValue; await lookupNutritionProduct(codes[0].rawValue); return; } } catch (_) { /* continue until cancelled */ }
      if (session !== nutritionBarcodeSession || generation !== state.sessionGeneration) return;
      nutritionBarcodeFrame = requestAnimationFrame(scan);
    };
    nutritionBarcodeFrame = requestAnimationFrame(scan);
  } catch (error) { if (session === nutritionBarcodeSession && generation === state.sessionGeneration) { status.textContent = `Kamera nicht verfügbar. Gib die EAN manuell ein. (${error.message})`; stopNutritionBarcode(); } }
}

document.querySelector("#nutritionProductSearch")?.addEventListener("submit", (event) => { event.preventDefault(); void loadNutritionProducts(document.querySelector("#nutritionProductQuery").value.trim()); });
document.querySelector("#nutritionLabelButton")?.addEventListener("click", () => document.querySelector("#nutritionLabelInput").click());
document.querySelector("#nutritionRefreshAllButton")?.addEventListener("click", () => void pollNutritionRefreshAll(true, true));
document.querySelector("#nutritionCleanupButton")?.addEventListener("click", () => void openNutritionCleanup());
document.querySelector("#nutritionCleanupDays")?.addEventListener("change", () => void loadNutritionCleanup());
document.querySelector("#nutritionCleanupCancel")?.addEventListener("click", () => document.querySelector("#nutritionCleanupDialog").close());
document.querySelector("#nutritionCleanupForm")?.addEventListener("submit", (event) => { event.preventDefault(); void runNutritionCleanup(); });
document.querySelector("#nutritionProductManual")?.addEventListener("click", () => saveNutritionProduct({ source: "manual", basis_amount: 100, basis_unit: "g" }));
document.querySelector("#nutritionLabelInput")?.addEventListener("change", (event) => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void extractNutritionProduct(file); });
document.querySelector("#nutritionBarcodeButton")?.addEventListener("click", () => void scanNutritionBarcode());
document.querySelector("#nutritionBarcodeCancel")?.addEventListener("click", () => document.querySelector("#nutritionBarcodeDialog")?.close());
document.querySelector("#nutritionBarcodeDialog")?.addEventListener("close", stopNutritionBarcode);
document.querySelector("#nutritionBarcodeForm")?.addEventListener("submit", (event) => { event.preventDefault(); void lookupNutritionProduct(document.querySelector("#nutritionBarcodeManual").value); });
document.querySelector("#nutritionProductUseCancel")?.addEventListener("click", () => document.querySelector("#nutritionProductUseDialog")?.close());
document.querySelector("#nutritionProductUseDialog")?.addEventListener("close", () => { nutritionProductForUse = null; nutritionOutboxEdit = null; ++nutritionProductUseSequence; });
document.querySelector("#nutritionProductUseForm")?.addEventListener("submit", (event) => { event.preventDefault(); void addNutritionProductToMeal(); });
document.querySelector("#nutritionProductUseForm")?.addEventListener("input", selectNutritionProductUseSize);
document.querySelector("#nutritionProductUseCoach")?.addEventListener("click", addNutritionProductToCoachDraft);
function handleNutritionRoute(route) {
  if (route === "nutrition/products") return;
  nutritionProductSectionsOpen.favorites = nutritionProductSectionsOpen.all = false;
  // The kept sections close too, so the next render does not read them back as open (#3735).
  for (const node of document.querySelector("#nutritionProducts")?.children || []) if (node.dataset?.section) node.open = false;
  nutritionProductSearchShown = "";
  nutritionProductSectionsBeforeSearch = null;
  const query = document.querySelector("#nutritionProductQuery"); if (query) query.value = "";
  stopNutritionBarcode(); document.querySelector("#nutritionBarcodeDialog")?.close();
}
globalThis.AppNutrition = Object.freeze({ handleRoute: handleNutritionRoute });
globalThis.addEventListener("pagehide", stopNutritionBarcode);
document.addEventListener("visibilitychange", () => { if (document.hidden) stopNutritionBarcode(); });
