const { test, expect } = require("./helpers/today");

// Illness and complaints are one row that starts closed unless the day has any (#4306); this opens it.
async function openUnwell(dialog) {
  const row = dialog.locator("details.checkin-unwell");
  if (await row.getAttribute("open") === null) await row.locator("summary").click();
  await expect(row).toHaveAttribute("open", "");
}

// Without a connected source the calendar is one empty state (#3936 G-13); the days need one.
test.use({ serviceWorkers: "block" });
test.beforeEach(async ({ page }) => {
  await page.route(/\/api\/(bootstrap|performance)(\?|$)/, async (route) => {
    try {
      const response = await route.fetch();
      const body = await response.json();
      if (body.garmin) body.garmin = { ...body.garmin, configured: true };
      await route.fulfill({ response, json: body });
    } catch {
      // The test ended with a request still in flight.
    }
  });
});

// #3953: today's calendar day opens the check-in form; a save shows its receipt and "Rückgängig" restores the day.
test("@responsive the calendar day's check-in form saves a correction and undoes it", async ({ page }) => {
  await page.goto("/#plan");
  const row = page.locator(".planned-day.is-today [data-checkin]");
  await expect(row).toBeVisible();
  await row.getByRole("button").click();
  const dialog = page.locator("#checkinDialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Stress (0–10)").fill("7");
  // Illness and complaints are one collapsed row (#4306).
  await openUnwell(dialog);
  await dialog.getByRole("button", { name: "+ Beschwerde" }).click();
  await dialog.locator("select[name=area]").selectOption("achilles");
  await dialog.locator("select[name=side]").selectOption("left");
  await dialog.locator("input[name=severity]").fill("4");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row).toContainText("Check-in gespeichert.");
  await expect(row).toContainText("Stress 7");
  await expect(page.locator(".planned-day.is-today")).toContainText("Achillessehne links 4/10");
  await row.getByRole("button", { name: "Rückgängig" }).click();
  await expect(row).toContainText("Zurückgenommen.");
  await expect(row).not.toContainText("Stress 7");
  await expect(page.locator(".planned-day.is-today")).not.toContainText("Achillessehne");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("@responsive an unchanged form saves nothing and a refused save keeps the form open", async ({ page }) => {
  const posts = [];
  page.on("request", (request) => { if (request.method() === "POST" && request.url().includes("/api/checkins")) posts.push(request.url()); });
  await page.goto("/#plan");
  const row = page.locator(".planned-day.is-today [data-checkin]");
  await row.getByRole("button").click();
  const dialog = page.locator("#checkinDialog");
  await dialog.getByRole("button", { name: "Abbrechen" }).click();
  await expect(dialog).toHaveCount(0);
  await row.getByRole("button").click();
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toHaveCount(0);
  expect(posts).toHaveLength(0);
  await row.getByRole("button").click();
  // The same area and side twice is refused by the server; the entered values stay.
  await openUnwell(dialog);
  for (const index of [0, 1]) {
    await dialog.getByRole("button", { name: "+ Beschwerde" }).click();
    await dialog.locator("input[name=severity]").nth(index).fill("2");
  }
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog.getByRole("status")).toContainText("jeden Bereich mit Seite nur einmal");
  await expect(dialog.locator("input[name=severity]").first()).toHaveValue("2");
  await expect(dialog).toBeVisible();
});

test("@responsive the form starts from the stored status and tags and keeps them when another tag is added", async ({ page }) => {
  await page.goto("/#plan");
  await page.evaluate(() => api("/api/checkins", { method: "POST", body: JSON.stringify({ day_status: "rest", tag_answers: { travel: true, late_meal: false }, symptoms: ["husten"] }) }));
  await page.reload();
  const row = page.locator(".planned-day.is-today [data-checkin]");
  await row.getByRole("button").click();
  const dialog = page.locator("#checkinDialog");
  await expect(dialog.getByLabel("Ruhetag")).toBeChecked();
  await expect(dialog.getByLabel("Reise")).toBeChecked();
  await expect(dialog.getByLabel("Husten")).toBeChecked();
  await dialog.getByLabel("Viel Stress").check();
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toHaveCount(0);
  const today = await page.evaluate(() => api("/api/feedback").then((feedback) => feedback.local_feedback.today));
  expect(today.tag_answers).toEqual({ travel: true, late_meal: false, high_stress: true });
  expect(today.day_status).toBe("rest");
  expect(today.symptoms).toEqual(["husten"]);
});

test("@responsive the reminder link opens the form, an earlier day is chosen by its date and a future day cannot be sent", async ({ page }) => {
  await page.goto("/#plan/checkin");
  const dialog = page.locator("#checkinDialog");
  await expect(dialog).toBeVisible();
  await expect(page).toHaveURL(/#plan\/overview$/);
  const [yesterday, tomorrow] = await page.evaluate(() => [addDateKey(todayIso(), -1), addDateKey(todayIso(), 1)]);
  const day = dialog.locator("input[name=checkin_date]");
  const save = dialog.getByRole("button", { name: "Speichern" });
  await day.fill(yesterday);
  // The chosen day's read replaces the fields: the form is typed into once it is back.
  await expect(save).toBeEnabled();
  await dialog.getByLabel("Stress (0–10)").fill("6");
  await save.click();
  await expect(page.locator("#toast")).toContainText("gespeichert");
  await expect(dialog).toHaveCount(0);
  const stored = await page.evaluate((date) => api(`/api/checkins?date=${date}`), yesterday);
  expect(stored.checkin.stress).toBe(6);
  await page.locator(".planned-day.is-today [data-checkin] button").click();
  await day.fill(tomorrow);
  await dialog.getByLabel("Stress (0–10)").fill("2");
  await save.click();
  // The field's own limit stops the form, so nothing is sent for a future day.
  expect(await day.evaluate((input) => input.validity.rangeOverflow)).toBe(true);
  await expect(dialog).toBeVisible();
});

test("@responsive the add button for a complaint follows the limit of eight", async ({ page }) => {
  await page.goto("/#plan");
  await page.locator(".planned-day.is-today [data-checkin] button").click();
  const dialog = page.locator("#checkinDialog");
  const add = dialog.getByRole("button", { name: "+ Beschwerde" });
  await openUnwell(dialog);
  for (let count = 0; count < 8; count += 1) await add.click();
  await expect(dialog.locator(".checkin-complaint")).toHaveCount(8);
  await expect(add).toBeHidden();
  await dialog.getByRole("button", { name: "Beschwerde entfernen" }).first().click();
  await expect(add).toBeVisible();
});

// #4306: the 0–10 scores are sliders. A key and a pointer set them, an untouched one stores nothing,
// and illness and complaints are one row that opens when the day has any.
test("@responsive the scores are sliders, an untouched one stores nothing, and complaints collapse", async ({ page }) => {
  await page.goto("/#plan/checkin");
  const dialog = page.locator("#checkinDialog");
  await expect(dialog).toBeVisible();
  const earlier = await page.evaluate(() => addDateKey(todayIso(), -3));
  await dialog.locator("input[name=checkin_date]").fill(earlier);
  const save = dialog.getByRole("button", { name: "Speichern" });
  await expect(save).toBeEnabled();
  const soreness = dialog.getByLabel("Muskelkater (0–10)");
  const stress = dialog.getByLabel("Stress (0–10)");
  const motivation = dialog.getByLabel("Motivation (0–10)");
  await expect(dialog.locator(".checkin-slider-value")).toHaveText(["–", "–", "–"]);
  await expect(motivation).toHaveAttribute("aria-valuetext", "nicht angegeben");
  // By keyboard: two steps up from the resting middle.
  await soreness.focus();
  await soreness.press("ArrowRight");
  await soreness.press("ArrowRight");
  await expect(soreness).toHaveValue("7");
  await expect(soreness).toHaveAttribute("aria-valuetext", "7");
  // By pointer: a tap on the track's left part.
  const box = await stress.boundingBox();
  await page.mouse.click(box.x + box.width * 0.2, box.y + box.height / 2);
  const tapped = Number(await stress.inputValue());
  expect(tapped).toBeLessThan(5);
  await expect(dialog.locator(".checkin-slider-value").nth(1)).toHaveText(String(tapped));
  // The thumb's row is a 44 px touch target.
  expect(box.height).toBeGreaterThanOrEqual(44);
  await expect(dialog.locator("details.checkin-unwell")).not.toHaveAttribute("open", "");
  await save.click();
  await expect(dialog).toHaveCount(0);
  const stored = await page.evaluate((date) => api(`/api/checkins?date=${date}`), earlier);
  expect([stored.checkin.soreness, stored.checkin.stress, stored.checkin.motivation ?? null]).toEqual([7, tapped, null]);
  // A day with a complaint opens the row at once and loads its scores onto the sliders.
  await page.evaluate((date) => api("/api/checkins", { method: "POST", body: JSON.stringify({ checkin_date: date, complaints: [{ area: "achilles", side: "left", severity: 3 }] }) }), earlier);
  await page.locator(".planned-day.is-today [data-checkin] button").click();
  await dialog.locator("input[name=checkin_date]").fill(earlier);
  await expect(dialog.locator("details.checkin-unwell")).toHaveAttribute("open", "");
  await expect(soreness).toHaveValue("7");
  await expect(dialog.locator(".checkin-slider-value").first()).toHaveText("7");
});
