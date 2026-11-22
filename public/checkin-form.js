// The daily check-in of one calendar day (#3953): a row opens a form that saves through the use case of
// the Coach's `save_checkin`. Only what the athlete changed is sent, so an omitted field keeps its value
// and the receipt carries an undo like every other change.
const CHECKIN_SCORES = [["soreness", "Muskelkater"], ["stress", "Stress"], ["motivation", "Motivation"]];
const CHECKIN_TAGS = [["travel", "Reise"], ["late_meal", "Spätes Essen"], ["high_stress", "Viel Stress"]];
const CHECKIN_DAY_STATUS = [["unknown", "Offen"], ["rest", "Ruhetag"], ["pause", "Pause"]];
const CHECKIN_SIDES = [["none", "ohne Seite"], ["left", "links"], ["right", "rechts"], ["both", "beidseitig"]];
const CHECKIN_MAX_COMPLAINTS = 8;
const checkinReceipts = new Map();

const checkinComplaintKey = (item) => `${item.area}|${item.side}`;
const checkinSorted = (items) => [...items].sort((a, b) => checkinComplaintKey(a).localeCompare(checkinComplaintKey(b)));
const checkinEntries = (object) => Object.entries(object).sort(([a], [b]) => a.localeCompare(b));
const checkinFilled = (value) => value !== null && value !== undefined && value !== "";

// Today's row: what is stored and the way into the form. A saved earlier day keeps its receipt row, without
// the button; the form reaches earlier days by its date.
function checkinRow(dateKey, todayKey, checkin) {
  const receipt = checkinReceipts.get(dateKey);
  if (dateKey !== todayKey && !receipt) return null;
  const stored = checkin && Object.keys(checkin).length ? checkin : null;
  const scores = stored ? CHECKIN_SCORES.filter(([key]) => Number.isFinite(stored[key])).map(([key, label]) => `${label} ${stored[key]}`) : [];
  const status = h("p", { className: "planned-move-status", role: "status" });
  if (receipt) status.append(receipt.text, ...(receipt.change?.id ? plannedMoveUndo(dateKey, receipt, status, checkinReceipts) : []));
  let open = null;
  if (dateKey === todayKey) {
    open = h("button", { type: "button", className: "secondary-button" }, stored ? "Ändern" : "Check-in");
    open.addEventListener("click", () => void openCheckinForm(dateKey));
  }
  return h("div", { className: "planned-day-context planned-day-checkin", dataset: { checkin: dateKey } },
    h("span", null, stored ? scores.join(" · ") || "Check-in" : "Noch kein Check-in"), open, status.childNodes.length ? status : null);
}

function checkinNumberField(name, label, value, max) {
  return h("label", null, label, h("input", { name, type: "number", min: 0, max, step: 1, inputMode: "numeric", value: checkinFilled(value) ? value : "" }));
}

function checkinSelect(name, label, options, value) {
  return h("select", { name, "aria-label": label }, options.map(([key, text]) => h("option", { value: key, selected: key === value }, text)));
}

// One complaint row; `onRemove` runs after the row is gone.
function checkinComplaintRow({ area = "other", side = "none", severity = "" }, onRemove) {
  const remove = h("button", { type: "button", className: "secondary-button", "aria-label": "Beschwerde entfernen" }, "×");
  remove.addEventListener("click", () => { remove.closest(".checkin-complaint").remove(); onRemove(); });
  return h("div", "checkin-complaint",
    checkinSelect("area", "Bereich", Object.entries(COMPLAINT_AREA_LABELS), area),
    checkinSelect("side", "Seite", CHECKIN_SIDES, side),
    h("input", { name: "severity", type: "number", min: 0, max: 10, step: 1, inputMode: "numeric", required: true, "aria-label": "Stärke 0 bis 10", value: severity }),
    remove);
}

function checkinChips(stored) {
  const answers = stored.tag_answers || {};
  return h("fieldset", "chip-field", h("legend", null, "Umstände"), h("div", "chip-group", CHECKIN_TAGS.map(([key, label]) =>
    h("label", "chip", h("input", { type: "checkbox", name: `tag_${key}`, checked: answers[key] === true }), h("span", null, label)))));
}

// What the form shows now, as plain values.
function checkinValues(form) {
  const text = (name) => form.elements[name].value.trim();
  return {
    texts: Object.fromEntries([...CHECKIN_SCORES.map(([name]) => name), "available_minutes", "illness", "notes"].map((name) => [name, text(name)])),
    day_status: form.elements.day_status.value,
    tags: Object.fromEntries(CHECKIN_TAGS.map(([key]) => [key, form.elements[`tag_${key}`].checked])),
    complaints: [...form.querySelectorAll(".checkin-complaint")].map((row) => ({ area: row.querySelector("[name=area]").value, side: row.querySelector("[name=side]").value, severity: Number(row.querySelector("[name=severity]").value) })),
  };
}

// The fields that differ from the stored day, with the emptied ones in `clear_fields`; null when nothing changed.
function checkinChanges(values, stored, dateKey) {
  const body = { checkin_date: dateKey };
  const clear = [];
  for (const [name, value] of Object.entries(values.texts)) {
    if (value === "") {
      if (checkinFilled(stored[name])) clear.push(name);
    } else if (String(stored[name] ?? "") !== value) body[name] = ["illness", "notes"].includes(name) ? value : Number(value);
  }
  if (values.day_status !== (stored.day_status || "unknown")) body.day_status = values.day_status;
  // A tag is answered once it was checked or already stored; an unchecked stored tag is a "no".
  const before = stored.tag_answers || {};
  const answers = Object.fromEntries(Object.entries(values.tags).filter(([key, on]) => on || key in before));
  if (JSON.stringify(checkinEntries(answers)) !== JSON.stringify(checkinEntries(before))) body.tag_answers = answers;
  if (JSON.stringify(checkinSorted(values.complaints)) !== JSON.stringify(checkinSorted(stored.complaints || []))) body.complaints = values.complaints;
  if (clear.length) body.clear_fields = clear;
  return Object.keys(body).length > 1 ? body : null;
}

function checkinFormNodes(stored) {
  const add = h("button", { type: "button", className: "secondary-button" }, "+ Beschwerde");
  const complaints = h("div", "checkin-complaints");
  // "+ Beschwerde" shows while another complaint fits.
  const refresh = () => { add.hidden = complaints.childElementCount >= CHECKIN_MAX_COMPLAINTS; };
  complaints.append(...(stored.complaints || []).map((item) => checkinComplaintRow(item, refresh)));
  add.addEventListener("click", () => { complaints.append(checkinComplaintRow({}, refresh)); refresh(); });
  refresh();
  const dayStatus = h("fieldset", "appearance-control", h("legend", null, "Tagesstatus"), h("div", "segmented-control", CHECKIN_DAY_STATUS.map(([key, label]) =>
    h("label", null, h("input", { type: "radio", name: "day_status", value: key, checked: key === (stored.day_status || "unknown") }), label))));
  return [
    dayStatus,
    ...CHECKIN_SCORES.map(([key, label]) => checkinNumberField(key, `${label} (0–10)`, stored[key], 10)),
    checkinNumberField("available_minutes", "Verfügbare Zeit (min)", stored.available_minutes, 1440),
    checkinChips(stored),
    h("label", null, "Krank", h("textarea", { name: "illness", rows: 2, maxLength: 1000, value: stored.illness || "" })),
    h("fieldset", "chip-field", h("legend", null, "Beschwerden"), complaints, add),
    h("label", null, "Notiz", h("textarea", { name: "notes", rows: 2, maxLength: 4000, value: stored.notes || "" })),
  ];
}

const checkinDay = async (day) => (await api(`/api/checkins?date=${day}`)).checkin || {};

// The form starts from the whole stored day (tags and status included) of the chosen date: today, or an
// earlier day by its date. Changing the date reads that day.
async function openCheckinForm(dateKey) {
  const today = todayIso();
  let stored;
  try {
    stored = await checkinDay(dateKey);
  } catch (error) {
    if (!isSessionEndedError(error)) toast(error.message, true);
    return;
  }
  const date = h("input", { name: "checkin_date", type: "date", required: true, max: today, value: dateKey });
  const status = h("output", { className: "tab-sync-detail", "aria-live": "polite" });
  const fields = h("div", "checkin-fields", ...checkinFormNodes(stored));
  const cancel = h("button", { type: "button", className: "secondary-button" }, "Abbrechen");
  const save = h("button", { type: "submit" }, "Speichern");
  const form = h("form", { className: "confirmation-dialog checkin-form" }, h("h2", { id: "checkinDialogTitle" }, "Check-in"),
    h("label", null, "Tag", date), status, fields, h("div", "dialog-actions", cancel, save));
  const dialog = h("dialog", { id: "checkinDialog", "aria-labelledby": "checkinDialogTitle" }, form);
  const close = () => dialog.close();
  cancel.addEventListener("click", close);
  dialog.addEventListener("close", () => { restoreDialogFocus(dialog); dialog.remove(); });
  // The date and the day the fields show stay in step: a late or failed read never leaves another day's values under it.
  let shown = dateKey;
  let reads = 0;
  date.addEventListener("change", async () => {
    if (!date.value || date.value > today) return;
    const read = ++reads;
    save.disabled = true;
    try {
      const next = await checkinDay(date.value);
      if (read !== reads) return;
      stored = next;
      shown = date.value;
      fields.replaceChildren(...checkinFormNodes(stored));
      status.textContent = "";
    } catch (error) {
      if (read !== reads) return;
      date.value = shown;
      if (!isSessionEndedError(error)) status.textContent = error.message;
    } finally {
      if (read === reads) save.disabled = false;
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const day = date.value;
    const body = checkinChanges(checkinValues(form), stored, day);
    if (!body) return close();
    save.disabled = true;
    let result;
    try {
      result = await api("/api/checkins", { method: "POST", body: JSON.stringify(body) });
    } catch (error) {
      save.disabled = false;
      if (!isSessionEndedError(error)) status.textContent = error.message;
      return;
    }
    checkinReceipts.set(day, { text: "Check-in gespeichert.", change: result.change });
    if (day !== today) toast(`Check-in für ${dateLabel(day)} gespeichert`);
    close();
    await globalThis.load?.();
  });
  document.querySelector("#checkinDialog")?.remove();
  document.body.append(dialog);
  showAccessibleDialog(dialog);
}

// A tap on the reminder opens "#plan/checkin" (the router shows the calendar): once the app has rendered,
// the form opens for today. The tap counts for ten seconds, so a declined navigation opens nothing later.
const CHECKIN_LINK = "#plan/checkin";
let checkinLinkAt = globalThis.location.hash.toLowerCase() === CHECKIN_LINK ? Date.now() : 0;
globalThis.addEventListener("hashchange", () => { checkinLinkAt = globalThis.location.hash.toLowerCase() === CHECKIN_LINK ? Date.now() : 0; });
document.addEventListener("app:route-rendered", () => {
  if (!checkinLinkAt || !state.data) return;
  const fresh = Date.now() - checkinLinkAt < 10_000;
  checkinLinkAt = 0;
  if (fresh && AppRouter.baseRoute() === "plan") void openCheckinForm(todayIso());
});
