const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions } = require("./frontend-harness.cjs");

const PROFILE_SAVE = ["profileSaveInFlight", "profileFormValues", "profileSaveConfirmed", "profileFieldText", "applyAdoptedFirstThreshold", "adoptFirstThreshold", "determineChangedProfileFields", "saveProfile"];
const COACH_PROPOSALS = ["isHttpClientRejection", "reconcileCoachActionProposal", "executeCoachActionProposal", "showCoachReceiptArea", "lockUnknownCoachActionOutcome", "rejectCoachActionProposal"];
const SYNC_POLL = ["changedSyncAreas", "renderSyncStatus", "renderMaintenanceStatus", "handleSyncStatus", "scheduleSyncPoll", "pollSyncStatus"];

// Objects created inside a vm context have their own prototypes; compare plain copies.
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

class FormDataStub {
  constructor(form) {
    this.entries_ = form.entries_.map(([key, value]) => [key, value]);
  }

  [Symbol.iterator]() {
    return this.entries_[Symbol.iterator]();
  }

  getAll(name) {
    return this.entries_.filter(([key]) => key === name).map(([, value]) => value);
  }
}

function submitEvent(formEntries) {
  const button = { textContent: "Speichern", disabled: false, setAttribute() {}, removeAttribute() {} };
  const form = { entries_: formEntries };
  return { event: { preventDefault() {}, currentTarget: form, submitter: button }, button };
}

function profileContext({ server, baseline, stateProfile, error }) {
  const calls = [];
  const toasts = [];
  const skinfoldResets = [];
  const state = { sessionGeneration: 1, data: { profile: stateProfile }, profileBaseline: baseline, profileDirty: false };
  const context = extractFunctions("settings.js", PROFILE_SAVE, {
    state,
    document: { getElementById: () => null },
    FormData: FormDataStub,
    api: async (path, options = {}) => {
      const method = options.method || "GET";
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ path, method, body });
      if (error && method === "PUT") throw error;
      if (path === "/api/profile" && method === "GET") return { profile: { ...server }, competitions: [] };
      return { ...body };
    },
    toast: (message, isError) => toasts.push({ message, isError: Boolean(isError) }),
    setDirtyIndicator() {},
    invalidateContextPreview() {},
    resetSkinfolds: () => skinfoldResets.push(true),
    load: async () => {},
  });
  return { context, state, calls, toasts, skinfoldResets };
}

test("a saved birth year makes the skinfold card read again; other fields do not", async () => {
  // Gitar #3120: the card kept "ohne Geburtsjahr" until the view was reloaded.
  for (const [entries, expected] of [[[["name", "Anna"], ["birth_year", "1986"]], 1], [[["name", "Anna M."], ["birth_year", ""]], 0]]) {
    const { context, skinfoldResets } = profileContext({
      server: { name: "Anna", birth_year: "" }, baseline: { name: "Anna", birth_year: "" }, stateProfile: { name: "Anna", birth_year: "" },
    });
    await context.saveProfile(submitEvent(entries).event);
    assert.equal(skinfoldResets.length, expected);
  }
});

test("profile save sends only the fields the athlete edited", async () => {
  const { context, calls, toasts } = profileContext({
    server: { name: "Anna", sports: "Cycling", typical_weekly_volume: "10 h", coaching_style: "Unterstützend", weight: "60" },
    baseline: { name: "Anna", sports: "Cycling", typical_weekly_volume: "8 h", coaching_style: "Unterstützend" },
    stateProfile: { name: "Anna", sports: "Cycling", typical_weekly_volume: "8 h", coaching_style: "Unterstützend", weight: "55" },
  });
  const { event } = submitEvent([
    ["name", "Anna M."],
    ["sports", "Cycling"],
    ["typical_weekly_volume", "8 h"],
    ["coaching_style", "Unterstützend"],
  ]);
  await context.saveProfile(event);
  const put = calls.find((call) => call.method === "PUT");
  assert.ok(put, "expected a PUT to /api/profile");
  assert.equal(calls.filter((call) => call.method === "GET").length, 0, "saving must not pre-read the profile");
  assert.deepEqual(Object.keys(plain(put.body)), ["name"]);
  assert.equal(plain(put.body).name, "Anna M.");
  assert.equal(toasts.at(-1).isError, false);
});

test("a profile save sends no Garmin export and reports its own failure", async () => {
  // #3640: weight and body fat left the profile, and with them its Garmin export.
  const { context, calls, toasts } = profileContext({ server: {}, baseline: { name: "Anna", sports: "" }, stateProfile: {} });
  await context.saveProfile(submitEvent([["name", "Anna M."]]).event);
  assert.deepEqual(plain(calls.find((call) => call.method === "PUT").body), { name: "Anna M." });
  assert.equal(toasts.at(-1).message, "Profil gespeichert");
  const failed = profileContext({ server: {}, baseline: { name: "Anna", sports: "" }, stateProfile: {}, error: new Error("Nicht gespeichert") });
  await failed.context.saveProfile(submitEvent([["name", "Anna M."]]).event);
  assert.deepEqual(plain(failed.toasts.at(-1)), { message: "Nicht gespeichert", isError: true });
});

test("a second profile save waits for the first", async () => {
  const { context, calls } = profileContext({ server: {}, baseline: { name: "Anna", sports: "" }, stateProfile: {} });
  let resolve;
  context.api = (_path, options) => {
    calls.push({ body: JSON.parse(options.body) });
    return new Promise((done) => { resolve = done; });
  };
  const pending = context.saveProfile(submitEvent([["name", "Anna M."]]).event);
  await context.saveProfile(submitEvent([["name", "Anna N."]]).event);
  assert.equal(calls.length, 1, "the second save must not issue a request");
  resolve({ name: "Anna M." });
  await pending;
});

test("profile save refuses to write before the rendered baseline is known", async () => {
  const { context, calls, toasts } = profileContext({
    server: { name: "Anna", weight: "60" },
    baseline: null,
    stateProfile: {},
  });
  const { event } = submitEvent([["name", "Anna M."]]);
  await context.saveProfile(event);
  assert.equal(calls.filter((call) => call.method === "PUT").length, 0);
  assert.equal(toasts.at(-1).isError, true);
});

function coachContext(apiImpl) {
  const state = { coachActionUnknownIds: new Set(), coachActionProposals: [], route: "plan" };
  const receipts = [];
  const toasts = [];
  const apiCalls = [];
  const loads = [];
  let renders = 0;
  const context = extractFunctions("coach.js", COACH_PROPOSALS, {
    state,
    api: async (path, options = {}) => {
      apiCalls.push(path);
      return apiImpl(path, options);
    },
    addCoachReceipt: (receipt) => receipts.push(receipt),
    renderCoachActionReview: () => { renders += 1; },
    toast: (message, isError) => toasts.push({ message, isError: Boolean(isError) }),
    AppRouter: { navigate: async () => {} },
    isSessionEndedError: () => false,
    load: async (path, areas) => { loads.push({ path, areas }); },
  });
  return { context, state, receipts, toasts, apiCalls, loads, renders: () => renders };
}

function confirmThen(executeError) {
  return async (path) => {
    if (path === "/api/coach/actions/confirm") return { action_token: "tok", proposed_action: { payload_hash: "hash" } };
    throw executeError;
  };
}

test("a lost execute response keeps the action locked and is not reported as a failed confirmation", async () => {
  const { context, state, receipts, apiCalls } = coachContext(confirmThen(new Error("Der Server antwortet nicht innerhalb von 55 Sekunden.")));
  const proposal = { id: "p1", status: "ready", action_type: "local_coach_write" };
  state.coachActionProposals = [proposal];
  const button = { disabled: false };
  await context.executeCoachActionProposal(proposal, button);
  assert.equal(state.coachActionUnknownIds.has("p1"), true);
  assert.equal(button.disabled, true);
  assert.equal(receipts.at(-1).title, "Ausführung unklar");
  assert.equal(receipts.some((receipt) => receipt.title === "Aktion nicht bestätigt"), false);

  await context.executeCoachActionProposal(proposal, { disabled: false });
  assert.equal(apiCalls.filter((path) => path === "/api/coach/actions/execute").length, 1, "a locked proposal must not be executed again");
});

test("a server error after execute was sent also counts as an unknown outcome", async () => {
  const error = Object.assign(new Error("Interner Fehler."), { status: 500 });
  const { context, state } = coachContext(confirmThen(error));
  const proposal = { id: "p2", status: "ready", action_type: "local_coach_write" };
  await context.executeCoachActionProposal(proposal, { disabled: false });
  assert.equal(state.coachActionUnknownIds.has("p2"), true);
});

test("a 409 rejection after execute was sent is a known failure and unlocks the proposal", async () => {
  const error = Object.assign(new Error("Diese Freigabe wurde bereits verwendet."), { status: 409, reason: "proposal_used" });
  const { context, state, receipts } = coachContext(confirmThen(error));
  const proposal = { id: "p3", status: "ready", action_type: "local_coach_write" };
  const button = { disabled: false };
  await context.executeCoachActionProposal(proposal, button);
  assert.equal(state.coachActionUnknownIds.has("p3"), false);
  assert.equal(button.disabled, false);
  assert.equal(proposal.status, "used");
  assert.equal(receipts.at(-1).title, "Aktion nicht bestätigt");
});

test("reconcile reports a proposal the server already consumed and keeps it locked", async () => {
  const { context, state, receipts, loads } = coachContext(async (path) => {
    if (path === "/api/chat/history?limit=1") return { proposed_actions: [{ id: "p1", status: "used" }] };
    throw new Error("unexpected call");
  });
  state.coachActionUnknownIds.add("p1");
  await context.reconcileCoachActionProposal("p1");
  assert.equal(state.coachActionUnknownIds.has("p1"), false);
  assert.deepEqual(plain(state.coachActionProposals), [{ id: "p1", status: "used" }]);
  assert.equal(receipts.at(-1).title, "Ausführung prüfen");
  assert.deepEqual(plain(loads), [{ path: "/api/bootstrap?local=1", areas: ["plan", "library", "profile", "feedback", "performance"] }]);
});

test("reconcile re-enables a proposal the server still reports as confirmable", async () => {
  const { context, state, receipts, loads } = coachContext(async () => ({ proposed_actions: [{ id: "p1", status: "ready" }] }));
  state.coachActionUnknownIds.add("p1");
  await context.reconcileCoachActionProposal("p1");
  assert.equal(state.coachActionUnknownIds.has("p1"), false);
  assert.equal(receipts.at(-1).title, "Nicht ausgeführt");
  assert.equal(loads.length, 0);
});

test("a failed reconcile keeps the unknown-outcome lock", async () => {
  const { context, state, toasts } = coachContext(async () => {
    throw new Error("Keine Verbindung zum Coach-Server.");
  });
  state.coachActionUnknownIds.add("p1");
  await context.reconcileCoachActionProposal("p1");
  assert.equal(state.coachActionUnknownIds.has("p1"), true);
  assert.equal(toasts.at(-1).isError, true);
});

test("resetting the coach chat waits while queued messages have not been sent", async () => {
  const state = { chatQueue: [{ id: 1 }] };
  const toasts = [];
  const apiCalls = [];
  let confirmations = 0;
  const context = extractFunctions("coach.js", ["resetCoachChat"], {
    state,
    $: () => ({ disabled: false, textContent: "" }),
    requestConfirmation: async () => { confirmations += 1; return true; },
    api: async (path) => { apiCalls.push(path); return {}; },
    toast: (message, isError) => toasts.push({ message, isError: Boolean(isError) }),
  });
  await context.resetCoachChat();
  assert.equal(confirmations, 0);
  assert.equal(apiCalls.length, 0);
  assert.equal(state.chatQueue.length, 1);
  assert.equal(toasts.at(-1).isError, true);
});

test("an in-flight sync poll starts no second request", async () => {
  let requests = 0;
  const context = extractFunctions("sync-status.js", SYNC_POLL, {
    state: { data: {}, syncPoll: { controller: {}, timer: null } },
    document: { visibilityState: "visible" },
    navigator: { onLine: true },
    SYNC_POLL_RETRY_MS: 1000,
    SYNC_POLL_IDLE_MS: 1000,
    SYNC_POLL_ACTIVE_MS: 1000,
    api: async () => { requests += 1; return {}; },
    providerIds: () => ({}),
  });
  await context.pollSyncStatus();
  assert.equal(requests, 0);
});

test("the first threshold is a stored bpm field the confirmation checks like any other (#3915)", async () => {
  const { context, state, toasts } = profileContext({ server: {}, baseline: { first_threshold_hr: "", sports: "" }, stateProfile: {} });
  context.api = async () => ({ first_threshold_hr: "150" });
  await context.saveProfile(submitEvent([["first_threshold_hr", "150"]]).event);
  assert.equal(toasts.some((entry) => entry.isError), false);
  assert.equal(state.profileBaseline.first_threshold_hr, "150");
  assert.equal(context.profileSaveConfirmed({ first_threshold_hr: "150", name: "A" }, { first_threshold_hr: "150" }), false);
  assert.equal(context.profileSaveConfirmed({ first_threshold_hr: "150" }, {}), false);
  assert.equal(context.profileSaveConfirmed({ first_threshold_hr: "150" }, { first_threshold_hr: "150" }), true);
});
