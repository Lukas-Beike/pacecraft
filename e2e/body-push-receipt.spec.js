const { test, expect } = require("@playwright/test");

// The weight save and the sync status are answered here, so the fixture keeps its state and no
// provider is involved; routes need the worker blocked (#3842).
test.use({ serviceWorkers: "block" });

// #4267 (option B): the save answers at once, the toast follows the Garmin and Wahoo pushes through
// the sync poll and ends on the done line, or on the existing pending text when a push failed.
async function saveWithPushes(page, finished) {
  const pushes = { phase: "running", day: "" };
  await page.route(/\/api\/body\/weight$/, (route) => (route.request().method() === "PUT"
    ? route.fulfill({ json: { entries: [], remote: "running", remote_wahoo: "running" } })
    : route.fallback()));
  await page.route(/\/api\/sync\/status$/, async (route) => {
    try {
      const response = await route.fetch();
      const body = await response.json();
      const running = pushes.phase === "running";
      body.body_push = running
        ? { garmin: { running: [pushes.day], pending: [] }, wahoo: { running: true, pending: true } }
        : finished(pushes.day);
      await route.fulfill({ response, json: body });
    } catch {
      // The test ended with a request still in flight.
    }
  });
  await page.goto("/#nutrition/body");
  await page.getByRole("button", { name: "Gewicht eintragen" }).first().click();
  await expect(page.locator("#weightDialog")).toBeVisible();
  pushes.day = await page.locator("#weightDialogForm [name=date]").inputValue();
  await page.locator("#weightDialogKg").fill("78.4");
  await page.locator("#weightDialogSave").click();
  await expect(page.locator("#weightDialog")).toBeHidden();
  const toast = page.locator("#toast");
  await expect(toast).toContainText("Gewicht 78,4 kg");
  await expect(toast).toContainText("gespeichert.");
  await expect(toast.getByRole("button", { name: "Rückgängig" })).toBeVisible();
  await expect(toast.locator(".body-push")).toHaveText("An Garmin und Wahoo senden…");
  await expect(toast.locator(".body-push .button-spinner")).toBeVisible();
  pushes.phase = "done";
  return toast.locator(".body-push");
}

test("@responsive a weight save shows the push line and then the done line", async ({ page }) => {
  const line = await saveWithPushes(page, () => ({ garmin: { running: [], pending: [] }, wahoo: { running: false, pending: false } }));
  await expect(line).toHaveText("✓ In Garmin und Wahoo gespeichert");
  await expect(line.locator(".button-spinner")).toHaveCount(0);
});

test("@responsive a failed push shows the existing pending text", async ({ page }) => {
  const line = await saveWithPushes(page, (day) => ({ garmin: { running: [], pending: [day] }, wahoo: { running: false, pending: false } }));
  await expect(line).toHaveText("✓ In Wahoo gespeichert · Garmin-Sync fehlgeschlagen – wird erneut versucht.");
});
