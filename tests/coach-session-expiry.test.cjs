const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions, loadPublicScript } = require("./frontend-harness.cjs");

const CHAT_REQUEST = ["chatRequestIsCurrent", "chatStreamResponse", "rejectChatStreamResponse", "parseChatStreamEvent", "applyStartedChatStreamEvent", "applyDeltaChatStreamEvent", "applyBackgroundChatStreamEvent", "applyCompletedChatStreamEvent", "applyChatStreamEvent", "consumeChatStreamBlock", "CHAT_STREAM_SILENCE_MS", "readChatStreamChunk", "readChatStream", "refreshCompletedChatStream", "finishChatStream", "OFFLINE_NOT_SENT", "unsentOffline", "recoverChatRequestFailure", "finishChatRequest", "requestCoachResponse"];

function attachments(count) {
  return Array.from({ length: count }, (_, index) => ({ name: `route-${index}.gpx`, data: "AA==" }));
}

function unauthorizedResponse() {
  return new Response(JSON.stringify({ error: "Deine Sitzung ist abgelaufen. Bitte erneut anmelden." }), {
    status: 401,
    headers: { "Content-Type": "application/json" },
  });
}

// Sends a chat message whose request is rejected with 401 and returns the observable outcome.
async function sendChatIntoExpiredSession({ inputValue = "", beforeResponse = () => {}, respond = unauthorizedResponse, onLine, data = null } = {}) {
  const draftAttachments = attachments(2);
  const input = { value: inputValue, style: {}, dispatchEvent() {}, focus() {} };
  const state = {
    sessionGeneration: 0,
    chatGeneration: 0,
    chatAttachments: [],
    chatDraftDirty: false,
    chatFollowLatest: false,
    chatRequest: null,
    chatStream: null,
    chatStreamText: "",
    chatServerOperationId: null,
    chatQueue: [],
    rejectedMessages: [],
    data,
  };
  const counts = { showLogin: 0, renderChatAttachments: 0 };
  const toasts = [];
  const drafts = [];
  const context = loadPublicScript("api.js", {
    AbortController,
    Headers,
    Response,
    setTimeout,
    clearTimeout,
    document: { cookie: "ic_csrf=csrf-test" },
    fetch: async () => { beforeResponse(state); return respond(); },
    navigator: { onLine },
    toast(message) { toasts.push(message); },
    rememberUnsentDraft(text) { drafts.push(text); },
    state,
    $: () => input,
    // Mirrors the generation and draft effects of public/auth.js showLogin().
    showLogin() {
      counts.showLogin += 1;
      state.sessionGeneration += 1;
      state.chatGeneration += 1;
      state.chatAttachments = [];
    },
    renderChatAttachments() { counts.renderChatAttachments += 1; },
    renderMessages() {},
    mergeChatMessages: (messages) => messages,
    updateChatControls() {},
    rememberChatTurn() {},
    cancelScheduledChatStreamRender() {},
    invalidateContextPreview() {},
    scheduleChatStatusPoll() {},
    loadChatHistoryFresh: async () => {},
    chatIsNearBottom: () => false,
    AppState: { secureToken: () => "turn-test" },
  });
  extractFunctions("coach.js", CHAT_REQUEST, context);
  const message = "Mein Entwurf";
  const result = await context.requestCoachResponse(message, null, draftAttachments);
  return { result, state, input, counts, draftAttachments, toasts, drafts };
}

test("a chat 401 restores the typed draft and attachments once and shows login once", async () => {
  const { result, state, input, counts, draftAttachments } = await sendChatIntoExpiredSession();
  assert.equal(result, false);
  assert.equal(counts.showLogin, 1);
  assert.equal(input.value, "Mein Entwurf");
  // Copy out of the vm realm so deepStrictEqual compares prototypes as well as values.
  assert.deepEqual([...state.chatAttachments], draftAttachments);
  assert.equal(counts.renderChatAttachments, 1);
  assert.equal(state.chatDraftDirty, true);
  assert.deepEqual(state.rejectedMessages, []);
});

test("a chat 401 keeps newer typed text and parks the rejected message once when the input was edited", async () => {
  const { result, state, input, counts, draftAttachments } = await sendChatIntoExpiredSession({ inputValue: "Neuer Entwurf" });
  assert.equal(result, false);
  assert.equal(counts.showLogin, 1);
  assert.equal(input.value, "Neuer Entwurf");
  assert.deepEqual([...state.chatAttachments], draftAttachments);
  assert.equal(state.rejectedMessages.length, 1);
  assert.equal(state.rejectedMessages[0].content, "Mein Entwurf");
  assert.equal(state.rejectedMessages[0].error, "Bitte erneut anmelden.");
});

test("a chat 401 for a superseded chat still shows login once without restoring the stale draft", async () => {
  const { result, state, input, counts } = await sendChatIntoExpiredSession({
    beforeResponse: (current) => { current.chatGeneration += 1; },
  });
  assert.equal(result, false);
  assert.equal(counts.showLogin, 1);
  assert.equal(input.value, "");
  assert.equal(state.chatDraftDirty, false);
});

test("a chat sent without a connection goes back to the composer at once and is kept as a draft", async () => {
  const { result, state, input, toasts, drafts } = await sendChatIntoExpiredSession({
    onLine: false,
    data: { messages: [] },
    respond: () => { throw new TypeError("Failed to fetch"); },
  });
  assert.equal(result, false);
  assert.equal(input.value, "Mein Entwurf");
  assert.equal(state.chatDraftDirty, true);
  assert.deepEqual(toasts, ["Keine Verbindung – nicht gesendet."]);
  assert.deepEqual(drafts, [null, "Mein Entwurf"]);
  assert.equal(state.chatRequest, null);
});

test("a chat that fails while the browser claims to be online stays with the recovery polls", async () => {
  const { result, state, drafts } = await sendChatIntoExpiredSession({
    onLine: true,
    data: { messages: [] },
    respond: () => { throw new TypeError("Failed to fetch"); },
  });
  assert.equal(result, "recovering");
  assert.equal(state.chatRequest.phase, "recovering");
  assert.deepEqual(drafts, [null]);
});
