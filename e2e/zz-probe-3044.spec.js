// TEMPORARY CI probe v3 for #3044 (draft PR, never merged). The real spec failed only on its
// first attempt, when test 2 ran right after test 1 in the same worker. This runs that pair
// 15 times in one worker and records, for test 2, how Playwright attributed the chat stream.
const { test, expect } = require("@playwright/test");

test.describe.configure({ retries: 0 });
const results = [];

const LOG_FETCHES = () => {
  globalThis.__probeFetches = 0;
  const original = globalThis.fetch;
  globalThis.fetch = function probeFetch(input, init) {
    if (String(input?.url || input).includes("/api/chat/stream")) globalThis.__probeFetches += 1;
    return original.call(this, input, init);
  };
};

for (let pair = 0; pair < 15; pair += 1) {
  test(`pair ${pair}: test 1`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop");
    await page.goto("/");
    await expect(page.locator("#appShell")).toBeVisible();
    await page.evaluate(() => jumpToLatestMessages());
    const file = { name: "route.gpx", mimeType: "application/gpx+xml", buffer: Buffer.from('<gpx><rte><rtept lat="0" lon="0"/><rtept lat="0" lon="0.01"/></rte></gpx>') };
    await page.locator("#attachmentInput").setInputFiles(file);
    await expect(page.locator("#chatAttachments")).toContainText("route.gpx");
    await page.getByRole("button", { name: "Anhang route.gpx entfernen" }).click();
    await page.locator("#attachmentInput").setInputFiles({ ...file, buffer: Buffer.from("invalid GPX") });
    await page.locator("#sendButton").click();
    await expect(page.locator("#chatAttachments")).toContainText("route.gpx");
    await page.getByRole("button", { name: "Anhang route.gpx entfernen" }).click();
    await page.locator("#messageInput").fill("");
    await page.locator("#attachmentInput").setInputFiles([file, { name: "chart.png", mimeType: "image/png", buffer: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFOsAAAAASUVORK5CYII=", "base64") }]);
    await expect(page.locator("#chatAttachments button")).toHaveCount(2);
    await page.locator("#sendButton").click();
    await expect(page.locator("#chatAttachments")).toBeHidden();
    await expect(page.locator("#messages")).toContainText("chart.png");
    await page.reload();
    await expect(page.locator("#messages")).toContainText("route.gpx");
  });

  test(`pair ${pair}: test 2`, async ({ page, context }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop");
    const seen = [];
    context.on("request", (request) => {
      if (!request.url().includes("/api/chat/stream")) return;
      let frame = "none";
      try { frame = request.frame() ? "frame" : "none"; } catch { frame = "throws"; }
      seen.push(`${request.serviceWorker() ? "sw" : "page"}:${frame}`);
    });
    await page.addInitScript(LOG_FETCHES);
    await page.goto("/");
    await expect(page.locator("#appShell")).toBeVisible();
    await page.evaluate(() => jumpToLatestMessages());
    const atRoute = await page.evaluate(() => !!navigator.serviceWorker.controller);
    let routed = 0;
    await page.route("**/api/chat/stream", async (route) => { routed += 1; await route.continue(); });
    await page.locator("#messageInput").fill(`First route ${pair}`);
    await page.locator("#attachmentInput").setInputFiles({ name: "first.gpx", mimeType: "application/gpx+xml", buffer: Buffer.from('<gpx><rte><rtept lat="0" lon="0"/></rte></gpx>') });
    await page.locator("#sendButton").click();
    await expect(page.locator("#messages")).toContainText(`First route ${pair}`);
    await page.waitForTimeout(1000);
    const atEnd = await page.evaluate(() => !!navigator.serviceWorker.controller);
    results.push(`${pair}:${+atRoute}${+atEnd}/r${routed}/f${await page.evaluate(() => globalThis.__probeFetches)}/${seen.join("+") || "-"}`);
  });
}

test("report", async ({}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop");
  throw new Error(`PROBE3044v3 ${results.join(" ")}`);
});
