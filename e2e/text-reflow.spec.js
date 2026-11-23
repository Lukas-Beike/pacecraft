const { test, expect } = require("@playwright/test");

const routes = [
  ["plan/season", (page) => page.locator("#seasonPreparation .competition-card").first()],
  ["nutrition/diary", (page) => page.locator("#nutritionTotals .nutrition-total").first()],
  ["nutrition/week", (page) => page.locator("#nutritionWeek .nutrition-week-chart-day").first()],
  ["nutrition/products", (page) => page.locator("#nutritionProductsPanel")],
  ["analysis/load", (page) => page.locator("#sessionPerformance").getByRole("heading", { name: /^Trainingsfokus/ })],
  ["analysis/running", (page) => page.locator("#performancePredictions .race-predictions-table")],
  ["analysis/recovery", (page) => page.locator("#personalRecovery .analysis-chart-card").first()],
];

test("@responsive navigation and banners keep their text inside at 200 % text size", async ({ page }) => {
  await page.goto("/#coach");
  await page.waitForFunction(() => state.initialStateLoaded);
  // Banner content and measurement share one evaluate so a sync poll cannot hide a banner in between.
  const measure = () => page.evaluate(() => {
    document.documentElement.style.fontSize = "200%";
    const action = { href: "#more/connections", label: "Anbindungen öffnen" };
    updateStatusSources({
      offline: { level: "warning", title: "Offline", detail: "Änderungen können gerade nicht gespeichert werden.", stand: true },
      coach_error: { level: "critical", title: "Coach benötigt Aufmerksamkeit", detail: "Das OpenAI-Guthaben ist aufgebraucht. Lade es im OpenAI-Konto auf." },
      provider: { level: "critical", title: "Anbindung benötigt Aufmerksamkeit", detail: "2 Anbindungen benötigen manuelles Eingreifen.", action },
      intervals_key: { level: "info", title: "Intervals.icu-API-Schlüssel fehlt", action, dismiss() {} },
    });
    document.querySelector("#statusStrip").dataset.expanded = "true";
    renderStatusStrip();
    const banners = [document.querySelector("#statusStrip")];
    const nav = [".bottom-nav", ".desktop-nav"].map((selector) => document.querySelector(selector)).filter((element) => element.getClientRects().length);
    const problems = nav.length === 1 ? [] : [`${nav.length} navigation bars visible`];
    for (const container of [...nav, ...banners]) {
      const box = container.getBoundingClientRect();
      if (!box.width) problems.push(`${container.id || container.className} not rendered`);
      if (box.left < 0 || box.right > document.documentElement.clientWidth + 1) problems.push(`${container.id || container.className} leaves the viewport`);
      for (const element of [container, ...container.querySelectorAll("*")]) {
        if (!element.getClientRects().length || element instanceof SVGElement) continue;
        // The low-screen strip cuts its one line with an ellipsis on purpose; the button opens the full text.
        if (getComputedStyle(element).textOverflow === "ellipsis") continue;
        // Inline labels report no scroll size, so their box must also stay inside the container.
        const inner = element.getBoundingClientRect();
        if (element.scrollWidth > element.clientWidth + 1 || inner.left < box.left - 1 || inner.right > box.right + 1) problems.push(`${container.id || container.className} > ${element.tagName.toLowerCase()}.${element.className} "${element.textContent.trim().slice(0, 30)}"`);
      }
    }
    return problems;
  });
  expect(await measure()).toEqual([]);
  // Phone projects also cover the 560-767 px band, where the bottom bar uses its larger label size.
  if (page.viewportSize().width < 560) {
    await page.setViewportSize({ width: 600, height: 900 });
    expect(await measure(), "600 px").toEqual([]);
  }
});

test("@responsive pages do not scroll horizontally at 200 % text size", async ({ page, request }) => {
  test.setTimeout(180_000);
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  expect((await request.get("/api/fixture/features")).ok()).toBeTruthy();
  await page.goto("/#coach");
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
  for (const [route, ready] of routes) {
    await page.evaluate((target) => AppRouter.navigate(target, { historyMode: "replace" }), route);
    await expect(ready(page), route).toBeVisible();
    if (route === "nutrition/week") await page.locator("#nutritionWeek").getByText("Tageswerte anzeigen", { exact: true }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), route).toBeLessThanOrEqual(0);
  }
});

// #3935 X-01: at 200 % text on the phone no control is cut off, runs past the edge or breaks inside a word
// that would fit on its own line. The check reads boxes, not pixels, so a hidden clip is found too.
const WHOLE_SCREENS = [
  "more/appearance", "more/profile", "more/equipment", "more/connections", "more/coach", "more/operations",
  "plan/season", "nutrition/products", "nutrition/diary", "analysis/records",
];

// Small helpers, installed once in the page so each check below stays short.
function installReflowHelpers() {
  const panel = () => document.querySelector(".panel.active");
  const scrolls = (element) => /(auto|scroll)/.test(getComputedStyle(element).overflowX);
  const name = (element) => {
    const first = typeof element.className === "string" ? element.className.trim().split(/\s+/)[0] : "";
    return element.tagName.toLowerCase() + (element.id ? "#" + element.id : "") + (first ? "." + first : "");
  };
  const inScroll = (element) => {
    for (let node = element.parentElement; node && node !== panel(); node = node.parentElement) if (scrolls(node)) return true;
    return false;
  };
  const text = (node, length) => (node.textContent || "").trim().slice(0, length);
  globalThis.reflow = { panel, scrolls, name, inScroll, text };
}

function boxProblems() {
  const { panel, scrolls, name, inScroll, text } = globalThis.reflow;
  const viewport = document.documentElement.clientWidth;
  const hidden = (element) => !element.getClientRects().length || element instanceof SVGElement;
  // Inside a scroll box the table may be wider than the box, but no cell or button may cut its own text.
  const cutInScroll = (element) => inScroll(element) && !hidden(element) && getComputedStyle(element).overflowX !== "visible" && element.scrollWidth > element.clientWidth + 1 && !element.matches(".sr-only, pre");
  const exempt = (element) => hidden(element) || inScroll(element) || element.matches(".sr-only, pre, input, textarea, select, .threshold-info");
  const clipped = (element, style) => element.scrollWidth > element.clientWidth + 1 && !scrolls(element) && style.textOverflow !== "ellipsis" && style.display !== "inline";
  const clipDetail = (element, box) => {
    const edge = box.right + 1;
    const over = [...element.querySelectorAll("*")].filter((inner) => [...inner.getClientRects()].some((rect) => rect.right > edge));
    const leaves = over.filter((inner) => !over.some((other) => other !== inner && inner.contains(other))).slice(0, 3);
    const by = leaves.map((inner) => name(inner) + ' "' + text(inner, 24) + '" w=' + Math.round(inner.getBoundingClientRect().width)).join(", ");
    return name(element) + " clips " + (element.scrollWidth - element.clientWidth) + 'px (own "' + text(element, 28) + '" w=' + Math.round(box.width) + ")" + (by ? " by " + by : "");
  };
  const problemOf = (element) => {
    if (cutInScroll(element)) return name(element) + " in a scroll box clips " + (element.scrollWidth - element.clientWidth) + 'px ("' + text(element, 20) + '")';
    if (exempt(element)) return null;
    const box = element.getBoundingClientRect();
    if (!box.width) return null;
    if (box.right > viewport + 1 || box.left < -1) return name(element) + " leaves the screen (" + Math.round(box.left) + ".." + Math.round(box.right) + " of " + viewport + ")";
    return clipped(element, getComputedStyle(element)) ? clipDetail(element, box) : null;
  };
  return [...panel().querySelectorAll("*")].map(problemOf).filter(Boolean);
}

function wordProblems() {
  const { panel, name } = globalThis.reflow;
  const texts = [];
  const walker = document.createTreeWalker(panel(), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) texts.push(node);
  const block = (node) => {
    let parent = node.parentElement;
    while (parent && getComputedStyle(parent).display.startsWith("inline")) parent = parent.parentElement;
    return parent;
  };
  const brokenIn = (node, container, match) => {
    const range = document.createRange();
    range.setStart(node, match.index);
    range.setEnd(node, match.index + match[0].length);
    const pieces = [...range.getClientRects()];
    const lines = new Set(pieces.map((piece) => Math.round(piece.top))).size;
    return lines > 1 && pieces.reduce((sum, piece) => sum + piece.width, 0) <= container.clientWidth - 2;
  };
  const inVisibleText = (node) => node.parentElement.getClientRects().length && !(node.parentElement instanceof SVGElement) && !node.parentElement.closest("pre, .sr-only");
  return texts.filter(inVisibleText).flatMap((node) => {
    const container = block(node);
    return [...node.textContent.matchAll(/[^\s\-/]{5,}/g)].filter((match) => brokenIn(node, container, match)).map((match) => '"' + match[0] + '" breaks inside the word in ' + name(container));
  });
}

async function wholeScreenProblems(page) {
  await page.evaluate(installReflowHelpers);
  const found = [...await page.evaluate(boxProblems), ...await page.evaluate(wordProblems)];
  return [...new Set(found)];
}

test("@responsive whole screens keep their controls and words intact at 200 % text size", async ({ page, request }, testInfo) => {
  test.skip(page.viewportSize().width > 480, "the phone layout is the one the text size breaks");
  test.setTimeout(240_000);
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  expect((await request.get("/api/fixture/features")).ok()).toBeTruthy();
  await page.goto("/#coach");
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.evaluate(() => document.documentElement.style.setProperty("font-size", "200%", "important"));
  const found = {};
  for (const route of WHOLE_SCREENS) {
    await page.evaluate((target) => AppRouter.navigate(target, { historyMode: "replace" }), route);
    await page.waitForTimeout(600);
    await page.evaluate(() => document.querySelectorAll(".panel.active details:not([open])").forEach((element) => { if (!element.matches(".planned-entry")) element.open = true; }));
    // A race plan is read when its card opens; the screen is measured with it, not before.
    await page.waitForFunction(() => !document.querySelector(".panel.active [data-state='loading']"));
    await page.waitForTimeout(400);
    const problems = await wholeScreenProblems(page);
    if (problems.length) {
      found[route] = problems;
      await page.screenshot({ path: testInfo.outputPath(`${route.replace("/", "-")}.png`), fullPage: true });
    }
  }
  await testInfo.attach("reflow", { body: JSON.stringify(found, null, 1), contentType: "application/json" });
  const lines = Object.entries(found).map(([route, problems]) => `${route}: ${problems.slice(0, 6).join("; ")}`);
  expect(lines, lines.join(" | ")).toEqual([]);
});

// The threshold names and values of the profile stay inside their box at the normal text size too: a header word wider
// than its column ran past the box edge and left its backdrop undecidable for the contrast sweep.
test("@responsive the profile's threshold grid stays inside its box at the phone width", async ({ page, request }) => {
  test.skip(page.viewportSize().width > 480, "the phone layout is the one that is tight");
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  await page.goto("/#more/profile");
  await page.waitForFunction(() => state.initialStateLoaded);
  // The zones load when their section opens.
  await page.evaluate(() => document.querySelectorAll(".panel.active details:not([open])").forEach((element) => { element.open = true; }));
  await expect(page.locator("[data-zone-box='thresholds']").first()).toBeVisible();
  const outside = await page.locator("[data-zone-box='thresholds']").evaluateAll((boxes) => boxes.flatMap((box) => {
    const edge = box.getBoundingClientRect();
    return [...box.querySelectorAll(".zone-thresholds > *")].filter((cell) => {
      // The text and inline content of the cell, not the enlarged tap area of its info button.
      const content = document.createRange();
      content.selectNodeContents(cell);
      const own = content.getBoundingClientRect();
      const cellBox = cell.getBoundingClientRect();
      return own.width && (own.right > edge.right + 0.5 || own.right > cellBox.right + 0.5);
    }).map((cell) => cell.textContent.trim().slice(0, 24));
  }));
  expect(outside).toEqual([]);
});
