const { test, expect } = require("@playwright/test");

// #1891: the measured first threshold form and the GPS-tracks switch.
test.use({ serviceWorkers: "block" });

async function openMore(page, segment) {
  await page.goto(`/#more/${segment}`);
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
}

test("saving a measured first threshold stores the bpm, shows its percent and leaves nothing to save", async ({ page }) => {
  // #4080: the save reaches this spec's own fixture server, so a background reload
  // after it brings back the stored value instead of an unsaved one.
  const puts = [];
  page.on("request", (request) => {
    if (request.method() === "PUT" && new URL(request.url()).pathname === "/api/profile") puts.push(request.postDataJSON());
  });
  await openMore(page, "profile");
  const form = page.locator("#profileForm");
  // #2262: the entry sits behind "Bearbeiten" on LT1 in Zonen & Schwellen.
  await page.locator("#zoneSettings > summary").click();
  await page.locator('#zoneOverview [data-zone-box="thresholds"]').getByRole("button", { name: "Bearbeiten" }).click();
  // #3915: one bpm field, stored as entered; no date and no percent input.
  await expect(form.locator('[name="first_threshold_on"]')).toHaveCount(0);
  await expect(form.locator('[name="first_threshold_pct"]')).toHaveCount(0);

  const original = await page.evaluate(() => state.profileBaseline.first_threshold_hr ?? "");
  try {
    await form.locator('[name="first_threshold_hr"]').fill("150");
    await form.getByRole("button", { name: "Profil speichern" }).click();

    await expect(page.locator("#toast")).toContainText("Profil gespeichert");
    await expect(page.locator("#toast")).not.toContainText("unvollständig");
    expect(puts[0]).toMatchObject({ first_threshold_hr: "150" });
    // A reload started before the save may still land; the one after it reads the stored value.
    await page.waitForFunction(() => !state.loadPromise && !state.pendingLoads.size && !state.deferredLoad);
    const baseline = await page.evaluate(() => state.profileBaseline);
    expect(baseline.first_threshold_hr).toBe("150");
    // Stored on the server as entered, with a measured origin.
    const stored = await (await page.request.get("/api/profile")).json();
    expect(stored.profile).toMatchObject({ first_threshold_hr: "150", first_threshold_origin: "measured" });

    // Nothing left to save: the bar is gone.
    await expect(form.locator(".save-bar")).toBeHidden();
    await expect(page.locator("#profileDirtyIndicator")).toBeHidden();
    await page.waitForTimeout(500);
    expect(puts.filter((body) => "first_threshold_hr" in body)).toHaveLength(1);
    await expect(page.locator("#toast")).not.toContainText("unvollständig");
  } finally {
    // Restore the fixture's threshold for the later tests, also after a failure.
    if (puts[0]) await page.evaluate((body) => api("/api/profile", { method: "PUT", body: JSON.stringify(body) }), { ...puts[0], first_threshold_hr: original });
  }
});

async function openPrivacy(page) {
  await openMore(page, "operations");
  const panel = page.locator("#privacySection");
  const loaded = page.waitForResponse((response) => response.url().endsWith("/api/privacy/gps") && response.request().method() === "GET");
  if (!(await panel.evaluate((element) => element.open))) await panel.locator("summary").click();
  // #2390: start from the state the server reported, once the switch shows it.
  const response = await loaded;
  expect(response.ok()).toBe(true);
  const enabled = Boolean((await response.json()).enabled);
  await expect(page.locator("#gpsTracksToggle")).toBeChecked({ checked: enabled });
  return enabled;
}

async function flipGpsTracks(page) {
  const saved = page.waitForResponse((response) => response.url().endsWith("/api/privacy/gps") && response.request().method() === "POST");
  await page.locator("#gpsTracksToggle").click();
  expect((await saved).ok()).toBe(true);
}

test("the GPS-tracks switch persists across a reload", async ({ page }) => {
  const before = await openPrivacy(page);
  try {
    await flipGpsTracks(page);
    await expect(page.locator("#gpsTracksToggle")).toBeChecked({ checked: !before });
    await page.reload();
    expect(await openPrivacy(page)).toBe(!before);
  } finally {
    // Restore the fixture state for later specs, also after a failure.
    await page.reload();
    if ((await openPrivacy(page)) !== before) await flipGpsTracks(page);
  }
  await expect(page.locator("#gpsTracksToggle")).toBeChecked({ checked: before });
});

test("the GPS-tracks switch takes clicks once loaded and a failed save restores the confirmed state", async ({ page }) => {
  // #2418: no click before the stored state is shown.
  const isGet = (response) => response.url().endsWith("/api/privacy/gps") && response.request().method() === "GET";
  let releaseLoad;
  let gets = 0;
  const loadHeld = new Promise((resolve) => { releaseLoad = resolve; });
  await page.route("**/api/privacy/gps", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    gets += 1;
    await loadHeld;
    return route.continue();
  });
  await openMore(page, "operations");
  const summary = page.locator("#privacySection summary");
  const toggle = page.locator("#gpsTracksToggle");
  await expect(page.locator("#privacySection")).not.toHaveAttribute("open", "");
  const loaded = page.waitForResponse(isGet);
  await summary.click();
  await expect(toggle).toBeDisabled();
  releaseLoad();
  const before = Boolean((await (await loaded).json()).enabled);
  await expect(toggle).toBeEnabled();
  await expect(toggle).toBeChecked({ checked: before });

  // A pending save takes no click, and a failed one restores the server-confirmed state.
  let failSave;
  const saveHeld = new Promise((resolve) => { failSave = resolve; });
  await page.route("**/api/privacy/gps", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await saveHeld;
    return route.fulfill({ status: 500, json: { error: "Synthetischer Fehler" } });
  });
  await toggle.click();
  await expect(toggle).toBeChecked({ checked: !before });
  await expect(toggle).toBeDisabled();
  // Reopening during the save loads nothing that could overwrite the switch.
  const getsBeforeReopen = gets;
  await summary.click();
  await summary.click();
  await expect(page.locator("#privacySection")).toHaveAttribute("open", "");
  // Give a load a moment to start; none may run, and the switch stays locked.
  await page.waitForTimeout(500);
  expect(gets).toBe(getsBeforeReopen);
  await expect(toggle).toBeDisabled();
  failSave();
  await expect(page.locator("#toast")).toBeVisible();
  await expect(toggle).toBeChecked({ checked: before });
  await expect(toggle).toBeEnabled();
});
