const { test, expect } = require("@playwright/test");
const { expandConnections } = require("./helpers/connections");

// The fixture runtime has no Intervals.icu or Garmin credentials, so provider
// actions must be disabled and the setup hint must be shown.

test("@responsive provider sync actions are disabled with a setup hint without configuration", async ({ page }) => {
  await page.goto("/#more/connections");
  await expect(page.locator("#appShell")).toBeVisible();
  await expandConnections(page);
  await expect(page.locator("#intervalsConnectionStatus")).not.toHaveText("Status wird geprüft…");
  await expect(page.locator("#systemIntervalsSyncButton")).toHaveCount(0);
  await expect(page.locator("#systemIntervalsFullResyncButton")).toHaveCount(0);
  await expect(page.locator("#garminSyncButton")).toBeDisabled();
  await expect(page.locator("#garminFullResyncButton")).toBeDisabled();

  const intervalsHint = page.locator("#intervalsSetupHint");
  await expect(intervalsHint).toBeVisible();
  await expect(intervalsHint).toContainText("Intervals.icu ist nicht eingerichtet");
  await expect(intervalsHint).toContainText("Server-Einstellungen");
  await expect(intervalsHint).not.toContainText("INTERVALS_API_KEY");
  await expect(page.locator("#garminSetupHint")).toBeVisible();
  // A-043: the hint names both in-app ways to connect Garmin.
  await expect(page.locator("#garminSetupHint")).toHaveText("Garmin ist nicht verbunden. Melde dich unter „Garmin-Konto“ an oder hinterlege unter „Personal API“ einen Schlüssel.");
});
