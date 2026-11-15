// TEMPORARY CI probe v2 for #3044/#3056 (draft PR, never merged). Each round opens a fresh
// context, so the service worker installs and claims anew.
// #3044: the route is added after goto, as in the real spec, while the worker may be taking over.
// #3056: every fetch is logged, so a read the app never sent shows what it waited for.
const { test, expect } = require("@playwright/test");

const ROUNDS = 30;
test.describe.configure({ retries: 0 });

const LOG_FETCHES = () => {
  const started = performance.now();
  globalThis.__probeFetches = [];
  const original = globalThis.fetch;
  globalThis.fetch = function probeFetch(input, init) {
    const url = String(input?.url || input).replace(location.origin, "");
    const row = { url: url.slice(0, 60), start: Math.round(performance.now() - started), end: null };
    globalThis.__probeFetches.push(row);
    const done = () => { row.end = Math.round(performance.now() - started); };
    const result = original.call(this, input, init);
    result.then(done, done);
    return result;
  };
};

async function freshPage(browser, blockWorkers) {
  const context = await browser.newContext({
    baseURL: process.env.E2E_BASE_URL,
    storageState: process.env.E2E_AUTH_STATE_PATH,
    viewport: { width: 1440, height: 1000 },
    serviceWorkers: blockWorkers ? "block" : "allow",
  });
  await context.addInitScript(LOG_FETCHES);
  return { context, page: await context.newPage() };
}

const fetchedCount = (page, part) => page.evaluate((p) => globalThis.__probeFetches.filter((row) => row.url.includes(p)).length, part);
const controlled = (page) => page.evaluate(() => !!(navigator.serviceWorker && navigator.serviceWorker.controller));
const gpx = (name) => ({ name, mimeType: "application/gpx+xml", buffer: Buffer.from('<gpx><rte><rtept lat="0" lon="0"/></rte></gpx>') });

async function chatRound(browser, blockWorkers, round) {
  const { context, page } = await freshPage(browser, blockWorkers);
  try {
    await page.goto("/");
    await expect(page.locator("#appShell")).toBeVisible();
    await page.evaluate(() => jumpToLatestMessages());
    const atRoute = await controlled(page);
    let routed = 0;
    await page.route("**/api/chat/stream", async (route) => { routed += 1; await route.continue(); });
    const text = `Probe ${blockWorkers ? "b" : "a"}${round}`;
    await page.locator("#messageInput").fill(text);
    await page.locator("#attachmentInput").setInputFiles(gpx(`p${round}.gpx`));
    await page.locator("#sendButton").click();
    await expect(page.locator("#messages")).toContainText(text);
    await page.waitForTimeout(1000);
    const row = { atRoute, atEnd: await controlled(page), routed, fetched: await fetchedCount(page, "/api/chat/stream") };
    return row;
  } finally {
    await context.close();
  }
}

async function fuellingRound(browser, request, blockWorkers) {
  const { context, page } = await freshPage(browser, blockWorkers);
  let routed = 0;
  try {
    await page.route("**/api/nutrition/session-fuelling*", (route) => { routed += 1; return route.fulfill({ json: { status: "not_long" } }); });
    await page.route("**/api/bootstrap**", async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      const today = new Intl.DateTimeFormat("en-CA", { timeZone: data.profile?.timezone || "Europe/Berlin" }).format(new Date());
      data.training_calendar = [{ id: "long-ride", local_id: "long-ride", date: today, name: "Lange Ausfahrt", type: "Ride", sport: "ride", environment: "outdoor", duration_minutes: 150, planned_duration_s: 9000 }];
      return route.fulfill({ json: data });
    });
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route("**/api/chat/history?limit=100", async (route) => { await held; await route.continue(); });
    await page.goto("/#coach");
    await page.waitForFunction(() => state.data && !state.replaceDataOnLoad);
    await expect(page.locator(".planned-entry[open]")).toHaveCount(1);
    await page.waitForTimeout(1500);
    release();
    await page.waitForTimeout(8000);
    const fetched = await fetchedCount(page, "session-fuelling");
    const row = { routed, fetched };
    if (!fetched || fetched !== routed) {
      Object.assign(row, await page.evaluate(async () => ({
        settledPending: await Promise.race([globalThis.AppApi.settled().then(() => false), new Promise((r) => setTimeout(() => r(true), 50))]),
        reads: plannedFuellingReads.size,
        loading: Boolean(state.loadPromise),
        initial: state.initialStateLoaded,
        open: document.querySelectorAll(".planned-entry[open]").length,
        sections: [...document.querySelectorAll(".planned-fuelling")].map((s) => s.dataset.state).join(","),
        open_fetches: globalThis.__probeFetches.filter((f) => f.end === null).map((f) => f.url).join(","),
        log: globalThis.__probeFetches.map((f) => `${f.url}@${f.start}-${f.end}`).join(" "),
      })));
    }
    return row;
  } finally {
    await context.close();
  }
}

function summary(rounds) {
  const odd = rounds.filter((r) => r.fetched !== r.routed || !r.fetched);
  return { rounds: rounds.length, missed: rounds.filter((r) => r.fetched > r.routed).length, unsent: rounds.filter((r) => !r.fetched).length, odd };
}

test("probe #3044: route added after goto sees the chat stream", async ({ browser }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop");
  test.setTimeout(1_500_000);
  const result = {};
  for (const block of [false, true]) {
    const rounds = [];
    for (let i = 0; i < ROUNDS; i += 1) rounds.push(await chatRound(browser, block, i));
    result[block ? "blocked" : "workers"] = summary(rounds);
    result[block ? "blockedRows" : "workerRows"] = rounds.map((r) => `${+r.atRoute}${+r.atEnd}/${r.routed}/${r.fetched}`).join(" ");
  }
  throw new Error(`PROBE3044 ${JSON.stringify(result)}`);
});

test("probe #3056: fuelling read sent and seen", async ({ browser, request }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop");
  test.setTimeout(1_500_000);
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  const result = {};
  for (const block of [false, true]) {
    const rounds = [];
    for (let i = 0; i < ROUNDS; i += 1) rounds.push(await fuellingRound(browser, request, block));
    result[block ? "blocked" : "workers"] = summary(rounds);
  }
  throw new Error(`PROBE3056 ${JSON.stringify(result)}`);
});
