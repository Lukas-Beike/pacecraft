const { test, expect } = require("@playwright/test");

// Playwright's messages for a request whose page went away (#3449).
const GONE = /Response has been disposed|Target page, context or browser has been closed/;

// The read is shaped here (the values come from the method tests); `shape` gets it and the diary day.
// A read still in flight when its page goes away needs no answer (#4115, as #3449); any other error fails.
async function shapeEnergy(page, shape) {
  await page.route("**/api/body/energy*", async (route) => {
    const day = new URL(route.request().url()).searchParams.get("date");
    try {
      const response = await route.fetch();
      await route.fulfill({ response, json: shape(await response.json(), day) });
    } catch (error) {
      if (!GONE.test(String(error))) throw error;
    }
  });
}

const formulaRow = (source, split, extra = {}) => ({ source, resting_kcal: split.resting_kcal, resting_band_kcal: 200, total_kcal: 2720, total_band_kcal: 210,
  errors_kcal: [], mean_error_kcal: null, pending_parts: [], split, ...extra });
const GOAL = { goal: "lose", goal_kcal: 300, source: "cunningham", total_kcal: 2720, target_kcal: 2420, eaten_kcal: 1400, remaining_kcal: 1020 };

// #3099, #3252, #3703: the diary's energy card shows "Offene Kalorien", the outlined source's parts
// with its day total and one source line, and no table of sources; the explanation sits behind the "i".
// The fixture has no skinfold: with a weigh-in and a body fat entered in Körper the formulas
// fall back to the entered body fat and name it (#3362, #3402; the profile holds none, #3640).
test("@responsive the diary shows Offene Kalorien and the outlined source's parts, with no source table", async ({ page }) => {
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#nutritionEnergy [data-energy]")).toBeVisible();
  await page.evaluate(async () => {
    const day = new Date(Date.now() - 3 * 864e5).toISOString().slice(0, 10);
    await api("/api/body/weight", { method: "PUT", body: JSON.stringify({ date: day, kg: 72 }) });
    await api("/api/body/body-fat", { method: "PUT", body: JSON.stringify({ date: day, pct: 18.5 }) });
  });
  await shapeEnergy(page, (data) => ({ ...data, day_partial: true, outlined: "cunningham", best: null, goal: GOAL, wellness: { as_of: "12:18", behind: false },
    strength: [{ rpe: 8, met: 5, kcal: 210 }], tef_estimated: true,
    // #3723: a provisional estimate, not yet qualified, as the row below counts it.
    learned_everyday: { katch_mcardle: null, cunningham: { kcal: 220, raw_kcal: 220, band_kcal: 180, windows: 2, independent: 1, qualified: false }, checked: null },
    rows: [...data.rows.filter((row) => row.source !== "cunningham"), formulaRow("cunningham", { resting_kcal: 1700, neat_kcal: 250, learned_kcal: 220, learned_band_kcal: 180, learned_provisional: true, sport_kcal: 350, tef_kcal: 200 })] }));
  const energy = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/body/energy");
  await page.reload();
  const read = await (await energy).json();
  // The body fat entered in Körper, named "Lokal" (#3402).
  expect(read.body.source).toBe("entered");
  expect(read.body.label).toBe("Lokal");
  const card = page.locator("#nutritionEnergy [data-energy]");
  await expect(card.locator(".energy-goal > p")).toHaveText("Offene Kalorien1.020 kcal");
  await expect(card.locator(".energy-goal progress")).toHaveAttribute("max", "2420");
  await expect(card.locator(".energy-goal-items li")).toHaveText(["gegessen 1.400", "Ziel heute 2.420 (300 Defizit)"]);
  await expect(card.locator(".energy-usage h3")).toHaveText("Verbrauch heute (Stand jetzt)");
  await expect(card.locator(".energy-split [data-part]")).toHaveCount(5);
  await expect(card.locator(".energy-parts li")).toHaveText([
    // #3719: Schritte & Treppen and Verdauung show the number only; the "i" names the time and the estimate.
    // #3723: the provisional learned everyday is named so and in the total, 1.700 + 250 + 220 + 350 + 200.
    "Ruhe1.700", "Schritte & Treppen250", "Übriger Alltag (vorläufig)+220", "Sport350 (Kraft RPE 8, MET 5)", "Verdauung200", /^Tag gesamt2\.720/,
  ]);
  // #3478: the day total with its low–high span on one line below it, and no "±" left in the energy views.
  const shown = card.locator(".energy-total .energy-span [aria-hidden]");
  await expect(shown).toHaveText("2.510–2.930");
  await expect(card.locator(".energy-total .energy-span .sr-only")).toHaveText("zwischen 2.510 und 2.930 kcal");
  const spanLines = await shown.evaluate((node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return new Set([...range.getClientRects()].map((rect) => Math.round(rect.top))).size;
  });
  expect(spanLines).toBe(1);
  await expect(card.locator(".energy-source")).toHaveText("Quelle: Cunningham mit übrigem Alltag");
  // The row already names it "vorläufig", so no learned line follows (#3723).
  await expect(card.locator(".energy-learned")).toHaveCount(0);
  // For the PR's screenshot review (#3723).
  await card.screenshot({ path: test.info().outputPath("Tagebuch-vorlaeufig.png") });
  await expect(card).not.toContainText("±");
  await expect(page.locator("#nutritionEnergy table")).toHaveCount(0);
  await expect(card.locator(".analysis-reference-note")).toHaveCount(0);
  const tooltip = card.locator(".analysis-info-tooltip");
  await expect(tooltip).toBeHidden();
  // #3336: the same small info icon as Analyse › Laufen › Entwicklung, not a yellow badge.
  const info = card.getByRole("button", { name: "Energieverbrauch: Informationen" });
  await expect(info).toHaveClass("analysis-legend-info analysis-info-icon");
  // The card re-renders when its read arrives, so the style check retries on the current node.
  await expect(info).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  // A late diary render keeps the shown card, so one click opens the popover for good (#3379).
  await info.click();
  await expect(tooltip).toBeVisible();
  await page.evaluate(() => renderDiaryEnergy());
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText("Datenbasis zu gering");
  await expect(tooltip).toContainText(`(${read.body.label}`);
  await expect(tooltip).toContainText("Schritte & Treppen nach dem letzten Garmin-Abruf, Stand 12:18");
  await expect(tooltip).toContainText("Verdauung geschätzt, wo Mahlzeiten keine Makros haben");
  await expect(page.locator("#nutritionExpenditure")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3703: without an outlined source the card names the heading and says so, with no parts.
test("@responsive the diary card says Noch keine Quelle without an outlined source", async ({ page }) => {
  await shapeEnergy(page, (data) => ({ ...data, day_partial: true, outlined: null, best: null }));
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEnergy [data-energy]");
  await expect(card.locator(".energy-usage h3")).toHaveText("Verbrauch heute (Stand jetzt)");
  await expect(card.locator(".energy-usage")).toContainText("Noch keine Quelle");
  await expect(card.locator(".energy-parts")).toHaveCount(0);
  await expect(page.locator("#nutritionEnergy table")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3577: the learned term is offered under the card; "Übernehmen" stores the dated
// check in the profile with a receipt and undo, and the formula rows then carry it.
test("@responsive the learned Übriger Alltag is taken over with a receipt and undo", async ({ page }) => {
  // #3689: offered once it qualifies (here 3 stable windows that share no day).
  const fit = (kcal) => ({ kcal, raw_kcal: kcal, band_kcal: 180, windows: 4, independent: 3, qualified: true });
  await page.route("**/api/body/energy*", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    const profile = await (await page.request.get("/api/profile")).json();
    const checked = (profile.profile || profile).learned_everyday_checked || null;
    // #3723: before the check the row counts it provisionally, after it with its sign.
    const rows = data.rows.map((row) => (row.source === "katch_mcardle"
      ? { ...row, split: { ...(row.split || { resting_kcal: 1839, neat_kcal: 220, sport_kcal: 0, tef_kcal: 250, shared: {} }), learned_kcal: 480, learned_band_kcal: 180, learned_provisional: !checked } }
      : row));
    await route.fulfill({ response, json: { ...data, rows, outlined: "katch_mcardle", learned_everyday: { katch_mcardle: fit(480), cunningham: fit(330), checked } } });
  });
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEnergy [data-energy]");
  const offered = card.locator("[data-learned='offered']");
  await expect(offered).toContainText("Übriger Alltag aus 4 Fenstern");
  await expect(card.locator(".energy-parts [data-part='learned']")).toHaveText("Übriger Alltag (vorläufig)+480");
  // "Noch nicht" stores nothing and hides the buttons for this view (Gitar on #3637).
  await offered.getByRole("button", { name: "Noch nicht" }).click();
  await expect(card.locator("[data-learned='offered'] button")).toHaveCount(0);
  await expect(card.locator("[data-learned='offered']")).toContainText("Übriger Alltag aus 4 Fenstern");
  await page.reload();
  await expect(offered).toContainText("Katch-McArdle +480 kcal/Tag (300–660) · Cunningham +330 (150–510)");
  await offered.getByRole("button", { name: "Übernehmen" }).click();
  await expect(page.locator("#toast")).toContainText("Übriger Alltag übernommen.");
  await expect(card.locator("[data-learned='checked']")).toContainText("übernommen am");
  await expect(card.locator(".energy-split [data-part='learned']")).toHaveCount(1);
  await expect(card.locator(".energy-parts [data-part='learned']")).toHaveText("Übriger Alltag+480");
  await expect(card.locator(".energy-source")).toHaveText("Quelle: Katch-McArdle mit übrigem Alltag");
  await page.locator("#toast").getByRole("button", { name: "Rückgängig" }).click();
  await expect(card.locator("[data-learned='offered']")).toBeVisible();
  await expect(card.locator(".energy-parts [data-part='learned']")).toHaveText("Übriger Alltag (vorläufig)+480");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3397: which source meets the weight moved to Verlauf; tapping the body-fat row opens the
// body fat dialog (#3402).
test("@responsive Körper opens the body fat dialog from body fat and no longer holds the fit", async ({ page }) => {
  await page.goto("/#nutrition/body");
  const root = page.locator("#bodyAnalysisCharts");
  await expect(root.locator("[data-body-overview]")).toBeVisible();
  await expect(root.locator("[data-energy-fit]")).toHaveCount(0);
  await expect(root.locator("[data-energy]")).toHaveCount(0);
  await root.locator("[data-body-overview] tr[data-metric='body_fat_pct'] th").click();
  await expect(page.locator("#bodyFatDialog")).toBeVisible();
  await page.locator("#bodyFatDialogCancel").click();
  await expect(page.locator("#bodyFatDialog")).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3100 part 2: a diary card marks its amounts as estimated and clears the mark, with undo.
test("@responsive a diary card marks a meal as estimated and clears the mark", async ({ page }) => {
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#nutritionDate")).not.toHaveValue("");
  await page.evaluate(() => api("/api/nutrition/entry", { method: "POST", body: JSON.stringify({ meal_type: "dinner", meal_time: "19:30", description: "Pizza beim Italiener", kcal: 950 }) }));
  await page.evaluate(() => loadNutrition());
  const card = page.locator("#nutritionEntries .nutrition-card").filter({ hasText: "Pizza beim Italiener" });
  await card.getByRole("button", { name: "Als geschätzt markieren" }).click();
  await expect(card.locator("h3")).toContainText("Menge geschätzt");
  await expect(card).toContainText("950 kcal");
  await card.getByRole("button", { name: "Markierung entfernen" }).click();
  await expect(card.locator("h3")).not.toContainText("Menge geschätzt");
  await expect(card.getByRole("button", { name: "Als geschätzt markieren" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3443, #3703: the wearable as outlined source names itself by the label the read gives it, and
// has no digestion part.
test("@responsive the wearable as outlined source names its label and its parts", async ({ page }) => {
  await shapeEnergy(page, (data) => ({ ...data, day_partial: false, outlined: "device", best: null,
    rows: [...data.rows.filter((row) => row.source !== "device"), formulaRow("device", { resting_kcal: 1745, neat_kcal: 610, sport_kcal: 765, tef_kcal: null }, { label: "Garmin Connect", total_band_kcal: null })] }));
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEnergy [data-energy]");
  await expect(card.locator(".energy-usage h3")).toHaveText("Verbrauch");
  await expect(card.locator(".energy-parts li")).toHaveText(["Ruhe1.745", "Schritte & Treppen610", "Sport765", "Verdauung–", "Tag gesamt2.720"]);
  await expect(card.locator(".energy-source")).toHaveText("Quelle: Garmin Connect");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3494, #3703: early morning today has no steps, sessions or meals yet; Tag gesamt is the rest so far.
test("@responsive the early-morning card totals its rest and names the parts without data yet", async ({ page }) => {
  await shapeEnergy(page, (data) => ({ ...data, day_partial: true, outlined: "katch_mcardle", best: null, wellness: null, goal: null,
    rows: [...data.rows.filter((row) => row.source !== "katch_mcardle"),
      formulaRow("katch_mcardle", { resting_kcal: 1800, neat_kcal: 0, sport_kcal: 0, tef_kcal: 0 }, { total_kcal: 1800, pending_parts: ["neat", "sport", "tef"] })] }));
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEnergy [data-energy]");
  await expect(card.locator(".energy-parts li")).toHaveText(["Ruhe1.800", "Schritte & Treppennoch keine Daten", "Sportnoch keine Daten", "Verdauungnoch keine Daten", /^Tag gesamt1\.800/]);
  await expect(card.locator(".energy-source")).toHaveText("Quelle: Katch-McArdle");
  await expect(card.locator(".energy-goal")).toHaveText("Ziel festlegen");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3395: the card shows the diary's selected day; switching the date reads that day.
test("@responsive the energy card follows the diary date: sport per day, today bis jetzt", async ({ page }) => {
  const asked = [];
  await page.route("**/api/body/energy*", async (route) => {
    const day = new URL(route.request().url()).searchParams.get("date");
    asked.push(day);
    const response = await route.fetch();
    const data = await response.json();
    const today = await page.evaluate(() => timezoneDateKey(state.data?.profile?.timezone, new Date()));
    // A session only on the day before today.
    const sport = day === today ? 0 : 418;
    for (const row of data.rows) {
      if (row.source !== "katch_mcardle" && row.source !== "cunningham") continue;
      Object.assign(row, { resting_kcal: 1700, resting_band_kcal: 200, total_kcal: 1950 + sport, total_band_kcal: 210, pending_parts: [],
        split: { resting_kcal: 1700, neat_kcal: 250, sport_kcal: sport, tef_kcal: null, shared: {} } });
    }
    // #3530: today's Garmin day summary ends at 12:18, before nothing synced later.
    const wellness = day === today ? { as_of: "12:18", behind: false } : null;
    await route.fulfill({ response, json: { ...data, day, day_partial: day === today, outlined: "katch_mcardle", best: null, skinfold_missing: null, wellness } });
  });
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEnergy [data-energy]");
  await expect(card.locator(".energy-usage h3")).toHaveText("Verbrauch heute (Stand jetzt)");
  await expect(card.locator(".energy-parts [data-part='sport']")).toHaveText(/^Sport0/);
  await expect(card.locator(".energy-parts [data-part='tef']")).toHaveText("Verdauungnicht erfasst");
  // #3530, #3719: where Garmin's day summary stands is in the "i", not on the row.
  await expect(card.locator(".energy-parts [data-part='neat']")).toHaveText("Schritte & Treppen250");
  await expect(card.locator(".analysis-info-tooltip")).toContainText("Stand 12:18");
  const today = await page.locator("#nutritionDate").inputValue();
  await page.getByRole("button", { name: "Vorheriger Tag" }).click();
  await expect(page.locator("#nutritionDate")).not.toHaveValue(today);
  await expect(card.locator(".energy-parts [data-part='sport']")).toHaveText(/^Sport418/);
  await expect(card.locator(".energy-parts [data-part='neat']")).toHaveText("Schritte & Treppen250");
  await expect(card.locator(".energy-usage h3")).toHaveText("Verbrauch");
  expect(asked.at(-1)).toBe(await page.locator("#nutritionDate").inputValue());
  expect(asked).toContain(today);
  // A future diary day reads nothing; Verlauf's fit, which is no day's, stays (Gitar #3423, #3397).
  await page.getByRole("button", { name: "Nächster Tag" }).click();
  await expect(page.locator("#nutritionDate")).toHaveValue(today);
  await page.getByRole("button", { name: "Nächster Tag" }).click();
  await expect(page.locator("#nutritionDate")).not.toHaveValue(today);
  await expect(page.locator("#nutritionEnergy")).toContainText("Tag liegt in der Zukunft");
  const reads = asked.length;
  await page.goto("/#nutrition/week");
  await expect(page.locator("#nutritionFitScale [data-energy-fit]")).toBeVisible();
  await expect(page.locator("#nutritionFitScale")).not.toContainText("Tag liegt in der Zukunft");
  // #3548: the 28-day own row gave way to the deficit card, which has no source rows.
  await expect(page.locator("#nutritionFitScale [data-energy-source]")).toHaveCount(0);
  expect(asked.length).toBeLessThanOrEqual(reads + 1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3397 (A2): Verlauf shows one kcal scale above "Tagesverlauf" for its period; the Tagebuch
// outlines the best source's row. The read is shaped here; the values come from the method tests.
const fitScale = (days, ownDays, own, band) => ({
  days, own_days: ownDays, own_kcal: own, own_band_kcal: band, weeks: days / 7, weeks_with_data: Math.min(3, days / 7),
  sources: [
    // #3723: Katch-McArdle with a provisional learned everyday, Cunningham without an estimate.
    { source: "katch_mcardle", total_kcal: 3768, learned_total_kcal: 3948, learned_qualified: false },
    { source: "cunningham", total_kcal: 3930 },
    { source: "device", label: "Garmin Connect", total_kcal: 3157 },
  ],
});
for (const theme of ["light", "dark"]) {
  test(`@responsive the fit scale follows Verlauf's period without overlaps, and the diary card takes the outlined source (${theme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: theme });
    await page.route("**/api/body/energy*", async (route) => {
      // A read still in flight when the test moves to the diary is disposed with the
      // old page; that request is gone and needs no answer (#3449).
      let response;
      let data;
      try {
        response = await route.fetch();
        data = await response.json();
      } catch (error) {
        if (GONE.test(String(error))) return;
        throw error;
      }
      for (const row of data.rows) if (row.source !== "own") row.mean_error_kcal = { katch_mcardle: 1042, cunningham: 1204, device: 431 }[row.source];
      // Every row carries its split, so the outlined row has its parts.
      for (const row of data.rows) row.split ??= { resting_kcal: 1650, neat_kcal: 520, sport_kcal: 610, tef_kcal: 230 };
      // #3613: the server names the one outlined source; 12 weeks name the weigh-ins' span.
      const quarter = { ...fitScale(91, 91, 2726, 454), span_start: "2026-08-21", span_end: "2026-11-13" };
      await route.fulfill({ response, json: { ...data, best: null, outlined: "device", fit_scale: { "28d": fitScale(28, 28, 2690, 720), "91d": quarter, "182d": { ...quarter, days: 182, own_days: 182, span_start: "2026-05-15" } } } })
        .catch((error) => { if (!GONE.test(String(error))) throw error; });
    });
    await page.goto("/#nutrition/week");
    const card = page.locator("#nutritionFitScale [data-energy-fit]");
    await expect(card).toHaveAttribute("data-energy-fit", "28d");
    // The shared title row names the period (#3704).
    await expect(card.locator(".verlauf-head")).toHaveText("Welche Quelle trifft dein Gewicht?4 Wochen");
    await expect(card.locator(".energy-fit-verdict")).toHaveText(/^In den letzten 4 Wochen verbrauchst du rund 2\.690 kcal am Tag\. Am besten bisher: Garmin Connect \(\+467\) – noch nicht sicher\.$/);
    await expect(card.locator("[data-energy-fit-note]")).toContainText("Ø 4 Wochen. ");
    // Above the Tagesverlauf.
    const [cardBox, chartBox] = await Promise.all([card.boundingBox(), page.locator("#nutritionWeekChartSection").boundingBox()]);
    expect(cardBox.y + cardBox.height).toBeLessThanOrEqual(chartBox.y);
    await page.getByRole("button", { name: "3 Monate" }).click();
    await expect(card).toHaveAttribute("data-energy-fit", "91d");
    await expect(card.locator(".energy-fit-verdict")).toHaveText(/^Vom 21\.08\. bis 13\.11\. verbrauchst du rund 2\.726 kcal am Tag\. Am besten bisher: Garmin Connect \(\+431\) – noch nicht sicher\.$/);    await expect(card.locator(".energy-fit-row")).toHaveText([/^Deine Daten2\.7262\.272–3\.180zwischen 2\.272 und 3\.180 kcal$/, /^Garmin Connect3\.157 \(\+431\)$/, /^Katch-McArdle3\.768 \(\+1\.042\)mit Alltag ≈ 3\.948 \(\+1\.222\)$/, /^Cunningham3\.930 \(\+1\.204\)$/]);
    await expect(card.locator(".energy-fit-row[data-inside='true']")).toHaveCount(1);
    // #3723: a ring and its connector only on the formula row with an estimate, the ring at the
    // "mit Alltag" value on the scale the dots share; the legend names it provisional.
    for (const [source, count] of [["katch_mcardle", 1], ["cunningham", 0], ["device", 0]]) {
      const row = card.locator(`.energy-fit-row[data-fit-source='${source}']`);
      await expect(row.locator(".energy-fit-learned")).toHaveCount(count);
      await expect(row.locator(".energy-fit-link")).toHaveCount(count);
    }
    const ring = await card.evaluate((element) => {
      const at = (selector) => { const box = element.querySelector(selector).getBoundingClientRect(); return box.left + box.width / 2; };
      const row = ".energy-fit-row[data-fit-source='katch_mcardle']";
      return { raw: at(`${row} .energy-fit-point`), learned: at(`${row} .energy-fit-learned`), cunningham: at(".energy-fit-row[data-fit-source='cunningham'] .energy-fit-point") };
    });
    // One linear scale: the ring sits (3948 - 3768) / (3930 - 3768) of the way from Katch's dot to Cunningham's.
    expect(ring.learned - ring.raw).toBeCloseTo(((3948 - 3768) / (3930 - 3768)) * (ring.cunningham - ring.raw), 0);
    await expect(card.locator("[data-energy-fit-legend]")).toHaveText("Formel alleinmit Alltag (vorläufig)");
    // At the project's own width first, for the PR's screenshot review (#3723).
    if (theme === "light") await card.screenshot({ path: test.info().outputPath("Fit-project-width.png") });
    // At 360 px a row's value stays right of its lane and inside the card.
    await page.setViewportSize({ width: 360, height: 800 });
    const clashes = await card.evaluate((element) => {
      const box = element.getBoundingClientRect();
      const found = [];
      for (const row of element.querySelectorAll(".energy-fit-row")) {
        const lane = row.querySelector(".energy-fit-lane").getBoundingClientRect();
        const value = row.querySelector(".energy-fit-value").getBoundingClientRect();
        if (value.left < lane.right - 1 || value.right > box.right + 1) found.push(`${value.left}-${value.right} outside`);
        // #3723: the "mit Alltag" line stays inside its value column.
        const learned = row.querySelector("[data-fit-learned]")?.getBoundingClientRect();
        if (learned && (learned.left < value.left - 1 || learned.right > value.right + 1)) found.push(`mit Alltag ${learned.left}-${learned.right} outside`);
      }
      const ticks = [...element.querySelectorAll(".energy-fit-tick")].map((tick) => tick.getBoundingClientRect());
      ticks.slice(1).forEach((tick, index) => { if (tick.left < ticks[index].right) found.push("ticks overlap"); });
      return found;
    });
    expect(clashes).toEqual([]);
    if (theme === "light") await card.screenshot({ path: test.info().outputPath("Fit.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    // The diary's card takes the server's outlined source, the nearest one while none is distinguishable.
    await page.goto("/#nutrition/diary");
    await expect(page.locator("#nutritionEnergy .energy-source")).toHaveText(/^Quelle: (?!Katch|Cunningham).+/);
    await expect(page.locator("#nutritionEnergy")).not.toContainText("passt am besten");
    // The day's totals render after the card and push it down; a capture taken before shows the
    // macro cards in its place, without an error to retry on (#3736). So it waits for the day to
    // render, and retries only a capture whose node a late energy read replaced.
    if (theme === "light") {
      await expect(page.locator("#nutritionStatus")).not.toHaveText("Ernährung wird geladen…");
      await expect(page.locator("#nutritionTotals")).not.toBeEmpty();
      await expect(() => page.locator("#nutritionEnergy [data-energy]").screenshot({ path: test.info().outputPath("Energieverbrauch.png"), timeout: 5_000 })).toPass();
    }
  });
}

// #3548, #3555: Verlauf's deficit card with measurements in the fat share and with the prior alone. The read's deficit is shaped here (the
// values come from the method tests); its hero, boxes and text must agree.
const DEFICITS = {
  measured: { days: 28, deficit_kcal: 565, band_kcal: 235, method: "measured", span_days: 28, weight_start_kg: 91.2, weight_end_kg: 89.2, weight_change_kg: -2.0,
    fat_pct_start: 15.5, fat_pct_end: 14.1, fat_date_start: "2026-10-07", fat_date_end: "2026-11-13", weight_last_kg: 89.2,
    weight_date_start: "2026-10-17", weight_date_end: "2026-11-13", edge_weighins: [7, 7],
    fat_change_kg: -1.6, fat_share_pct: 80, fat_share: { share_pct: 80, prior_pct: 57, prior_source: "forbes", measured_pct: 28 },
    stored_kcal: -15820, uncertainty: "fat_share",
    total_kcal: 3800, device_total_kcal: 3100, goal: { goal: "lose", goal_kcal: 500, status: "in", distance: "near" } },
  prior: { days: 28, deficit_kcal: 420, band_kcal: 260, method: "prior", span_days: 27, weight_start_kg: 80.4, weight_end_kg: 78.9, weight_change_kg: -1.5,
    fat_pct_start: null, fat_pct_end: 18, fat_change_kg: -1.1, fat_share_pct: 70, fat_share: { share_pct: 70, prior_pct: 70, measured_pct: 0 },
    stored_kcal: -11340, uncertainty: "fat_share",
    total_kcal: 3300, device_total_kcal: 2950, goal: null },
};
// The read holds the deficit per Verlauf period (#3704); the 4 weeks' card is the one shown first.
// #3739: 3 months price the weight change from the means of their first and last 14 days of weigh-ins.
const EDGES = { days: 91, edge_weighins: [12, 9], weight_start_kg: 93.5, weight_end_kg: 89.1, weight_change_kg: -4.4, weight_date_start: "2026-08-27", weight_date_end: "2026-11-13" };
const withDeficit = (read, deficit) => ({ ...read, deficit, fit_scale: { ...read.fit_scale, "28d": { ...read.fit_scale["28d"], deficit },
  "91d": { ...read.fit_scale["91d"], deficit: { ...deficit, ...EDGES } } } });
for (const [method, deficit] of Object.entries(DEFICITS)) {
  test(`@responsive Verlauf's deficit card shows the stores' terms (${method})`, async ({ page }) => {
    await page.route("**/api/body/energy*", async (route) => {
      let response;
      try {
        response = await route.fetch();
        await route.fulfill({ response, json: withDeficit(await response.json(), deficit) });
      } catch (error) {
        if (!GONE.test(String(error))) throw error;
      }
    });
    await page.goto("/#nutrition/week");
    const card = page.locator(`#nutritionFitScale [data-energy-deficit='${method}']`);
    // The shared title row: the title left, the selected period right (#3704).
    await expect(card.locator(".verlauf-head")).toHaveText("Defizit aus Gewicht und Körperfett4 Wochen");
    const kcal = deficit.deficit_kcal.toLocaleString("de-DE");
    await expect(card.locator(".energy-deficit-hero strong")).toHaveText(`≈ ${kcal} kcal`);
    const boxes = card.locator("[data-deficit-box]");
    await expect(boxes).toHaveCount(4);
    await expect(boxes.nth(3).locator("strong")).toHaveText(`≈ ${kcal} kcal`);
    await expect(boxes.nth(3).locator("small").last()).toHaveText(`${Math.abs(deficit.stored_kcal).toLocaleString("de-DE")} kcal ÷ ${deficit.span_days} Tage`);
    // #3606: the card comes before "Welche Quelle trifft dein Gewicht?", and its text is two lines.
    const order = await page.locator("#nutritionFitScale > section").evaluateAll((cards) => cards.map((node) => ["energyDeficit", "energyFit"].find((key) => key in node.dataset) || "other"));
    expect(order.slice(0, 2)).toEqual(["energyDeficit", "energyFit"]);
    const text = card.locator(".energy-deficit-text");
    await expect(text).toHaveText(`Echter Verbrauch: ≈ ${deficit.total_kcal.toLocaleString("de-DE")} kcal am Tag (Garmin Connect: ${deficit.device_total_kcal.toLocaleString("de-DE")}).`);
    await expect(card.locator(".energy-deficit-uncertainty")).toHaveText(/^Unsicher vor allem: .+\.$/);
    if (method === "measured") {
      // #3704: Gewicht and Körperfett are built alike: change, from → to, dates.
      await expect(boxes.nth(0).locator("strong, small")).toHaveText(["Gewicht", "−2,0 kg", "Ø 91,2 → Ø 89,2 kg", "17.10. → 13.11."]);
      // #3739: 3 months show the measured edge means, marked "Ø", on their mean dates.
      await page.getByRole("button", { name: "3 Monate" }).click();
      await expect(card.locator(".verlauf-head")).toHaveText("Defizit aus Gewicht und Körperfett3 Monate");
      await expect(boxes.nth(0).locator("strong, small")).toHaveText(["Gewicht", "−4,4 kg", "Ø 93,5 → Ø 89,1 kg", "27.08. → 13.11."]);
      await card.screenshot({ path: test.info().outputPath("Defizit-3-Monate.png") });
      await page.getByRole("button", { name: "4 Wochen" }).click();
      await expect(boxes.nth(0).locator("strong, small")).toHaveText(["Gewicht", "−2,0 kg", "Ø 91,2 → Ø 89,2 kg", "17.10. → 13.11."]);
      await expect(boxes.nth(1).locator("strong, small")).toHaveText(["Körperfett", "−1,4 %-Pkt.", "15,5 → 14,1 %", "07.10. → 13.11."]);
      // #3598: the readings span the phase, not the 28 days, and the box says so.
      await expect(boxes.nth(1)).toContainText("07.10. → 13.11.");
      // #3684: the prior follows Forbes at the current fat mass.
      await expect(boxes.nth(2)).toContainText("Forbes 57 %, zu 28 % gemessen");
      await expect(card.locator(".energy-deficit-status")).toHaveText("Ziel: 500 kcal Defizit · ✓ im Ziel");
      // #3606: 65 kcal from the goal is "near": the number and the mark take the accent colour.
      await expect(card).toHaveAttribute("data-goal-distance", "near");
      const colours = await card.evaluate((node) => {
        const accent = getComputedStyle(node).getPropertyValue("--accent").trim();
        const probe = document.createElement("span");
        probe.style.color = accent;
        node.append(probe);
        const expected = getComputedStyle(probe).color;
        probe.remove();
        return { expected, hero: getComputedStyle(node.querySelector(".energy-deficit-hero strong")).color,
          mark: getComputedStyle(node.querySelector(".energy-deficit-mark")).backgroundColor };
      });
      expect([colours.hero, colours.mark]).toEqual([colours.expected, colours.expected]);
      // The span is drawn, and the estimate and the goal marker sit inside it, in their order.
      const marks = await card.locator(".energy-deficit-bar").evaluate((bar) => Object.fromEntries([...bar.children].map((node) => {
        const rect = node.getBoundingClientRect();
        return [node.className, { left: rect.left, right: rect.right, width: rect.width }];
      })));
      const band = marks["energy-deficit-band"];
      expect(band.width).toBeGreaterThan(20);
      for (const name of ["energy-deficit-mark", "energy-deficit-goal"]) {
        expect(marks[name].left).toBeGreaterThanOrEqual(band.left - 2);
        expect(marks[name].right).toBeLessThanOrEqual(band.right + 2);
      }
      // Goal 500 lies left of the estimate 565.
      expect(marks["energy-deficit-goal"].left).toBeLessThan(marks["energy-deficit-mark"].left);
      // #3596: each marker sits where its value puts it on the 230–900 kcal scale (330–800 ± 100).
      const bar = await card.locator(".energy-deficit-bar").boundingBox();
      const share = (name) => ((marks[name].left + marks[name].right) / 2 - bar.x) / bar.width;
      expect(Math.abs(share("energy-deficit-mark") - (565 - 230) / 670)).toBeLessThan(0.03);
      expect(Math.abs(share("energy-deficit-goal") - (500 - 230) / 670)).toBeLessThan(0.03);
    } else {
      await expect(boxes.nth(1)).toContainText("nur eine Messung");
      await expect(boxes.nth(2)).toContainText("≈ 70 % · Faustregel 70 %");
      await expect(card.locator(".energy-deficit-bar")).toHaveCount(0);
      // No goal, no distance class: the number keeps its colour.
      await expect(card).not.toHaveAttribute("data-goal-distance", /.+/);
    }
    // The boxes stay inside the card, also on a phone.
    const overflow = await card.evaluate((node) => [...node.querySelectorAll("[data-deficit-box]")].some((box) => box.getBoundingClientRect().right > node.getBoundingClientRect().right + 1));
    expect(overflow).toBeFalsy();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    await expect(() => card.screenshot({ path: test.info().outputPath(`Defizit-${method}.png`), timeout: 5_000 })).toPass();
  });
}

// #3603 ("Nur Waage"): without logged meals the weigh-ins still carry the card; only the
// expenditure is missing and says why.
test("@responsive the deficit card stays without logged meals", async ({ page }) => {
  const deficit = { ...DEFICITS.measured, total_kcal: null, expenditure_missing: "intake_days" };
  // #3604: the intake from weight and activity, labelled as an estimate.
  const estimated_intake = { kcal: 2100, band_kcal: 230, calibration_end: "2026-09-19", calibration_age_days: 56, missing: null };
  await page.route("**/api/body/energy*", async (route) => {
    try {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...withDeficit(await response.json(), deficit), estimated_intake } });
    } catch (error) {
      if (!GONE.test(String(error))) throw error;
    }
  });
  await page.goto("/#nutrition/week");
  const card = page.locator("#nutritionFitScale [data-energy-deficit]");
  await expect(card.locator(".energy-deficit-hero strong")).toHaveText("≈ 565 kcal");
  await expect(card.locator("[data-deficit-box]")).toHaveCount(4);
  await expect(card.locator(".energy-deficit-text")).toHaveText([
    "Echter Verbrauch: braucht erfasste Mahlzeiten.",
    "Geschätzte Aufnahme aus Gewicht und Aktivität: ≈ 2.100 kcal am Tag (1.870–2.330) (erfasst bis 19.09.).",
  ]);
  await expect(card.locator(".energy-deficit-status")).toHaveText("Ziel: 500 kcal Defizit · ✓ im Ziel");
});

// #3547, #3930: without a goal the diary links to it; the dialog opens in place, and the diary's energy
// card then opens with "Offene Kalorien" from the outlined source's day total.
test("@responsive the energy goal is set from the diary and priced there", async ({ page }) => {
  await page.goto("/#nutrition/diary");
  const strip = page.locator("#nutritionEnergy [data-energy-goal]");
  await expect(strip).toHaveText("Ziel festlegen");
  await strip.getByRole("button", { name: "Ziel festlegen" }).click();
  // #3930: the dialog opens over the diary; the athlete does not leave it.
  await expect(page).toHaveURL(/#nutrition\/diary$/);
  const dialog = page.locator("#energyGoalDialog");
  await expect(dialog).toBeVisible();
  // #3757: the goal control is the app's segmented control: one row of three segments at every
  // width, the radio hidden under its whole label, the checked segment highlighted.
  const segments = dialog.locator(".appearance-control label");
  await expect(segments).toHaveText(["Abnehmen", "Halten", "Aufbauen"]);
  const layout = await segments.evaluateAll((labels) => labels.map((label) => ({
    top: Math.round(label.getBoundingClientRect().top),
    opacity: getComputedStyle(label.querySelector("input")).opacity,
    background: getComputedStyle(label).backgroundColor,
  })));
  expect(new Set(layout.map((item) => item.top)).size).toBe(1);
  expect(layout.map((item) => item.opacity)).toEqual(["0", "0", "0"]);
  expect(layout[1].background).toBe(layout[2].background);
  expect(layout[0].background).not.toBe(layout[1].background);
  // The hidden radio covers its whole segment, so a tap near the edge hits it and selects that
  // segment only; the highlight follows the checked one.
  const build = await segments.nth(2).boundingBox();
  const radio = await dialog.getByRole("radio", { name: "Aufbauen" }).boundingBox();
  for (const key of ["x", "y", "width", "height"]) expect(Math.abs(radio[key] - build[key])).toBeLessThan(1);
  await page.mouse.click(build.x + 4, build.y + build.height - 4);
  await expect(dialog.getByRole("radio", { name: "Aufbauen" })).toBeChecked();
  const backgrounds = () => segments.evaluateAll((labels) => labels.map((label) => getComputedStyle(label).backgroundColor));
  const [lose, hold, built] = await backgrounds();
  expect(lose).toBe(hold);
  expect(built).not.toBe(hold);
  await segments.nth(0).click();
  await expect(dialog.getByRole("radio", { name: "Abnehmen" })).toBeChecked();
  // Keyboard focus shows the focus ring on the segment.
  await page.keyboard.press("ArrowRight");
  await expect(segments.nth(1)).toHaveCSS("outline-style", "solid");
  await page.keyboard.press("ArrowLeft");
  await expect(dialog.getByRole("radio", { name: "Abnehmen" })).toBeChecked();
  await dialog.screenshot({ path: test.info().outputPath("Energieziel.png") });
  // Any whole kcal the API accepts can be entered (Gitar on #3558).
  await dialog.getByLabel("Defizit pro Tag (kcal)").fill("420");
  expect(await dialog.getByLabel("Defizit pro Tag (kcal)").evaluate((input) => input.validity.valid)).toBeTruthy();
  await dialog.getByLabel("Defizit pro Tag (kcal)").fill("500");
  // #3618: body weight per week at the energy view's density (70 % fat by the rule of thumb).
  await expect(dialog.locator("#energyGoalDialogWeek")).toHaveText("≈ 0,49 kg Körpergewicht pro Woche");
  await dialog.getByText("Halten").click();
  await expect(dialog.getByLabel("Defizit pro Tag (kcal)")).toBeHidden();
  await dialog.getByText("Abnehmen").click();
  try {
    // The save prices the diary card again: the read starts with the click, not with a navigation.
    const energy = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/body/energy");
    await dialog.getByRole("button", { name: "Speichern" }).click();
    await expect(dialog).toBeHidden();
    await expect(page.locator("#energyGoalRow")).toContainText("Abnehmen, 500 kcal/Tag");
    const read = await (await energy).json();
    expect(read.goal).toMatchObject({ goal: "lose", goal_kcal: 500 });
    if (read.goal.remaining_kcal == null) {
      await expect(strip).toHaveText("Noch kein Tagesverbrauch für dein Ziel");
    } else {
      await expect(strip.locator("p")).toContainText("Offene Kalorien");
      await expect(strip.locator("li").last()).toHaveText(/^Ziel heute [\d.]+ \(500 Defizit\)$/);
      await expect(strip.locator("progress")).toBeVisible();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  } finally {
    // The fixture keeps no goal for the other scenarios.
    await page.evaluate(() => api("/api/profile", { method: "PUT", body: JSON.stringify({ energy_goal: "", energy_goal_kcal: "" }) }));
  }
});


// #3852: "gegessen" in Offene Kalorien follows the diary totals after every meal write, with no
// manual reload: a changed day intake reads the energy table again instead of keeping the old one.
test("@responsive Offene Kalorien follow the diary totals after a meal is removed", async ({ page }) => {
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#nutritionEnergy [data-energy]")).toBeVisible();
  const today = await page.evaluate(() => todayIso());
  const meal = await page.evaluate((day) => api("/api/nutrition/entry", { method: "POST", body: JSON.stringify({ meal_date: day, meal_type: "lunch", description: "Nudeln #3852", kcal: 640 }) }), today);
  await page.evaluate(() => api("/api/profile", { method: "PUT", body: JSON.stringify({ energy_goal: "lose", energy_goal_kcal: "300" }) }));
  try {
    // The fixture's own total is shaped; "gegessen" stays the server's own count of the day.
    await shapeEnergy(page, (data) => ({ ...data, goal: { ...GOAL, eaten_kcal: data.goal.eaten_kcal, remaining_kcal: GOAL.target_kcal - data.goal.eaten_kcal } }));
    await page.reload();
    const items = page.locator("#nutritionEnergy .energy-goal-items li").first();
    const eaten = await page.evaluate((day) => api(`/api/nutrition/day?date=${day}`), today);
    const shown = (kcal) => `gegessen ${new Intl.NumberFormat("de-DE").format(kcal)}`;
    await expect(items).toHaveText(shown(eaten.total_kcal));
    // A meal is deleted at once (G-03): no confirmation.
    await page.getByRole("button", { name: "Mahlzeit löschen: Nudeln #3852" }).click();
    await expect(page.getByRole("button", { name: "Mahlzeit löschen: Nudeln #3852" })).toHaveCount(0);
    await expect(items).toHaveText(shown(eaten.total_kcal - 640));
  } finally {
    await page.evaluate(async (id) => {
      await api("/api/profile", { method: "PUT", body: JSON.stringify({ energy_goal: "", energy_goal_kcal: "" }) });
      await api("/api/nutrition/entry/delete", { method: "POST", body: JSON.stringify({ id }) }).catch(() => null);
    }, meal.id);
  }
});
