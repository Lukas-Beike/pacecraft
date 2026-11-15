const { test, expect } = require("@playwright/test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

// #3009: the post-release walk fails a step whose chart or panel is empty, and presses nothing but opening controls.
const PAGE = `<main>
  <nav><a id="tab-a" href="#a" data-analysis-segment="a" aria-current="page">A</a></nav>
  <section id="good"><svg viewBox="0 0 10 10" width="100" height="100"><path d="M0 0 L10 10"/></svg><p id="banned">Temperatur</p></section>
  <section id="bare"><svg viewBox="0 0 10 10" width="100" height="100"><line x1="0" y1="0" x2="9" y2="9"/></svg></section>
  <section id="figures">12,5 km</section>
  <section id="hollow"></section>
  <select id="gear"><option>Rad</option><option>Keiner</option></select>
  <select id="lonely"><option>-</option></select>
  <details id="more"><summary>Details</summary><p id="inner">42 W</p></details>
  <details class="c"><summary>Mo</summary><details class="m"><summary>Details</summary><p class="v"></p></details></details>
  <details class="c"><summary>Di</summary><details class="m"><summary>Details</summary><p class="v">7 km</p></details></details>
  <button id="delete" type="button">Löschen</button>
</main>`;

test("a step fails on an empty chart or panel and passes on a drawn one", async ({ page }) => {
  const { runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  await page.setContent(PAGE);
  const run = (step) => runStep(page, { name: "x", covers: "test", ...step }, 300);
  expect((await run({ ready: "#good svg", kind: "chart" })).status).toBe("ok");
  expect((await run({ ready: "#figures", kind: "numbers" })).status).toBe("ok");
  expect((await run({ ready: "#gear", kind: "options" })).status).toBe("ok");
  expect((await run({ pre: ["#more > summary"], ready: "#inner", kind: "numbers" })).status).toBe("ok");
  // The feature switched off: an svg without drawn marks, a panel without text, a select without a choice.
  const off = [{ ready: "#bare svg", kind: "chart" }, { ready: "#hollow", kind: "text" }, { ready: "#lonely", kind: "options" }, { ready: "#figures", kind: "chart" }];
  for (const step of off) expect((await run(step)).status, step.ready).toBe("fail");
  // A step whose ready element shows next to a `never` element fails (#3036); without it, it passes.
  const banned = await run({ ready: "#good svg", kind: "chart", never: "#banned" });
  expect([banned.status, banned.why]).toEqual(["fail", "shows #banned"]);
  expect((await run({ ready: "#good svg", kind: "chart", never: "#missing" })).status).toBe("ok");
  // An optional step without its data is skipped, not failed.
  expect((await run({ ready: "#missing", kind: "text", optional: true })).status).toBe("skip");
  // Each candidate is checked with its own controls: the second session passes although the first is empty.
  const own = { each: "details.c > summary", opens: ["details.c[open] details.m > summary"], ready: "details.c[open] details.m[open] .v", kind: "numbers" };
  expect((await run(own)).status).toBe("ok");
  // A control that is not an opening one is refused, even if the step names it.
  const refused = await run({ pre: ["#delete"], ready: "#good svg", kind: "chart" });
  expect(refused.status).toBe("fail");
  expect(refused.why).toMatch(/opening control/);
});
