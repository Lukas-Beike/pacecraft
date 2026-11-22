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
const checkinFilled = (value) => value !== null && value !== undefined && value !== "";

// The day's row: what is stored, and the way into the form. Future days have none.
function checkinRow(dateKey, todayKey, checkin) {
  if (dateKey > todayKey) return null;
  const stored = checkin && Object.keys(checkin).length ? checkin : null;
  const scores = stored ? CHECKIN_SCORES.filter(([key]) => Number.isFinite(stored[key])).map(([key, label]) => `${label} ${stored[key]}`) : [];
  const status = h("p", { className: "planned-move-status", role: "status" });
  const receipt = checkinReceipts.get(dateKey);
  if (receipt) status.append(receipt.text, ...(receipt.change?.id ? plannedMoveUndo(dateKey, receipt, status, checkinReceipts) : []));
  const open = h("button", { type: "button", className: "secondary-button" }, stored ? "Ändern" : "Check-in");
  open.addEventListener("click", () => void openCheckinForm(dateKey, stored || {}));
  return h("div", { className: "planned-day-context planned-day-checkin", dataset: { checkin: dateKey } },
    h("span", null, stored ? scores.join(" · ") || "Check-in" : "Noch kein Check-in"), open, status.childNodes.length ? status : null);
}

function checkinNumberField(name, label, value, max) {
  return h("label", null, label, h("input", { name, type: "number", min: 0, max, step: 1, inputMode: "numeric", value: checkinFilled(value) ? value : "" }));
}

function checkinSelect(name, label, options, value) {
  return h("select", { name, "aria-label": label }, options.map(([key, text]) => h("option", { value: key, selected: key === value }, text)));
}

function checkinComplaintRow(item = { area: "other", side: "none", severity: "" }) {
  const remove = h("button", { type: "button", className: "secondary-button", "aria-label": "Beschwerde entfernen" }, "×");
  remove.addEventListener("click", () => remove.closest(".checkin-complaint").remove());
  return h("div", "checkin-complaint",
    checkinSelect("area", "Bereich", Object.entries(COMPLAINT_AREA_LABELS), item.area),
    checkinSelect("side", "Seite", CHECKIN_SIDES, item.side),
    h("input", { name: "severity", type: "number", min: 0, max: 10, step: 1, inputMode: "numeric", required: true, "aria-label": "Stärke 0 bis 10", value: item.severity }),
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
  if (JSON.stringify(Object.entries(answers).sort()) !== JSON.stringify(Object.entries(before).sort())) body.tag_answers = answers;
  if (JSON.stringify(checkinSorted(values.complaints)) !== JSON.stringify(checkinSorted(stored.complaints || []))) body.complaints = values.complaints;
  if (clear.length) body.clear_fields = clear;
  return Object.keys(body).length > 1 ? body : null;
}

function checkinFormNodes(stored, dateKey) {
  const complaints = h("div", "checkin-complaints", (stored.complaints || []).map(checkinComplaintRow));
  const add = h("button", { type: "button", className: "secondary-button" }, "+ Beschwerde");
  add.addEventListener("click", () => {
    complaints.append(checkinComplaintRow());
    add.hidden = complaints.childElementCount >= CHECKIN_MAX_COMPLAINTS;
  });
  add.hidden = complaints.childElementCount >= CHECKIN_MAX_COMPLAINTS;
  const dayStatus = h("fieldset", "appearance-control", h("legend", null, "Tagesstatus"), h("div", "segmented-control", CHECKIN_DAY_STATUS.map(([key, label]) =>
    h("label", null, h("input", { type: "radio", name: "day_status", value: key, checked: key === (stored.day_status || "unknown") }), label))));
  return [
    dayStatus,
    ...CHECKIN_SCORES.map(([key, label]) => checkinNumberField(key, `${label} (0–10)`, stored[key], 10)),
    checkinNumberField("available_minutes", "Verfügbare Zeit (min)", stored.available_minutes, 1440),
    checkinChips(stored),
    h("label", null, "Krank", h("input", { name: "illness", type: "text", maxLength: 1000, autocomplete: "off", value: stored.illness || "" })),
    h("fieldset", "chip-field", h("legend", null, "Beschwerden"), complaints, add),
    h("label", null, "Notiz", h("textarea", { name: "notes", rows: 2, maxLength: 4000, value: stored.notes || "" })),
  ];
}

async function openCheckinForm(dateKey, stored) {
  const title = h("h2", { id: "checkinDialogTitle" }, `Check-in · ${dateLabel(dateKey)}`);
  const status = h("output", { className: "tab-sync-detail", "aria-live": "polite" });
  const cancel = h("button", { type: "button", className: "secondary-button" }, "Abbrechen");
  const save = h("button", { type: "submit" }, "Speichern");
  const form = h("form", { className: "confirmation-dialog checkin-form" }, title, ...checkinFormNodes(stored, dateKey), status, h("div", "dialog-actions", cancel, save));
  const dialog = h("dialog", { id: "checkinDialog", "aria-labelledby": "checkinDialogTitle" }, form);
  const close = () => { dialog.close(); dialog.remove(); };
  cancel.addEventListener("click", close);
  dialog.addEventListener("close", () => restoreDialogFocus(dialog));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = checkinChanges(checkinValues(form), stored, dateKey);
    if (!body) return close();
    save.disabled = true;
    try {
      const result = await api("/api/checkins", { method: "POST", body: JSON.stringify(body) });
      checkinReceipts.set(dateKey, { text: "Check-in gespeichert.", change: result.change });
      close();
      await globalThis.load?.();
    } catch (error) {
      save.disabled = false;
      if (!isSessionEndedError(error)) status.textContent = error.message;
    }
  });
  document.querySelector("#checkinDialog")?.remove();
  document.body.append(dialog);
  showAccessibleDialog(dialog);
}
