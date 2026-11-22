const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// #3402: the body fat pencil opens one dialog, "Prozent | Hautfalten" (default Prozent); both ways
// save and delete, the newest own value is the KFA, and no separate skinfold card is left.
for (const scheme of ["light", "dark"]) {
  test(`@responsive body fat is entered as a percentage or as skinfolds (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/#nutrition/body");
    const row = page.locator("[data-body-overview] tr[data-metric='body_fat_pct']");
    const dialog = page.locator("#bodyFatDialog");
    await expect(row).toBeVisible();
    await expect(page.locator("[data-skinfolds]")).toHaveCount(0);
    await page.evaluate(async () => {
      await api("/api/profile", { method: "PUT", body: JSON.stringify({ birth_year: "1986" }) });
      state.data.profile.birth_year = "1986";
    });

    await row.getByRole("button", { name: "Körperfett eintragen" }).click();
    await expect(dialog).toBeVisible();
    const percent = dialog.getByRole("button", { name: "Prozent" });
    const folds = dialog.getByRole("button", { name: "Hautfalten" });
    await expect(percent).toHaveAttribute("aria-pressed", "true");
    await expect(dialog.getByLabel("Brust (mm)")).toBeHidden();
    // The inactive segment, the secondary buttons and the labels keep AA contrast (#3402).
    expect(await new AxeBuilder({ page }).include("#bodyFatDialog").withTags(["wcag2aa"]).analyze()).toEqual(expect.objectContaining({ violations: [] }));

    // Hautfalten: Jackson-Pollock with the age in 2026 (40): 16/20/17 mm -> 17,1 %.
    await folds.click();
    await expect(folds).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("#bodyFatDialogPct")).toBeHidden();
    await page.locator("#bodyFatDialogDate").fill("2026-11-10");
    await page.locator("#bodyFatDialogDate").dispatchEvent("change");
    await dialog.getByLabel("Brust (mm)").fill("16");
    await dialog.getByLabel("Bauch (mm)").fill("20");
    await dialog.getByLabel("Oberschenkel (mm)").fill("17");
    await expect(page.locator("#bodyFatDialogResult")).toHaveText("Ergibt 17,1 % Körperfett");
    expect(await new AxeBuilder({ page }).include("#bodyFatDialog").withTags(["wcag2aa"]).analyze()).toEqual(expect.objectContaining({ violations: [] }));
    await dialog.getByRole("button", { name: "Speichern" }).click();
    await expect(dialog).toBeHidden();
    await expect(row.locator("td").first()).toHaveText("17,1 %");
    await expect(row.locator("th small")).toHaveText("Eingetragen (Hautfalten) · 10.11.2026");

    // Prozent, today: the newer own value is the KFA, and the lean mass follows from it.
    await row.getByRole("button", { name: "Körperfett eintragen" }).click();
    await expect(percent).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("#bodyFatDialogRemove")).toBeHidden();
    await page.locator("#bodyFatDialogPct").fill("14.3");
    await dialog.getByRole("button", { name: "Speichern" }).click();
    await expect(dialog).toBeHidden();
    await expect(row.locator("td").first()).toHaveText("14,3 %");
    await expect(row.locator("th small")).toContainText("Eingetragen · ");
    await expect(page.locator("[data-body-overview] tr[data-metric='lean_mass_kg'] button")).toHaveCount(0);

    // A day with an entry fills the dialog; Löschen asks first.
    await row.getByRole("button", { name: "Körperfett eintragen" }).click();
    await expect(page.locator("#bodyFatDialogPct")).toHaveValue("14.3");
    await page.locator("#bodyFatDialogRemove").click();
    await page.locator("#confirmationDialog").getByRole("button", { name: "Löschen" }).click();
    await expect(dialog).toBeHidden();
    await expect(row.locator("td").first()).toHaveText("17,1 %");

    await row.getByRole("button", { name: "Körperfett eintragen" }).click();
    await folds.click();
    await page.locator("#bodyFatDialogDate").fill("2026-11-10");
    await page.locator("#bodyFatDialogDate").dispatchEvent("change");
    await expect(dialog.getByLabel("Brust (mm)")).toHaveValue("16");
    await page.locator("#bodyFatDialogRemove").click();
    await page.locator("#confirmationDialog").getByRole("button", { name: "Löschen" }).click();
    await expect(dialog).toBeHidden();
    await expect(row.locator("td").first()).toHaveText("—");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  });
}

// #3556: each site takes up to three readings, the result uses their median, and with enough
// repeats the dialog shows the athlete's own repeatability.
test("@responsive three readings per site give their median and the repeatability", async ({ page }) => {
  await page.goto("/#nutrition/body");
  // Four earlier days with repeats: 12 sites, pooled 1.054 mm -> ± 0,4 %-Punkte (unit test).
  await page.evaluate(async () => {
    await api("/api/profile", { method: "PUT", body: JSON.stringify({ birth_year: "1986" }) });
    for (const date of ["2026-10-20", "2026-10-27", "2026-11-03", "2026-11-04"]) {
      await api("/api/body/skinfolds", { method: "PUT", body: JSON.stringify({ date, folds_mm: { chest: [16, 17, 18], abdomen: [20, 20, 22], thigh: [17, 18, 19] } }) });
    }
  });
  await page.reload();
  const row = page.locator("[data-body-overview] tr[data-metric='body_fat_pct']");
  const dialog = page.locator("#bodyFatDialog");
  await row.getByRole("button", { name: "Körperfett eintragen" }).click();
  await dialog.getByRole("button", { name: "Hautfalten" }).click();
  await page.locator("#bodyFatDialogDate").fill("2026-11-10");
  await page.locator("#bodyFatDialogDate").dispatchEvent("change");
  // Brust 16, 18, 17 -> median 17; 17/20/17 mm at 40 -> 17,4 %.
  await dialog.getByLabel("Brust (mm)").fill("16");
  await dialog.getByLabel("Brust, Messung 2 (mm)").fill("18");
  await dialog.getByLabel("Brust, Messung 3 (mm)").fill("17");
  await dialog.getByLabel("Bauch (mm)").fill("20");
  await dialog.getByLabel("Oberschenkel (mm)").fill("17");
  await expect(page.locator("#bodyFatDialogResult")).toHaveText("Ergibt 17,4 % Körperfett · Deine Messgenauigkeit: ± 0,4 %-Punkte (aus 12 Wiederholungen)");
  expect(await new AxeBuilder({ page }).include("#bodyFatDialog").withTags(["wcag2aa"]).analyze()).toEqual(expect.objectContaining({ violations: [] }));
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  await expect(row.locator("td").first()).toHaveText("17,4 %");
  // Reopened, the day fills its readings again.
  await row.getByRole("button", { name: "Körperfett eintragen" }).click();
  await dialog.getByRole("button", { name: "Hautfalten" }).click();
  await page.locator("#bodyFatDialogDate").fill("2026-11-10");
  await page.locator("#bodyFatDialogDate").dispatchEvent("change");
  await expect(dialog.getByLabel("Brust, Messung 2 (mm)")).toHaveValue("18");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});

// #3252: the pencil in the weight row enters a weight with its date; it becomes the latest
// weight ("Lokal") and can be removed again.
test("@responsive a weight is entered and removed through the weight dialog", async ({ page }) => {
  await page.goto("/#nutrition/body");
  const row = page.locator("tr[data-metric='weight_kg']");
  const dialog = page.locator("#weightDialog");
  await expect(row).toBeVisible();
  await expect(dialog).toBeHidden();

  await row.getByRole("button", { name: "Gewicht eintragen" }).click();
  await expect(dialog).toBeVisible();
  await expect(page.locator("#weightDialogRemove")).toBeHidden();
  const today = await page.locator("#weightDialogDate").inputValue();
  await page.locator("#weightDialogKg").fill("123.4");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  await expect(row.locator("td").first()).toContainText("123,4");
  // #3464: one weight series; the entry is today's mark, no second series beside Garmin's.
  const chart = page.locator("#bodyAnalysisCharts [data-body-metric='weight_kg']").first();
  await expect(chart.locator("circle[data-series='0']").last()).toBeAttached();
  await expect(chart.locator("circle[data-series='1']")).toHaveCount(0);
  await chart.locator("details > summary", { hasText: "Werte ansehen" }).click();
  // Newest first: today's entry is the first row.
  await expect(chart.locator(".analysis-chart-table tbody tr").first()).toContainText("eingetragen");

  await row.getByRole("button", { name: "Gewicht eintragen" }).click();
  await expect(page.locator("#weightDialogDate")).toHaveValue(today);
  await expect(page.locator("#weightDialogKg")).toHaveValue("123.4");
  await page.locator("#weightDialogRemove").click();
  await page.locator("#confirmationDialog").getByRole("button", { name: "Löschen" }).click();
  await expect(dialog).toBeHidden();
  await expect(row.locator("td").first()).not.toContainText("123,4");

  // #3403: the receipt's "Rückgängig" puts the removed weight back. The fixture has no Garmin
  // write login, so nothing is sent and the receipt names only the local change.
  const toast = page.locator("#toast");
  await expect(toast).toContainText("Gewicht gelöscht.");
  await toast.getByRole("button", { name: "Rückgängig" }).click();
  await expect(toast).toContainText("Zurückgenommen.");
  await expect(row.locator("td").first()).toContainText("123,4");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
});
