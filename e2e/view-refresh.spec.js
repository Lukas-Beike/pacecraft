const { test, expect } = require("@playwright/test");

test("@responsive view refresh targets its active area and ignores stale views", async ({ page }) => {
  const posts = [];
  const broadRefreshes = [];
  let releaseRefresh;
  let holdNutritionRefresh = false;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (request.method() === "POST" && (url.pathname.startsWith("/api/sync") || url.pathname === "/api/nutrition/sync")) broadRefreshes.push(url.pathname);
  });
  await page.route("**/api/analysis/refresh", async (route) => {
    posts.push({ path: "/api/analysis/refresh", body: route.request().postDataJSON() });
    await route.fulfill({ json: { status: "complete" } });
  });
  await page.route("**/api/nutrition/refresh", async (route) => {
    posts.push({ path: "/api/nutrition/refresh", body: route.request().postDataJSON() });
    if (holdNutritionRefresh) await new Promise((resolve) => { releaseRefresh = resolve; });
    await route.fulfill({ json: { status: "partial" } });
  });

  await page.goto("/#analysis/load");
  await expect(page.locator("#appShell")).toBeVisible();
  const analysisRefresh = page.locator("#analysisRefreshButton");
  await expect(analysisRefresh).toHaveAttribute("aria-label", "Analyse aktualisieren: Belastung");
  const analysisBox = await analysisRefresh.boundingBox();
  expect(analysisBox.width).toBeGreaterThanOrEqual(44);
  expect(analysisBox.height).toBeGreaterThanOrEqual(44);
  await analysisRefresh.click();
  await expect(page.locator("#analysisRefreshStatus")).toHaveText("Aktualisiert.");

  await page.evaluate(() => AppRouter.navigate("nutrition/diary", { historyMode: "none" }));
  await page.locator("#nutritionDate").fill("2026-10-19");
  await page.locator("#nutritionDate").dispatchEvent("change");
  const nutritionRefresh = page.locator("#nutritionRefreshButton");
  await expect(nutritionRefresh).toHaveAttribute("aria-label", "Ernährung aktualisieren: Tagebuch");
  await nutritionRefresh.click();
  await expect(page.locator("#nutritionRefreshStatus")).toHaveText("Teilweise aktualisiert.");

  await page.evaluate(() => AppRouter.navigate("nutrition/week", { historyMode: "none" }));
  await page.getByRole("button", { name: "6 Monate", exact: true }).click();
  await expect(nutritionRefresh).toHaveAttribute("aria-label", "Ernährung aktualisieren: Verlauf, 6 Monate");
  holdNutritionRefresh = true;
  releaseRefresh = null;
  await nutritionRefresh.click();
  await expect.poll(() => typeof releaseRefresh).toBe("function");
  expect(posts.filter((post) => post.path === "/api/nutrition/refresh")).toHaveLength(2);
  await page.getByRole("button", { name: "4 Wochen", exact: true }).click();
  releaseRefresh();
  await expect(nutritionRefresh).toBeEnabled();
  await expect(page.locator("#nutritionRefreshStatus")).toBeEmpty();

  holdNutritionRefresh = true;
  releaseRefresh = null;
  await nutritionRefresh.click();
  await expect.poll(() => typeof releaseRefresh).toBe("function");
  expect(posts.filter((post) => post.path === "/api/nutrition/refresh")).toHaveLength(3);
  await page.evaluate(() => AppRouter.navigate("analysis/running", { historyMode: "none" }));
  releaseRefresh();
  await expect(nutritionRefresh).toBeEnabled();
  await expect(page.locator("#dataPanel")).toHaveClass(/active/);
  await expect(page.locator("#nutritionRefreshStatus")).toBeEmpty();

  expect(posts).toEqual([
    { path: "/api/analysis/refresh", body: { area: "load", days: 91 } },
    { path: "/api/nutrition/refresh", body: { area: "diary", date: "2026-10-19" } },
    { path: "/api/nutrition/refresh", body: { area: "week", days: 182 } },
    { path: "/api/nutrition/refresh", body: { area: "week", days: 28 } },
  ]);
  expect(broadRefreshes).toEqual([]);
});

// #4212: the real server takes the refresh of every Analyse tab at every period the tab offers; the route once
// refused the 28 and 91 days the client sends ("Ungültiger Analysezeitraum.").
test("@responsive the Analyse refresh is accepted on every tab at every period", async ({ page, request }) => {
  test.setTimeout(240_000);
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  const bodies = [];
  page.on("request", (item) => { if (item.method() === "POST" && new URL(item.url()).pathname === "/api/analysis/refresh") bodies.push(item.postDataJSON()); });
  await page.goto("/#analysis/running");
  await expect(page.locator("#appShell")).toBeVisible();
  for (const route of ["running", "cycling", "strength", "load", "recovery"]) {
    await page.evaluate((hash) => AppRouter.navigate(hash, { historyMode: "none" }), `analysis/${route}`);
    await expect(page.locator("#analysisRefreshButton")).toBeVisible();
    // The periods this tab offers: none on a tab without a period selector.
    const periods = [];
    for (const name of ["4 Wochen", "3 Monate", "6 Monate", "12 Monate"]) {
      if (await page.locator("#dataPanel").getByRole("button", { name, exact: true }).first().isVisible()) periods.push(name);
    }
    for (const period of periods.length ? periods : [null]) {
      if (period) await page.locator("#dataPanel").getByRole("button", { name: period, exact: true }).first().click();
      const answered = page.waitForResponse((reply) => new URL(reply.url()).pathname === "/api/analysis/refresh" && reply.request().method() === "POST");
      await page.locator("#analysisRefreshButton").click();
      expect((await answered).status(), `${route} at ${period}`).toBe(200);
      await expect(page.locator("#analysisRefreshButton")).toBeEnabled();
      await expect(page.locator("#analysisRefreshStatus")).not.toContainText("Ungültiger");
    }
  }
  // Belastung and Erholung sent a window; the other tabs sent none.
  expect(bodies.some((body) => body.area === "load" && body.days === 91)).toBeTruthy();
  expect(bodies.some((body) => body.area === "recovery" && body.days === 28)).toBeTruthy();
});
