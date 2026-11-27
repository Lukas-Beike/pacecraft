const assert = require("node:assert/strict");
const { test } = require("node:test");
const { loadPublicScript } = require("./frontend-harness.cjs");

const networkError = () => Object.assign(new Error("Keine Verbindung"), { status: 0, reason: "network_error" });
const serverError = (status, message = "abgelehnt") => Object.assign(new Error(message), { status, reason: "http_error" });

// public/outbox.js on an in-memory device store and a scripted api().
function outbox(respond = async () => ({ ok: true })) {
  const stored = new Map();
  const requests = [];
  const strip = [];
  const toasts = [];
  const storage = {
    all: async () => [...stored.values()].map((entry) => structuredClone(entry)),
    put: async (entry) => { stored.set(entry.key, structuredClone(entry)); },
    remove: async (key) => { stored.delete(key); },
    clear: async () => { stored.clear(); },
  };
  const context = loadPublicScript("outbox.js", {
    crypto,
    navigator: { onLine: true },
    state: { data: {} },
    document: { querySelector: () => null },
    api: async (path, options) => { requests.push({ path, ...options, headers: options.headers }); return respond(path, options); },
    updateStatusSources: (change) => strip.push(change.outbox),
    toast: (message) => toasts.push(message),
    scheduleSyncPoll() {},
    console,
  });
  context.outboxUseStorage(storage);
  return { context, stored, requests, strip, toasts };
}

const save = (context, kind, payload, send) => context.saveOwnEntry({ kind, method: "PUT", path: `/api/${kind}`, payload, label: `${kind} ${payload.n}`, send });

test("only a request that cannot reach the server is queued, with its key", async () => {
  const { context, stored, requests } = outbox();
  const sent = await save(context, "weight", { n: 1 });
  assert.deepEqual(sent, { ok: true });
  assert.match(requests[0].headers["Idempotency-Key"], /^[0-9a-f-]{36}$/);
  assert.equal(stored.size, 0);

  const offline = outbox(async () => { throw networkError(); });
  const queued = await save(offline.context, "weight", { n: 2 });
  assert.equal(queued.outboxEntry.state, "waiting");
  assert.equal(queued.outboxEntry.key, offline.requests[0].headers["Idempotency-Key"]);
  assert.equal(offline.stored.size, 1);
  assert.equal(offline.strip.at(-1).title, "1 Eintrag wartet");
});

test("a server answer is never queued, whatever its status", async () => {
  for (const status of [400, 401, 403, 409, 500]) {
    const { context, stored } = outbox(async () => { throw serverError(status); });
    await assert.rejects(save(context, "weight", { n: 1 }), (error) => error.status === status);
    assert.equal(stored.size, 0, String(status));
  }
  const timeout = outbox(async () => { throw new Error("Der Server antwortet nicht innerhalb von 25 Sekunden."); });
  await assert.rejects(save(timeout.context, "weight", { n: 1 }));
  assert.equal(timeout.stored.size, 0);
});

test("without a device store a save fails with the offline error as before", async () => {
  const { context } = outbox(async () => { throw networkError(); });
  context.outboxUseStorage({ all: async () => { throw new Error("no IndexedDB"); }, put: async () => { throw new Error("no IndexedDB"); }, clear: async () => {}, remove: async () => {} });
  await assert.rejects(save(context, "weight", { n: 1 }), (error) => error.reason === "network_error");
});

test("the replay sends in creation order, one at a time, with the key of the first attempt", async () => {
  let offline = true;
  const { context, stored, requests, toasts } = outbox(async () => { if (offline) throw networkError(); return { ok: true }; });
  for (const n of [1, 2, 3]) await save(context, "weight", { n });
  const keys = [...stored.keys()];
  requests.length = 0;
  offline = false;
  await context.replayOutbox();
  assert.deepEqual(requests.map((request) => JSON.parse(request.body).n), [1, 2, 3]);
  assert.deepEqual(requests.map((request) => request.headers["Idempotency-Key"]), keys);
  assert.equal(stored.size, 0);
  assert.deepEqual(toasts, ["3 Einträge gesendet"]);
});

test("a replayed body entry whose pushes still run is sent once and reported as sent (#4267)", async () => {
  // The answer's running Garmin and Wahoo pushes change nothing for the outbox: it is removed,
  // the notice counts it, and no receipt or push line follows.
  let offline = true;
  const { context, stored, requests, toasts } = outbox(async () => {
    if (offline) throw networkError();
    return { entries: [], remote: "running", remote_wahoo: "running" };
  });
  await save(context, "weight", { n: 1 });
  requests.length = 0;
  offline = false;
  await context.replayOutbox();
  assert.equal(requests.length, 1);
  assert.equal(stored.size, 0);
  assert.deepEqual(toasts, ["1 Eintrag gesendet"]);
});

test("a network failure leaves the entry and stops the pass; the next trigger sends it", async () => {
  let offline = true;
  const { context, stored, requests } = outbox(async () => { if (offline) throw networkError(); return { ok: true }; });
  await save(context, "weight", { n: 1 });
  await save(context, "weight", { n: 2 });
  // The second save queued behind the first and started a pass; let it end first.
  await context.replayOutbox();
  requests.length = 0;
  await context.replayOutbox();
  assert.equal(requests.length, 1, "the pass stopped at the first failure");
  assert.equal(stored.size, 2);
  offline = false;
  await context.replayOutbox();
  assert.equal(stored.size, 0);
});

test("a refused entry is kept as rejected and the later ones go on", async () => {
  let offline = true;
  const { context, stored, strip } = outbox(async (_path, options) => {
    if (offline) throw networkError();
    if (JSON.parse(options.body).n === 1) throw serverError(400, "Das Gewicht muss zwischen 30 und 300 kg liegen.");
    return { ok: true };
  });
  await save(context, "weight", { n: 1 });
  await save(context, "weight", { n: 2 });
  offline = false;
  await context.replayOutbox();
  const [left] = [...stored.values()];
  assert.equal(stored.size, 1);
  assert.equal(left.state, "rejected");
  assert.equal(left.last_error, "Das Gewicht muss zwischen 30 und 300 kg liegen.");
  assert.equal(left.attempts, 1);
  assert.equal(strip.at(-1).title, "1 abgelehnt");
  // A rejected entry is not sent again by the next pass; "Erneut senden" does.
  await context.replayOutbox();
  assert.equal(stored.size, 1);
  await context.outboxDiscard(left.key);
  assert.equal(stored.size, 0);
});

test("an expired session or a server error keeps the entry waiting", async () => {
  for (const status of [401, 403, 429, 503]) {
    let offline = true;
    const { context, stored } = outbox(async () => { if (offline) throw networkError(); throw serverError(status); });
    await save(context, "weight", { n: 1 });
    offline = false;
    await context.replayOutbox();
    assert.equal([...stored.values()][0].state, "waiting", String(status));
  }
});

test("nothing is replayed before the data is loaded or while the browser is offline", async () => {
  let offline = true;
  const { context, stored, requests } = outbox(async () => { if (offline) throw networkError(); return { ok: true }; });
  await save(context, "weight", { n: 1 });
  offline = false;
  requests.length = 0;
  context.navigator.onLine = false;
  await context.replayOutbox();
  context.navigator.onLine = true;
  context.state.data = null;
  await context.replayOutbox();
  assert.equal(requests.length, 0);
  assert.equal(stored.size, 1);
});

test("logout and the purge clear the store and the strip line", async () => {
  const { context, stored, strip } = outbox(async () => { throw networkError(); });
  await save(context, "weight", { n: 1 });
  assert.equal(stored.size, 1);
  await context.clearOutbox();
  assert.equal(stored.size, 0);
  assert.equal(strip.at(-1), null);
});

// Review on #3761: a new save never overtakes an older waiting entry.
test("a save while an older entry waits queues behind it, and the pass keeps their order", async () => {
  let offline = true;
  const { context, stored, requests } = outbox(async () => { if (offline) throw networkError(); return { ok: true }; });
  await save(context, "weight", { n: 1 });
  offline = false;
  const queued = await save(context, "weight", { n: 2 });
  assert.ok(queued.outboxEntry, "the newer weight waits behind the older one");
  await context.replayOutbox();
  const sent = requests.filter((request) => !request.failed).map((request) => JSON.parse(request.body).n);
  assert.deepEqual(sent.slice(-2), [1, 2]);
  assert.equal(stored.size, 0);
});

test("an entry discarded during a pass is not sent and does not come back", async () => {
  let offline = true;
  let context;
  let keys = [];
  const created = outbox(async (_path, options) => {
    if (offline) throw networkError();
    // While the first entry is in flight the athlete discards both; its answer is a server error.
    if (JSON.parse(options.body).n === 1) {
      for (const key of keys) await context.outboxDiscard(key);
      throw serverError(500);
    }
    return { ok: true };
  });
  context = created.context;
  await save(context, "weight", { n: 1 });
  await save(context, "weight", { n: 2 });
  keys = [...created.stored.keys()];
  await new Promise((resolve) => setTimeout(resolve, 0));
  created.requests.length = 0;
  offline = false;
  await context.replayOutbox();
  assert.deepEqual(created.requests.map((request) => JSON.parse(request.body).n), [1]);
  assert.equal(created.stored.size, 0);
});

// Gitar on #3816: a server that keeps failing one entry must not hold back later saves.
test("an entry failing with server errors is set aside after five tries and the later ones go on", async () => {
  let offline = true;
  const { context, stored } = outbox(async (_path, options) => {
    if (offline) throw networkError();
    if (JSON.parse(options.body).n === 1) throw serverError(500, "Serverfehler");
    return { ok: true };
  });
  await save(context, "weight", { n: 1 });
  await save(context, "weight", { n: 2 });
  await context.replayOutbox();
  offline = false;
  for (let pass = 0; pass < 4; pass += 1) await context.replayOutbox();
  assert.equal(stored.size, 2, "four server errors keep both waiting in order");
  await context.replayOutbox();
  const left = [...stored.values()];
  assert.equal(left.length, 1);
  assert.equal(left[0].state, "rejected");
  assert.equal(left[0].attempts, 5);
});

// #3817: the first entry is refused by the server, the second still waits behind the network.
async function rejectedFirst() {
  let mode = "offline";
  const created = outbox(async (_path, options) => {
    const n = JSON.parse(options.body).n;
    if (mode === "offline" || n === 2 && mode !== "accept") throw networkError();
    if (n === 1) throw serverError(400, "abgelehnt");
    return { ok: true };
  });
  await save(created.context, "weight", { n: 1 });
  await save(created.context, "weight", { n: 2 });
  mode = "refuse";
  await created.context.replayOutbox();
  const entries = [...created.stored.values()].sort((a, b) => a.created_at.localeCompare(b.created_at));
  assert.deepEqual(entries.map((entry) => entry.state), ["rejected", "waiting"]);
  created.requests.length = 0;
  return { ...created, entries, accept: () => { mode = "accept"; } };
}

const edited = (context, replaces, n) => context.saveOwnEntry({ kind: "weight", method: "PUT", path: "/api/weight", payload: { n }, label: `weight ${n}`, replaces });

test("an edit replaces a rejected entry in place: new key, same place, cleared error, new payload", async () => {
  const { context, stored, requests, entries, accept } = await rejectedFirst();
  const [rejected, waiting] = entries;
  const result = await edited(context, context.outboxEditToken(rejected.key), 9);
  const replaced = result.outboxEntry;
  assert.notEqual(replaced.key, rejected.key);
  assert.equal(replaced.created_at, rejected.created_at);
  assert.equal(replaced.attempts, 0);
  assert.equal(replaced.last_error, "");
  assert.equal(replaced.state, "waiting");
  assert.equal(replaced.label, "weight 9");
  assert.equal(stored.has(rejected.key), false);
  assert.equal(stored.size, 2);
  // The edit started a pass; with the server answering again, both go out in the old order.
  accept();
  requests.length = 0;
  await context.replayOutbox();
  assert.deepEqual(requests.map((request) => JSON.parse(request.body).n), [9, 2]);
  assert.equal(requests[0].headers["Idempotency-Key"], replaced.key);
  assert.equal(requests[1].headers["Idempotency-Key"], waiting.key);
  assert.equal(stored.size, 0);
});

test("an unchanged resend keeps its key; a waiting entry has no edit", async () => {
  const { context, requests, entries } = await rejectedFirst();
  assert.equal(context.outboxEditToken(entries[1].key), null);
  await context.outboxResend(entries[0].key);
  assert.equal(requests[0].headers["Idempotency-Key"], entries[0].key);
});

test("an edit of a discarded or cleared entry does nothing and brings nothing back", async () => {
  const discarded = await rejectedFirst();
  const token = discarded.context.outboxEditToken(discarded.entries[0].key);
  await discarded.context.outboxDiscard(discarded.entries[0].key);
  assert.equal(await discarded.context.outboxReplace(token, { payload: { n: 5 }, label: "x" }), null);
  await assert.rejects(edited(discarded.context, token, 5), /verworfen/);
  assert.deepEqual([...discarded.stored.values()].map((entry) => entry.payload.n), [2]);

  const cleared = await rejectedFirst();
  const clearedToken = cleared.context.outboxEditToken(cleared.entries[0].key);
  await cleared.context.clearOutbox();
  await assert.rejects(edited(cleared.context, clearedToken, 5));
  assert.equal(cleared.stored.size, 0);
  assert.equal(cleared.requests.length, 0);
});
