const { test, expect } = require("@playwright/test");
const { renderCalendarSessions, openSessionDetails } = require("./helpers/ui");

// Routes miss requests that pass through the service worker's fetch listener; this spec
// does not test the worker, so it is blocked and every route applies (#3842).
test.use({ serviceWorkers: "block" });

test("@responsive activity details preserve calendar without Coach shortcuts", async ({ page, request }) => {
  const seed = await request.get("/api/fixture/activity");
  expect(seed.ok()).toBeTruthy();
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.evaluate(async () => { await AppRouter.navigate("plan/overview", { historyMode: "replace" }); });
  const entry = page.locator(".planned-entry").filter({ hasText: "Synthetic ride <img src=x>" }).first();
  // On wide screens a unit of the week may already be open as the detail pane.
  if (!(await entry.evaluate((element) => element.open))) await entry.locator(":scope > summary").click();
  const open = entry.getByRole("button", { name: "Feedback" });
  await open.click();
  const dialog = page.getByRole("dialog", { name: "Synthetic ride <img src=x>" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("img", { name: "Leistung, Zeitachse in Minuten" })).toBeVisible();
  await expect(dialog.getByText(/Diagramme vereinfacht/)).toBeVisible();
  expect(await dialog.locator(".activity-series").first().locator("svg text").count()).toBeGreaterThan(3);
  await dialog.locator(".activity-values summary").first().click();
  await expect(dialog.locator(".activity-values table tbody tr").first()).toBeVisible();
  expect(await dialog.locator("img").count()).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await dialog.getByRole("button", { name: "Zurück zum Kalender" }).click();
  await expect(entry).toHaveAttribute("open", "");
  await expect(open).toBeFocused();
  await open.click();
  await page.goBack();
  await expect(dialog).not.toBeVisible();
  await expect(entry).toHaveAttribute("open", "");
  await open.click();
  await expect(dialog.getByRole("button", { name: "Feedback speichern" })).toBeVisible();
  const rpe = dialog.getByLabel("Session-RPE (0–10)");
  await expect(rpe.locator("option", { hasText: "5 · Schwer" })).toHaveCount(1);
  await rpe.selectOption("6.5");
  await dialog.getByLabel("Abweichungsgrund").fill("Heat");
  await dialog.getByLabel("Notizen").fill("Synthetic feedback");
  await dialog.getByRole("button", { name: "Feedback speichern" }).click();
  await expect(dialog.locator('[role="status"]').filter({ hasText: "Feedback gespeichert" })).toBeVisible();
  await expect(dialog.getByText("RPE-Belastung: 403 RPE-Punkte · RPE 6,5 (Athletenfeedback) × 62 min")).toBeVisible();
  await dialog.getByRole("button", { name: "Zurück zum Kalender" }).click();
  await page.reload();
  const response = await request.get("/api/activities/9000001");
  const payload = await response.json();
  expect(payload.contract).toBe("activity_detail.v1");
  expect(payload.detail_data.display_sampled).toBe(true);
  expect(payload.streams.time_s.length).toBe(2000);
  expect(payload.session.started_at_local).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/);
  expect(payload.activity_feedback.session_rpe).toBe(6.5);
  expect(payload.activity_feedback.notes).toBe("Synthetic feedback");
});


test("@responsive load tab shows the session-RPE load with its coverage", async ({ page }) => {
  await page.goto("/#analysis/load");
  await expect(page.locator("#appShell")).toBeVisible();
  const heading = page.locator("#analysisLoadCharts").getByRole("heading", { name: /^RPE-Belastung · \d+ von \d+ Einheiten bewertet/ });
  await expect(heading).toBeVisible();
});


test("@responsive Garmin charts select named measurements and athlete maintenance dates", async ({ page }) => {
  await page.goto("/#analysis/load");
  await expect(page.locator("#appShell")).toBeVisible();
  const values = await page.evaluate(() => ({
    endurance: providerMetricValue("endurance_score", { timestamp: 123, overallScore: 700 }),
    tolerance: providerMetricValue("running_tolerance", { age: 35, runningTolerance: 42 }),
    unknown: providerMetricValue("endurance_score", { timestamp: 123 }) ?? null,
    invalid: providerMetricValue("running_tolerance", { tolerance: "42" }) ?? null,
  }));
  expect(values).toEqual({ endurance: 700, tolerance: 42, unknown: null, invalid: null });
  const date = await page.evaluate(async () => {
    const previousApi = api;
    const previousState = state.data;
    const RealDate = Date;
    let sent;
    try {
      state.data = { profile: { timezone: "Pacific/Honolulu" } };
      globalThis.Date = class extends RealDate {
        constructor(...args) { super(...(args.length ? args : ["2026-10-07T00:30:00Z"])); }
      };
      api = async (_url, options) => { if (options?.body) sent = JSON.parse(options.body); return { items: [] }; };
      maintenanceButton({ id: "synthetic-equipment" }).click();
      await Promise.resolve();
      return sent.date;
    } finally {
      api = previousApi;
      state.data = previousState;
      globalThis.Date = RealDate;
    }
  });
  expect(date).toBe("2026-10-06");
});

test("@responsive activity details chart each document channel once", async ({ page }) => {
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  const headings = await page.evaluate(async () => {
    const channel = (values) => ({ values, source: { label: "Intervals.icu" }, aligned: true });
    const data = {
      contract: "activity_detail.v1",
      session: { activity_id: "act_channels", name: "Synthetic channel activity", sport: "ride" },
      streams: { time_s: [0, 1, 2], channels: { power_w: channel([200, 210, 220]), speed_mps: channel([6, 7, 8]), moving: channel([1, 1, 1]) } },
      detail_data: { providers: [] },
    };
    await ActivityDetails.open({ id: "act_channels" }, { api: async () => data, showDialog: (dialog) => dialog.showModal() });
    const result = [...document.querySelectorAll(".activity-series h3")].map((node) => node.textContent);
    ActivityDetails.close(false);
    return result;
  });
  expect(headings).toEqual(["Leistung (W)", "Geschwindigkeit (m/s)"]);
});

test("@responsive Garmin-only session shows per-provider notes and its own refresh", async ({ page }) => {
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.evaluate(async () => {
    const entry = (provider, label, fields) => ({ provider, label, kinds: ["streams"], fetched_at: "2026-10-17T09:00:00Z", refreshable: false, stale: false, load_error: null, coverage: {}, ...fields });
    const data = {
      contract: "activity_detail.v1",
      session: { activity_id: "act_garmin", name: "Synthetic Garmin ride <img src=x>", sport: "ride", started_at: "2026-10-17T06:00:00+00:00" },
      metrics: { duration_s: { value: 3600, unit: "s" } },
      streams: { time_s: [0, 60, 120], channels: { power_w: { values: [180, 190, 200], source: { label: "Garmin Connect" }, aligned: true } } },
      laps: null,
      zones: {},
      analysis: null,
      detail_data: {
        providers: [
          entry("garmin", "Garmin Connect", { refreshable: true, stale: true, load_error: { message: "Garmin Connect lieferte keine Runden.", observed_at: "2026-10-17T09:30:00Z" }, coverage: { power_w: { points: 3, valid_points: 3 } } }),
          entry("wahoo", "Wahoo", { load_error: { message: "Wahoo-Datei unvollständig.", observed_at: "2026-10-17T09:40:00Z" } }),
        ],
        display_sampled: false,
      },
      activity_feedback: null,
      equipment: [],
    };
    globalThis.syntheticCalls = [];
    const fakeApi = async (url, options) => {
      syntheticCalls.push({ url, body: options?.body ? JSON.parse(options.body) : null });
      if (url === "/api/sync/jobs") return { id: "synthetic-job", status: "completed" };
      return data;
    };
    await ActivityDetails.open({ id: "act_garmin" }, { api: fakeApi, showDialog: (dialog) => dialog.showModal() });
  });
  const dialog = page.getByRole("dialog", { name: "Synthetic Garmin ride <img src=x>" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("img", { name: "Leistung, Zeitachse in Minuten" })).toBeVisible();
  await expect(dialog.getByText("Garmin Connect: Die Zusammenfassung hat sich seit dem Laden geändert.", { exact: false })).toBeVisible();
  await expect(dialog.locator(".activity-analysis-error").filter({ hasText: "Garmin Connect: Garmin Connect lieferte keine Runden." })).toBeVisible();
  await expect(dialog.locator(".activity-analysis-error").filter({ hasText: "Wahoo: Wahoo-Datei unvollständig." })).toBeVisible();
  // One muted source line; the per-provider facts sit in a folded section with readable channel names.
  await expect(dialog.getByText("Quelle: Garmin Connect, Wahoo · Stand")).toBeVisible();
  await expect(dialog.getByText("Datenabdeckung Garmin Connect")).toBeHidden();
  await dialog.getByText("Herkunft im Detail").click();
  await expect(dialog.getByText("Datenabdeckung Garmin Connect · Leistung: 3 von 3 Messwerten")).toBeVisible();
  await expect(dialog).not.toContainText("power_w");
  await expect(dialog.getByRole("button", { name: /Wahoo/ })).toHaveCount(0);
  await dialog.getByRole("button", { name: "Garmin Connect: Detaildaten aktualisieren" }).click();
  await expect.poll(() => page.evaluate(() => syntheticCalls.filter((call) => call.url === "/api/activities/act_garmin").length)).toBe(2);
  const job = await page.evaluate(() => syntheticCalls.find((call) => call.url === "/api/sync/jobs").body);
  expect(job).toEqual({ provider: "garmin", type: "activity_details", payload: { activity_id: "act_garmin" } });
  expect(await dialog.locator("img").count()).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.evaluate(() => ActivityDetails.close(false));
});

test("@responsive running cadence shows step frequency of both legs", async ({ page }) => {
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  const headings = await page.evaluate(async () => {
    const result = {};
    for (const type of ["Run", "Ride"]) {
      const data = { session: { activity_id: `synthetic-${type}`, name: `Synthetic ${type}`, sport: type.toLowerCase() }, streams: { time_s: [0, 1, 2], channels: { cadence_rpm: { values: [86, 88, 90] } } }, detail_data: { providers: [] } };
      await ActivityDetails.open({ id: `synthetic-${type}` }, { api: async () => data, showDialog: (dialog) => dialog.showModal() });
      result[type] = [...document.querySelectorAll(".activity-series")].map((node) => node.textContent);
      ActivityDetails.close(false);
    }
    return result;
  });
  expect(headings.Run.find((text) => text.startsWith("Schrittfrequenz (spm)"))).toContain("180");
  expect(headings.Ride.find((text) => text.startsWith("Trittfrequenz (rpm)"))).toContain("90");
});

test("@responsive calendar details show running step frequency and cycling cadence", async ({ page }) => {
  const sessions = [
    { id: "synthetic-run", name: "Synthetic run", sport: "run", environment: "outdoor", started_at_local: "2026-10-17T08:00:00", moving_duration_s: 3 },
    { id: "synthetic-ride", name: "Synthetic ride cadence", sport: "ride", environment: "outdoor", started_at_local: "2026-10-16T08:00:00", moving_duration_s: 3 },
  ];
  await page.route(/\/api\/activities\//, (route) => {
    const id = new URL(route.request().url()).pathname.split("/")[3];
    return route.fulfill({ json: {
      session: { activity_id: id, sport: id === "synthetic-run" ? "run" : "ride" },
      metrics: { avg_cadence: { value: 86 }, max_cadence: { value: 90 } },
      streams: { time_s: [0, 1, 2], channels: { cadence_rpm: { values: [84, 86, 90] } } },
      detail_data: { providers: [] },
    } });
  });
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await renderCalendarSessions(page, sessions);
  const run = await openSessionDetails(page, "Synthetic run");
  await expect(run.locator(".activity-analysis-stats div").filter({ hasText: "Ø Schrittfrequenz" })).toContainText("172 spm");
  await expect(run.getByRole("img", { name: /^Schrittfrequenz: Ø 172/ })).toBeVisible();
  const ride = await openSessionDetails(page, "Synthetic ride cadence");
  await expect(ride.locator(".activity-analysis-stats div").filter({ hasText: "Ø Trittfrequenz" })).toContainText("86 rpm");
  await expect(ride.getByRole("img", { name: /^Trittfrequenz: Ø 86/ })).toBeVisible();
});

test("@responsive calendar details show the route history chart first and every visit below it", async ({ page }) => {
  // #3763 (athlete: layout B): "Pace je Besuch", then the open visit table, newest first, six at a time.
  const visits = Array.from({ length: 8 }, (_, index) => ({
    activity_id: `act_route_${8 - index}`, date: `2026-10-${String(17 - index).padStart(2, "0")}`, sport: "run",
    moving_duration_s: 2400 + index * 10, avg_hr: index === 1 ? null : 150 - index, avg_speed_mps: 2.7 - index * 0.02, temperature_c: 10 + index,
  }));
  await page.route(/\/api\/activities\//, (route) => route.fulfill({ json: {
    session: { activity_id: "act_route_7", sport: "run" },
    metrics: {},
    route: { label: "Hausrunde", repeat_index: 7, repeat_count: 8, route_id: "r1", visits },
    detail_data: { providers: [] },
  } }));
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await renderCalendarSessions(page, [{ id: "act_route_7", name: "Synthetic route run", sport: "run", started_at_local: "2026-10-16T07:00:00", moving_duration_s: 2410 }]);
  const details = await openSessionDetails(page, "Synthetic route run");
  const card = details.locator(".activity-route");
  await expect(card.getByRole("heading", { name: "Hausrunde · 7. von 8 Besuchen" })).toBeVisible();
  await expect(card.getByRole("button", { name: "Umbenennen" })).toBeVisible();
  await expect(card.locator(".analysis-chart-card h3")).toHaveText("Pace je Besuch");
  await expect(card.locator("details.activity-values > summary")).toHaveText("Alle 8 Besuche ansehen");
  // The visit table replaces the chart's own value list: one toggle only.
  await expect(card.locator("details")).toHaveCount(1);
  const rows = card.locator(".route-visits tbody tr");
  await expect(rows).toHaveCount(6);
  await expect(rows.nth(1)).toHaveAttribute("aria-current", "true");
  await expect(rows.nth(1).locator("td").nth(1)).toHaveText("–");
  await card.getByRole("button", { name: "Ältere Besuche" }).click();
  await expect(rows).toHaveCount(8);
  await expect(card.getByRole("button", { name: "Ältere Besuche" })).toBeHidden();
  const text = await card.textContent();
  expect(text).not.toMatch(/latitude|longitude|51\.|cell/i);
});

test("@responsive calendar details chart running dynamics per second and retry a failed read", async ({ page }) => {
  let fail = true;
  const times = Array.from({ length: 30 }, (_, index) => index * 10);
  await page.route(/\/api\/activities\//, (route) => (fail
    ? route.fulfill({ status: 503, json: { error: "E2E fixture: Details nicht erreichbar" } })
    : route.fulfill({ json: {
      session: { activity_id: "act_dyn", sport: "run" },
      metrics: { ground_contact_time_ms: { value: 250 }, step_length_m: { value: 1.1 } },
      streams: { time_s: times, channels: { heart_rate_bpm: { values: times.map((time) => 140 + (time % 20)) } } },
      running_dynamics_streams: {
        ground_contact_time_ms: { values: times.map((time) => 240 + (time % 30)) },
        step_length_m: { values: times.map((time) => 1 + (time % 20) / 100) },
      },
      detail_data: { providers: [] },
    } })));
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await renderCalendarSessions(page, [{ id: "act_dyn", name: "Synthetic dynamics run", sport: "run", started_at_local: "2026-10-17T07:00:00", moving_duration_s: 300 }]);
  const details = await openSessionDetails(page, "Synthetic dynamics run");
  // A failed read shows the retry card; the retry loads the details.
  await expect(details.locator(".error-state")).toContainText("Details nicht erreichbar");
  fail = false;
  await details.getByRole("button", { name: "Erneut versuchen" }).click();
  await expect(details.getByRole("img", { name: /^Bodenkontaktzeit: Ø 250/ })).toBeVisible();
  await expect(details.getByRole("img", { name: /^Schrittlänge: / })).toBeVisible();
  await expect(details.locator(".error-state")).toHaveCount(0);
});

test("@responsive calendar details chart the breathing rate and show Garmin's lap GAP beside the pace", async ({ page }) => {
  const times = Array.from({ length: 20 }, (_, index) => index * 15);
  const lap = (index, gap) => ({ index, start_offset_s: index * 150, end_offset_s: index * 150 + 150, distance_m: 500, avg_speed_mps: 3.2, avg_grade_adjusted_speed_mps: gap });
  await page.route(/\/api\/activities\//, (route) => {
    const id = new URL(route.request().url()).pathname.split("/")[3];
    // #3374: Garmin's undocumented values arrive labelled by the server as unofficial.
    const unofficial = { provider: "garmin", label: "Garmin, inoffiziell", kind: "unofficial", recorded_by: "garmin" };
    const breathing = id === "act_breath" ? {
      respiration_rate_brpm: { values: times.map((time) => 22 + (time % 30) / 5) },
      performance_condition: { values: times.map((time) => 3 - time / 60), source: unofficial },
      stamina_137_pct: { values: times.map((time) => 96 - time / 30), source: unofficial },
      stamina_138_pct: { values: times.map((time) => 99 - time / 60), source: unofficial },
    } : {};
    return route.fulfill({ json: {
      session: { activity_id: id, sport: "run" },
      metrics: {},
      streams: { time_s: times, channels: { heart_rate_bpm: { values: times.map((time) => 140 + (time % 20)) }, ...breathing } },
      laps: { laps: [lap(0, id === "act_breath" ? 3.5 : null), lap(1, null)] },
      detail_data: { providers: [] },
    } });
  });
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await renderCalendarSessions(page, [
    { id: "act_breath", name: "Synthetic breathing run", sport: "run", started_at_local: "2026-10-17T07:00:00", moving_duration_s: 300 },
    { id: "act_plain", name: "Synthetic plain run", sport: "run", started_at_local: "2026-10-16T07:00:00", moving_duration_s: 300 },
  ]);
  const breath = await openSessionDetails(page, "Synthetic breathing run");
  await expect(breath.getByRole("img", { name: /^Atemfrequenz: Ø \d+ · Max \d+ \/min/ })).toBeVisible();
  // Performance condition and both stamina fields carry the server's "Garmin, inoffiziell".
  for (const label of ["Leistungszustand", "Ausdauer A", "Ausdauer B"]) {
    await expect(breath.getByRole("img", { name: new RegExp(`^${label}: Ø `) })).toBeVisible();
    await expect(breath.locator(".activity-analysis-chart h4").filter({ hasText: label })).toHaveText(`${label}Garmin, inoffiziell`);
  }
  await expect(breath.locator(".activity-analysis-chart h4").filter({ hasText: "Atemfrequenz" }).locator(".source-chip")).toHaveCount(0);
  const headers = breath.locator(".activity-analysis-laps th[scope=col]");
  await expect(headers).toHaveText(["#", "Zeit", "Distanz", "Pace", "GAP Garmin"]);
  // 1000 / 3.5 m/s is 4:46 min/km; the lap without a value shows a dash.
  const gapCells = breath.locator(".activity-analysis-laps tbody tr td:nth-child(5)");
  await expect(gapCells).toHaveText(["4:46", "—"]);
  const plain = await openSessionDetails(page, "Synthetic plain run");
  await expect(plain.getByRole("img", { name: /^Herzfrequenz: / })).toBeVisible();
  await expect(plain.getByRole("img", { name: /^Atemfrequenz/ })).toHaveCount(0);
  await expect(plain.getByRole("img", { name: /^(Leistungszustand|Ausdauer)/ })).toHaveCount(0);
  await expect(plain.locator(".activity-analysis-laps th[scope=col]")).toHaveText(["#", "Zeit", "Distanz", "Pace"]);
});

test("@responsive the lap table keeps every value and word on one line and scrolls sideways", async ({ page }, testInfo) => {
  // #3465: twelve full laps, so the lap number, times, paces and power all reach their usual width.
  const laps = Array.from({ length: 12 }, (_, index) => ({ index, start_offset_s: index * 365, end_offset_s: index * 365 + 365, distance_m: 1000,
    avg_speed_mps: 2.74, avg_grade_adjusted_speed_mps: 2.9, avg_power: 361, avg_hr: 152, elevation_gain_m: 12 }));
  await page.route(/\/api\/activities\//, (route) => route.fulfill({ json: {
    session: { activity_id: "act_laps", sport: "run" }, metrics: {},
    streams: { time_s: [0, 15, 30], channels: { heart_rate_bpm: { values: [140, 150, 145] } } },
    laps: { laps }, detail_data: { providers: [] },
  } }));
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await renderCalendarSessions(page, [{ id: "act_laps", name: "Synthetic lap run", sport: "run", started_at_local: "2026-10-17T07:00:00", moving_duration_s: 4380 }]);
  const details = await openSessionDetails(page, "Synthetic lap run");
  const section = details.locator(".activity-analysis-laps");
  await expect(section.locator("th[scope=col]")).toHaveText(["#", "Zeit", "Distanz", "Pace", "GAP Garmin", "Ø W", "Ø Herzfrequenz", "Höhenmeter"]);
  await section.scrollIntoViewIfNeeded();
  if (testInfo.project.name === "mobile") await section.screenshot({ path: testInfo.outputPath("Runden.png") });
  const layout = await section.evaluate((root) => {
    // The lines one text occupies: distinct line tops of its rendered boxes.
    const lines = (node, start = 0, end = node.textContent.length) => {
      const range = document.createRange();
      range.setStart(node, start); range.setEnd(node, end);
      return new Set([...range.getClientRects()].filter((rect) => rect.width > 0).map((rect) => Math.round(rect.top))).size;
    };
    const split = [];
    for (const cell of root.querySelectorAll("th, td")) {
      const text = cell.firstChild;
      if (text?.nodeType !== Node.TEXT_NODE) continue;
      const header = cell.matches("th[scope=col]");
      // A value never wraps; a header may wrap only between its words.
      if (!header && lines(text) > 1) split.push(cell.textContent);
      if (header) for (const match of text.textContent.matchAll(/\S+/g)) if (lines(text, match.index, match.index + match[0].length) > 1) split.push(cell.textContent);
    }
    const region = root.querySelector(".analysis-chart-table");
    return { split, overflowX: getComputedStyle(region).overflowX, fits: region.scrollWidth <= region.clientWidth || region.scrollWidth >= region.querySelector("table").scrollWidth };
  });
  expect(layout.split).toEqual([]);
  expect(["auto", "scroll"]).toContain(layout.overflowX);
  expect(layout.fits).toBe(true);
});

test("@responsive the session dialog charts the breathing rate", async ({ page }) => {
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  const headings = await page.evaluate(async () => {
    const data = { session: { activity_id: "synthetic-breath", name: "Synthetic breath", sport: "run" }, streams: { time_s: [0, 1, 2], channels: { respiration_rate_brpm: { values: [20, 22, 24] } } }, detail_data: { providers: [] } };
    await ActivityDetails.open({ id: "synthetic-breath" }, { api: async () => data, showDialog: (dialog) => dialog.showModal() });
    const result = [...document.querySelectorAll(".activity-series h3")].map((node) => node.textContent);
    ActivityDetails.close(false);
    return result;
  });
  expect(headings).toEqual(["Atemfrequenz (/min)"]);
});

test("@responsive the calendar details open a Garmin-only indoor session; old Einheiten links land on the calendar", async ({ page }) => {
  await page.route(/\/api\/activities\//, (route) => route.fulfill({ json: { session: { activity_id: "act_garmin_only", sport: "ride", environment: "virtual" }, metrics: {}, detail_data: { providers: [] } } }));
  await page.goto("/#analysis/sessions");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  // The Einheiten segment is gone (#2258): its link lands on the calendar, and Analyse lists no such tab.
  await expect(page).toHaveURL(/#plan\/overview$/);
  await expect(page.getByRole("navigation", { name: "Analysebereiche" }).getByRole("link", { name: "Einheiten" })).toHaveCount(0);
  await renderCalendarSessions(page, [{ id: "act_garmin_only", name: "Indoor-Fahrt", sport: "ride", environment: "virtual", record_ids: { garmin: "910002" }, started_at_local: "2026-10-17T08:00:00", moving_duration_s: 3600, distance_m: 30000 }]);
  const details = await openSessionDetails(page, "Indoor-Fahrt");
  await expect(details.locator(".activity-analysis-panel")).toBeVisible();
  await expect(details.locator(".loading-state")).toHaveCount(0);
});

test("@responsive the calendar pages back past the loaded weeks and loads older sessions", async ({ page }) => {
  // #2258 (athlete option A): paging back has no limit; older sessions come from the session list.
  let oldDay = null;
  let listed = 0;
  await page.route(/\/api\/activities(\?|\/)/, (route) => {
    const url = new URL(route.request().url());
    const id = url.pathname.split("/")[3];
    if (id) return route.fulfill({ json: { session: { activity_id: id, sport: "run" }, metrics: { avg_hr: { value: 141 } }, detail_data: { providers: [] } } });
    listed += 1;
    const day = url.searchParams.get("cursor") ? null : oldDay;
    return route.fulfill({ json: { activities: day ? [{ id: "act_old", name: "Alter Lauf", sport: "run", started_at_local: `${day}T07:00:00`, moving_duration_s: 1800 }] : [], snapshot_synced_at: null, next_cursor: null } });
  });
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  oldDay = await page.evaluate(() => addDateKey(timezoneDateKey(state.data?.profile?.timezone, new Date()), -35));
  const previous = page.getByRole("button", { name: "Vorherige Woche" });
  for (let step = 0; step < 6 && !(await page.locator(".planned-entry").filter({ hasText: "Alter Lauf" }).count()); step += 1) {
    await expect(previous).toBeEnabled();
    await previous.click();
    await page.waitForTimeout(200);
  }
  await expect(page.locator(".planned-entry").filter({ hasText: "Alter Lauf" })).toBeVisible();
  expect(listed).toBeGreaterThan(0);
  const details = await openSessionDetails(page, "Alter Lauf");
  await expect(details.locator(".activity-analysis-stats")).toContainText("141 bpm");
});

test("@responsive calendar details expand Garmin-style charts", async ({ page, request }) => {
  const seed = await request.get("/api/fixture/activity");
  expect(seed.ok()).toBeTruthy();
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.evaluate(async () => { await AppRouter.navigate("plan/overview", { historyMode: "replace" }); });
  const card = page.locator(".planned-entry").filter({ hasText: "Synthetic ride <img src=x>" }).first();
  await card.evaluate((element) => { element.open = true; });
  const item = card.locator(".planned-activity-more");
  await item.locator(":scope > summary").click();
  await expect(item.locator(".activity-analysis-stats")).toContainText("25,0 km");
  await expect(item.locator(".activity-analysis-stats div").filter({ hasText: "Normalized Power" })).toContainText("205 W");
  const power = item.getByRole("img", { name: /^Leistung: Ø 200/ });
  await expect(power).toBeVisible();
  await expect(item.getByRole("img", { name: /^Herzfrequenz: Ø 140/ })).toBeVisible();
  const box = await power.boundingBox();
  await power.dispatchEvent("pointerdown", { clientX: box.x + box.width * 0.6, clientY: box.y + box.height / 2 });
  await expect(item.locator(".is-power .activity-analysis-readout")).toContainText("200 W");
  await expect(item.locator(".activity-analysis-zones").filter({ hasText: "Zeit in Herzfrequenzzonen" })).toContainText("Z2");
  // The session detail and the report overview share one zone order and palette (#1782).
  const zoneRows = await item.locator(".activity-analysis-zones").filter({ hasText: "Zeit in Herzfrequenzzonen" }).locator("li").evaluateAll((rows) => {
    const color = (node) => getComputedStyle(node).getPropertyValue("--zone-color").trim();
    const detail = rows.map((row) => ({ zone: row.dataset.zone, color: color(row), track: row.querySelector(".activity-analysis-zone-bar").getBoundingClientRect().width }));
    const overview = trainingFocusDistribution("Herzfrequenzzonen", trainingFocusZoneSeconds([{ seconds: Object.fromEntries(detail.map(({ zone }, index) => [zone, (index + 1) * 600])) }]), "seconds", "");
    rows[0].closest(".activity-analysis-panel").append(overview);
    const result = { detail, overview: [...overview.querySelectorAll(".training-focus-row")].map((row) => ({ zone: row.dataset.zone, color: color(row), track: row.querySelector("progress").getBoundingClientRect().width })) };
    overview.remove();
    return result;
  });
  expect(zoneRows.detail.length).toBeGreaterThan(1);
  expect(zoneRows.detail.map(({ zone, color }) => [zone, color])).toEqual(zoneRows.overview.map(({ zone, color }) => [zone, color]));
  expect(Number(zoneRows.detail[0].zone.slice(1))).toBeGreaterThan(Number(zoneRows.detail.at(-1).zone.slice(1)));
  expect(new Set(zoneRows.detail.map(({ track }) => Math.round(track))).size).toBe(1);
  expect(new Set(zoneRows.overview.map(({ track }) => Math.round(track))).size).toBe(1);
  // The stored watch temperature is never charted or shown; without activity weather it is unknown (#3036).
  await expect(item.locator('.activity-analysis-panel svg[aria-label^="Temperatur"]')).toHaveCount(0);
  await expect(item.locator(".activity-analysis-stats dt").filter({ hasText: "Temperatur" })).toHaveCount(0);
  await expect(item).toContainText("Datenabdeckung");
  expect(await item.locator("img").count()).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test("@responsive feedback save and undo show the returned load without a detail refetch", async ({ page }) => {
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.evaluate(async () => {
    const load = (value) => ({ value, rpe: 5, rpe_source_label: "dein Feedback", duration_min: 60 });
    const data = { session: { activity_id: "load-1", name: "Synthetic load ride", sport: "ride" }, streams: { time_s: [0, 1, 2], channels: {} }, detail_data: { providers: [] }, activity_feedback: null, equipment: [], session_rpe_load: null };
    globalThis.syntheticCalls = [];
    const fakeApi = async (url, options) => {
      syntheticCalls.push(url);
      if (url === "/api/change-history/undo") globalThis.undoBody = JSON.parse(options.body);
      if (url.endsWith("/feedback")) return { status: "ok", activity_feedback: { session_rpe: 5 }, change: { id: "c1", after_hash: "h" }, session_rpe_load: load(300) };
      if (url === "/api/change-history/undo") return { status: "undone", session_rpe_load: null };
      return data;
    };
    await ActivityDetails.open({ id: "load-1" }, { api: fakeApi, showDialog: (dialog) => dialog.showModal() });
  });
  const dialog = page.getByRole("dialog", { name: "Synthetic load ride" });
  await dialog.getByLabel("Session-RPE (0–10)").selectOption("5");
  await dialog.getByRole("button", { name: "Feedback speichern" }).click();
  await expect(dialog.locator("[data-session-rpe-load]")).toContainText("300 RPE-Punkte");
  await dialog.getByRole("button", { name: "Rückgängig" }).click();
  await expect(dialog.locator("[data-session-rpe-load]")).toBeHidden();
  expect(await page.evaluate(() => undoBody.activity_id)).toBe("load-1");
  expect(await page.evaluate(() => syntheticCalls.filter((url) => url === "/api/activities/load-1").length)).toBe(1);
  await page.evaluate(() => ActivityDetails.close(false));
});

test("@responsive marking the session purpose sends it with a receipt whose undo restores the previous one", async ({ page }) => {
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.evaluate(async () => {
    const data = { session: { activity_id: "mark-1", name: "Synthetic marked ride", sport: "ride", purpose: "race" }, streams: { time_s: [0, 1, 2], channels: {} }, detail_data: { providers: [] }, activity_feedback: null, equipment: [], session_rpe_load: null };
    globalThis.feedbackBodies = [];
    const fakeApi = async (url, options) => {
      if (url.endsWith("/feedback")) {
        const body = JSON.parse(options.body);
        feedbackBodies.push(body);
        return { status: "ok", activity_feedback: body.purpose ? { purpose: body.purpose } : null, change: { id: "c1", after_hash: "h" } };
      }
      if (url === "/api/change-history/undo") return { status: "undone" };
      return data;
    };
    await ActivityDetails.open({ id: "mark-1" }, { api: fakeApi, showDialog: (dialog) => dialog.showModal() });
  });
  const dialog = page.getByRole("dialog", { name: "Synthetic marked ride" });
  const purpose = dialog.getByLabel("Art der Einheit");
  await expect(purpose).toHaveValue("");
  await expect(purpose.locator("option").first()).toHaveText("Automatisch · Wettkampf");
  await purpose.selectOption("training");
  await dialog.getByRole("button", { name: "Feedback speichern" }).click();
  await expect(dialog.locator('[role="status"]').filter({ hasText: "Feedback gespeichert" })).toBeVisible();
  expect(await page.evaluate(() => feedbackBodies.at(-1).purpose)).toBe("training");
  await dialog.getByRole("button", { name: "Rückgängig" }).click();
  await expect(purpose).toHaveValue("");
  await purpose.selectOption("commute");
  await dialog.getByRole("button", { name: "Feedback speichern" }).click();
  await expect(purpose).toHaveValue("commute");
  expect(await page.evaluate(() => feedbackBodies.at(-1).purpose)).toBe("commute");
  await purpose.selectOption("");
  await dialog.getByRole("button", { name: "Feedback speichern" }).click();
  expect(await page.evaluate(() => feedbackBodies.at(-1).purpose)).toBeNull();
  await page.evaluate(() => ActivityDetails.close(false));
});

test("@responsive provider copies split with a receipt whose undo restores the bundle", async ({ page }) => {
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.evaluate(async () => {
    const member = (session_id, provider, label) => ({ session_id, provider, label, recorded_by: "garmin", sport: "ride", started_at: "2026-09-10T05:00:00+00:00", duration_s: 3600 });
    const bundled = { activity_id: "act_a", link_kind: "heuristic", athlete_confirmed: false, members: [member("act_a", "intervals", "Intervals.icu (Garmin)"), member("act_b", "garmin", "Garmin")], merge_candidates: [] };
    const split = { ...bundled, link_kind: "single", athlete_confirmed: true, members: [bundled.members[0]], merge_candidates: [member("act_b", "garmin", "Garmin")] };
    let bundle = bundled;
    globalThis.sessionBodies = [];
    const fakeApi = async (url, options) => {
      if (url === "/api/activities/act_a/session") { sessionBodies.push(JSON.parse(options.body)); bundle = split; return { status: "split", activity_id: "act_a", change: { id: "c2", after_hash: "h2" } }; }
      if (url === "/api/change-history/undo") { sessionBodies.push(JSON.parse(options.body)); bundle = bundled; return { status: "undone" }; }
      return { session: { activity_id: "act_a", name: "Synthetic bundle ride", sport: "ride" }, streams: { time_s: [0, 1, 2], channels: {} }, detail_data: { providers: [] }, activity_feedback: null, equipment: [], session_rpe_load: null, session_bundle: bundle };
    };
    await ActivityDetails.open({ id: "act_a" }, { api: fakeApi, showDialog: (dialog) => dialog.showModal() });
  });
  const dialog = page.getByRole("dialog", { name: "Synthetic bundle ride" });
  const section = dialog.locator(".activity-bundle");
  await expect(section.getByRole("button", { name: "Trennen" })).toHaveCount(2);
  await section.getByRole("listitem").filter({ hasText: /^Garmin/ }).getByRole("button", { name: "Trennen" }).click();
  await expect(section.getByRole("status")).toContainText("Aufzeichnung getrennt.");
  await expect(section.getByRole("button", { name: "Zusammenführen" })).toHaveCount(1);
  await section.getByRole("button", { name: "Rückgängig" }).click();
  await expect(section.getByRole("status")).toContainText("Zurückgenommen.");
  await expect(section.getByRole("button", { name: "Trennen" })).toHaveCount(2);
  expect(await page.evaluate(() => sessionBodies)).toEqual([{ action: "split", member_id: "act_b" }, { change_id: "c2", expected_current_hash: "h2" }]);
  await page.evaluate(() => ActivityDetails.close(false));
});

test("@responsive deleting an activity locally asks first and can be undone", async ({ page }) => {
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.evaluate(async () => {
    const bundle = { activity_id: "act_d", link_kind: "single", athlete_confirmed: false, members: [{ session_id: "act_d", provider: "garmin", label: "Garmin", recorded_by: "garmin", sport: "ride", started_at: "2026-09-10T05:00:00+00:00", duration_s: 3600 }], merge_candidates: [] };
    globalThis.deleteBodies = [];
    const fakeApi = async (url, options) => {
      if (url === "/api/activities/act_d/session" || url === "/api/change-history/undo") { deleteBodies.push(JSON.parse(options.body)); return url.endsWith("/session") ? { status: "deleted", activity_id: null, change: { id: "c3", after_hash: "h3" } } : { status: "undone" }; }
      return { session: { activity_id: "act_d", name: "Synthetic delete ride", sport: "ride" }, streams: { time_s: [0, 1, 2], channels: {} }, detail_data: { providers: [] }, activity_feedback: null, equipment: [], session_rpe_load: null, session_bundle: bundle };
    };
    await ActivityDetails.open({ id: "act_d" }, { api: fakeApi, showDialog: (dialog) => dialog.showModal() });
  });
  const dialog = page.getByRole("dialog", { name: "Synthetic delete ride" });
  await dialog.getByRole("button", { name: "Aktivität löschen" }).click();
  await page.getByRole("dialog", { name: "Aktivität löschen?" }).getByRole("button", { name: "Löschen" }).click();
  await expect(dialog.getByRole("status").filter({ hasText: "Aktivität gelöscht." })).toBeVisible();
  await dialog.getByRole("button", { name: "Rückgängig" }).click();
  await expect(dialog.locator(".activity-bundle").getByRole("status")).toContainText("Zurückgenommen.");
  expect(await page.evaluate(() => deleteBodies)).toEqual([{ action: "delete", confirmed: true }, { change_id: "c3", expected_current_hash: "h3" }]);
  await page.evaluate(() => ActivityDetails.close(false));
});

test("@responsive a strength session leads with its total time, a run keeps its moving time", async ({ page }) => {
  // #2317: rest between sets is part of a strength session's work.
  await page.route(/\/api\/activities\//, (route) => {
    const strength = route.request().url().includes("act_str");
    return route.fulfill({ json: {
      session: { activity_id: strength ? "act_str" : "act_run45", sport: strength ? "strength" : "run" },
      metrics: strength
        ? { elapsed_duration_s: { value: 3480 }, moving_duration_s: { value: 1860 } }
        : { elapsed_duration_s: { value: 3000 }, moving_duration_s: { value: 2700 } },
      detail_data: { providers: [] },
    } });
  });
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await renderCalendarSessions(page, [
    { id: "act_str", name: "Synthetic strength", sport: "strength", started_at_local: "2026-10-17T07:00:00", elapsed_duration_s: 3480, moving_duration_s: 1860 },
    { id: "act_run45", name: "Synthetic easy run", sport: "run", started_at_local: "2026-10-17T18:00:00", elapsed_duration_s: 3000, moving_duration_s: 2700 },
  ]);
  const card = (name) => page.locator(".planned-entry.is-completed").filter({ hasText: name }).first();
  await expect(card("Synthetic strength").locator(".planned-session-value strong")).toHaveText("58 min");
  await expect(card("Synthetic easy run").locator(".planned-session-value strong")).toHaveText("45 min");
  // The week sums the same durations as the cards: 58 + 45 min.
  await expect(page.locator(".planned-week-metrics").first()).toContainText("1:43 h");
  const strength = await openSessionDetails(page, "Synthetic strength");
  const strengthTimes = strength.locator(".activity-analysis-stats div").filter({ hasText: /^(Gesamtzeit|Bewegungszeit)/ });
  await expect(strengthTimes).toHaveText([/^Gesamtzeit\s*58:00$/, /^Bewegungszeit\s*31:00$/]);
  const run = await openSessionDetails(page, "Synthetic easy run");
  const runTimes = run.locator(".activity-analysis-stats div").filter({ hasText: /^(Gesamtzeit|Bewegungszeit)/ });
  await expect(runTimes).toHaveText([/^Bewegungszeit\s*45:00$/, /^Gesamtzeit\s*50:00$/]);
});

test("@responsive a completed session's card shows its equipment as a chip and assigns it only by Übernehmen, with receipt and undo", async ({ page }) => {
  // #2879, #3011: the server lists the items, the own choice and the suggestion; nothing is assigned by reading.
  let assigned = null;
  let explicit = false;
  const posts = [];
  await page.route(/\/api\/activities\/act_eq$/, (route) => route.fulfill({ json: {
    session: { activity_id: "act_eq", sport: "run" }, metrics: {}, detail_data: { providers: [] },
    equipment_choice: { options: [{ id: "shoe-a", name: "Pegasus 40" }, { id: "shoe-b", name: "Vaporfly 3" }], assigned, explicit, suggested: "shoe-a", effective: null },
  } }));
  await page.route("**/api/equipment/assignment", (route) => {
    posts.push(route.request().postDataJSON());
    assigned = posts.at(-1).equipment_id;
    explicit = true;
    return route.fulfill({ json: { ok: true, change: { id: "change-eq", after_hash: "hash-eq" } } });
  });
  await page.route("**/api/change-history/undo", (route) => { assigned = null; explicit = false; return route.fulfill({ json: { ok: true } }); });
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await renderCalendarSessions(page, [{ id: "act_eq", name: "Synthetic shoe run", sport: "run", started_at_local: "2026-10-17T07:00:00", moving_duration_s: 2700, distance_m: 9000 }]);
  const card = page.locator(".planned-entry.is-completed").filter({ hasText: "Synthetic shoe run" }).first();
  await card.evaluate((element) => { element.open = true; });
  // The chip is in the head line right after opening, without "Details".
  const chip = card.locator(".planned-equipment-chip");
  await expect(chip).toBeVisible();
  await expect(chip).toHaveText("\u{1F45F} Pegasus 40 · Vorschlag ▾");
  await expect(card.locator(".planned-activity-more")).not.toHaveAttribute("open", "");
  expect(posts).toEqual([]);
  await chip.click();
  const row = card.locator(".planned-equipment");
  await expect(row.getByRole("combobox", { name: "Schuh" })).toHaveValue("shoe-a");
  await row.getByRole("button", { name: "Übernehmen" }).click();
  await expect.poll(() => posts.at(-1)).toEqual({ activity_id: "act_eq", equipment_id: "shoe-a" });
  // #3151: after saving, the chip names the shoe once and the editor closes; only the receipt stays.
  const receipt = card.locator(".planned-equipment-receipt");
  await expect(receipt).toContainText("Gespeichert.");
  await expect(chip).toHaveText("\u{1F45F} Pegasus 40 ▾");
  await expect(row.getByRole("combobox", { name: "Schuh" })).toBeHidden();
  await expect(chip).toHaveAttribute("aria-expanded", "false");
  // "Keiner" is a normal choice; the chip opens the editor again.
  await chip.click();
  await row.getByRole("combobox", { name: "Schuh" }).selectOption("");
  await row.getByRole("button", { name: "Übernehmen" }).click();
  await expect.poll(() => posts.at(-1)).toEqual({ activity_id: "act_eq", equipment_id: null });
  await expect(chip).toHaveText("\u{1F45F} Keiner ▾");
  await expect(row.getByRole("combobox", { name: "Schuh" })).toBeHidden();
  await receipt.getByRole("button", { name: "Rückgängig" }).click();
  await expect(receipt).toContainText("Zurückgenommen.");
  await expect(chip).toHaveText("\u{1F45F} Pegasus 40 · Vorschlag ▾");
  await expect(row.getByRole("combobox", { name: "Schuh" })).toBeHidden();
  await chip.click();
  await expect(row.getByRole("combobox", { name: "Schuh" })).toHaveValue("shoe-a");
  // An own choice that can no longer be assigned stays shown and offers no replacement by itself.
  await page.route(/\/api\/activities\/act_eq2$/, (route) => route.fulfill({ json: {
    session: { activity_id: "act_eq2", sport: "run" }, metrics: {}, detail_data: { providers: [] },
    equipment_choice: { options: [{ id: "shoe-a", name: "Pegasus 40" }], assigned: "shoe-retired", explicit: true, suggested: "shoe-a", effective: { equipment_id: "shoe-retired", source: "manual" } },
  } }));
  await renderCalendarSessions(page, [{ id: "act_eq2", name: "Synthetic retired shoe run", sport: "run", started_at_local: "2026-10-17T09:00:00", moving_duration_s: 1800, distance_m: 6000 }]);
  const again = page.locator(".planned-entry.is-completed").filter({ hasText: "Synthetic retired shoe run" }).first();
  await again.evaluate((element) => { element.open = true; });
  await expect(again.locator(".planned-equipment-chip")).toHaveText("\u{1F45F} Nicht mehr verfügbar ▾");
  await again.locator(".planned-equipment-chip").click();
  await expect(again.locator(".planned-equipment").getByRole("combobox", { name: "Schuh" })).toHaveValue("shoe-retired");
  await expect(again.locator(".planned-equipment").getByRole("button", { name: "Übernehmen" })).toBeHidden();
  // Without an own choice, Garmin's gear link is the labelled default, ahead of the suggestion.
  await page.route(/\/api\/activities\/act_eq3$/, (route) => route.fulfill({ json: {
    session: { activity_id: "act_eq3", sport: "run" }, metrics: {}, detail_data: { providers: [] },
    equipment_choice: { options: [{ id: "shoe-a", name: "Pegasus 40" }, { id: "shoe-b", name: "Vaporfly 3" }], assigned: null, explicit: false, suggested: "shoe-a", effective: { equipment_id: "shoe-b", source: "garmin_gear" } },
  } }));
  await renderCalendarSessions(page, [{ id: "act_eq3", name: "Synthetic linked shoe run", sport: "run", started_at_local: "2026-10-17T11:00:00", moving_duration_s: 1800, distance_m: 6000 }]);
  const linked = page.locator(".planned-entry.is-completed").filter({ hasText: "Synthetic linked shoe run" }).first();
  await linked.evaluate((element) => { element.open = true; });
  await expect(linked.locator(".planned-equipment-chip")).toHaveText("\u{1F45F} Vaporfly 3 · Garmin ▾");
  await linked.locator(".planned-equipment-chip").click();
  await expect(linked.locator(".planned-equipment").getByRole("combobox", { name: "Schuh" })).toHaveValue("shoe-b");
  await expect(linked.locator(".planned-equipment").getByRole("button", { name: "Übernehmen" })).toBeHidden();
  // A planned "Keiner" carried to the session shows as "Keiner" from the plan, not as the suggestion.
  await page.route(/\/api\/activities\/act_eq4$/, (route) => route.fulfill({ json: {
    session: { activity_id: "act_eq4", sport: "run" }, metrics: {}, detail_data: { providers: [] },
    equipment_choice: { options: [{ id: "shoe-a", name: "Pegasus 40" }], assigned: null, explicit: false, suggested: "shoe-a", effective: { equipment_id: null, source: "planned" } },
  } }));
  await renderCalendarSessions(page, [{ id: "act_eq4", name: "Synthetic no shoe run", sport: "run", started_at_local: "2026-10-17T13:00:00", moving_duration_s: 1800, distance_m: 6000 }]);
  const none = page.locator(".planned-entry.is-completed").filter({ hasText: "Synthetic no shoe run" }).first();
  await none.evaluate((element) => { element.open = true; });
  await expect(none.locator(".planned-equipment-chip")).toHaveText("\u{1F45F} Keiner · Plan ▾");
});

test("@responsive a planned run's card shows its shoe chip right after opening and saves the choice for the unit", async ({ page }) => {
  // #3011: a planned unit takes its own choice; a rule is its default; nothing reaches a provider.
  const posts = [];
  let own = null;
  await page.route(/\/api\/equipment\/planned\?unit_id=unit-run$/, (route) => route.fulfill({ json: {
    options: [{ id: "shoe-a", name: "Pegasus 40" }, { id: "shoe-b", name: "Vaporfly 3" }],
    assigned: own, explicit: own !== null, suggested: "shoe-a",
    effective: own === null ? { equipment_id: "shoe-b", source: "rule" } : null,
  } }));
  await page.route("**/api/equipment/assignment", (route) => {
    posts.push(route.request().postDataJSON());
    own = posts.at(-1).equipment_id;
    return route.fulfill({ json: { ok: true, change: { id: "change-plan", after_hash: "hash-plan" } } });
  });
  await page.goto("/#plan/overview");
  await expect(page.locator("#appShell")).toBeVisible();
  await page.waitForFunction(() => state.initialStateLoaded);
  await page.evaluate(async () => {
    await AppRouter.navigate("plan/overview", { historyMode: "replace" });
    const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
    state.data.training_calendar = [{ id: "unit-run", local_id: "unit-run", date: today, name: "Langer, lockerer Lauf", type: "Run", duration_minutes: 80 }];
    renderPlanned(state.data.training_calendar);
  });
  const card = page.locator(".planned-entry").filter({ hasText: "Langer, lockerer Lauf" }).first();
  await card.evaluate((element) => { element.open = true; });
  const chip = card.locator(".planned-equipment-chip");
  await expect(chip).toBeVisible();
  await expect(chip).toHaveText("\u{1F45F} Vaporfly 3 · Regel ▾");
  await chip.click();
  const row = card.locator(".planned-equipment");
  await expect(row.getByRole("combobox", { name: "Schuh" })).toHaveValue("shoe-b");
  await expect(row.getByRole("button", { name: "Übernehmen" })).toBeHidden();
  await row.getByRole("combobox", { name: "Schuh" }).selectOption("shoe-a");
  await row.getByRole("button", { name: "Übernehmen" }).click();
  await expect.poll(() => posts.at(-1)).toEqual({ planned_unit_id: "unit-run", equipment_id: "shoe-a" });
  // #3151: one shoe, a closed editor and the receipt.
  await expect(card.locator(".planned-equipment-receipt")).toContainText("Gespeichert.");
  await expect(chip).toHaveText("\u{1F45F} Pegasus 40 ▾");
  await expect(row.getByRole("combobox", { name: "Schuh" })).toBeHidden();
  await chip.click();
  await expect(row.getByRole("combobox", { name: "Schuh" })).toHaveValue("shoe-a");
});
