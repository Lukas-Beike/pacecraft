const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// Routes miss requests that pass through the service worker's fetch listener; this spec
// does not test the worker, so it is blocked and every route applies (#3842).
test.use({ serviceWorkers: "block" });

// #3273: LT2 from the provider beside the local estimates, adoption per value (F8), the
// second step above 5 % (F9) and the offers of a new LT1 or LT2 (#4135).
const PROVIDER_HR = { key: "garmin|heart_rate|run||2026-11-09", source: "garmin", label: "Garmin", sport: "run", metric: "heart_rate", environment: null, valid_from: "2026-11-09", valid_until: null, threshold: 158, maximum: 186, limits: [130, 145, 155, 158, 186], percent_limits: null, percent_basis: null, threshold_origin: null };
const ZONES = {
  sports: ["ride", "run", "swim"],
  today: "2026-11-16",
  configurations: [PROVIDER_HR],
  effective: [{ sport: "run", metric: "heart_rate", environment: null, key: PROVIDER_HR.key }],
};
// The run only: the ride has no estimates here, so its notice values stay unknown.
const ESTIMATES = { run: {
  critical_speed: { status: "unknown", sessions: 0, reason: "Keine unabhängigen 3, 7, 12-Minuten-Belastungen in 42 Tagen." },
  dfa_alpha1: { status: "ok", quality: "good", sessions: 6, window_days: 60, heart_rate_bpm: 169, pace_seconds_per_km: 256 },
} };
const METRICS = {
  run_threshold_pace_seconds_per_km: { value: 252, unit: "s/km", source: "Garmin", source_provider: "garmin", observed_at: "2026-11-03" },
  run_threshold_hr_bpm: { value: 158, unit: "bpm", source: "Garmin", source_provider: "garmin", observed_at: "2026-11-03" },
};

// #4135: the open offers as the profile read returns them; `routeOffers` serves them on every profile read.
const OFFER_LT2 = { key: "lt2:run:heart_rate:dfa_alpha1:172", threshold: "lt2", sport: "run", metric: "heart_rate", source: "dfa_alpha1", value: 172, unit: "bpm", current: 168, sessions: 4, window_days: 60, quality: "good", spread_pct: 2.1, newest: "2026-11-21", adopt: { sport: "run", source: "dfa_alpha1", value: "heart_rate", shown: 172 } };
const OFFER_LT1 = { ...OFFER_LT2, key: "lt1:run:heart_rate:dfa_alpha1:151", threshold: "lt1", value: 151, current: 148, sessions: 6, spread_pct: 3.4, adopt: { percent_lthr: 88 } };

async function routeOffers(page, offers) {
  const served = { offers };
  await page.route("**/api/profile", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const response = await route.fetch();
    // A reload still in flight when the test ends finds its response disposed; nothing waits for it.
    const json = await response.json().catch(() => null);
    if (json) await route.fulfill({ response, json: { ...json, threshold_offers: served.offers } });
  });
  return served;
}

async function openRunThresholds(page, { estimates = ESTIMATES, thresholds = {}, offers = [] } = {}) {
  await page.route("**/api/zones", (route) => (route.request().method() === "GET" ? route.fulfill({ json: ZONES }) : route.fallback()));
  const served = await routeOffers(page, offers);
  await page.goto("/#more/profile");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.evaluate(({ estimates: shown, metrics, thresholds: extra }) => {
    const performance = state.data.performance;
    state.data.performance = { ...performance, metrics: { ...performance?.metrics, ...metrics }, thresholds: { ...performance?.thresholds, ...extra } };
    state.data.lt2_estimates = shown;
  }, { estimates, metrics: METRICS, thresholds });
  await page.locator("#zoneSettings > summary").click();
  await page.locator('#zoneSportTabs [data-zone-sport="run"]').click();
  return served;
}

test("@responsive Schwellen lists the provider, DFA-α1 and CS with their basis or reason", async ({ page }) => {
  await openRunThresholds(page);
  const sources = page.locator('[data-zone-box="thresholds"] .lt2-source');
  await expect(sources).toHaveCount(3);
  await expect(sources.nth(0)).toContainText("Garmin");
  await expect(sources.nth(0)).toContainText("4:12 /km · 158 bpm");
  await expect(sources.nth(0)).toContainText("gilt");
  // #3297: the quality without the word "Schätzung"; the method name stays.
  await expect(sources.nth(1)).toContainText("Qualität gut");
  await expect(page.locator('[data-zone-box="thresholds"] .lt2-sources')).not.toContainText("Schätzung");
  await expect(sources.nth(1)).toContainText("169 bpm · 4:16 /km");
  await expect(sources.nth(1)).toContainText("aus 6 Läufen der letzten 60 Tage");
  await expect(sources.nth(2)).toContainText("unbekannt");
  await expect(sources.nth(2).getByRole("button", { name: "Übernehmen" })).toHaveCount(0);
  const results = await new AxeBuilder({ page }).include('[data-zone-box="thresholds"]').analyze();
  expect(results.violations).toEqual([]);
});

test("@responsive the notes sit under the value they concern (#3920)", async ({ page }) => {
  const estimates = { run: { ...ESTIMATES.run, dfa_alpha1: { ...ESTIMATES.run.dfa_alpha1, range: { heart_rate_bpm: [-24, 16], basis: "Limits of Agreement gegen VT2, Rogers et al. 2021 (R76)" } } } };
  const thresholds = {
    unsupported_by_recent_effort: { run: { threshold_pace_seconds_per_km: 252, best_pace_seconds_per_km: 278, distance_meters: 5000, window_days: 90, date: "2026-11-01" } },
    recorded_max_hr_above: { value: 191, date: "2026-09-14", stored_max_hr: 186, window_days: 365 },
  };
  await openRunThresholds(page, { estimates, thresholds });
  const box = page.locator('[data-zone-box="thresholds"]');
  await expect(box.locator('[data-threshold-note="effort"]')).toHaveText("5-km-Bestwert 4:38 /km (90 T.) · Test empfohlen");
  await expect(box.locator('[data-lt2-source="dfa_alpha1"] .lt2-source-values')).toHaveText("169 bpm (−24/+16) · 4:16 /km");
  await expect(box.locator('[data-lt2-source="critical_speed"] .lt2-source-values')).toHaveText("unbekannt");
  await expect(page.locator('[data-zone-box="heart_rate"] [data-zone-note="max-hr"]')).toContainText("Brustgurt 191 bpm am ");
  const results = await new AxeBuilder({ page }).include("#zoneOverview").analyze();
  expect(results.violations).toEqual([]);
});

test("adopting DFA-α1 offers each value and asks once more above 5 %", async ({ page }) => {
  await openRunThresholds(page);
  const requests = [];
  await page.route("**/api/zones/lt2/adopt", async (route) => {
    const body = route.request().postDataJSON();
    requests.push(body);
    if (!body.confirm_deviation) {
      await route.fulfill({ status: 409, json: { error: "Herzfrequenz 169 bpm weicht 7 % vom geltenden Wert ab.", reason: "lt2_deviation" } });
      return;
    }
    await route.fulfill({ json: { configuration: { threshold: 169 }, change: { id: "change-1", entity_type: "zone_configuration", action: "create", after_hash: "x" } } });
  });
  await page.locator('[data-lt2-source="dfa_alpha1"]').getByRole("button", { name: "Übernehmen" }).click();
  const dialog = page.locator("#lt2AdoptDialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".chip")).toHaveText(["Herzfrequenz 169 bpm", "Pace 4:16 /km"]);
  // F8: only the pulse is adopted.
  // The chip's checkbox lies over its label, so the checkbox itself is unchecked.
  await dialog.getByRole("checkbox", { name: "Pace 4:16 /km" }).uncheck();
  await dialog.getByRole("button", { name: "Übernehmen" }).click();
  const confirmation = page.locator("#confirmationDialog");
  await expect(confirmation.locator("#confirmationDialogTitle")).toHaveText("Große Abweichung");
  await expect(confirmation.locator("#confirmationDialogMessage")).toContainText("weicht 7 %");
  await confirmation.getByRole("button", { name: "Trotzdem übernehmen" }).click();
  await expect(page.locator("#zoneReceipt")).toContainText("DFA-α1: Herzfrequenz 169 bpm übernommen");
  expect(requests.map((body) => [body.value, body.shown, Boolean(body.confirm_deviation)])).toEqual([["heart_rate", 169, false], ["heart_rate", 169, true]]);
});

test("@responsive the offers are cards above the thresholds, from 1100 px rows of one card (#4135)", async ({ page }) => {
  await openRunThresholds(page, { offers: [OFFER_LT1, OFFER_LT2] });
  const box = page.locator("#zoneOverview [data-threshold-offers]");
  await expect(box).toBeVisible();
  expect(await box.evaluate((element) => element.nextElementSibling.dataset.zoneBox)).toBe("thresholds");
  await expect(box.locator('[data-threshold-offer="lt2:run:heart_rate:dfa_alpha1:172"]')).toContainText("172 bpm");
  if (page.viewportSize().width >= 1100) {
    await expect(box.locator(".threshold-offer")).toHaveCount(1);
    await expect(box.locator("h3")).toHaveText("Neue Schwellen nach dem Lauf am 21.11.");
    await expect(box.locator(".threshold-offer-row")).toHaveText([/LT1151 bpmbisher 148 · DFA-α1 0,75 · 6 Läufe in 60 Tagen · gut/, /LT2172 bpmbisher 168 · DFA-α1 0,5 · 4 harte Läufe in 60 Tagen · gut/]);
  } else {
    await expect(box.locator(".threshold-offer")).toHaveCount(2);
    const card = box.getByRole("region", { name: "Neue Schwelle LT2" });
    await expect(card.locator("h3")).toHaveText("Neue LT2");
    await expect(card).toContainText("nach dem Lauf am 21.11.");
    await expect(card.locator(".threshold-offer-value")).toHaveText(/172 bpm\s*bisher 168/);
    await expect(card.locator("dd")).toHaveText(["DFA-α1 0,5 · Brustgurt", "4 harte Läufe in 60 Tagen", "gut · Spanne 2,1 %"]);
  }
  for (const button of await box.getByRole("button").all()) expect((await button.boundingBox()).height).toBeGreaterThanOrEqual(44);
  expect((await new AxeBuilder({ page }).include("[data-threshold-offers]").analyze()).violations).toEqual([]);
});

test("Übernehmen posts the offer's adopt body, Verwerfen removes its card (#4135)", async ({ page }) => {
  const served = await openRunThresholds(page, { offers: [OFFER_LT1, OFFER_LT2] });
  const adopted = [];
  await page.route("**/api/zones/lt2/adopt", async (route) => {
    adopted.push(route.request().postDataJSON());
    await route.fulfill({ json: { configuration: { threshold: 172 }, change: { id: "change-1", entity_type: "zone_configuration", action: "create", after_hash: "x" } } });
  });
  const dismissed = [];
  await page.route("**/api/zones/threshold-offers/dismiss", async (route) => {
    dismissed.push(route.request().postDataJSON());
    served.offers = [OFFER_LT1];
    await route.fulfill({ json: { threshold_offers: served.offers } });
  });
  const offer = page.locator('#zoneOverview [data-threshold-offer="lt2:run:heart_rate:dfa_alpha1:172"]');
  await offer.getByRole("button", { name: "Übernehmen" }).click();
  await expect(page.locator("#zoneReceipt")).toContainText("DFA-α1 0,5: LT2 172 bpm übernommen");
  expect(adopted).toEqual([{ sport: "run", source: "dfa_alpha1", value: "heart_rate", shown: 172 }]);
  await page.locator('#zoneOverview [data-threshold-offer="lt2:run:heart_rate:dfa_alpha1:172"]').getByRole("button", { name: "Verwerfen" }).click();
  await expect(page.locator('#zoneOverview [data-threshold-offer="lt2:run:heart_rate:dfa_alpha1:172"]')).toHaveCount(0);
  await expect(page.locator('#zoneOverview [data-threshold-offer="lt1:run:heart_rate:dfa_alpha1:151"]')).toBeVisible();
  expect(dismissed).toEqual([{ key: "lt2:run:heart_rate:dfa_alpha1:172" }]);
});

test("@responsive Analyse shows Neue Schwelle above Aktuelle Werte for its sport only, and Verwerfen hides it (#4135)", async ({ page }) => {
  const served = await routeOffers(page, [OFFER_LT2, OFFER_LT1]);
  const dismissed = [];
  await page.route("**/api/zones/threshold-offers/dismiss", async (route) => {
    const { key } = route.request().postDataJSON();
    dismissed.push(key);
    served.offers = served.offers.filter((offer) => offer.key !== key);
    await route.fulfill({ json: { threshold_offers: served.offers } });
  });
  // Laufen needs values of its own, or its empty state stands in for the whole tab.
  await page.route("**/api/performance", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.performance.metrics = { ...data.performance.metrics, ...METRICS };
    await route.fulfill({ response, json: data });
  });
  await page.goto("/#analysis/running");
  await expect(page.locator("#appShell")).toBeVisible();
  const card = page.getByRole("region", { name: "Neue Schwelle", exact: true });
  await expect(card).toBeVisible();
  expect(await card.evaluate((element) => element.parentElement.nextElementSibling.id)).toBe("currentPerformance");
  await expect(card.locator(".threshold-offer-line")).toHaveText([/LT2 · 172 bpm\s*bisher 168 · DFA-α1 · 4 Läufe/, /LT1 · 151 bpm\s*bisher 148 · DFA-α1 · 6 Läufe/]);
  await expect(card.getByRole("button", { name: "Übernehmen" })).toHaveCount(2);
  await expect(card.getByRole("link", { name: "Details in Zonen & Schwellen" })).toBeVisible();
  expect((await new AxeBuilder({ page }).include("#analysisThresholdOffers").analyze()).violations).toEqual([]);
  await page.evaluate(async () => { await AppRouter.navigate("analysis/cycling", { historyMode: "replace" }); });
  await expect(card).toBeHidden();
  await page.evaluate(async () => { await AppRouter.navigate("analysis/running", { historyMode: "replace" }); });
  await card.getByRole("button", { name: "Verwerfen" }).click();
  await expect(page.locator("#analysisThresholdOffers .threshold-offer")).toHaveCount(0);
  expect(dismissed).toEqual([OFFER_LT2.key, OFFER_LT1.key]);
});

test("@responsive without offers there is no offer card (#4135)", async ({ page }) => {
  await openRunThresholds(page);
  await expect(page.locator('[data-zone-box="thresholds"]')).toBeVisible();
  await expect(page.locator("#zoneOverview [data-threshold-offers]")).toBeHidden();
  await expect(page.locator(".threshold-offer")).toHaveCount(0);
});

test("@responsive a zone box without a max-HR note shows no stray text (#4230)", async ({ page }) => {
  await openRunThresholds(page);
  const boxes = page.locator("#zoneOverview [data-zone-box]");
  await expect(boxes.first()).toBeVisible();
  // DOM append wrote a missing note as the text "null" below the zones.
  const stray = await boxes.evaluateAll((nodes) => nodes.flatMap((node) => [...node.childNodes])
    .filter((child) => child.nodeType === Node.TEXT_NODE && child.textContent.trim()).map((child) => child.textContent));
  expect(stray).toEqual([]);
});
