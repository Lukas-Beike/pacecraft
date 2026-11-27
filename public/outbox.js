// Offline outbox for the athlete's own entries (#3761): a meal, a weight, a body fat
// or a session feedback that never reached the server waits on the device and is sent
// once the app is reachable again. Every request carries an Idempotency-Key, so the
// server answers a repeat from its stored result and writes nothing twice.
// The outbox holds no credentials; logout and the local data purge clear it.

const OUTBOX_DB_NAME = "pacecraft-outbox";
const OUTBOX_STORE = "entries";
// A server answer that says nothing about the entry itself: it stays waiting.
const OUTBOX_UNDECIDED_STATUSES = new Set([401, 403, 408, 429]);
// Server errors (5xx) for one entry before it is set aside as rejected, so later saves go on.
const OUTBOX_SERVER_ATTEMPTS = 5;

let outboxStorage = null;
let outboxEntries = [];
let outboxLoaded = null;
// The passes run one after another: a trigger during a pass waits for it, then sends what is left.
let outboxPasses = Promise.resolve();
let outboxClock = 0;
// Raised by a clear, so a send that finishes afterwards writes nothing back.
let outboxEpoch = 0;

function outboxIndexedDb() {
  let opened = null;
  const open = () => opened ??= new Promise((resolve, reject) => {
    const request = indexedDB.open(OUTBOX_DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(OUTBOX_STORE, { keyPath: "key" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const run = async (mode, action) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(OUTBOX_STORE, mode);
      const request = action(transaction.objectStore(OUTBOX_STORE));
      transaction.oncomplete = () => resolve(request.result);
      transaction.onerror = transaction.onabort = () => reject(transaction.error);
    });
  };
  return {
    all: () => run("readonly", (store) => store.getAll()),
    put: (entry) => run("readwrite", (store) => store.put(entry)),
    remove: (key) => run("readwrite", (store) => store.delete(key)),
    clear: () => run("readwrite", (store) => store.clear()),
  };
}

function outboxStore() {
  outboxStorage ??= outboxIndexedDb();
  return outboxStorage;
}

// Tests replace the device storage; a missing IndexedDB leaves the outbox empty and off.
function outboxUseStorage(storage) {
  outboxStorage = storage;
  outboxLoaded = null;
  outboxEntries = [];
}

function outboxOrder(a, b) {
  return a.created_at.localeCompare(b.created_at) || a.key.localeCompare(b.key);
}

// Strictly rising, so the order of two saves in the same millisecond is kept.
function outboxStamp() {
  outboxClock = Math.max(Date.now(), outboxClock + 1);
  return new Date(outboxClock).toISOString();
}

function outboxKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function outboxLoad() {
  outboxLoaded ??= outboxStore().all().then(
    (entries) => { outboxEntries = entries.sort(outboxOrder); renderOutbox(); },
    () => { outboxEntries = []; },
  );
  return outboxLoaded;
}

// Keeps one entry on the device and in memory; false when the device cannot store it.
async function outboxPut(entry) {
  try {
    await outboxLoad();
    await outboxStore().put(entry);
  } catch {
    return false;
  }
  outboxEntries = [...outboxEntries.filter((item) => item.key !== entry.key), entry].sort(outboxOrder);
  renderOutbox();
  return true;
}

async function outboxRemove(key) {
  outboxEntries = outboxEntries.filter((item) => item.key !== key);
  renderOutbox();
  try { await outboxStore().remove(key); } catch { }
}

// Saves one entry of the athlete. A request that cannot reach the server is queued and answers
// `{ outboxEntry }`; any server answer, and a device without storage, throws as before.
// With `replaces` (the edit of a rejected entry) nothing is sent directly: the entry is replaced in place.
async function saveOwnEntry({ kind, method, path, payload, label, send = api, queuedPayload = payload, replaces = null }) {
  if (replaces) {
    const entry = await outboxReplace(replaces, { payload: queuedPayload, label });
    if (!entry) throw Object.assign(new Error("Der Eintrag wurde inzwischen verworfen oder gesendet."), { reason: "outbox_entry_gone" });
    void replayOutbox();
    return { outboxEntry: entry };
  }
  const key = outboxKey();
  // While older entries wait, a new one queues behind them, so a stale entry never
  // overwrites a newer save (review on #3761); the pass then sends them in order.
  if (outboxEntries.some((item) => item.state === "waiting")) {
    const entry = { key, kind, method, path, payload: queuedPayload, label, created_at: outboxStamp(), attempts: 0, last_error: "", state: "waiting" };
    if (await outboxPut(entry)) {
      void replayOutbox();
      return { outboxEntry: entry };
    }
  }
  try {
    return await send(path, { method, body: JSON.stringify(payload), headers: { "Idempotency-Key": key } });
  } catch (error) {
    if (error?.reason !== "network_error") throw error;
    const entry = { key, kind, method, path, payload: queuedPayload, label, created_at: outboxStamp(), attempts: 0, last_error: "", state: "waiting" };
    if (!await outboxPut(entry)) throw error;
    return { outboxEntry: entry };
  }
}

// "sent", "rejected" (the server refused the entry, or failed it OUTBOX_SERVER_ATTEMPTS times),
// "skipped" (discarded while in flight) or "stop" (try again at the next trigger).
async function outboxSend(entry) {
  const epoch = outboxEpoch;
  try {
    await api(entry.path, { method: entry.method, body: JSON.stringify(entry.payload), headers: { "Idempotency-Key": entry.key } });
  } catch (error) {
    if (epoch !== outboxEpoch || error?.reason === "network_error") return "stop";
    const serverError = error?.status >= 500;
    const attempts = entry.attempts + 1;
    // A server that keeps failing one entry must not hold back every later save (Gitar on #3816).
    const refused = (error?.status >= 400 && error.status < 500 && !OUTBOX_UNDECIDED_STATUSES.has(error.status))
      || (serverError && attempts >= OUTBOX_SERVER_ATTEMPTS);
    if (!refused && !serverError) return "stop";
    // A discarded entry stays discarded, whatever its in-flight answer was; the pass goes on.
    if (!outboxEntries.some((item) => item.key === entry.key)) return "skipped";
    await outboxPut({ ...entry, attempts, last_error: error.message, state: refused ? "rejected" : entry.state });
    return refused ? "rejected" : "stop";
  }
  if (epoch !== outboxEpoch) return "stop";
  await outboxRemove(entry.key);
  return "sent";
}

// Sends the waiting entries one at a time in creation order. A rejected entry is kept and the
// later ones go on; a request that fails without a decision ends the pass.
function replayOutbox() {
  outboxPasses = outboxPasses.then(outboxPass, outboxPass);
  return outboxPasses;
}

async function outboxPass() {
  if (!state.data || !navigator.onLine) return;
  const sent = [];
  await outboxLoad();
  for (const entry of outboxEntries.filter((item) => item.state === "waiting")) {
    // An entry discarded during the pass is not sent.
    if (!outboxEntries.some((item) => item.key === entry.key)) continue;
    const outcome = await outboxSend(entry);
    if (outcome === "sent") sent.push(entry);
    else if (outcome === "stop") break;
  }
  if (sent.length) outboxSentNotice(sent);
}

function outboxSentNotice(sent) {
  toast(sent.length === 1 ? "1 Eintrag gesendet" : `${sent.length} Einträge gesendet`);
  scheduleSyncPoll(0);
  if (sent.some((entry) => entry.kind === "weight" || entry.kind === "body-fat") && typeof bodyEntryChanged === "function") void bodyEntryChanged();
}

async function outboxDiscard(key) {
  await outboxRemove(key);
}

async function outboxResend(key) {
  const entry = outboxEntries.find((item) => item.key === key);
  if (entry && await outboxPut({ ...entry, state: "waiting" })) await replayOutbox();
}

// What the dialog of a rejected entry needs to edit it: the entry's key, the epoch it was read
// in and its queued payload; null for any other entry.
function outboxEditToken(key) {
  const entry = outboxEntries.find((item) => item.key === key);
  return entry?.state === "rejected" ? { key, epoch: outboxEpoch, kind: entry.kind, path: entry.path, payload: entry.payload } : null;
}

// Replaces a rejected entry by the edited one at the same place in the order (its created_at),
// under a new key, because the server answers a repeated key with its stored result. Null when the
// entry was discarded, sent or cleared meanwhile, so an edit never brings it back.
async function outboxReplace({ key, epoch }, { payload, label }) {
  await outboxLoad();
  const entry = outboxEntries.find((item) => item.key === key);
  if (epoch !== outboxEpoch || entry?.state !== "rejected") return null;
  const next = { ...entry, key: outboxKey(), payload, label, attempts: 0, last_error: "", state: "waiting" };
  if (!await outboxPut(next)) return null;
  if (epoch !== outboxEpoch) { await outboxRemove(next.key); return null; }
  await outboxRemove(key);
  return next;
}

// A rejected queued feedback opens in its feedback sheet, filled with its fields.
function editQueuedFeedback(edit) {
  const id = decodeURIComponent(/^\/api\/activities\/([^/]+)\/feedback$/.exec(edit.path)?.[1] ?? "");
  if (!id) return;
  return globalThis.ActivityFeedback.open({ id, name: edit.payload.activity_name }, { api, showDialog: showAccessibleDialog, edit });
}

function outboxEdit(key) {
  const edit = outboxEditToken(key);
  if (!edit) return;
  document.querySelector("#outboxDialog")?.close();
  const open = { weight: openWeightDialog, "body-fat": openBodyFatDialog, meal: editQueuedMeal, feedback: editQueuedFeedback }[edit.kind];
  void open?.(edit);
}

// Logout and the local data purge: nothing the athlete entered stays on the device.
async function clearOutbox() {
  outboxEpoch += 1;
  outboxEntries = [];
  outboxLoaded = Promise.resolve();
  renderOutbox();
  try { await outboxStore().clear(); } catch { }
}

// What a logout would throw away, for its confirmation.
function outboxLossNote() {
  const count = outboxEntries.length;
  if (!count) return "";
  const noun = count === 1 ? "1 nicht gesendeter Eintrag geht" : `${count} nicht gesendete Einträge gehen`;
  return ` ${noun} verloren.`;
}

function outboxRow(entry) {
  const text = entry.state === "rejected" ? `abgelehnt: ${entry.last_error}` : "wartet";
  const resend = h("button", { type: "button", className: "secondary-button" }, "Erneut senden");
  resend.addEventListener("click", () => void outboxResend(entry.key));
  const discard = h("button", { type: "button", className: "secondary-button" }, "Verwerfen");
  discard.addEventListener("click", () => void outboxDiscard(entry.key));
  const edit = messageActionButton("edit", `Eintrag bearbeiten: ${entry.label}`);
  edit.addEventListener("click", () => outboxEdit(entry.key));
  return h("li", { dataset: { outboxKey: entry.key, state: entry.state } }, h("span", "outbox-text", h("strong", null, entry.label), ` ${text}`), ...(entry.state === "rejected" ? [edit] : []), resend, discard);
}

function renderOutbox() {
  const rejected = outboxEntries.filter((entry) => entry.state === "rejected").length;
  const waiting = outboxEntries.length - rejected;
  const parts = [];
  if (waiting) parts.push(waiting === 1 ? "1 Eintrag wartet" : `${waiting} Einträge warten`);
  if (rejected) parts.push(`${rejected} abgelehnt`);
  updateStatusSources({ outbox: parts.length ? { level: "warning", title: parts.join(" · "), action: { label: "Anzeigen", onClick: openOutboxDialog } } : null });
  const list = document.querySelector("#outboxList");
  if (!list) return;
  list.replaceChildren(...outboxEntries.map(outboxRow));
  if (!outboxEntries.length) document.querySelector("#outboxDialog")?.close();
}

function openOutboxDialog() {
  const dialog = document.querySelector("#outboxDialog");
  if (dialog && !dialog.open) dialog.showModal();
}

function setupOutbox() {
  globalThis.addEventListener("online", () => void replayOutbox());
  document.querySelector("#outboxDialogClose")?.addEventListener("click", () => document.querySelector("#outboxDialog").close());
  void outboxLoad();
}
