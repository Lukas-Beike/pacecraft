const { test, expect, todayIn } = require("./helpers/today");

// #3954: Mehr › Betrieb lists the local change history; the list is read only once its section opens.
test("@responsive the change history loads when opened and undoes its newest entry", async ({ page, request }) => {
  const historyRequests = [];
  page.on("request", (item) => { if (item.method() === "GET" && item.url().includes("/api/change-history")) historyRequests.push(item.url()); });
  const timeZone = (await (await request.get("/api/bootstrap")).json()).profile?.timezone || undefined;
  await page.goto("/#more/operations");
  await expect(page.locator("#appShell")).toBeVisible();
  // A change to list: the answer "Ich bin krank" writes today's check-in with its history entry.
  await page.evaluate((date) => api("/api/infection-hint/ill", { method: "POST", body: JSON.stringify({ date }) }), todayIn(timeZone));
  expect(historyRequests).toHaveLength(0);
  const section = page.locator("#changeHistorySection");
  await section.locator("summary").click();
  const entry = page.locator("#changeHistoryList .change-history-item").first();
  await expect(entry).toContainText("Check-in");
  await expect(entry.getByRole("button", { name: "Rückgängig" })).toBeVisible();
  expect(historyRequests).toHaveLength(1);
  await expect(page.locator("#diagnosticsButton")).toBeAttached();
  await entry.getByRole("button", { name: "Rückgängig" }).click();
  await page.getByRole("button", { name: "Zurücknehmen" }).click();
  await expect(page.locator("#toast")).toContainText("Änderung zurückgenommen");
  const first = page.locator("#changeHistoryList .change-history-item").first();
  await expect(first).toContainText("zurückgenommen");
  await expect(first.getByRole("button")).toHaveCount(0);
  await expect(page.locator("#changeHistoryList").getByText("überholt")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
