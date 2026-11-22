// Web Push (#3914): the server sends each notification once; the page only subscribes.
function pushSupported() {
  return "Notification" in globalThis && "PushManager" in globalThis && "serviceWorker" in navigator;
}

async function currentPushSubscription() {
  if (!pushSupported()) return null;
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
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

async function enablePush() {
  if (await Notification.requestPermission() !== "granted") return;
  const { public_key: key } = await api("/api/push/key", { method: "POST", body: "{}" });
  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: pushKeyBytes(key) });
  try {
    await api("/api/push/subscribe", { method: "POST", body: JSON.stringify(subscription.toJSON()) });
  } catch (error) {
    // A push service the server refuses must not stay subscribed in the browser.
    await subscription.unsubscribe().catch(() => {});
    throw error;
  }
  toast("Benachrichtigungen aktiviert");
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
    || (typeof providerCredentialBusy !== "undefined" && providerCredentialBusy.size > 0)
    || Boolean($("#calendarDisplaySaveButton")?.disabled)
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
