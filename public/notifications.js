// Web Push (#3914): the server sends each notification once; the page only subscribes.
// Set when the server refused this browser's push service (only Chrome and Firefox work).
let pushRefused = false;

function pushSupported() {
  return !pushRefused && "Notification" in globalThis && "PushManager" in globalThis && "serviceWorker" in navigator;
}

// The active worker registration, or null when none is registered (ready would never settle).
async function pushRegistration() {
  if (!pushSupported() || !await navigator.serviceWorker.getRegistration()) return null;
  return navigator.serviceWorker.ready;
}

async function currentPushSubscription() {
  const registration = await pushRegistration();
  return registration ? registration.pushManager.getSubscription() : null;
}

// The per-type switches (#3914), in the server's order of the types; all on by default.
const PUSH_TYPE_LABELS = Object.freeze({
  coach_answer: "Coach-Antwort fertig",
  morning_plan: "Tagesplan am Morgen",
  provider_problem: "Probleme mit Anbindungen",
  upcoming: "Wettkampf, Stufentest, Infekt-Hinweis",
  new_activity: "Neue Einheit synchronisiert",
  morning_data: "Morgenwerte verfügbar",
  checkin: "Check-in erinnern",
  maintenance: "Wartung fällig",
  personal_best: "Neue Bestleistung",
  missing_feedback: "Feedback fehlt am Abend",
  server_backup: "Server oder Backup",
  weather: "Wetter für Outdoor-Einheit",
});
// The endpoint whose switches are shown or being read, so a repeated render reads them once.
let pushTypesEndpoint = null;

async function renderPushTypes(subscription) {
  const list = $("#notificationTypes");
  if (!list) return;
  list.hidden = !subscription;
  if (!subscription) { pushTypesEndpoint = null; return; }
  if (pushTypesEndpoint === subscription.endpoint) return;
  pushTypesEndpoint = subscription.endpoint;
  let types;
  try {
    ({ types } = await api(`/api/push?endpoint=${encodeURIComponent(subscription.endpoint)}`));
  } catch (error) {
    pushTypesEndpoint = null;
    throw error;
  }
  // A save in flight keeps its switch; the next render reads the list again.
  if (list.querySelector?.("input[data-push-type]:disabled")) { pushTypesEndpoint = null; return; }
  list.replaceChildren(list.querySelector("legend"), ...Object.entries(PUSH_TYPE_LABELS).map(([type, label]) => h("label", "chip",
    h("input", { type: "checkbox", checked: types?.[type] !== false, dataset: { pushType: type } }),
    h("span", null, label))));
}

async function savePushType(event) {
  const input = event.target.closest?.("input[data-push-type]");
  if (!input) return;
  input.disabled = true;
  try {
    await api("/api/push/preferences", { method: "POST", body: JSON.stringify({ types: { [input.dataset.pushType]: input.checked } }) });
  } catch (error) {
    input.checked = !input.checked;
    toast(error.message || "Die Einstellung konnte nicht gespeichert werden", true);
  } finally {
    input.disabled = false;
  }
}

async function renderNotificationStatus() {
  const node = $("#notificationStatus");
  const button = $("#notificationEnableButton");
  if (!node || !button) return;
  const subscription = pushSupported() && Notification.permission === "granted" ? await currentPushSubscription().catch(() => null) : null;
  const active = Boolean(subscription);
  if (!pushSupported()) node.textContent = "Von diesem Browser nicht unterstützt";
  else if (Notification.permission === "denied") node.textContent = "Im Browser blockiert";
  else node.textContent = active ? "Aktiv" : "Noch nicht aktiviert";
  button.hidden = !pushSupported() || Notification.permission === "denied";
  button.textContent = active ? "Benachrichtigungen ausschalten" : "Benachrichtigungen aktivieren";
  button.dataset.active = active ? "1" : "";
  await renderPushTypes(subscription).catch(() => { $("#notificationTypes").hidden = true; });
}

function pushKeyBytes(text) {
  const base64 = text.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - text.length % 4) % 4);
  return Uint8Array.from(atob(base64), (char) => char.codePointAt(0));
}

async function enableNotifications() {
  if (!pushSupported()) { toast("Dieser Browser unterstützt keine Push-Benachrichtigungen", true); return; }
  const button = $("#notificationEnableButton");
  button.disabled = true;
  try {
    if (button.dataset.active) await disablePush();
    else await enablePush();
  } catch (error) {
    toast(error.message || "Benachrichtigungen konnten nicht geändert werden", true);
  } finally {
    button.disabled = false;
    await renderNotificationStatus();
  }
}

// Store the browser's subscription on the server; a refused push service is removed again.
async function storePushSubscription(subscription) {
  try {
    await api("/api/push/subscribe", { method: "POST", body: JSON.stringify(subscription.toJSON()) });
  } catch (error) {
    if (error.status === 400) {
      pushRefused = true;
      await subscription.unsubscribe().catch(() => {});
    }
    throw error;
  }
}

async function enablePush() {
  const registration = await pushRegistration();
  if (!registration) throw new Error("Dieser Browser unterstützt keine Push-Benachrichtigungen");
  if (await Notification.requestPermission() !== "granted") return;
  const { public_key: key } = await api("/api/push/key", { method: "POST", body: "{}" });
  const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: pushKeyBytes(key) });
  await storePushSubscription(subscription);
  toast("Benachrichtigungen aktiviert");
}

// On each app start the browser's subscription is sent again, so a server that lost it
// (a restore, a purge, a replaced endpoint) receives it before the status says "Aktiv".
// A subscription made with another server key (a restored or new database) is renewed,
// because the push service refuses messages signed with a key it was not made for.
async function syncPushSubscription() {
  // Each start reads the switches again: another device may have changed them.
  pushTypesEndpoint = null;
  if (!pushSupported() || Notification.permission !== "granted") return;
  try {
    const registration = await pushRegistration();
    let subscription = registration && await registration.pushManager.getSubscription();
    if (subscription) {
      const { public_key: key } = await api("/api/push/key", { method: "POST", body: "{}" });
      const current = pushKeyBytes(key);
      const bound = new Uint8Array(subscription.options?.applicationServerKey || new ArrayBuffer(0));
      if (bound.length !== current.length || bound.some((byte, index) => byte !== current[index])) {
        await subscription.unsubscribe();
        subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: current });
      }
      await storePushSubscription(subscription);
    }
  } catch {
    // The status below shows what the browser holds; the next start tries again.
  }
  await renderNotificationStatus();
}

async function disablePush() {
  const subscription = await currentPushSubscription();
  if (subscription) {
    await api("/api/push/unsubscribe", { method: "POST", body: JSON.stringify({ endpoint: subscription.endpoint }) });
    await subscription.unsubscribe();
  }
  toast("Benachrichtigungen ausgeschaltet");
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  // The first install also takes control of the page; only a replaced worker brings new assets.
  const replacesWorker = Boolean(navigator.serviceWorker.controller);
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!replacesWorker) return;
    serviceWorkerReplaced = true;
    reloadForUpdateWhenSafe();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") reloadForUpdateWhenSafe();
  });
  trackUnsavedForms();
  // Leaving a field may end the last edit; focus moves on after focusout. A pointer
  // press on a save button must reach its click first, so the check waits a frame
  // after the press ends.
  document.addEventListener("focusout", () => { if (!pointerHeld) requestAnimationFrame(reloadForUpdateWhenSafe); });
  document.addEventListener("pointerdown", () => { pointerHeld = true; }, true);
  for (const type of ["pointerup", "pointercancel"]) document.addEventListener(type, () => { pointerHeld = false; setTimeout(() => requestAnimationFrame(reloadForUpdateWhenSafe), 0); }, true);
  navigator.serviceWorker.register("/service-worker.js").catch(() => {});
}

// A replaced service worker serves the new assets, so the page reloads once to run
// them, but only while nothing would be lost. Otherwise the reload waits for the
// next check: when a chat or load state ends, or when the page is hidden.
let serviceWorkerReplaced = false;
let serviceWorkerReloaded = false;
let pointerHeld = false;

// Any form the athlete typed into counts as unsaved until it is submitted or reset.
function trackUnsavedForms() {
  document.addEventListener("input", (event) => {
    const scope = event.isTrusted && event.target.closest?.("form:not(#chatForm), #garminSyncDays");
    if (scope) scope.dataset.dirty = "1";
  });
  // A submitted form stays unsaved until the server confirms a change, so a rejected save keeps its input.
  document.addEventListener("submit", (event) => { if (event.target.dataset) event.target.dataset.saving = "1"; });
  document.addEventListener("reset", (event) => { delete event.target.dataset?.dirty; });
  globalThis.addEventListener("pacecraft:saved", () => {
    for (const form of document.querySelectorAll("form[data-saving]")) { delete form.dataset.dirty; delete form.dataset.saving; }
  });
}

// An edited form counts unless it sits in a closed dialog (cancelled or dismissed).
function editedFormPending() {
  return [...document.querySelectorAll("form[data-dirty], #garminSyncDays[data-dirty]")].some((field) => !field.closest?.("dialog:not([open])"));
}

// Settings fields that start empty, so any value in them is still being entered.
const UPDATE_RELOAD_INPUT_FIELDS = "#cred-form-openai input, #cred-form-anthropic input, #cred-form-intervals input, #cred-form-wahoo input, #cred-form-garmin_personal input, #garmin-login-form input, #garmin-mfa-form input, #nutritionProductQuery";

function pageInputPending() {
  const active = document.activeElement;
  return Boolean(active?.matches?.("input, textarea, select") && !active.closest?.("#chatForm"))
    || [...document.querySelectorAll(UPDATE_RELOAD_INPUT_FIELDS)].some((field) => field.value.trim())
    || Boolean($("#notificationTypes input[data-push-type]:disabled"))
    || (typeof providerCredentialBusy !== "undefined" && providerCredentialBusy.size > 0)
    || editedFormPending()
    || Boolean($("#nutritionExtraction") && !$("#nutritionExtraction").hidden && $("#nutritionExtraction").childElementCount);
}

function updateReloadIsSafe() {
  return !hasUnsavedChanges() && !pageInputPending()
    && !(state.chatAttachments || []).length && !state.chatAttachmentsLoading
    && !chatStatusJobActive()
    && !state.loadPromise && !$("#backupRestoreButton")?.disabled && !$("dialog[open]");
}

function reloadForUpdateWhenSafe() {
  if (!serviceWorkerReplaced || serviceWorkerReloaded || !updateReloadIsSafe()) return;
  serviceWorkerReloaded = true;
  location.reload();
}
