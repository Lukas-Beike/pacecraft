// TEMPORARY CI probe for #3044/#3056 (draft PR, never merged). Each round opens a fresh
// context, so the service worker installs and claims anew, and records whether a request the
// page made (resource timing) was also seen by page.route.
const { test, expect } = require("@playwright/test");

const ROUNDS = 20;

async function freshPage(browser, blockWorkers) {
  const context = await browser.newContext({
    baseURL: process.env.E2E_BASE_URL,
    storageState: process.env.E2E_AUTH_STATE_PATH,
    viewport: { width: 1440, height: 1000 },
    serviceWorkers: blockWorkers ? "block" : "allow",
  });
  return { context, page: await context.newPage() };
}

const fetched = (page, part) => page.evaluate((p) => performance.getEntriesByType("resource").filter((e) => e.name.includes(p)).length, part);
const controlled = (page) => page.evaluate(() => !!(navigator.serviceWorker && navigator.serviceWorker.controller));

async function chatRound(browser, blockWorkers, round) {
  const { context, page } = await freshPage(browser, blockWorkers);
  let routed = 0;
  try {
    await page.route("**/api/chat/stream", async (route) => { routed += 1; await route.continue(); });
    await page.goto("/");
    await expect(page.locator("#appShell")).toBeVisible();
    await page.evaluate(() => jumpToLatestMessages());
    const atSend = await controlled(page);
    await page.locator("#messageInput").fill(`Probe ${blockWorkers ? "b" : "a"}${round}`);
    await page.locator("#sendButton").click();
    await expect(page.locator("#messages")).toContainText(`Probe ${blockWorkers ? "b" : "a"}${round}`);
    await page.waitForTimeout(1500);
    return { atSend, routed, fetched: await fetched(page, "/api/chat/stream") };
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
    const before = routed;
    const atRelease = await controlled(page);
    release();
    await page.waitForTimeout(8000);
    const section = await page.evaluate(() => document.querySelector(".planned-fuelling")?.dataset.state || "none");
    return { before, atRelease, routed, fetched: await fetched(page, "session-fuelling"), section };
  } finally {
    await context.close();
  }
}

function summary(rounds) {
  const missed = rounds.filter((r) => r.fetched > r.routed).length;
  const unsent = rounds.filter((r) => r.fetched === 0).length;
  return { rounds: rounds.length, missed, unsent, rows: rounds.map((r) => Object.values(r).map(String).join("/")).join(" ") };
}

test("probe #3044: chat stream seen by page.route", async ({ browser }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop");
  test.setTimeout(900_000);
  const result = {};
  for (const block of [false, true]) {
    const rounds = [];
    for (let i = 0; i < ROUNDS; i += 1) rounds.push(await chatRound(browser, block, i));
    result[block ? "blocked" : "workers"] = summary(rounds);
  }
  throw new Error(`PROBE3044 ${JSON.stringify(result)}`);
});

test("probe #3056: fuelling read seen by page.route", async ({ browser, request }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop");
  test.setTimeout(900_000);
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  const result = {};
  for (const block of [false, true]) {
    const rounds = [];
    for (let i = 0; i < ROUNDS; i += 1) rounds.push(await fuellingRound(browser, request, block));
    result[block ? "blocked" : "workers"] = summary(rounds);
  }
  throw new Error(`PROBE3056 ${JSON.stringify(result)}`);
});
