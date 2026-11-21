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
