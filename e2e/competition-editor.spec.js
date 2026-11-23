const { test, expect } = require("@playwright/test");

// #3837: the athlete adds, edits and deletes competitions in Kalender → Wettkämpfe.
const inDays = (days) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);

async function openSeason(page) {
  await page.goto("/#plan/season");
  // The fixture holds no competition, so the list starts empty.
  await expect(page.locator("#competitionList")).toContainText("Noch keine Wettkämpfe gespeichert.");
  await expect(page.locator("#competitionReadStatus")).toBeEmpty();
  await expect(page.locator("#competitionAdd")).toBeVisible();
}

async function addCompetition(page, fields) {
  const dialog = page.locator("#competitionDialog");
  await page.locator("#competitionAdd").click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "Wettkampf", exact: true })).toBeVisible();
  await expect(page.locator("#competitionDialogRemove")).toBeHidden();
  await dialog.getByLabel("Name").fill(fields.name);
  await dialog.getByLabel("Datum").fill(fields.date);
  if (fields.time) await dialog.getByLabel("Startzeit").fill(fields.time);
  if (fields.sport) await dialog.getByLabel("Sportart").selectOption({ label: fields.sport });
  if (fields.priority) await dialog.locator(`label:has(input[value="${fields.priority}"])`).click();
  if (fields.distance) await dialog.getByLabel("Distanz (km)").fill(fields.distance);
  if (fields.duration) await dialog.getByLabel("Erwartete Dauer").fill(fields.duration);
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
}

// A test that fails partway must not leave its competitions for the next one (review on #3837).
test.afterEach(async ({ page }) => {
  await page.evaluate(async () => {
    const { competitions = [] } = await api("/api/bootstrap?local=1");
    for (const item of competitions.filter((entry) => String(entry.name || "").startsWith("E2E "))) {
      await api("/api/competitions/remove", { method: "PUT", body: JSON.stringify({ id: item.id, confirmed: true }) });
    }
  }).catch(() => null);
});

const card = (page, name) => page.locator("#competitionList details.competition-card").filter({ hasText: name });

// "Löschen" in the dialog deletes at once; the receipt offers the undo (#3934 G-03).
async function removeFromDialog(page) {
  const dialog = page.locator("#competitionDialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Löschen" }).click();
}

test("@responsive an upcoming competition is added, edited and deleted at once with an undo", async ({ page }) => {
  await openSeason(page);
  const date = inDays(30);
  await addCompetition(page, { name: "E2E Stadtlauf", date, time: "09:30", sport: "Laufen", priority: "A", distance: "10", duration: "45:00" });
  await expect(page.locator("#competitionReceipt")).toContainText("Wettkampf gespeichert.");
  const created = card(page, "E2E Stadtlauf");
  await expect(created).toHaveCount(1);
  await expect(created.locator("summary")).toContainText("Laufen · 10,0 km · Priorität A");

  // The pencil opens the dialog with the stored values and leaves the card closed.
  await created.getByRole("button", { name: "Wettkampf bearbeiten" }).click();
  const dialog = page.locator("#competitionDialog");
  await expect(dialog).toBeVisible();
  await expect(created).not.toHaveAttribute("open", "");
  await expect(dialog.getByLabel("Name")).toHaveValue("E2E Stadtlauf");
  await expect(dialog.getByLabel("Datum")).toHaveValue(date);
  await expect(dialog.getByLabel("Startzeit")).toHaveValue("09:30");
  await expect(dialog.getByLabel("Sportart")).toHaveValue("Run");
  await expect(dialog.getByLabel("Distanz (km)")).toHaveValue("10");
  await expect(dialog.getByLabel("Erwartete Dauer")).toHaveValue("45:00");
  await expect(dialog.locator('input[name="priority"]:checked')).toHaveValue("A");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await dialog.getByLabel("Name").fill("E2E Stadtlauf 10 km");
  await dialog.getByLabel("Ort", { exact: true }).fill("Musterstadt");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  const edited = card(page, "E2E Stadtlauf 10 km");
  await expect(edited).toHaveCount(1);
  await edited.locator("summary .competition-summary-main").click();
  await expect(edited).toContainText("Musterstadt");
  await expect(edited).toContainText("09:30");

  // Löschen deletes at once, with no second step.
  await edited.getByRole("button", { name: "Wettkampf bearbeiten" }).click();
  await removeFromDialog(page);
  await expect(dialog).toBeHidden();
  await expect(card(page, "E2E Stadtlauf")).toHaveCount(0);
  await expect(page.locator("#competitionReceipt")).toContainText("Wettkampf gelöscht.");

  // The receipt takes the deletion back.
  await page.locator("#competitionReceipt .message-undo").click();
  await expect(page.locator("#competitionReceipt")).toContainText("Zurückgenommen.");
  await expect(card(page, "E2E Stadtlauf 10 km")).toHaveCount(1);
  await card(page, "E2E Stadtlauf 10 km").getByRole("button", { name: "Wettkampf bearbeiten" }).click();
  await removeFromDialog(page);
  await expect(card(page, "E2E Stadtlauf")).toHaveCount(0);
});

test("@responsive a past competition in the archive is edited with its derived result shown as text", async ({ page }) => {
  // The result is derived from the matching activity; the fixture has none, so the read adds one.
  await page.route("**/api/profile", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const response = await route.fetch();
    const data = await response.json();
    const result = { sport: "run", duration_s: 2400, distance_m: 10000, avg_speed_mps: 10000 / 2400, source: "Garmin Connect" };
    return route.fulfill({ json: { ...data, competitions: data.competitions.map((item) => (item.name === "E2E Archiv" ? { ...item, result } : item)) } });
  });
  await openSeason(page);
  await addCompetition(page, { name: "E2E Archiv", date: "2020-03-01", sport: "Radfahren", priority: "C" });
  const archive = page.locator(".competition-archive");
  await archive.locator(":scope > summary").click();
  const past = archive.locator("details.competition-card").filter({ hasText: "E2E Archiv" });
  await expect(past).toHaveCount(1);
  await past.getByRole("button", { name: "Wettkampf bearbeiten" }).click();
  const dialog = page.locator("#competitionDialog");
  await expect(dialog).toBeVisible();
  await expect(page.locator("#competitionDialogResult")).toContainText("Ergebnis");
  await expect(page.locator("#competitionDialogResult")).toContainText("40:00 · 10,0 km · 4:00 /km (Garmin Connect)");
  await expect(page.locator("#competitionDialogResult input, #competitionDialogResult textarea")).toHaveCount(0);
  await dialog.getByLabel("Notizen").fill("Nur ein Test");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(dialog).toBeHidden();
  await expect(past).toHaveCount(1);
  await past.locator("summary .competition-summary-main").click();
  await expect(past).toContainText("Nur ein Test");
  await past.getByRole("button", { name: "Wettkampf bearbeiten" }).click();
  await removeFromDialog(page);
  await expect(dialog).toBeHidden();
  await expect(archive.locator("details.competition-card").filter({ hasText: "E2E Archiv" })).toHaveCount(0);
});

test("@responsive a bad duration and a failed save keep the dialog open with the message", async ({ page }) => {
  await openSeason(page);
  const dialog = page.locator("#competitionDialog");
  await page.locator("#competitionAdd").click();
  await dialog.getByLabel("Name").fill("E2E Fehler");
  await dialog.getByLabel("Datum").fill(inDays(20));
  await dialog.getByLabel("Erwartete Dauer").fill("abc");
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(page.locator("#competitionDialogStatus")).toContainText("h:mm:ss");
  await dialog.getByLabel("Erwartete Dauer").fill("");
  await page.context().setOffline(true);
  await dialog.getByRole("button", { name: "Speichern" }).click();
  await expect(page.locator("#competitionDialogStatus")).not.toBeEmpty();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Speichern" })).toBeEnabled();
  await page.context().setOffline(false);
  await dialog.getByRole("button", { name: "Abbrechen" }).click();
  await expect(dialog).toBeHidden();
  await expect(card(page, "E2E Fehler")).toHaveCount(0);
});
