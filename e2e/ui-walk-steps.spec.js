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
  <p id="words">nullable, undefinedx und NaNa sind Wörter, kein Leck</p>
  <details id="leaky"><summary>Leck</summary><p id="leak">Wert null</p></details>
  <div id="messages"><p>Bilanz auf null gesetzt</p></div>
  <details class="release-changelog" open><summary>2.2.38</summary><ul><li>fix(ui): opened planned units no longer show "null"; "undefined" and "NaN" quoted too</li></ul></details>
</main>`;

test("a step fails on an empty chart or panel and passes on a drawn one", async ({ page }) => {
  const { runStep, leakedValues } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
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
  // A step that opens text with a leaked value fails and names the word and the element, not the text (#4285).
  const leaked = await run({ pre: ["#leaky > summary"], ready: "#leak", kind: "text" });
  expect([leaked.status, leaked.why]).toEqual(["fail", "shows null in p"]);
  expect(await leakedValues(page)).toBe("null in p");
  expect(await leakedValues(page, "#more")).toBe("");
  await page.evaluate(() => { document.querySelector("#leaky").open = false; });
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

test("a step waits until the app's data load ends before it checks the view", async ({ page }) => {
  const { runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  // 2.1.25: right after a restart a reload re-rendered the calendar while the first step checked it.
  await page.setContent(`<section id="late" hidden><p class="v">7 km</p></section><script>
    globalThis.AppState = { state: { loadPromise: Promise.resolve(), loadingAreas: new Set(["plan"]) } };
    setTimeout(() => { document.querySelector("#late").hidden = false; AppState.state.loadingAreas.clear(); AppState.state.loadPromise = null; }, 1500);
  </script>`);
  expect((await runStep(page, { name: "x", covers: "test", ready: "#late .v", kind: "numbers" }, 300)).status).toBe("ok");
});

test("a seek step passes a week whose units lack the element and pages back to where it started", async ({ page }) => {
  const { runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  // 2.1.26: the week's units had no fuelling advice; a unit two weeks on has it.
  await page.setContent(`<p id="week">0</p>
    <button type="button" aria-label="Nächste Woche" onclick="week.textContent = +week.textContent + 1; show()">›</button>
    <button type="button" aria-label="Vorherige Woche" onclick="week.textContent = +week.textContent - 1; show()">‹</button>
    <details class="planned-entry is-planned"><summary>Di</summary><p class="f"></p></details>
    <script>function show() { document.querySelector(".f").textContent = week.textContent === "2" ? "0,5 l" : ""; }</script>`);
  const step = { name: "x", covers: "test", each: ".planned-entry.is-planned > summary", ready: ".planned-entry[open] .f", kind: "numbers", optional: true };
  expect(await runStep(page, step, 300)).toMatchObject({ status: "skip", why: "none of 1 has it" });
  const later = { ...step, seek: 'button[aria-label="Nächste Woche"]', unseek: 'button[aria-label="Vorherige Woche"]' };
  expect((await runStep(page, later, 300)).status).toBe("ok");
  await expect(page.locator("#week")).toHaveText("0");
});

test("a seek step stops at a disabled paging control, and a required step does not page past an empty unit", async ({ page }) => {
  const { runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  // The calendar pages four weeks ahead; here one, then "Nächste Woche" is disabled.
  await page.setContent(`<p id="week">0</p>
    <button type="button" aria-label="Nächste Woche" onclick="week.textContent = 1; this.disabled = true; document.querySelector('.f').textContent = '0,5 l'">›</button>
    <button type="button" aria-label="Vorherige Woche" onclick="week.textContent = 0">‹</button>
    <details class="planned-entry is-planned"><summary>Di</summary><p class="f"></p></details>`);
  const step = { name: "x", covers: "test", each: ".planned-entry.is-planned > summary", ready: ".planned-entry[open] .f", kind: "numbers", seek: 'button[aria-label="Nächste Woche"]', unseek: 'button[aria-label="Vorherige Woche"]' };
  // Required: the shown week's unit is empty, which stays a finding instead of a later unit passing.
  expect(await runStep(page, step, 300)).toMatchObject({ status: "fail", why: "missing or empty" });
  await expect(page.locator("#week")).toHaveText("0");
  // Optional: it pages on, and the disabled control ends the search without a failure.
  await page.locator('button[aria-label="Nächste Woche"]').evaluate((button) => { button.disabled = false; });
  await page.locator(".f").evaluate((cell) => { cell.textContent = ""; });
  await page.locator('button[aria-label="Nächste Woche"]').evaluate((button) => { button.onclick = () => { week.textContent = 1; button.disabled = true; }; });
  expect(await runStep(page, { ...step, optional: true }, 300)).toMatchObject({ status: "skip", why: "none of 2 has it" });
});

test("an element is waited for while the view shows it is loading", async ({ page }) => {
  const { runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  // 2.1.26: right after the restart "Ausrüstung" loaded longer than the step's cap.
  await page.setContent(`<section class="panel active"><div class="loading-state" aria-busy="true">lädt</div><p id="gear" hidden>Schuh 1</p></section>
    <script>setTimeout(() => { document.querySelector(".loading-state").remove(); document.querySelector("#gear").hidden = false; }, 1500);</script>`);
  expect((await runStep(page, { name: "x", covers: "test", ready: "#gear", kind: "text" }, 300)).status).toBe("ok");
});

test("a fuelling read still running is waited for past the candidate cap", async ({ page }) => {
  const { runStep } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  // 2.1.37: "Verpflegung" took 2008-2075 ms against the 2 s candidate cap (#3298).
  await page.setContent(`<section class="panel active"><details class="planned-entry is-planned"><summary>Lauf</summary>
    <section class="planned-fuelling" data-state="loading"><output>Verpflegung wird berechnet …</output></section></details></section>
    <script>document.querySelector(".planned-entry > summary").addEventListener("click", () => setTimeout(() => {
      const section = document.querySelector(".planned-fuelling");
      section.dataset.state = "ready";
      section.textContent = "60 g Kohlenhydrate pro Stunde";
    }, 3000));</script>`);
  const step = { name: "x", covers: "test", each: ".planned-entry:is(.is-planned, .is-today) > summary", ready: ".planned-entry[open] .planned-fuelling", kind: "numbers", optional: true };
  expect((await runStep(page, step)).status).toBe("ok");
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

test("an optional step's skip names what the page showed instead of its element", async ({ page }) => {
  const { runStep, STEPS } = await import(pathToFileURL(path.join(__dirname, "..", "scripts", "ui_walk.mjs")).href);
  // The week review step skipped on a phone walk without saying why (2.2.23): the card is gone, failed, or loads.
  const { saw } = STEPS.find((step) => step.name === "Kalender: Wochenrückblick");
  const step = { name: "x", covers: "test", ready: ".week-review-tile", kind: "numbers", optional: true, saw };
  const week = (card) => `<section class="planned-week">${card}</section>`;
  const cases = {
    "card missing": week(""),
    "plan line instead": week('<p class="planned-week-totals">3 von 4 erledigt</p>'),
    "nicht verfügbar": week('<section class="week-review" data-failed="true"><p class="empty">Wochenrückblick gerade nicht verfügbar</p></section>'),
    "still loading": week('<section class="week-review"><p class="loading-state">Wochenrückblick wird geladen…</p></section>'),
  };
  for (const [label, html] of Object.entries(cases)) {
    await page.setContent(html);
    expect(await runStep(page, step, 300), label).toMatchObject({ status: "skip", why: `none of 1 has it (${label})` });
  }
  // A step without `saw` keeps its plain reason, and a shown tile still passes.
  await page.setContent(cases["card missing"]);
  expect(await runStep(page, { ...step, saw: undefined }, 300)).toMatchObject({ status: "skip", why: "none of 1 has it" });
  await page.setContent(week('<section class="week-review"><div class="week-review-tile">8 Einheiten</div></section>'));
  expect((await runStep(page, step, 300)).status).toBe("ok");
});
