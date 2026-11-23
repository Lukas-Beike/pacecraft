const { test, expect } = require("@playwright/test");

async function openCoachWithScrollableChat(page) {
  await page.goto("/#coach");
  await expect(page.locator("#appShell")).toBeVisible();
  await expect.poll(() => page.evaluate(() => !state.loadPromise)).toBe(true);
  await expect(page.locator("#chatPanel")).toHaveClass(/active/);
  // Synthetic spacer that makes the chat page taller than the viewport.
  await page.evaluate(() => {
    const spacer = document.createElement("div");
    spacer.dataset.e2eSpacer = "true";
    spacer.style.height = "2400px";
    document.querySelector("#messages").append(spacer);
  });
  // Let scroll anchoring settle before the test measures and changes the page position.
  await nextFrames(page);
}

async function nextFrames(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function expectComposerBottomStable(page) {
  const maxScroll = await page.evaluate(() => document.documentElement.scrollHeight - globalThis.innerHeight);
  expect(maxScroll).toBeGreaterThan(0);

  const composer = page.locator("#chatForm");
  const bottoms = [];
  for (const top of [0, Math.round(maxScroll / 2), maxScroll]) {
    await page.evaluate((y) => globalThis.scrollTo({ top: y, behavior: "instant" }), top);
    await nextFrames(page);
    const scrollY = await page.evaluate(() => globalThis.scrollY);
    expect(Math.abs(scrollY - top)).toBeLessThanOrEqual(1);
    const box = await composer.boundingBox();
    expect(box).not.toBeNull();
    bottoms.push(box.y + box.height);
  }

  for (const bottom of bottoms) {
    expect(Math.abs(bottom - bottoms[0])).toBeLessThanOrEqual(1);
  }
}

test("coach composer keeps a stable bottom edge and publishes its height @responsive", async ({ page }) => {
  await openCoachWithScrollableChat(page);

  const composerHeight = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--composer-height").trim());
  expect(composerHeight).toMatch(/^\d+(\.\d+)?px$/);
  expect(Number.parseFloat(composerHeight)).toBeGreaterThan(0);

  await expectComposerBottomStable(page);
});

test("coach composer keeps a stable bottom edge in phone landscape @responsive", async ({ page }) => {
  await page.setViewportSize({ width: 700, height: 400 });
  await openCoachWithScrollableChat(page);

  await expectComposerBottomStable(page);
});

test("empty coach chat does not promise quick actions and keeps the composer gap @responsive", async ({ page }) => {
  await page.goto("/#coach");
  await expect(page.locator("#appShell")).toBeVisible();
  await expect.poll(() => page.evaluate(() => !state.loadPromise)).toBe(true);
  await expect(page.locator("#chatPanel")).toHaveClass(/active/);
  // Quick actions appear only after inactivity, so a first visit must not be pointed at them.
  await page.evaluate(() => { state.quickTemplatesVisible = false; renderQuickMessageTemplates(); });
  const empty = page.locator("#messages .empty-state");
  await expect(empty).toBeVisible();
  await expect(empty).not.toContainText("Schnellaktion");
  await expect(page.locator("#chatJumpToComposer")).toBeHidden();
  // A first start lists the three setup steps and offers the first open one (#3936 G-13).
  await expect(empty.locator(".coach-setup")).toContainText("Einrichten ·");
  await expect(empty.locator(".coach-setup li")).toHaveCount(3);
  await page.evaluate(() => { state.data.profile = {}; renderMessages([]); });
  await expect(empty.locator('li[data-step="profile"]')).toHaveAttribute("data-done", "false");
  // The one action asks the Coach to fill the profile; the request is caught before it is sent.
  await page.evaluate(() => document.querySelector("#chatForm").addEventListener("submit", (event) => {
    globalThis.e2eProfileRequest = document.querySelector("#messageInput").value;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, { capture: true, once: true }));
  await empty.getByRole("button", { name: "Profil ausfüllen" }).click();
  expect(await page.evaluate(() => globalThis.e2eProfileRequest)).toBe("Hilf mir, mein Profil und meine Ziele auszufüllen.");
  await expect(page.locator("#messageInput")).toHaveValue("");

  const nav = page.locator(".bottom-nav");
  const composerGap = async () => {
    await nextFrames(page);
    const composer = await page.locator("#chatForm").boundingBox();
    return (await nav.boundingBox()).y - (composer.y + composer.height);
  };
  if (!(await nav.isVisible())) return;
  const emptyGap = await composerGap();
  await page.evaluate(() => {
    const spacer = document.createElement("div");
    spacer.dataset.e2eSpacer = "true";
    spacer.style.height = "2400px";
    document.querySelector("#messages").append(spacer);
  });
  expect(Math.abs(emptyGap - (await composerGap()))).toBeLessThanOrEqual(1);
});

test("@responsive the desktop Coach view reads the fuelling of the open unit once, after its own reads", async ({ page, request }, testInfo) => {
  // #2991: on wide screens the week opens its next unit, and the start read the same
  // fuelling twice while the view's own reads were still running.
  test.skip(testInfo.project.name !== "desktop", "Only the wide calendar opens a unit by itself.");
  expect((await request.get("/api/fixture/demo")).ok()).toBeTruthy();
  let reads = 0;
  await page.route("**/api/nutrition/session-fuelling*", (route) => {
    reads += 1;
    return route.fulfill({ json: { status: "not_long" } });
  });
  // #3056: the calendar comes with /api/plan and replaces the bootstrap's; patched only in the
  // bootstrap, a fixture unit opened instead, and a completed one reads no fuelling at all.
  for (const area of ["/api/bootstrap", "/api/plan"]) {
    await page.route((url) => url.pathname === area, async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      const today = new Intl.DateTimeFormat("en-CA", { timeZone: data.profile?.timezone || "Europe/Berlin" }).format(new Date());
      data.training_calendar = [{ id: "long-ride", local_id: "long-ride", date: today, name: "Lange Ausfahrt", type: "Ride", sport: "ride", environment: "outdoor", duration_minutes: 150, planned_duration_s: 9000 }];
      return route.fulfill({ json: data });
    });
  }
  // The Coach view's chat history answers late, as a slow read on a long history would.
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/chat/history?limit=100", async (route) => { await held; await route.continue(); });
  await page.goto("/#coach");
  await page.waitForFunction(() => state.data && !state.replaceDataOnLoad);
  await expect(page.locator(".planned-entry[open]")).toHaveCount(1);
  // Justified wait: the read would go out now if it joined the view's burst; nothing shows it.
  await page.waitForTimeout(1500);
  expect(reads).toBe(0);
  release();
  await expect.poll(() => reads).toBe(1);
  await expect.poll(() => page.evaluate(() => state.initialStateLoaded && !state.loadPromise)).toBe(true);
  await page.waitForTimeout(1500);
  expect(reads).toBe(1);
  await expect(page.locator(".planned-entry[open]")).toContainText("Lange Ausfahrt");
});
