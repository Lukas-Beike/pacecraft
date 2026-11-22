const { test, expect } = require("@playwright/test");
const { captureReadFixture, installReadFixture } = require("./read-fixture");

const TECHNICAL_TEXT = /TypeError|Failed to fetch|network_error|NetworkError/;
const NETWORK_MESSAGE = "Keine Verbindung zum Coach-Server. Prüfe dein Netzwerk und versuche es erneut.";

let fixture;
test.beforeAll(async ({ request }) => { fixture = await captureReadFixture(request); });
test.beforeEach(async ({ page }) => { await installReadFixture(page, fixture); });

async function ready(page) {
  // The SSE transport is irrelevant to these checks; abort it so it does not hold fixture slots.
  await page.route("**/api/state/events*", (route) => route.abort());
  await page.goto("/");
  await expect(page.locator("#appShell")).toBeVisible();
  await expect.poll(() => page.evaluate(() => state.loadPromise === null)).toBe(true);
  await expect.poll(() => page.evaluate(() => Boolean(state.data))).toBe(true);
}

test("a failed request without a server response shows an honest German message @responsive", async ({ page }) => {
  await ready(page);
  await page.route("**/api/profile", (route) => (route.request().method() === "PUT"
    ? route.abort("internetdisconnected")
    : route.fallback()));
  await page.evaluate(() => { location.hash = "more/profile"; });
  await expect(page.locator("#profilePanel")).toBeVisible();
  // The save bar appears with a change the field reports.
  await page.locator('#profileForm [name="name"]').evaluate((field) => {
    field.value = `${field.value} `;
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.getByRole("button", { name: "Profil speichern" }).click();
  const toast = page.locator("#toast");
  await expect(toast).toContainText(NETWORK_MESSAGE);
  await expect(toast).toHaveClass(/error/);
  await expect(toast).not.toContainText(TECHNICAL_TEXT);
  await expect(page.locator("body")).not.toContainText(TECHNICAL_TEXT);
});

test("the offline banner says that entries wait and are sent later @responsive", async ({ page, context }) => {
  await ready(page);
  // Under 500 px height only critical notices show text (#1784); the offline notice needs a tall screen.
  await page.setViewportSize({ width: page.viewportSize().width, height: 800 });
  const notice = page.locator("#statusStrip .status-strip-main .status-strip-text");
  await context.setOffline(true);
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("Offline Eigene Einträge warten und werden später gesendet.");
  await expect(notice).toContainText(/Stand (gerade eben|vor \d+ min)/);
  await context.setOffline(false);
  await expect(page.locator("#statusStrip")).not.toContainText("Offline");
});

test("stale provider data is a polite status while real provider errors stay alerts @responsive", async ({ page }) => {
  await ready(page);
  const banner = page.locator("#statusStrip");
  await page.evaluate(() => renderProviderAttention({
    provider_freshness: [{ configured: true, state: "stale", label: "Garmin" }],
  }));
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Garmin benötigt manuelles Eingreifen.");
  await expect(banner).toHaveAttribute("role", "status");
  await expect(banner).toHaveAttribute("aria-live", "polite");
  await page.evaluate(() => renderProviderAttention({
    provider_freshness: [{ configured: true, state: "error", error_code: "auth_required", label: "Wahoo" }],
  }));
  await expect(banner).toHaveAttribute("role", "alert");
  await expect(banner).toHaveAttribute("aria-live", "assertive");
});

test("a Coach message sent without a connection returns to the composer and survives a reload @responsive", async ({ page, context }) => {
  await ready(page);
  await page.evaluate(() => { location.hash = "coach"; });
  const input = page.locator("#messageInput");
  await expect(input).toBeVisible();
  await context.setOffline(true);
  await input.fill("Dieser Satz wurde ohne Verbindung geschrieben.");
  await page.getByRole("button", { name: "Senden", exact: true }).click();
  await expect(page.locator("#toast")).toContainText("Keine Verbindung – nicht gesendet.");
  await expect(input).toHaveValue("Dieser Satz wurde ohne Verbindung geschrieben.");
  await expect(page.locator("#coachWorking")).toHaveCount(0);
  await expect(page.locator("#messages")).not.toContainText("Verbindung unterbrochen");
  await context.setOffline(false);
  await page.reload();
  await expect(page.locator("#appShell")).toBeVisible();
  await expect(page.locator("#messageInput")).toHaveValue("Dieser Satz wurde ohne Verbindung geschrieben.");
});

test("a toast does not follow the athlete to another screen @responsive", async ({ page }) => {
  await ready(page);
  await page.evaluate(() => { toast("Wettkampf gespeichert", false); });
  await expect(page.locator("#toast")).toHaveClass(/show/);
  // A toast a moment old belongs to the route change after it; the athlete's own tap comes later.
  await page.waitForTimeout(1600);
  await page.evaluate(() => { location.hash = "nutrition/diary"; });
  await expect(page.locator("#nutritionPanel")).toBeVisible();
  await expect(page.locator("#toast")).not.toHaveClass(/show/);
});
