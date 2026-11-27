const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { waitForSettledState } = require("./helpers/settled-state");

async function open(page, route, ready) {
  await page.goto(`/#${route}`);
  await expect(page.locator("#appShell")).toBeVisible();
  await expect(page.locator(ready).first()).toBeVisible({ timeout: 30_000 });
}

// Interactive controls below 44 px on the route; chart bars (SVG) and controls inside closed details are excluded.
async function smallControls(page) {
  return page.evaluate(() => [...document.querySelectorAll("button, a[href], select, summary, input:not([type=hidden]):not([type=checkbox]):not([type=radio])")]
    .filter((element) => element.getClientRects().length && !(element instanceof SVGElement) && !element.closest("[hidden], [inert]"))
    .map((element) => ({ element, box: element.getBoundingClientRect() }))
    .filter(({ box }) => box.width < 43.5 || box.height < 43.5)
    .map(({ element, box }) => `${element.tagName.toLowerCase()}.${element.className} ${Math.round(box.width)}x${Math.round(box.height)} ${(element.getAttribute("aria-label") || element.textContent).trim().slice(0, 30)}`));
}

test.beforeEach(async ({ page }) => {
  expect((await page.request.get("/api/fixture/demo")).ok()).toBe(true);
  await page.addInitScript(() => { try { localStorage.setItem("intervals-coach-appearance", "light"); } catch { /* storage unavailable */ } });
});

test("@responsive light analysis load has no axe violation and no control below 44 px", async ({ page }) => {
  test.setTimeout(120_000);
  await open(page, "analysis/load", "#sessionPerformance h3");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  const focus = page.locator("#sessionPerformance");
  await expect(focus.locator(".training-focus-legend strong").first()).toBeVisible();
  // #2973: both colours from one moment, retried; one early h3 sample could stay unreachable.
  const colours = () => focus.evaluate((root) => [":scope > h3", ".training-focus-legend strong"].map((selector) => {
    const node = root.querySelector(selector);
    return node ? getComputedStyle(node).color : `no ${selector}`; // mid re-render: try again
  }));
  await expect.poll(async () => {
    const [heading, legend] = await colours();
    return legend === heading ? "same" : `legend ${legend}, heading ${heading}`;
  }).toBe("same");
  expect((await new AxeBuilder({ page }).include("#dataPanel").analyze()).violations).toEqual([]);
  expect(await smallControls(page)).toEqual([]);
  await page.goto("/#analysis/running");
  await expect(page.locator("#performancePredictions .race-predictions-table")).toBeVisible({ timeout: 30_000 });
  expect(await smallControls(page)).toEqual([]);
});

test("@responsive derived values carry no estimate chip and race predictions show seconds only for 5 and 10 km", async ({ page }) => {
  test.setTimeout(120_000);
  await open(page, "analysis/running", "#performancePredictions .race-predictions-table");
  const predictions = page.locator("#performancePredictions");
  await expect(predictions.getByRole("heading", { name: "Laufprognosen", exact: true })).toBeVisible();
  await expect(page.locator("#dataPanel .estimate-chip")).toHaveCount(0);
  const rows = await predictions.locator("tbody tr").evaluateAll((items) => items.map((row) => [row.querySelector("th").textContent, row.querySelector("td").textContent]));
  expect(rows.length).toBeGreaterThan(0);
  // #2263, #4194: 5 and 10 km as m:ss, the longer races as h:mm:ss on the clock, none with a unit.
  for (const [distance, time] of rows) expect(time).toMatch(["5 km", "10 km"].includes(distance) ? /^(\d{1,2}:\d{2}|\d:\d{2}:\d{2})$/ : /^\d{1,2}:\d{2}:\d{2}$/);
  await open(page, "analysis/load", "#sessionPerformance h3");
  for (const title of [/^RPE-Belastung/, /^RPE-Belastung kurz und lang/]) {
    await expect(page.getByRole("heading", { name: title }).first().locator(".estimate-chip")).toHaveCount(0);
  }
  // Every legend entry names a series that is drawn: a series without a line has no entry (#3928). The dash of a legend
  // entry is pinned by the analysis-pace golden of tests/chart-golden.test.cjs.
  const unmatched = await page.locator("#dataPanel .analysis-chart-card").evaluateAll((cards) => cards.flatMap((card) => {
    const entries = card.querySelectorAll(".analysis-chart-legend li").length;
    const drawn = new Set([...card.querySelectorAll("svg [data-series]")].map((node) => node.dataset.series)).size;
    return entries && entries !== drawn ? [`${card.querySelector("h3")?.textContent}: ${entries} entries, ${drawn} drawn`] : [];
  }));
  expect(unmatched).toEqual([]);
});

// A-02, A-03 (#3928): beyond 3 months all four Erholung charts plot the mean of each calendar week and say so; the
// readiness axis is 0 to 100; the table behind a chart has real columns, newest week first, and opens its older rows.
test("@responsive recovery at 12 Monate plots weekly means for every chart and lists them in a table of columns", async ({ page }) => {
  test.setTimeout(120_000);
  await open(page, "analysis/recovery", "#personalRecovery .analysis-subchart");
  // The longer history arrives once and draws the charts again; read it before anything is opened.
  const history = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/analysis/trend-history");
  await page.locator("#personalRecovery .analysis-period-controls").getByRole("button", { name: "12 Monate", exact: true }).click();
  await history;
  const cards = page.locator("#personalRecovery .analysis-subchart");
  await expect(cards.first().locator(".analysis-week-caption")).toHaveText("Woche ab", { timeout: 30_000 });
  const captions = await cards.evaluateAll((nodes) => nodes.map((card) => ({
    title: card.querySelector("h3")?.textContent,
    caption: card.querySelector("svg .analysis-axis-unit")?.textContent,
    week: Boolean(card.querySelector(".analysis-week-caption")),
  })));
  expect(captions.length).toBeGreaterThanOrEqual(3);
  for (const { title, caption, week } of captions) {
    expect(caption, `${title} names what a point is`).toContain("Wochenmittel");
    expect(week, `${title} dates its points by week start`).toBe(true);
  }
  const readiness = cards.filter({ has: page.getByRole("heading", { name: /^Trainingsbereitschaft/ }) });
  if (await readiness.count()) {
    await expect(readiness.locator(".analysis-value-tick")).toHaveText(["0", "25", "50", "75", "100"]);
  }
  // The demo's sleep history is the longest of the four.
  const chart = cards.filter({ has: page.getByRole("heading", { name: /^Schlafdauer/ }) });
  const details = chart.locator("details").filter({ has: page.getByText("Werte ansehen", { exact: true }) });
  await expect(async () => {
    if (!(await details.evaluate((node) => node.open))) await details.locator(":scope > summary").click();
    await expect(chart.locator("tbody tr:visible")).toHaveCount(6, { timeout: 1000 });
  }).toPass();
  await expect(chart.locator("thead th").first()).toHaveText("Woche ab");
  const dates = await chart.locator("tbody tr th").allTextContents();
  const keys = dates.map((text) => text.split(".").reverse().join("-"));
  expect(keys).toEqual([...keys].sort((a, b) => b.localeCompare(a)));
  await chart.getByRole("button", { name: /^Alle \d+ Wochen anzeigen$/ }).click();
  await expect(chart.locator("tbody tr:visible")).toHaveCount(dates.length);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive nutrition diary shows calories as hero with one equal macro row", async ({ page }) => {
  await open(page, "nutrition/diary", "#nutritionTotals .nutrition-total");
  const tiles = page.locator("#nutritionTotals .nutrition-total");
  // Zucker and Salz are one line below, not tiles (#4300).
  await expect(tiles).toHaveCount(4);
  await expect(tiles.first()).toHaveClass(/is-hero/);
  const boxes = await tiles.evaluateAll((nodes) => nodes.map((node) => {
    const box = node.getBoundingClientRect();
    const value = node.querySelector("strong").getBoundingClientRect();
    return { top: Math.round(box.top), height: Math.round(box.height), width: Math.round(box.width), valueHeight: Math.round(value.height) };
  }));
  expect(boxes[1].top).toBe(boxes[2].top);
  expect(boxes[2].top).toBe(boxes[3].top);
  expect(boxes[1].height).toBe(boxes[2].height);
  expect(boxes[2].height).toBe(boxes[3].height);
  expect(boxes[0].width).toBeGreaterThan(boxes[1].width * 2);
  // One line per value: the unit does not wrap below the number.
  expect(boxes[0].valueHeight).toBeLessThan(60);
  await expect(tiles.first().locator(".nutrition-unit")).toHaveText("kcal");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive the diary toolbar lines up with the date field and sugar and salt are one full-width line (#4300)", async ({ page }) => {
  const edges = () => page.evaluate(() => {
    const bottom = (selector) => Math.round(document.querySelector(selector).getBoundingClientRect().bottom * 10) / 10;
    return { input: bottom("#nutritionDate"), buttons: ["#nutritionPrevious", "#nutritionNext", "#nutritionToday"].map(bottom) };
  });
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 800 });
    await open(page, "nutrition/diary", "#nutritionTotals .nutrition-total");
    const { input, buttons } = await edges();
    // Side by side the buttons end where the date field ends; wrapped below the day (narrow), they share one row.
    const [first, ...rest] = buttons;
    for (const edge of rest) expect(Math.abs(edge - first)).toBeLessThanOrEqual(1);
    if (width >= 390) expect(Math.abs(first - input)).toBeLessThanOrEqual(1);
    const boxes = await page.evaluate(() => ["#nutritionTotals .is-hero", "#nutritionTotals .nutrition-minor-line"].map((selector) => Math.round(document.querySelector(selector).getBoundingClientRect().width)));
    expect(Math.abs(boxes[1] - boxes[0])).toBeLessThanOrEqual(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
});

test("@responsive nutrition history collapses days without data and products keep one primary action", async ({ page }) => {
  await open(page, "nutrition/week", "#nutritionWeek .nutrition-week-chart-day");
  const empty = page.locator("#nutritionWeek .nutrition-week-chart-day.is-empty");
  await expect(empty.first()).toContainText("keine Daten");
  // #4339: the first chart row can show before the week's data settles; poll the heights, which names both in a failure.
  await waitForSettledState(page);
  await expect.poll(() => page.locator("#nutritionWeek .nutrition-week-chart-day").evaluateAll((nodes) => {
    const heights = nodes.map((node) => [node.classList.contains("is-empty"), node.getBoundingClientRect().height]);
    const emptyHeight = Math.max(...heights.filter(([isEmpty]) => isEmpty).map(([, height]) => height));
    const fullHeight = Math.min(...heights.filter(([isEmpty]) => !isEmpty).map(([, height]) => height));
    return emptyHeight < fullHeight / 1.5 ? "collapsed" : `empty ${emptyHeight} px, full ${fullHeight} px`;
  })).toBe("collapsed");
  expect(await smallControls(page)).toEqual([]);
  await open(page, "nutrition/products", "#nutritionBarcodeButton");
  const actions = page.locator(".nutrition-tools > .nutrition-actions");
  await expect(actions.locator(".push-button")).toHaveCount(1);
  const widths = await actions.locator("button").evaluateAll((nodes) => nodes.map((node) => Math.round(node.getBoundingClientRect().width)));
  expect(widths[1]).toBe(widths[2]);
  expect(widths[0]).toBeGreaterThan(widths[1]);
  await expect(page.getByText("Nährwerttabelle auswählen", { exact: true })).toHaveCount(0);
});

test("@responsive desktop uses a readable text size, two charts per row and a grouped rail", async ({ page }) => {
  test.skip(!(page.viewportSize().width >= 1100), "Desktop layout");
  await open(page, "analysis/load", "#sessionPerformance h3");
  const metrics = await page.evaluate(() => {
    const cards = [...document.querySelectorAll("#analysisLoadCharts > .analysis-chart-card")];
    const noteSize = parseFloat(getComputedStyle(document.querySelector(".analysis-chart-card .analysis-chart-note, .analysis-chart-card p, .analysis-chart-card summary")).fontSize);
    const chartText = parseFloat(getComputedStyle(document.querySelector(".analysis-chart-card > svg text")).fontSize) * (document.querySelector(".analysis-chart-card > svg").getBoundingClientRect().width / document.querySelector(".analysis-chart-card > svg").viewBox.baseVal.width);
    const rail = [...document.querySelectorAll(".desktop-nav .nav-item")].map((item) => Math.round(item.getBoundingClientRect().top));
    return { firstRow: cards.slice(0, 2).map((card) => Math.round(card.getBoundingClientRect().top)), noteSize, chartText, rail };
  });
  expect(metrics.firstRow[0]).toBe(metrics.firstRow[1]);
  expect(metrics.noteSize).toBeGreaterThanOrEqual(14.5);
  expect(metrics.chartText).toBeGreaterThanOrEqual(11);
  expect(metrics.rail.at(-1) - metrics.rail.at(-2)).toBeLessThan(80);
  await open(page, "more/profile", "#profileForm");
  const { widest, limit } = await page.evaluate(() => ({
    widest: Math.max(...[...document.querySelectorAll("#profileForm .profile-section[open] > label")].map((node) => node.getBoundingClientRect().width)),
    limit: 40 * parseFloat(getComputedStyle(document.documentElement).fontSize),
  }));
  expect(widest).toBeLessThanOrEqual(limit + 1);
});

test("@responsive desktop Analyse rows: two cards at equal height, a lone card across the row", async ({ page }) => {
  // The athlete's rule (#3154): no half-width card beside an empty half, no ragged pair.
  test.skip(!(page.viewportSize().width >= 1100), "Desktop layout");
  const seen = { pairs: 0, lone: 0 };
  for (const [tab, ready] of [["running", "#currentPerformance table[data-analysis-sport='run']"], ["cycling", "#currentPerformance table[data-analysis-sport='ride']"], ["load", "#trainingZoneCharts .analysis-chart-card"], ["recovery", "#personalRecovery svg"]]) {
    await open(page, `analysis/${tab}`, ready);
    const rows = await page.evaluate(() => [...document.querySelectorAll("#dataPanel .analysis-history-charts")].filter((grid) => grid.offsetParent).flatMap((grid) => {
      const width = grid.getBoundingClientRect().width;
      const byTop = new Map();
      for (const kid of [...grid.children].filter((node) => node.offsetParent)) {
        const box = kid.getBoundingClientRect();
        byTop.set(Math.round(box.top), [...(byTop.get(Math.round(box.top)) || []), { width: box.width / width, height: box.height }]);
      }
      return [...byTop.values()];
    }));
    for (const row of rows) {
      expect(row.length, tab).toBeLessThanOrEqual(2);
      if (row.length === 2) {
        seen.pairs += 1;
        expect(Math.abs(row[0].height - row[1].height), tab).toBeLessThanOrEqual(1);
      } else {
        seen.lone += 1;
        expect(row[0].width, tab).toBeGreaterThan(0.99);
      }
    }
  }
  // The fixture's Belastung holds both cases, so neither rule passes vacuously.
  expect(seen.pairs).toBeGreaterThan(0);
  expect(seen.lone).toBeGreaterThan(0);
  // Inside a combined card the same rule holds: an odd last chart spans the card. The fixture's
  // combined cards hold even counts, so one more chart is added for the measurement.
  await open(page, "analysis/recovery", "#personalRecovery .analysis-chart-stack .analysis-subchart");
  const widths = await page.evaluate(() => {
    const stack = document.querySelector("#personalRecovery .analysis-chart-stack");
    const charts = stack.querySelectorAll(":scope > .analysis-subchart");
    stack.append(charts[0].cloneNode(true));
    const all = [...stack.querySelectorAll(":scope > .analysis-subchart")];
    return { first: all[0].getBoundingClientRect().width, last: all.at(-1).getBoundingClientRect().width, count: all.length };
  });
  expect(widths.count % 2).toBe(1);
  // A paired chart takes half the row less the gap; the lone last one the whole row.
  expect(widths.last).toBeGreaterThan(widths.first * 1.9);
});
