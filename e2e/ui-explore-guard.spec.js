const { test, expect } = require("@playwright/test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

// #3913: the explorer aborts every write a plan step causes and fails the run; it refuses submit and denied controls.
const PAGE = `<main>
  <button id="open" type="button" onclick="fetch('/api/plan/x', { method: 'POST', body: '{}' }).catch(() => null)">Öffnen</button>
  <button id="peek" type="button" onclick="fetch('/api/bootstrap', { method: 'HEAD' }).catch(() => null)">Ansehen</button>
  <form onsubmit="return false"><button id="submit"><span>Weiter</span></button></form>
  <button id="save" type="button">Speichern</button>
  <button id="confirm" type="button" aria-label="Bestätigen">✓</button>
  <p id="body">42 km</p>
</main>`;

test("a write from a plan step is aborted and fails the run; denied controls are refused", async ({ browser, baseURL }) => {
  const explorer = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_explore.mjs")).href);
  const { context, aborted } = await explorer.exploreContext(browser, { url: baseURL, viewport: "phone" });
  const page = await context.newPage();
  // The test page itself is served here; every other request falls through to the explorer's guard.
  await page.route("**/explore-test", (route) => route.fulfill({ contentType: "text/html; charset=utf-8", body: PAGE }));
  await page.goto("/explore-test");
  const plan = explorer.parsePlan({ steps: [{ click: "#peek" }, { click: "#open" }, { click: "#submit span" }, { click: "#save" }, { click: "#confirm" }, { text: "#body" }] });
  const steps = await explorer.runPlan(page, plan.steps);
  expect(steps.map((item) => `${item.status} ${item.why || ""}`.trim())).toEqual(["ok", "ok", "fail refused: a form's submit control", "fail refused: denied label", "fail refused: denied label", "ok"]);
  await expect.poll(() => aborted).toEqual(["POST /api/plan/x"]);
  expect(explorer.exitCode([{ aborted, steps: [] }])).toBe(1);
  expect(explorer.exitCode([{ aborted: [], steps: steps.slice(0, 2) }])).toBe(0);
  // Service workers are blocked in the explorer's context: none starts, so none serves or sends requests.
  await page.evaluate(() => navigator.serviceWorker?.register("/service-worker.js").catch(() => null));
  await page.waitForTimeout(1500);
  expect(context.serviceWorkers()).toEqual([]);
  expect(await page.evaluate(() => navigator.serviceWorker?.getRegistration().then((registration) => registration?.active?.state ?? "none"))).toBe("none");
  // Login and logout are the only writes that pass.
  const passed = await page.evaluate(() => fetch("/api/logout", { method: "POST", body: "{}" }).then((response) => response.status, () => "aborted"));
  expect(passed).not.toBe("aborted");
  expect(aborted).toEqual(["POST /api/plan/x"]);
  await context.close();
});

test("a plan names exactly one known action per step", async () => {
  const { parsePlan } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_explore.mjs")).href);
  for (const plan of [{}, { steps: [] }, { steps: [{ click: "a", go: "plan" }] }, { steps: [{ type: "x" }] }, { steps: [{ go: "https://x" }] }, { steps: [{ shot: "../x" }] }, { steps: [{ text: "p" }], viewports: ["tv"] }, { steps: [{ text: "p" }], textZoom: 300 }, { steps: [{ shot: "a", nth: 1 }] }]) {
    expect(() => parsePlan(plan), JSON.stringify(plan)).toThrow(/^plan: /);
  }
  expect(parsePlan({ steps: [{ go: "analysis/running" }, { scroll: 400 }, { shot: "a-1", full: true }] })).toMatchObject({ viewports: ["phone", "desktop"], colorScheme: "light", textZoom: 100 });
});
