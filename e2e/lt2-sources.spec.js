const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// #3273: LT2 from the provider beside the local estimates, adoption per value (F8), the
// second step above 5 % (F9) and the change notice in the status strip (F10).
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

async function openRunThresholds(page, baselines) {
  await page.route("**/api/zones", (route) => (route.request().method() === "GET" ? route.fulfill({ json: ZONES }) : route.fallback()));
  const seen = [];
  await page.route("**/api/zones/lt2/notice-baseline", async (route) => {
    const body = route.request().postDataJSON();
    seen.push(body);
    await route.fulfill({ json: { lt2_notice_baselines: { ...baselines, [body.sport]: body.values } } });
  });
  await page.goto("/#more/profile");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.evaluate(({ estimates, metrics, baselines: stored }) => {
    state.data.performance = { ...state.data.performance, metrics: { ...state.data.performance?.metrics, ...metrics } };
    state.data.lt2_estimates = estimates;
    state.data.lt2_notice_baselines = stored;
  }, { estimates: ESTIMATES, metrics: METRICS, baselines });
  await page.locator("#zoneSettings > summary").click();
  await page.locator('#zoneSportTabs [data-zone-sport="run"]').click();
  return seen;
}

test("@responsive Schwellen lists the provider, DFA-α1 and CS with their basis or reason", async ({ page }) => {
  await openRunThresholds(page, { run: { provider_hr: 158, provider_pace: 252, dfa_hr: 169, dfa_pace: 256, cs_pace: null } });
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

test("adopting DFA-α1 offers each value and asks once more above 5 %", async ({ page }) => {
  await openRunThresholds(page, { run: { provider_hr: 158, provider_pace: 252, dfa_hr: 169, dfa_pace: 256, cs_pace: null } });
  const requests = [];
  await page.route("**/api/zones/lt2/adopt", async (route) => {
    const body = route.request().postDataJSON();
    requests.push(body);
    if (!body.confirm_deviation) {
      await route.fulfill({ status: 409, json: { error: "Puls 169 bpm weicht 7 % vom geltenden Wert ab.", reason: "lt2_deviation" } });
      return;
    }
    await route.fulfill({ json: { configuration: { threshold: 169 }, change: { id: "change-1", entity_type: "zone_configuration", action: "create", after_hash: "x" } } });
  });
  await page.locator('[data-lt2-source="dfa_alpha1"]').getByRole("button", { name: "Übernehmen" }).click();
  const dialog = page.locator("#lt2AdoptDialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".chip")).toHaveText(["Puls 169 bpm", "Pace 4:16 /km"]);
  // F8: only the pulse is adopted.
  // The chip's checkbox lies over its label, so the checkbox itself is unchecked.
  await dialog.getByRole("checkbox", { name: "Pace 4:16 /km" }).uncheck();
  await dialog.getByRole("button", { name: "Übernehmen" }).click();
  const confirmation = page.locator("#confirmationDialog");
  await expect(confirmation.locator("#confirmationDialogTitle")).toHaveText("Große Abweichung");
  await expect(confirmation.locator("#confirmationDialogMessage")).toContainText("weicht 7 %");
  await confirmation.getByRole("button", { name: "Trotzdem übernehmen" }).click();
  await expect(page.locator("#zoneReceipt")).toContainText("DFA-α1: Puls 169 bpm übernommen");
  expect(requests.map((body) => [body.value, body.shown, Boolean(body.confirm_deviation)])).toEqual([["heart_rate", 169, false], ["heart_rate", 169, true]]);
});

test("a changed value raises one notice, and closing it stores the seen values", async ({ page }) => {
  const seen = await openRunThresholds(page, {
    run: { provider_hr: 158, provider_pace: 252, dfa_hr: 169, dfa_pace: 256, cs_pace: 255 },
    ride: { provider_power: null, provider_hr: null, cp_power: null },
  });
  await page.evaluate(() => refreshLt2Notice());
  const strip = page.locator("#statusStrip");
  await expect(strip).toContainText("LT2 Laufen geändert");
  await expect(strip).toContainText("CS 4:15 → unbekannt");
  const before = seen.length;
  await strip.getByRole("button", { name: "Für diese Sitzung ausblenden" }).click();
  await expect.poll(() => seen.length).toBeGreaterThan(before);
  expect(seen.at(-1)).toEqual({ sport: "run", values: { provider_hr: 158, provider_pace: 252, dfa_hr: 169, dfa_pace: 256, cs_pace: null } });
  await expect(strip).not.toContainText("LT2 Laufen geändert");
});
