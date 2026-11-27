/* The feedback sheet of one session: "Wie war die Einheit?" with the effort row, the note and "Mehr angeben" (#4199).
   It is its own compact dialog, not part of the activity details. A tap on the effort row saves at once with
   "Rückgängig"; the note saves when its field is left. Entries that cannot reach the server wait in the outbox. */
(() => {
  let dialog;
  let generation = 0;

  // The athlete's own marking wins over Garmin's event type and a competition hint; "" removes it.
  const PURPOSES = [["training", "Training"], ["race", "Wettkampf"], ["commute", "Pendeln"], ["warmup", "Einfahren"], ["cooldown", "Ausfahren"]];
  const RPE_STEPS = Array.from({ length: 10 }, (_, index) => index + 1);
  // Garmin's feel scale, as stored: 1 very strong .. 5 very weak.
  const FEEL_STEPS = [[1, "sehr stark"], [2, "stark"], [3, "normal"], [4, "schwach"], [5, "sehr schwach"]];
  const SESSION_LOAD_REASONS = { no_rating: "kein RPE", conflicting_provider_ratings: "widersprüchliche Provider-RPE", no_elapsed_duration: "keine Gesamtdauer", invalid_duration: "ungültige Dauer" };

  function purposeSelect(marked, detected) {
    const detectedLabel = PURPOSES.find(([key]) => key === detected)?.[1];
    const automatic = h("option", { value: "" }, !marked && detectedLabel ? `Automatisch · ${detectedLabel}` : "Automatisch");
    const select = h("select", { name: "purpose" }, automatic, PURPOSES.map(([key, label]) => h("option", { value: key }, label)));
    select.value = PURPOSES.some(([key]) => key === marked) ? marked : "";
    return select;
  }

  function sessionLoadText(load) {
    if (!load || load.reason === "no_rating") return "";
    if (load.value == null) return `RPE-Belastung: offen · ${SESSION_LOAD_REASONS[load.reason] || "unbekannt"}`;
    const value = AppFormat.number(load.value, { digits: 1 });
    const rpe = Number(load.rpe).toLocaleString("de-DE");
    const minutes = AppFormat.number(load.duration_min, { digits: 1 });
    const text = `RPE-Belastung: ${value} RPE-Punkte · RPE ${rpe} (${load.rpe_source_label}) × ${minutes} min`;
    if (load.duration_flag !== "deviating_duration") return text;
    const moving = AppFormat.number(load.moving_duration_min, { digits: 1 });
    return `${text} · lange Unterbrechung, ${moving} min in Bewegung`;
  }

  // The watch's effort, as the step it falls on; none outside 1 to 10.
  function watchStep(value) {
    const step = Math.round(Number(value));
    return value != null && Number.isFinite(step) && step >= 1 && step <= 10 ? step : null;
  }

  // A row of taps. The own value is pressed, the watch's value is dashed until the athlete rates; tapping the
  // pressed step again takes the rating back.
  function tapRow(className, label, steps, onPick) {
    const row = h("div", { className, role: "group", "aria-label": label });
    const buttons = steps.map(([step, text]) => {
      const button = h("button", { type: "button", className: "feedback-step", dataset: { step: String(step) }, "aria-pressed": "false" }, text);
      button.addEventListener("click", () => onPick(step));
      return button;
    });
    row.append(...buttons);
    const paint = (own, watch) => {
      for (const button of buttons) {
        const step = Number(button.dataset.step);
        button.setAttribute("aria-pressed", String(own === step));
        button.classList.toggle("is-watch", own == null && watch === step);
        button.title = own == null && watch === step ? "Wert der Uhr, tippen übernimmt ihn" : "";
      }
    };
    return { row, paint };
  }

  function effortRow(onPick) {
    const { row, paint } = tapRow("feedback-effort", "Anstrengung (RPE)", RPE_STEPS.map((step) => [step, String(step)]), onPick);
    const note = h("p", "muted feedback-effort-note", "");
    const render = (own, watch) => {
      paint(own, watch);
      const offGrid = own != null && !RPE_STEPS.includes(own) ? `Gespeichert: ${Number(own).toLocaleString("de-DE")}` : "";
      note.textContent = [offGrid, watch != null && own == null ? `Uhr: ${watch}` : ""].filter(Boolean).join(" · ");
      note.hidden = !note.textContent;
    };
    return { row, note, render };
  }

  // The athlete's own feel; the watch's feel is dashed until the athlete rates.
  function feelRow(onPick) {
    const { row, paint } = tapRow("feedback-feel", "Gefühl", FEEL_STEPS, onPick);
    return { row, render: paint };
  }

  // The watch's feel, as the step it falls on; none outside 1 to 5.
  function watchFeel(value) {
    const step = Math.round(Number(value));
    return value != null && Number.isFinite(step) && step >= 1 && step <= 5 ? step : null;
  }

  // Undo restores the feedback rows as they were before this save or removal.
  function undoButton(api, change, activityId, status, begin, restored) {
    const undo = h("button", { className: "message-undo", type: "button" }, "Rückgängig");
    undo.addEventListener("click", async () => {
      setSaving(undo, true); const token = begin();
      try {
        const response = await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: change.id, expected_current_hash: change.after_hash, activity_id: activityId }) });
        setSaving(undo, false); undo.disabled = true;
        restored(response, token); status.textContent = "Zurückgenommen.";
      } catch (error) { setSaving(undo, false); status.textContent = error.message; }
    });
    return undo;
  }

  // `edit` is a rejected queued feedback from the outbox (#3817): the sheet starts with its fields and the next
  // save replaces the queued entry once.
  function renderSheet(payload, api, activityId, content, edit) {
    const session = payload.session || {};
    const feedback = payload.activity_feedback;
    const watch = watchStep(ActivityDetails.metricValues(payload).rpe);
    const watchFeelStep = watchFeel(ActivityDetails.metricValues(payload).feel);
    const current = { session_rpe: feedback?.session_rpe ?? null, feel: feedback?.feel ?? null, notes: feedback?.notes || "", deviation_reason: feedback?.deviation_reason || "", purpose: feedback?.purpose || "" };
    const effort = effortRow((step) => { current.session_rpe = current.session_rpe === step ? null : step; paint(); void queueSave(); });
    const feel = feelRow((step) => { current.feel = current.feel === step ? null : step; paint(); void queueSave(); });
    const notes = h("textarea", { name: "notes", rows: 3, maxLength: 4000 });
    const purpose = purposeSelect(current.purpose, session.purpose);
    const reason = h("input", { name: "deviation_reason", maxLength: 500 });
    const status = h("p", "muted", ""); status.setAttribute("role", "status");
    const load = h("p", { dataset: { sessionRpeLoad: "" } });
    const showLoad = (value) => { load.textContent = sessionLoadText(value); load.hidden = !load.textContent; };
    showLoad(payload.session_rpe_load);
    const more = h("details", "feedback-more", h("summary", null, "Mehr angeben"),
      h("label", null, "Art der Einheit", purpose), h("label", null, "Abweichung vom Plan", reason));
    const paint = () => {
      effort.render(current.session_rpe, watch);
      feel.render(current.feel, watchFeelStep);
      notes.value = current.notes; reason.value = current.deviation_reason;
      purpose.value = PURPOSES.some(([key]) => key === current.purpose) ? current.purpose : "";
    };
    let replaces = edit;
    if (edit) Object.assign(current, { session_rpe: edit.payload.session_rpe ?? null, feel: edit.payload.feel ?? null, notes: edit.payload.notes || "", deviation_reason: edit.payload.deviation_reason || "", purpose: edit.payload.purpose || "" });
    paint();
    // Only the newest request may write the load, so a slow save response cannot overwrite a later undo.
    let loadGeneration = 0;
    const applyLoad = async (token, response) => {
      if (response && "session_rpe_load" in response) {
        if (token === loadGeneration) showLoad(response.session_rpe_load);
        return;
      }
      try {
        const fresh = await api(`/api/activities/${encodeURIComponent(activityId)}`);
        if (token === loadGeneration) showLoad(fresh.session_rpe_load);
      } catch { /* the saved feedback stays shown */ }
    };
    let shown = { ...current };
    let dirty = false;
    const save = async () => {
      dirty = false;
      status.textContent = "Speichere …";
      const value = { activity_name: session.name || "", activity_date: session.started_at_local || "", notes: current.notes, deviation_reason: current.deviation_reason, session_rpe: current.session_rpe, feel: current.feel, purpose: current.purpose || null };
      try {
        const token = ++loadGeneration;
        const path = `/api/activities/${encodeURIComponent(activityId)}/feedback`;
        const result = await saveOwnEntry({ kind: "feedback", method: "POST", path, payload: value, label: `Feedback ${session.name || "zur Einheit"}`, send: api, replaces });
        if (result.outboxEntry) { replaces = null; shown = { ...current }; status.textContent = "Feedback wartet, noch nicht gesendet."; return; }
        const previous = shown;
        shown = { ...current };
        // A strength session's RPE sets its energy (#3530): the next energy table reads it anew.
        resetEnergy();
        status.textContent = result.activity_feedback ? "Feedback gespeichert. " : "Feedback entfernt. ";
        if (result.change?.id) status.append(undoButton(api, result.change, activityId, status, () => ++loadGeneration, (response, undoToken) => { Object.assign(current, previous); shown = { ...previous }; paint(); resetEnergy(); void applyLoad(undoToken, response); }));
        void applyLoad(token, result);
      } catch (error) {
        // A queued entry gone meanwhile: the next save goes out as a new one (review on #3817).
        if (error?.reason === "outbox_entry_gone") replaces = null;
        // The typed text stays for the next try; the taps go back to the last saved values unless a newer change waits.
        if (!dirty) {
          Object.assign(current, { session_rpe: shown.session_rpe, feel: shown.feel, purpose: shown.purpose });
          effort.render(current.session_rpe, watch);
          feel.render(current.feel, watchFeelStep);
          purpose.value = PURPOSES.some(([key]) => key === current.purpose) ? current.purpose : "";
        }
        status.textContent = error.message;
      }
    };
    // Taps and field changes queue behind each other; each save posts the latest values.
    let chain = Promise.resolve();
    const queueSave = () => { dirty = true; chain = chain.then(() => (dirty ? save() : undefined)); return chain; };
    notes.addEventListener("change", () => { current.notes = notes.value; void queueSave(); });
    reason.addEventListener("change", () => { current.deviation_reason = reason.value; void queueSave(); });
    purpose.addEventListener("change", () => { current.purpose = purpose.value; void queueSave(); });
    const explicit = edit ? h("button", { type: "button", className: "push-button" }, "Speichern") : null;
    explicit?.addEventListener("click", async () => {
      setSaving(explicit, true);
      try { await queueSave(); } finally { setSaving(explicit, false); }
    });
    // Text typed when the sheet closes is saved with it: Escape and Back remove the field without leaving it.
    content.flushFeedback = () => {
      const changed = notes.value !== current.notes || reason.value !== current.deviation_reason;
      Object.assign(current, { notes: notes.value, deviation_reason: reason.value });
      if (changed) void queueSave();
    };
    content.replaceChildren(
      h("p", "muted", [session.name, session.started_at ? formatTime(session.started_at) : null].filter(Boolean).join(" · ")),
      h("div", "feedback-field", h("span", "feedback-label", "Anstrengung (RPE)"), effort.row, effort.note),
      h("div", "feedback-field", h("span", "feedback-label", "Gefühl"), feel.row),
      h("label", "feedback-field", h("span", "feedback-label", "Notiz"), notes),
      more, load, ...(explicit ? [explicit] : []), status);
  }

  // `edit` is the rejected queued entry of the outbox; `backLabel` names where closing returns to.
  async function open(activity, { api, showDialog, edit = null, backLabel = "Schließen" }) {
    close(false);
    const activityId = String(activity.id ?? activity.activity_id ?? "");
    if (!activityId) return;
    const token = ++generation;
    const navigationState = { ...history.state };
    delete navigationState.activityFeedback;
    history.replaceState(navigationState, "", location.href);
    history.pushState({ ...navigationState, activityFeedback: activityId }, "", location.href);
    dialog = h("dialog", { className: "activity-detail-dialog activity-feedback-sheet", "aria-labelledby": "activityFeedbackTitle" });
    const title = h("h2", { id: "activityFeedbackTitle" }, "Wie war die Einheit?");
    const back = h("button", { className: "secondary-button", type: "button" }, backLabel);
    back.addEventListener("click", close);
    const content = h("div", "activity-feedback");
    const status = h("p", "muted", "Wird geladen …");
    status.setAttribute("role", "status");
    dialog.append(back, title, content, status);
    document.body.append(dialog);
    const currentDialog = dialog;
    currentDialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
    const returnFocus = document.activeElement;
    currentDialog.addEventListener("close", () => {
      if (token === generation) generation += 1;
      currentDialog.remove();
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    }, { once: true });
    showDialog(currentDialog, back);
    try {
      const payload = await api(`/api/activities/${encodeURIComponent(activityId)}`);
      if (token !== generation) return;
      renderSheet(payload, api, activityId, content, edit);
      status.textContent = "";
      dialog.flushFeedback = content.flushFeedback;
    } catch (error) { if (token === generation) status.textContent = error.message; }
  }

  function close(navigateBack = true) {
    dialog?.flushFeedback?.();
    generation += 1;
    if (dialog?.open) dialog.close();
    dialog?.remove();
    dialog = null;
    if (navigateBack && history.state?.activityFeedback) history.back();
  }

  globalThis.addEventListener("popstate", () => { if (dialog) close(false); });

  globalThis.ActivityFeedback = { open, close, sessionLoadText, watchStep, watchFeel };
})();
