const { test, expect } = require("@playwright/test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

// The explorer's own context blocks the worker too; this keeps the spec's default context from routing past it (#3842).
test.use({ serviceWorkers: "block" });

const load = () => import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_explore.mjs")).href);

// #3913: the explorer aborts every write a plan step causes and fails the run; it refuses submit and denied controls.
const PAGE = `<main>
  <button id="open" type="button" onclick="fetch('/api/plan/x', { method: 'POST', body: '{}' }).catch(() => null)">Öffnen</button>
  <button id="peek" type="button" onclick="fetch('/api/bootstrap', { method: 'HEAD' }).catch(() => null)">Ansehen</button>
  <button id="whole" type="button" onclick="fetch('/api/privacy/backup').catch(() => null); fetch('/api/diagnostics').catch(() => null)">Ansehen</button>
  <form onsubmit="return false"><button id="submit"><span>Weiter</span></button></form>
  <button id="save" type="button">Speichern</button>
  <button id="confirm" type="button" aria-label="Bestätigen">✓</button>
  <details><summary>Einstellungen werden geladen</summary><p>x</p></details>
  <p id="body">42 km</p>
  <section class="panel active"><p id="load">Ernährung wird geladen…</p><p id="long">${"x".repeat(5000)}</p>
    <button type="button" disabled>Antwort wird geladen…</button></section>
</main>
<script>
  // A route renders at once, but its data line loads behind a text for 1.5 s (#3969).
  addEventListener("hashchange", () => {
    document.dispatchEvent(new Event("app:route-rendered"));
    setTimeout(() => { document.getElementById("load").textContent = "1200 kcal"; }, 1500);
  });
</script>`;

async function testPage(explorer, browser, baseURL, options = {}) {
  const { context, aborted } = await explorer.exploreContext(browser, { url: baseURL, viewport: "phone", ...options });
  const page = await context.newPage();
  // The test page itself is served here; every other request falls through to the explorer's guard.
  // The app's own style rule: no <style> element is applied (#3969).
  const headers = { "content-security-policy": "style-src 'self'" };
  await page.route("**/explore-test", (route) => route.fulfill({ contentType: "text/html; charset=utf-8", headers, body: `<!doctype html><html><head></head><body>${PAGE}</body></html>` }));
  await page.goto("/explore-test");
  return { context, aborted, page };
}

test("a write from a plan step is aborted and fails the run; denied controls are refused", async ({ browser, baseURL }) => {
  const explorer = await load();
  const { context, aborted, page } = await testPage(explorer, browser, baseURL);
  const plan = explorer.parsePlan({ steps: [{ click: "#peek" }, { click: "#open" }, { click: "#whole" }, { click: "#submit span" }, { click: "#save" }, { click: "#confirm" }, { click: "summary" }, { text: "#body" }] });
  const steps = await explorer.runPlan(page, plan.steps);
  expect(steps.map((item) => `${item.status} ${item.why || ""}`.trim())).toEqual(["ok", "ok", "ok", "fail refused: a form's submit control", "fail refused: denied label", "fail refused: denied label", "ok", "ok"]);
  // The write and the whole-data reads (backup, diagnostics report) are aborted, the HEAD passes.
  await expect.poll(() => [...aborted].sort()).toEqual(["GET /api/diagnostics", "GET /api/privacy/backup", "POST /api/plan/x"]);
  expect(explorer.exitCode([{ aborted, steps: [], logout: 200 }])).toBe(1);
  expect(explorer.exitCode([{ aborted: [], steps: steps.slice(0, 3), logout: 200 }])).toBe(0);
  expect(explorer.exitCode([{ aborted: [], steps: [], logout: 403 }])).toBe(1);
  // Service workers are blocked in the explorer's context: none starts, so none serves or sends requests.
  await page.evaluate(() => navigator.serviceWorker?.register("/service-worker.js").catch(() => null));
  await page.waitForTimeout(1500);
  expect(context.serviceWorkers()).toEqual([]);
  expect(await page.evaluate(() => navigator.serviceWorker?.getRegistration().then((registration) => registration?.active?.state ?? "none"))).toBe("none");
  // Login and logout are the only writes that pass.
  const passed = await page.evaluate(() => fetch("/api/logout", { method: "POST", body: "{}" }).then((response) => response.status, () => "aborted"));
  expect(passed).not.toBe("aborted");
  expect(aborted).toHaveLength(3);
  await context.close();
});

test("go waits out a loading text line; wait and a longer text read are steps (#3969)", async ({ browser, baseURL }) => {
  const explorer = await load();
  const { context, page } = await testPage(explorer, browser, baseURL);
  const plan = explorer.parsePlan({ steps: [{ go: "day" }, { text: "#load" }, { wait: 200 }, { wait: "#body" }, { text: "#long" }, { text: "#long", max: 6000 }] });
  const steps = await explorer.runPlan(page, plan.steps);
  // Ready once the line loaded; the button's own "wird geladen…" label is no loading line.
  expect(steps[0]).toMatchObject({ status: "ok" });
  expect(steps[0].ms).toBeGreaterThanOrEqual(1400);
  expect(steps[0].ms).toBeLessThan(10000);
  expect(steps[1]).toMatchObject({ status: "ok", text: "1200 kcal" });
  expect(steps.slice(2, 4).map((item) => item.status)).toEqual(["ok", "ok"]);
  expect(steps[4]).toMatchObject({ truncated: true });
  expect(steps[4].text).toHaveLength(4000);
  expect(steps[5].text).toHaveLength(5000);
  expect(steps[5].truncated).toBeUndefined();
  await context.close();
});

test("a wait for a control that never shows fails the step (#3969)", async ({ browser, baseURL }) => {
  const explorer = await load();
  const { context, page } = await testPage(explorer, browser, baseURL);
  const steps = await explorer.runPlan(page, explorer.parsePlan({ steps: [{ wait: "#absent" }] }).steps);
  expect(steps).toEqual([{ step: "wait #absent", status: "fail", why: "control missing" }]);
  await context.close();
});

test("a plan's text zoom scales the root font size", async ({ browser, baseURL }) => {
  const explorer = await load();
  for (const [textZoom, size] of [[100, "16px"], [150, "24px"]]) {
    const { context, page } = await testPage(explorer, browser, baseURL, { textZoom });
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).fontSize)).toBe(size);
    await context.close();
  }
});

test("a plan names exactly one known action per step", async () => {
  const { parsePlan } = await load();
  const bad = [{}, { steps: [] }, { steps: [{ click: "a", go: "plan" }] }, { steps: [{ type: "x" }] }, { steps: [{ go: "https://x" }] }, { steps: [{ shot: "../x" }] }, { steps: [{ text: "p" }], viewports: ["tv"] }, { steps: [{ text: "p" }], textZoom: 300 }, { steps: [{ shot: "a", nth: 1 }] }, { steps: [{ wait: 50 }] }, { steps: [{ wait: 20000 }] }, { steps: [{ text: "p", max: 60000 }] },
    // A repeated name or viewport would overwrite a file; inherited keys are not actions.
    { steps: [{ shot: "a" }, { shot: "a" }] }, { steps: [{ text: "p" }], viewports: ["phone", "phone"] }, { steps: [{ toString: "x" }] }, { steps: [{ click: "a", constructor: 1 }] }, { steps: [{ text: "p" }], viewports: ["toString"] }, { steps: [JSON.parse('{"__proto__": 1}')] }];
  for (const plan of bad) expect(() => parsePlan(plan), JSON.stringify(plan)).toThrow(/^plan: /);
  expect(parsePlan({ steps: [{ go: "analysis/running" }, { scroll: 400 }, { shot: "a-1", full: true }] })).toMatchObject({ viewports: ["phone", "desktop"], colorScheme: "light", textZoom: 100 });
});
