const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { waitForSettledState } = require("./helpers/settled-state");
const { waitForOlderSessions } = require("./helpers/calendar");
const { contrastRatio, showCalendarEntry, openSessionDetails } = require("./helpers/ui");

// #2530: WCAG AA text contrast of every main view and segment, collapsibles opened, in both themes.
const THEMES = ["light", "dark"];
// [view label, route, calendar view]. One test per group and theme keeps each test short.
const GROUPS = {
  "Coach und Kalender": [
    ["Coach", "coach"],
    ["Kalender Woche", "plan/overview"],
    ["Kalender Monat", "plan/overview", "month"],
    ["Kalender Bibliothek", "plan/library"],
    ["Kalender Pläne", "plan/plans"],
    ["Kalender Wettkämpfe", "plan/season"],
  ],
  Analyse: ["running", "cycling", "load", "recovery"].map((segment) => [`Analyse ${segment}`, `analysis/${segment}`]),
  "Ernährung": ["diary", "week", "body", "products"].map((segment) => [`Ernährung ${segment}`, `nutrition/${segment}`]),
  Mehr: ["appearance", "profile", "equipment", "connections", "coach", "operations"].map((segment) => [`Mehr ${segment}`, `more/${segment}`]),
};
// The active view plus the shell around it: sidebar, topbar, status strip and bottom navigation with the active item.
const SCOPES = [".panel.active", ".desktop-nav", ".topbar", "#statusStrip", ".bottom-nav"];
// Content that a collapsible loads or renders when it opens; the scan waits for it.
const READY = { "more/profile": ["#zoneOverview [data-zone-box]"], "more/operations": ["#gpsTracksToggle:enabled"] };
// Nodes axe reports as "incomplete" because it has no text to measure or cannot sample their backdrop. Each entry
// names the node and axe's message and why it is accepted; anything else fails the sweep.
const INCOMPLETE_ALLOWED = [
  {
    target: /^\.status-strip-main > \.status-strip-dismiss|^#nutrition(Previous|Next)$|\.nutrition-favorite/,
    message: /only non-text characters/,
    reason: "Icon-only buttons (a cross, week arrows, a favourite star) hold a glyph without text content; there is no text contrast to measure.",
  },
  {
    target: /svg|^text\[|^\.analysis-(date-tick|value-tick|axis-unit)\[/,
    message: /image node|overlapped by another element|too short to determine/,
    reason: "Chart axis ticks, units and value labels are SVG text drawn over the plot elements (or a one-character unit); axe reports an image node, an overlap or too little text and cannot decide.",
  },
  {
    target: /^\.analysis-point-value\[/,
    message: /image node|overlapped by another element|too short to determine/,
    reason: "A chart value label is drawn with a halo over the plot lines; when it has one or two characters, crosses a line or sits on a plot mark axe cannot decide. Its fill against its halo and the card is asserted in \"chart value labels meet AA contrast\" below for every analysis view (#2708).",
  },
  {
    target: /(^| > )tr:nth-child\(\d+\) > t[dh]/,
    message: /partially obscured/,
    reason: "Rows of a value table scrolled out of its own max-height scroll area (.analysis-chart-table); the visible rows are decided.",
  },
  {
    target: /^#group-info-\d+ > p/,
    message: /partially overlaps other elements/,
    reason: "A paragraph of the opened explanation popover on the narrow viewport; the popover is a top-layer box over the chart, whose elements axe sees overlapping it. Its other nodes are decided.",
  },
];

// Only finite animations: a spinner or pulse never ends and must not hang the test.
async function finishAnimations(page) {
  await page.evaluate(() => Promise.allSettled(document.getAnimations()
    .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
    .map((animation) => animation.finished)));
}

// The dark page background is a flat colour under two soft radial glows (at most 15 % alpha) that axe cannot flatten
// and reports every text on as undecided. The scan measures against the flat colour instead; the light theme has no glow.
// The sticky save bar and the bottom navigation stay in the flow: a line of text that straddles their edge, at the
// scroll position the scan has, makes its backdrop undecidable, and a few pixels of layout decide that (#3930).
// Their own contrast is measured at their place in the page.
async function flattenPageGlow(page) {
  await page.evaluate(() => {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync("body { background-image: none !important; } .save-bar, .bottom-nav { position: static !important; } .bottom-nav { transform: none !important; margin-inline: auto; }");
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  });
}

async function applyTheme(page, theme) {
  await page.evaluate((selected) => { document.documentElement.dataset.theme = selected; }, theme);
  await finishAnimations(page);
}

// `opened` also waits for what the opened collapsibles render.
async function settle(page, route, opened = false) {
  await waitForSettledState(page);
  const panel = page.locator(".panel.active");
  await expect(panel.locator(".loading-state, [aria-busy='true'], .skeleton")).toHaveCount(0);
  if (opened) for (const selector of READY[route] || []) await expect(panel.locator(selector).first()).toBeVisible();
}

// Opens the collapsibles of the active view and again those that opening them rendered. Calendar cards stay
// closed: they are an exclusive accordion and a session's "Details" starts a read per session.
async function openCollapsibles(page, route) {
  for (let pass = 0; pass < 5; pass += 1) {
    const opened = await page.evaluate(() => {
      const closed = [...document.querySelectorAll(".panel.active details:not([open])")].filter((element) => !element.matches(".planned-entry"));
      for (const element of closed) element.open = true;
      return closed.length;
    });
    await settle(page, route, true);
    if (!opened) return;
  }
}

// Opens the completed session that holds `match` with its "Details" panel once it has loaded.
async function openSessionWithDetails(page, match) {
  await showCalendarEntry(page, match);
  const card = page.locator(".planned-entry.is-completed").filter({ hasText: match }).first();
  await card.evaluate((element) => {
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") parent.open = true;
    }
  });
}

// The session's details dialog, measured on its own: the page behind a modal dialog is hidden, so the dialog's text sits
// on its own background (#4221).
async function expectDetailsContrast(page, view, theme, match) {
  const panel = (await openSessionDetails(page, match)).locator(".activity-analysis-panel");
  await expect(panel).not.toBeEmpty();
  await expect(panel.locator(".loading-state")).toHaveCount(0);
  await expect(panel.locator(".error-state")).toHaveCount(0);
  await page.locator("#appShell").evaluate((element) => element.style.setProperty("visibility", "hidden", "important"));
  try { await expectContrast(page, `${view} Details`, theme, ["dialog.activity-detail-dialog[open]"]); } finally {
    await page.locator("#appShell").evaluate((element) => element.style.removeProperty("visibility"));
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !history.state?.activityDetail);
  }
}

async function showView(page, route, calendarView) {
  await page.evaluate((target) => AppRouter.navigate(target, { historyMode: "replace" }), route);
  await settle(page, route);
  if (route === "plan/overview" && calendarView === "month") {
    await page.locator('.planned-view-switch [data-view="month"]').click();
    await expect(page.locator('.planned-view-switch [data-view="month"]')).toHaveAttribute("aria-pressed", "true");
    await waitForOlderSessions(page);
    // The month shows the week of a tapped day with sessions below the grid.
    await page.locator(".planned-month-day:not([disabled]):has(.planned-month-mark)").last().click();
    await waitForOlderSessions(page);
    const entry = page.locator("#plannedCalendar .planned-entry").first();
    await expect(entry).toBeVisible();
    await entry.evaluate((element) => { element.open = true; });
  } else if (route === "plan/overview") {
    await openSessionWithDetails(page, "Synthetic completed");
  }
  await openCollapsibles(page, route);
}

// #3933: one spelling per kind of value. Each pattern names the spelling it rejects; the text is what the athlete reads.
const TEXT_FORMATS = [
  [/\d\.\d{1,2} (kg|g|kcal|km|km\/h|%|W|bpm|ml|min)\b/, "a decimal point instead of a comma"],
  [/(^|[\s(])-\d/, "a hyphen as minus sign"],
  [/\d%/, "a percent sign without a space"],
  [/(^|[\s(])[-−]0(,0+)? (%|kg|W)/, "a negative zero"],
  [/\b1 (Einheiten|Messungen|Sätze|Wiederholungen|Aktivitäten|Portionen)\b/, "a plural after one"],
  [/\d{2}\.\d{2}\.\d{4},? \d{2}:\d{2}:\d{2}/, "a timestamp with seconds"],
  [/\bPortion\(en\)/, "an either-or plural"],
  [/\b[A-Z][A-Z0-9]+_[A-Z0-9_]+\b/, "an internal setting name"],
  [/\b(Ø Puls|Pulszonen|HF-Zonen|Kadenz|Power-Zonen|Kurzzeitbelastung|Session-RPE-Last|Athletenkontext|Nicht gesetzt|nicht konfiguriert)\b/, "a word the term list replaced"],
  // #3981: the two loads, the English names and the stored step notation.
  [/\b(Trainingsbelastung|Training Effect|Load-Focus|Thinking|Personal API|SDK-Login|Ultrafast|Ruhepuls|Schwellenpuls)\b/, "a word the term list replaced"],
  [/^\s*-\s*\d+(h|m|s|km|mtr)\b/, "raw step notation"],
  [/^\s*\d+x\s*$/, "a raw repeat header"],
  [/\b(Main Set|Warmup|Cooldown)\b/, "a raw step section name"],
  [/\bWeightTraining · \d/, "a sport type with a time"],
  [/\b\d{4}-\d{2}-\d{2}T\d{2}:|\|\s*active\b|Datensätze in \d+ Zeitfenstern/, "an internal timestamp or status"],
  [/(^|[^\d.])\d{2}\.\d{2}\.\d{2}($|[^\d.])/, "a two-digit year"],
];

async function expectTextFormats(page, view) {
  // The Coach context preview shows the raw JSON the Coach receives and the changelog the release notes; neither is a displayed value.
  const text = await page.locator(".panel.active").evaluate((panel) => {
    const raw = [...panel.querySelectorAll("pre, .release-changelog, .context-preview-content")];
    raw.forEach((node) => { node.hidden = true; });
    const shown = panel.innerText;
    raw.forEach((node) => { node.hidden = false; });
    return shown;
  });
  const found = text.split("\n").flatMap((line) => TEXT_FORMATS.filter(([pattern]) => pattern.test(line)).map(([, reason]) => `${view}: ${reason} in "${line.trim()}"`));
  expect(found, `value formats: ${found.join(" | ")}`).toEqual([]);
}

// #3935 X-02: the structural rules axe found on the fixture views; none may come back.
// No control sits inside a summary: the competition pencil and the conflict and Garmin buttons of a calendar card stand beside the details (#3981).
const STRUCTURE_RULES = ["nested-interactive", "label-content-name-mismatch", "scrollable-region-focusable", "page-has-heading-one", "heading-order", "landmark-unique"];

async function expectStructure(page, view) {
  const { violations } = await new AxeBuilder({ page }).withRules(STRUCTURE_RULES).analyze();
  const found = violations.flatMap((violation) => violation.nodes.map((node) => `${view}: ${violation.id} ${node.target.join(" ")}`));
  expect(found, `axe structure: ${found.join(" | ")}`).toEqual([]);
}

// #3935 X-03: on the phone every control that is not a link inside a sentence keeps a 44 px hit area.
async function expectTouchTargets(page, view) {
  const small = await page.evaluate(() => {
    const found = new Map();
    const name = (element) => {
      const first = typeof element.className === "string" ? element.className.trim().split(/\s+/)[0] : "";
      return element.tagName.toLowerCase() + (element.id ? "#" + element.id : "") + (first ? "." + first : "");
    };
    for (const element of document.querySelectorAll(".panel.active :is(button, a[href], select, summary, input:not([type=hidden], [type=checkbox], [type=radio], [type=range]), [role=button]), .bottom-nav a")) {
      if (!element.getClientRects().length || getComputedStyle(element).display === "inline" || element.closest("[hidden], .sr-only") || element.matches(".threshold-info")) continue;
      const box = element.getBoundingClientRect();
      if (box.width < 1 || box.height < 1) continue;
      if (box.height < 43.5 || box.width < 43.5) found.set(name(element), `${Math.round(box.width)}x${Math.round(box.height)}`);
    }
    return [...found].map(([key, size]) => `${key} ${size}`);
  });
  expect(small, `touch targets under 44 px: ${view}: ${small.join(" | ")}`).toEqual([]);
}

// Fails with the view, the theme and every failing node with its measured and expected contrast.
// #4108: a late status notice (or a text loading line) can move the view while axe samples a backdrop; the scan
// starts once the active panel's place and size and the page height stayed unchanged for STABLE_MS.
const STABLE_MS = 400;
async function waitForStableLayout(page) {
  // Each scan measures afresh: a key left by an earlier view must not count as stable time.
  await page.evaluate(() => { delete globalThis.__contrastLayout; });
  await page.waitForFunction((stableMs) => {
    const panel = document.querySelector(".panel.active");
    if (!panel) return false;
    const box = panel.getBoundingClientRect();
    const key = [box.top, box.height, document.documentElement.scrollHeight].map(Math.round).join(":");
    const loading = [...panel.querySelectorAll("p, div, span, small")].some((node) => !node.children.length && node.textContent.trim().endsWith("wird geladen…") && node.checkVisibility());
    const last = globalThis.__contrastLayout;
    const since = last?.key === key && !loading ? last.since : performance.now();
    globalThis.__contrastLayout = { key, since };
    return performance.now() - since >= stableMs;
  }, STABLE_MS, { polling: 100, timeout: 15_000 });
}

// A node that straddles the edge of the viewport cannot be sampled where it lies (axe reports it overlapped or
// obscured); scrolled to the middle of the screen it can. A few pixels of layout decide which nodes straddle (#4037).
const STRADDLING = /overlapped by another element|partially obscured/;

function undecidedNodes(results) {
  return results.flatMap((result) => result.nodes.map((node) => ({
    target: node.target.join(" "), message: node.any[0]?.message || result.help,
    by: (node.any[0]?.relatedNodes || []).map((related) => related.target.join(" ")).join(", "),
  })));
}

const isAccepted = ({ target, message }) => INCOMPLETE_ALLOWED.some((allowed) => allowed.target.test(target) && allowed.message.test(message));

function contrastText(view, theme, node) {
  const data = node.any.find((check) => check.id === "color-contrast")?.data || {};
  return `${view} [${theme}] ${node.target.join(" ")}: ${data.contrastRatio} < ${data.expectedContrastRatio} (${data.fgColor} on ${data.bgColor}, ${data.fontSize}, ${data.fontWeight})`;
}

// Measures each straddling node again in the middle of the screen: what it then fails is a violation, what stays
// undecided stays undecided.
async function remeasureStraddling(page, view, theme, nodes) {
  const open = [];
  const failures = [];
  for (const entry of nodes) {
    if (!STRADDLING.test(entry.message) || isAccepted(entry)) { open.push(entry); continue; }
    await page.locator(entry.target).first().evaluate((node) => node.scrollIntoView({ block: "center" }));
    const again = await new AxeBuilder({ page }).withRules(["color-contrast"]).include(entry.target).analyze();
    failures.push(...again.violations.flatMap((violation) => violation.nodes.map((node) => contrastText(view, theme, node))));
    // What lies over a node that stays undecided is the first thing to read in the failure.
    const stack = again.incomplete.length ? await page.locator(entry.target).first().evaluate((node) => {
      const box = node.getBoundingClientRect();
      const name = (over) => over.tagName.toLowerCase() + (over.id ? "#" + over.id : "") + (over.className ? "." + String(over.className).trim().split(" ")[0] : "");
      // The top element at nine points of the box: a point that is not the node itself is what overlaps it.
      const tops = [0.1, 0.5, 0.9].flatMap((row) => [0.05, 0.5, 0.95].map((column) => {
        const over = document.elementsFromPoint(box.left + box.width * column, box.top + box.height * row)[0];
        if (!over) return "none";
        return over === node ? "self" : name(over);
      }));
      return tops.join(",") + " box " + Math.round(box.left) + "," + Math.round(box.top) + " " + Math.round(box.width) + "x" + Math.round(box.height) + " of " + innerWidth + "x" + innerHeight + " fonts " + document.fonts.status;
    }) : "";
    open.push(...undecidedNodes(again.incomplete).map((node) => ({ ...node, by: node.by || stack })));
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  return { open, failures };
}

async function expectContrast(page, view, theme, scopes = SCOPES) {
  await page.mouse.move(0, 0);
  await finishAnimations(page);
  await waitForStableLayout(page);
  const builder = new AxeBuilder({ page }).withRules(["color-contrast"]);
  for (const scope of scopes) builder.include(scope);
  const { violations, incomplete, passes } = await builder.analyze();
  // A scope that matches nothing would pass without measuring anything.
  expect(passes.concat(violations).reduce((count, rule) => count + rule.nodes.length, 0), `${view} [${theme}]: no node measured`).toBeGreaterThan(0);
  const remeasured = await remeasureStraddling(page, view, theme, undecidedNodes(incomplete));
  const failures = [...violations.flatMap((violation) => violation.nodes.map((node) => contrastText(view, theme, node))), ...remeasured.failures];
  expect(failures, `colour contrast violations: ${failures.join(" | ")}`).toEqual([]);
  // The elements axe names as covering the node say what to fix.
  const undecided = remeasured.open
    .filter((entry) => !isAccepted(entry))
    .map(({ target, message, by }) => `${view} [${theme}] ${target}: ${message}` + (by ? " (by " + by + ")" : ""));
  expect(undecided, `colour contrast axe could not decide: ${undecided.join(" | ")}`).toEqual([]);
}

test.describe("colour contrast sweep", () => {
  for (const [group, views] of Object.entries(GROUPS)) {
    for (const theme of THEMES) {
      test(`@responsive ${group} meets AA contrast in the ${theme} theme`, async ({ page, request }) => {
        test.setTimeout(120_000);
        expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
        await page.goto("/");
        await page.waitForFunction(() => state.initialStateLoaded);
        await flattenPageGlow(page);
        for (const [view, route, calendarView] of views) {
          await showView(page, route, calendarView);
          await applyTheme(page, theme);
          await expectContrast(page, view, theme);
          if (route === "plan/overview" && !calendarView) await expectDetailsContrast(page, view, theme, "Synthetic completed");
          if (theme === "light") await expectTextFormats(page, view);
          if (theme === "light") await expectStructure(page, view);
          if (theme === "light" && page.viewportSize().width <= 480) await expectTouchTargets(page, view);
          if (route === "analysis/running") {
            // One opened explanation popover, named so a new chart legend cannot change it (#2766).
            await page.locator(".panel.active").getByRole("button", { name: "Entwicklung: Informationen", exact: true }).click();
            await expect(page.locator(".analysis-info-tooltip:not([popover]):not([hidden])")).toHaveCount(1);
            await expectContrast(page, `${view} Erklärung`, theme);
            await page.keyboard.press("Escape");
          }
          if (route === "analysis/load" || route === "analysis/recovery") {
            // The other period of the view (the page default is scanned above).
            await page.locator(".panel.active .analysis-period-controls:visible button[aria-pressed='false']").first().click();
            await openCollapsibles(page, route);
            await expectContrast(page, `${view} Zeitraum`, theme);
          }
        }
      });
    }
  }

  // #2708: the value labels' own contrast, measured from computed colours, whatever their length.
  test("@responsive chart value labels meet AA contrast against their halo and card in both themes", async ({ page, request }) => {
    expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
    await page.goto("/");
    await page.waitForFunction(() => state.initialStateLoaded);
    for (const [segment, theme] of ["running", "cycling", "load", "recovery"].flatMap((name) => THEMES.map((mode) => [name, mode]))) {
      await showView(page, `analysis/${segment}`);
      await applyTheme(page, theme);
      const labels = await page.locator(".panel.active .analysis-point-value").evaluateAll((nodes) => nodes.map((node) => {
        let holder = node.parentElement;
        while (holder && /rgba\([^)]*,\s*0\)|transparent/.test(getComputedStyle(holder).backgroundColor)) holder = holder.parentElement;
        const style = getComputedStyle(node);
        return { text: node.textContent, fill: style.fill, halo: style.stroke, card: getComputedStyle(holder || document.body).backgroundColor };
      }));
      if (segment === "load" || segment === "recovery") expect(labels.length, `${segment} ${theme}`).toBeGreaterThan(0);
      for (const label of labels) {
        expect(contrastRatio(label.fill, label.card), `${segment} ${theme} ${label.text} on card`).toBeGreaterThanOrEqual(4.5);
        expect(contrastRatio(label.fill, label.halo), `${segment} ${theme} ${label.text} on halo`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  // #3981 X-05: a secondary button's outline is a non-text component and keeps 3:1 against the white or grey card it sits on.
  test("@responsive secondary button borders keep 3:1 against their card in the light theme", async ({ page, request }) => {
    expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
    await page.goto("/");
    await page.waitForFunction(() => state.initialStateLoaded);
    // A border mid-transition after the theme switch is no resting colour.
    await page.evaluate(() => {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync("*, *::before, *::after { transition: none !important; }");
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    });
    for (const route of ["plan/season", "more/connections", "more/operations", "nutrition/diary"]) {
      await showView(page, route);
      await applyTheme(page, "light");
      const buttons = await page.locator(".panel.active .secondary-button:visible").evaluateAll((nodes) => nodes.map((node) => {
        let holder = node.parentElement;
        while (holder && /rgba\([^)]*,\s*0\)|transparent/.test(getComputedStyle(holder).backgroundColor)) holder = holder.parentElement;
        return { text: node.textContent.trim().slice(0, 24), border: getComputedStyle(node).borderTopColor, card: getComputedStyle(holder || document.body).backgroundColor };
      }));
      expect(buttons.length, route).toBeGreaterThan(0);
      for (const button of buttons) expect(contrastRatio(button.border, button.card), `${route} ${button.text} ${button.border} on ${button.card}`).toBeGreaterThanOrEqual(3);
    }
  });

  test.describe("login dialog", () => {
    // Start unauthenticated; the project default storage state holds the authenticated session.
    test.use({ storageState: { cookies: [], origins: [] } });

    test("@responsive meets AA contrast in both themes", async ({ page }) => {
      await page.goto("/");
      await expect(page.locator("#loginDialog")).toBeVisible();
      await flattenPageGlow(page);
      for (const theme of THEMES) {
        await applyTheme(page, theme);
        await expectContrast(page, "Anmeldung", theme, ["#loginDialog"]);
      }
    });
  });
});
