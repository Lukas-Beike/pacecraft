const { test, expect } = require("@playwright/test");

test("retained Body Battery is dated instead of being presented as current", async ({ page }) => {
  await page.goto("/#more/connections");
  await expect(page.locator("#appShell")).toBeVisible();
  await expect.poll(() => page.evaluate(() => !state.loadPromise)).toBe(true);
  await page.evaluate(() => renderGarmin({
    available: true, configured: true, source: "library", last_sync_at: "2026-09-08T11:10:00Z",
    morning_body_battery: { status: "ready", sleep_date: "2026-09-07", before_sleep: { value: 30 }, morning: { value: 78 } },
  }));
  await expect(page.locator("#garminDetail")).toContainText("Body Battery am");
  await expect(page.locator("#garminDetail")).toContainText("78 nach dem Aufwachen");
  await expect(page.locator("#garminDetail")).not.toContainText("78 aktuell");
});

test("@responsive a watch that has not uploaded for a day is named in the Garmin card and at recovery", async ({ page }) => {
  await page.goto("/#more/connections");
  await expect(page.locator("#appShell")).toBeVisible();
  await expect.poll(() => page.evaluate(() => !state.loadPromise)).toBe(true);
  const garmin = (hoursAgo) => ({
    available: true, configured: true, source: "library", last_sync_at: new Date().toISOString(),
    device_synced_at: new Date(Date.now() - hoursAgo * 3_600_000).toISOString(),
  });
  // #2807: a fresh upload needs no sentence; from 24 hours on the card says so.
  await page.evaluate((value) => renderGarmin(value), garmin(3));
  await expect(page.locator("#garminDetail .device-sync-stale")).toHaveCount(0);
  await page.evaluate((value) => { state.data.garmin = value; renderGarmin(value); }, garmin(50));
  await expect(page.locator("#garminDetail .device-sync-stale")).toHaveText("Die Uhr hat seit 2 Tagen nichts übertragen.");
  await page.evaluate(async () => { await AppRouter.navigate("analysis/recovery", { historyMode: "replace" }); renderPersonalRecovery(state.data.performance?.personal_recovery); });
  await expect(page.locator("#personalRecovery > .device-sync-stale")).toHaveText("Die Uhr hat seit 2 Tagen nichts übertragen.");
});
