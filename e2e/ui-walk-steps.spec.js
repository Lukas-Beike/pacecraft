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

test("a seek step pages back to a week with a completed unit, a step without seek does not", async ({ page }) => {
  const { runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  // A Monday: the shown week has no completed unit; the week before has one.
  const week = `<button type="button" aria-label="Vorherige Woche" onclick="document.querySelector('#before').hidden = false">‹</button>
    <section id="before" hidden><details class="planned-entry is-completed"><summary>So</summary><p class="d">5 km</p></details></section>`;
  const step = { name: "x", covers: "test", each: ".planned-entry.is-completed > summary", ready: ".planned-entry[open] .d", kind: "numbers" };
  await page.setContent(week);
  expect(await runStep(page, step, 300)).toMatchObject({ status: "skip", why: "no such session" });
  await page.setContent(week);
  expect((await runStep(page, { ...step, seek: 'button[aria-label="Vorherige Woche"]' }, 300)).status).toBe("ok");
});

test("the changelog step passes on an entry of only Technik changes and not on an entry without changes", async ({ page }) => {
  const { STEPS, runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  const step = { ...STEPS.find((item) => item.name === "Mehr: Betrieb, Änderungen"), pre: [] };
  const entry = (body) => `<details class="release-changelog" open><summary>Version 2.1.20 · Änderungen</summary>${body}<details><summary>Frühere Versionen (1)</summary><ul><li>older</li></ul></details></details>`;
  // 2.1.20: only a perf and a build commit, both under the collapsed "Technik (2)".
  await page.setContent(entry('<details class="release-technical"><summary>Technik (2)</summary><ul><li>quality: ratchet</li></ul></details>'));
  expect((await runStep(page, step, 300)).status).toBe("ok");
  await page.setContent(entry(""));
  expect((await runStep(page, step, 300)).status).not.toBe("ok");
});
