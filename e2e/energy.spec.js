const { test, expect } = require("@playwright/test");

// #3099, #3252: the energy table sits in the diary, its explanation behind the small "i". The
// fixture has no skinfold: with a weigh-in and a profile body fat, the formula rows fall back
// to the scale's or the profile's body fat and name it (#3362).
test("@responsive the diary shows the energy table and explains it behind the info button", async ({ page }) => {
  await page.goto("/#nutrition/diary");
  await expect(page.locator("#nutritionEnergy [data-energy]")).toBeVisible();
  await page.evaluate(async () => {
    const day = new Date(Date.now() - 3 * 864e5).toISOString().slice(0, 10);
    await api("/api/body/weight", { method: "PUT", body: JSON.stringify({ date: day, kg: 72 }) });
    await api("/api/profile", { method: "PUT", body: JSON.stringify({ body_fat_pct: "18,5" }) });
  });
  const energy = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/body/energy");
  await page.reload();
  // #3323: the device row's name is the provider label the read gives it, never a client literal.
  const read = await (await energy).json();
  const device = read.rows.find((row) => row.source === "device");
  expect(device.label).toBeTruthy();
  expect(device.label).not.toContain("Schätzung");
  const card = page.locator("#nutritionEnergy [data-energy]");
  await expect(card.locator("tbody tr[data-energy-source]")).toHaveCount(4);
  // A body fat saved in the profile is a dated entered measurement since #3477.
  expect(["device", "entered", "profile"]).toContain(read.body.source);
  // The diary opens on today, whose rows are still partial (#3395).
  const kfa = `KFA: ${read.body.label} · bis jetzt`;
  // Today's Garmin rest names the full day it comes from, if any (#3443).
  const deviceNote = device.resting_from ? `${device.label}Ruhe ` : `${device.label}`;
  await expect(card.locator("tbody th")).toHaveText([`Katch-McArdle${kfa}`, `Cunningham${kfa}`, new RegExp(`^${deviceNote}.*bis jetzt$`), "Aus deinen DatenØ pro Tag, letzte 28 Tage"]);
  // #3478: the estimate with its low–high span on one line below it, and no "±" left in the energy views.
  const resting = card.locator("[data-energy-source='katch_mcardle'] td").first();
  const shown = resting.locator(".energy-span [aria-hidden]");
  await expect(shown).toHaveText(/^\d{1,3}(?:\.\d{3})*–\d{1,3}(?:\.\d{3})*$/);
  const [low, high] = (await shown.textContent()).split("–");
  await expect(resting.locator(".energy-span .sr-only")).toHaveText(`zwischen ${low} und ${high} kcal`);
  const spanLines = await shown.evaluate((node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return new Set([...range.getClientRects()].map((rect) => Math.round(rect.top))).size;
  });
  expect(spanLines).toBe(1);
  await expect(card).not.toContainText("±");
  await expect(card.locator(".analysis-reference-note")).toHaveCount(0);
  const tooltip = card.locator(".analysis-info-tooltip");
  await expect(tooltip).toBeHidden();
  // #3336: the same small info icon as Analyse › Laufen › Entwicklung, not a yellow badge.
  const info = card.getByRole("button", { name: "Energieverbrauch: Informationen" });
  await expect(info).toHaveClass("analysis-legend-info analysis-info-icon");
  // The table re-renders when its read arrives, so the style check retries on the current node.
  await expect(info).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  // A late diary render keeps the shown table, so one click opens the popover for good (#3379).
  await info.click();
  await expect(tooltip).toBeVisible();
  await page.evaluate(() => renderDiaryEnergy());
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText("Datenbasis zu gering");
  await expect(tooltip).toContainText(`(${read.body.label}`);
  await expect(page.locator("#nutritionExpenditure")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3336, #3362: without any body fat both formula rows name the reason and open its dialog.
test("@responsive formula rows without a body fat name the reason and open the dialog", async ({ page }) => {
  // The read carries the diary day as `?date=` (#3395).
  await page.route("**/api/body/energy*", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    for (const row of data.rows) if (row.source === "katch_mcardle" || row.source === "cunningham") Object.assign(row, { resting_kcal: null, resting_band_kcal: null, total_kcal: null, total_band_kcal: null, split: null });
    await route.fulfill({ response, json: { ...data, body: null, skinfold_missing: "no_skinfold" } });
  });
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEnergy [data-energy]");
  const missing = card.locator("[data-energy-missing='no_skinfold']");
  await expect(missing).toHaveText(["Körperfett fehlt", "Körperfett fehlt"]);
  // Without a body fat no row names a KFA source.
  await expect(card.locator("tbody")).not.toContainText("KFA:");
  await missing.first().click();
  await expect(page.locator("#bodyFatDialog")).toBeVisible();
  await page.locator("#bodyFatDialogCancel").click();
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
  await card.getByRole("button", { name: "Nicht geschätzt" }).click();
  await expect(card.locator("h3")).not.toContainText("Menge geschätzt");
  await expect(card.getByRole("button", { name: "Als geschätzt markieren" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3443: today Garmin's rest is still counting; its row shows yesterday's, or why it has none.
test("@responsive today's Garmin row shows yesterday's rest with its label, or the reason", async ({ page }) => {
  let device = null;
  await page.route("**/api/body/energy*", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    const row = data.rows.find((item) => item.source === "device");
    for (const key of ["resting_from", "resting_missing"]) delete row[key];
    Object.assign(row, device(data.day));
    await route.fulfill({ response, json: { ...data, day_partial: true } });
  });
  device = (day) => {
    const yesterday = new Date(`${day}T12:00:00Z`);
    yesterday.setUTCDate(yesterday.getUTCDate() - 1);
    return { resting_kcal: 1800, resting_band_kcal: null, total_kcal: 2200, total_band_kcal: null, resting_from: yesterday.toISOString().slice(0, 10) };
  };
  await page.goto("/#nutrition/diary");
  const row = page.locator("#nutritionEnergy tr[data-energy-source='device']");
  await expect(row.locator("th small")).toHaveText("Ruhe wie gestern · bis jetzt");
  await expect(row.locator("td")).toHaveText([/^1\.800/, /^2\.200/]);
  device = () => ({ resting_kcal: null, resting_band_kcal: null, total_kcal: null, total_band_kcal: null, split: null, resting_missing: "no_recent_resting" });
  await page.evaluate(() => { resetEnergy(); return loadEnergy(); });
  // The array form retries while the two value cells are still being replaced.
  await expect(row.locator("td")).toHaveText(["kein Ruhewert der letzten Tage"]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3494: early morning today has no steps, sessions or meals yet; Tag gesamt is the rest so far.
test("@responsive the early-morning row totals its rest and names the parts without data yet", async ({ page }) => {
  await page.route("**/api/body/energy*", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    const row = data.rows.find((item) => item.source === "katch_mcardle");
    Object.assign(row, { resting_kcal: 1800, resting_band_kcal: 200, total_kcal: 1800, total_band_kcal: 200,
      split: { resting_kcal: 1800, neat_kcal: 0, sport_kcal: 0, tef_kcal: 0, shared: {} }, pending_parts: ["neat", "sport", "tef"] });
    await route.fulfill({ response, json: { ...data, day_partial: true, skinfold_missing: null } });
  });
  await page.goto("/#nutrition/diary");
  const row = page.locator("#nutritionEnergy tr[data-energy-source='katch_mcardle']");
  await expect(row.locator("th small")).toContainText("bis jetzt");
  await expect(row.locator("td")).toHaveText([/^1\.800/, /^1\.800/]);
  await expect(page.locator("#nutritionEnergy tr.energy-split-row").first()).toContainText("Ruhe 1.800 (1.600–2.000) · Alltag noch keine Daten · Sport noch keine Daten · Verdauung noch keine Daten");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3395: the table shows the diary's selected day; switching the date reads that day.
test("@responsive the energy table follows the diary date: sport per day, today bis jetzt, own daily mean of 28 days", async ({ page }) => {
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
      Object.assign(row, { resting_kcal: 1700, resting_band_kcal: 200, total_kcal: 1950 + sport, total_band_kcal: 210,
        split: { resting_kcal: 1700, neat_kcal: 250, sport_kcal: sport, tef_kcal: null, shared: {} } });
    }
    await route.fulfill({ response, json: { ...data, day, day_partial: day === today, skinfold_missing: null } });
  });
  await page.goto("/#nutrition/diary");
  const card = page.locator("#nutritionEnergy [data-energy]");
  const katch = card.locator("tr[data-energy-source='katch_mcardle']");
  const split = card.locator("tr.energy-split-row").first();
  await expect(katch.locator("th small")).toContainText("bis jetzt");
  await expect(split).toContainText("Sport 0 · Verdauung nicht erfasst");
  // #3513: the own row is a daily mean over its window, never the selected day.
  await expect(card.locator("tr[data-energy-source='own'] th small")).toHaveText("Ø pro Tag, letzte 28 Tage");
  const today = await page.locator("#nutritionDate").inputValue();
  await page.getByRole("button", { name: "Vorheriger Tag" }).click();
  await expect(page.locator("#nutritionDate")).not.toHaveValue(today);
  await expect(split).toContainText("Sport 418");
  await expect(katch.locator("th small")).not.toContainText("bis jetzt");
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
  expect(asked.length).toBeLessThanOrEqual(reads + 1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3397 (A2): Verlauf shows one kcal scale above "Tagesverlauf" for its period; the Tagebuch
// outlines the best source's row. The read is shaped here; the values come from the method tests.
const fitScale = (days, ownDays, own, band) => ({
  days, own_days: ownDays, own_kcal: own, own_band_kcal: band, weeks: days / 7, weeks_with_data: Math.min(3, days / 7),
  sources: [
    { source: "katch_mcardle", total_kcal: 3768 },
    { source: "cunningham", total_kcal: 3930 },
    { source: "device", label: "Garmin Connect", total_kcal: 3157 },
  ],
});
// Playwright's messages for a request whose page went away (#3449).
const GONE = /Response has been disposed|Target page, context or browser has been closed/;
for (const theme of ["light", "dark"]) {
  test(`@responsive the fit scale follows Verlauf's period without overlaps, and the diary outlines the best source (${theme})`, async ({ page }) => {
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
      // Every row carries its split, so the outlined row has its bar and detail line (#3476).
      for (const row of data.rows) row.split ??= { resting_kcal: 1650, neat_kcal: 520, sport_kcal: 610, tef_kcal: 230 };
      await route.fulfill({ response, json: { ...data, best: null, fit_scale: { "14d": fitScale(14, 28, 2690, 720), "12w": fitScale(84, 84, 2726, 454) } } })
        .catch((error) => { if (!GONE.test(String(error))) throw error; });
    });
    await page.goto("/#nutrition/week");
    const card = page.locator("#nutritionFitScale [data-energy-fit]");
    await expect(card).toHaveAttribute("data-energy-fit", "14d");
    await expect(card.locator(".energy-fit-verdict")).toHaveText(/^In den letzten 4 Wochen verbrauchst du rund 2\.690 kcal am Tag\. Am nächsten: Garmin Connect \(\+467\) – noch nicht sicher\.$/);
    await expect(card.locator("[data-energy-fit-note]")).toContainText("Ø letzte 14 Tage, dein Wert aus 4 Wochen");
    // Above the Tagesverlauf.
    const [cardBox, chartBox] = await Promise.all([card.boundingBox(), page.locator("#nutritionWeekChartSection").boundingBox()]);
    expect(cardBox.y + cardBox.height).toBeLessThanOrEqual(chartBox.y);
    await page.getByRole("button", { name: "12 Wochen" }).click();
    await expect(card).toHaveAttribute("data-energy-fit", "12w");    await expect(card.locator(".energy-fit-row")).toHaveText([/^Deine Daten2\.7262\.272–3\.180zwischen 2\.272 und 3\.180 kcal$/, /^Garmin Connect3\.157 \(\+431\)$/, /^Katch-McArdle3\.768 \(\+1\.042\)$/, /^Cunningham3\.930 \(\+1\.204\)$/]);
    await expect(card.locator(".energy-fit-row[data-inside='true']")).toHaveCount(1);
    // At 360 px nothing in a lane overlaps another label or leaves the card.
    await page.setViewportSize({ width: 360, height: 800 });
    const clashes = await card.evaluate((element) => {
      const box = element.getBoundingClientRect();
      const found = [];
      for (const lane of element.querySelectorAll(".energy-fit-lane")) {
        const label = lane.querySelector(".energy-fit-label").getBoundingClientRect();
        if (label.left < box.left - 1 || label.right > box.right + 1) found.push(`${label.left}-${label.right} outside`);
      }
      const ticks = [...element.querySelectorAll(".energy-fit-tick")].map((tick) => tick.getBoundingClientRect());
      ticks.slice(1).forEach((tick, index) => { if (tick.left < ticks[index].right) found.push("ticks overlap"); });
      return found;
    });
    expect(clashes).toEqual([]);
    if (theme === "light") await card.screenshot({ path: test.info().outputPath("Fit.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    // The diary outlines the nearest source dashed while none is distinguishable.
    await page.goto("/#nutrition/diary");
    const device = page.locator("#nutritionEnergy tr[data-energy-source='device']");
    await expect(device).toHaveAttribute("data-fit", "nearest");
    // The diary re-renders the table when its reads land, so the style check retries on the current cell.
    await expect(device.locator("th")).toHaveCSS("border-top-style", "dashed");
    await expect(page.locator("#nutritionEnergy")).not.toContainText("passt am besten");
    // #3476: the outline encloses the whole source row (head line, bar and detail line), with room inside its border.
    // The diary re-renders when its reads land, so each try reads the current row.
    const outlineProblems = () => device.evaluate((row) => {
      const split = row.nextElementSibling?.matches(".energy-split-row") ? row.nextElementSibling : null;
      if (!split) return ["no split row"];
      const cells = [...row.children, ...split.children];
      const drawn = (cell, side) => getComputedStyle(cell)[`border${side}Style`] === "dashed";
      const box = (node) => node.getBoundingClientRect();
      const edge = {
        left: Math.min(...cells.filter((cell) => drawn(cell, "Left")).map((cell) => box(cell).left)),
        right: Math.max(...cells.filter((cell) => drawn(cell, "Right")).map((cell) => box(cell).right)),
        top: Math.min(...cells.filter((cell) => drawn(cell, "Top")).map((cell) => box(cell).top)),
        bottom: Math.max(...cells.filter((cell) => drawn(cell, "Bottom")).map((cell) => box(cell).bottom)),
      };
      const outside = [];
      for (const node of [split?.querySelector(".energy-split"), split?.querySelector("small")]) {
        const rect = node && box(node);
        if (!rect || rect.left < edge.left + 2 || rect.right > edge.right - 2 || rect.top < edge.top + 2 || rect.bottom > edge.bottom - 2) outside.push(node?.className || node?.tagName || "missing");
      }
      // The colour dot is the name cell's ::before, so the name cell's content must start clear of the border.
      const name = row.querySelector("th");
      const inset = box(name).left + parseFloat(getComputedStyle(name).borderLeftWidth) + parseFloat(getComputedStyle(name).paddingLeft) - edge.left;
      // The room inside the outline must not push the table past its card.
      const card = row.closest("[data-energy]");
      const cardRight = box(card).right - parseFloat(getComputedStyle(card).paddingRight);
      if (box(row.closest("table")).right > cardRight + 1) outside.push("table past its card");
      return inset < 6 ? [...outside, `name ${inset}px from the border`] : outside;
    }).catch((error) => [String(error)]);
    await expect.poll(outlineProblems).toEqual([]);
    // A late read can still re-render the card mid-capture, so the capture is retried.
    if (theme === "light") await expect(() => page.locator("#nutritionEnergy [data-energy]").screenshot({ path: test.info().outputPath("Energieverbrauch.png"), timeout: 5_000 })).toPass();
  });
}

