// The local change history (#3954): newest first, 30 per page. An entry that is still its record's
// newest change offers "Rückgängig"; any older one reads "überholt".
let changeHistoryCursor = null;
let changeHistoryReads = 0;

function changeHistoryEntry(change) {
  const meta = [formatTime(change.created_at), String(change.source).startsWith("coach") ? "Coach" : ""].filter(Boolean).join(" · ");
  const item = h("li", { className: "change-history-item", dataset: { changeId: change.id } },
    h("div", null, h("strong", null, change.line || "Änderung"), h("small", null, meta)));
  if (change.can_undo) {
    const undo = h("button", { type: "button", className: "secondary-button" }, "Rückgängig");
    undo.addEventListener("click", () => void undoChangeHistoryEntry(change, undo));
    item.append(undo);
  } else if (change.action !== "undo" && change.undoable !== false && change.remote_sync !== "remote") item.append(h("span", "change-history-state", "überholt"));
  return item;
}

async function loadChangeHistory(more = false) {
  const list = $("#changeHistoryList");
  const status = $("#changeHistoryStatus");
  const button = $("#changeHistoryMore");
  if (!list || !status || !button) return;
  const read = ++changeHistoryReads;
  button.disabled = true;
  if (!more) { list.replaceChildren(); changeHistoryCursor = null; status.textContent = "Wird geladen…"; }
  try {
    const query = more && changeHistoryCursor ? `?cursor=${encodeURIComponent(changeHistoryCursor)}` : "";
    const result = await api(`/api/change-history${query}`);
    // A newer read replaces this one's page.
    if (read !== changeHistoryReads) return;
    list.append(...result.changes.map(changeHistoryEntry));
    changeHistoryCursor = result.next_cursor;
    status.textContent = list.childElementCount ? "" : "Noch keine Änderungen.";
  } catch (error) {
    if (read === changeHistoryReads) status.textContent = error.message;
  } finally {
    if (read === changeHistoryReads) { button.hidden = !changeHistoryCursor; button.disabled = false; }
  }
}

async function undoChangeHistoryEntry(change, button) {
  const confirmed = await requestConfirmation(`Die Änderung „${change.line}“ wird zurückgenommen. Anbieter bleiben unverändert.`, {
    title: "Änderung zurücknehmen?", confirmLabel: "Zurücknehmen",
  });
  if (!confirmed) return;
  button.disabled = true;
  try {
    await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: change.id, expected_current_hash: change.after_hash }) });
    toast("Änderung zurückgenommen");
    await loadChangeHistory();
    await globalThis.load?.();
  } catch (error) {
    toast(error.message, true);
    // The entry may no longer be its record's newest: read the list again.
    await loadChangeHistory();
  }
}

function formatLogEntry(entry) {
  const timestamp = entry.timestamp ? `[${formatTime(entry.timestamp)}] ` : "";
  const level = entry.level ? `${entry.level} ` : "";
  const event = entry.event ? `${entry.event}: ` : "";
  const context = entry.context ? ` ${JSON.stringify(entry.context)}` : "";
  return `${timestamp}${level}${event}${entry.message || ""}${context}`;
}

async function loadLogs() {
  const output = $("#logsOutput");
  const button = $("#logsRefreshButton");
  if (!output || !button) return;
  // The box scrolls, so the keyboard must reach it.
  makeScrollRegionFocusable(output, "Server-Logs");
  button.disabled = true;
  output.textContent = "Logs werden geladen…";
  try {
    const result = await api("/api/logs?limit=250");
    output.textContent = result.entries?.length ? result.entries.map(formatLogEntry).join("\n") : "Noch keine Log-Einträge vorhanden.";
    output.scrollTop = output.scrollHeight;
  } catch (error) { output.textContent = error.message; }
  finally { button.disabled = false; }
}

async function downloadServerLogs() {
  const button = $("#logsDownloadButton");
  button.disabled = true;
  try {
    const url = URL.createObjectURL(await downloadRequest("/api/logs/download", "Server-Logs konnten nicht heruntergeladen werden.", 130_000));
    const link = document.createElement("a");
    link.href = url;
    link.download = `pacecraft-server-logs-${todayIso()}.jsonl`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Server-Logs heruntergeladen");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function deleteServerLogs() {
  const confirmed = await requestConfirmation("Alle gespeicherten Server-Logs werden gelöscht.", {
    title: "Server-Logs löschen?",
    danger: true, confirmLabel: "Löschen",
  });
  if (!confirmed) return;
  const button = $("#logsDeleteButton");
  if (button) button.disabled = true;
  try {
    await api("/api/logs/delete", { method: "POST" });
    const output = $("#logsOutput");
    if (output) output.textContent = "Noch keine Log-Einträge vorhanden.";
    toast("Server-Logs gelöscht");
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}

async function downloadDiagnostics() {
  const button = $("#diagnosticsButton");
  button.disabled = true;
  button.textContent = "Wird vorbereitet…";
  try {
    renderConnectivityStatus(true);
    const blob = await downloadRequest("/api/diagnostics", "Diagnose konnte nicht heruntergeladen werden.", 130_000);
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `pacecraft-diagnostics-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Diagnose heruntergeladen");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Diagnose herunterladen"; }
}


async function deleteDiagnostics() {
  const confirmed = await requestConfirmation("Alle gespeicherten Diagnosedaten werden gelöscht.", {
    title: "Diagnose löschen?",
    danger: true, confirmLabel: "Löschen",
  });
  if (!confirmed) return;
  const button = $("#diagnosticsDeleteButton");
  if (button) button.disabled = true;
  try {
    await api("/api/diagnostics/delete", { method: "POST" });
    if (state.data?.diagnostic_capture) {
      state.data.diagnostic_capture.entries = 0;
    }
    renderDiagnosticCapture(state.data?.diagnostic_capture || { entries: 0 });
    toast("Diagnose gelöscht");
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}


function renderDiagnosticCapture(capture = {}) {
  const status = $("#diagnosticCaptureStatus");
  if (status) status.textContent = `${Number(capture.entries || 0)} technische Einträge gespeichert.`;
}
