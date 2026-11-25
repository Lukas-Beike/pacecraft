const SESSION_SPORT_LABELS = { ride: "Radfahren", run: "Laufen", swim: "Schwimmen", strength: "Kraft" };

// Takes a planned/provider `type` or a session's neutral `sport` and `environment`.
function activitySportLabel(activity) {
  if (!activity?.type && activity?.sport) {
    if (activity.sport === "ride" && ["indoor", "virtual"].includes(activity.environment)) return "Rad indoor";
    return SESSION_SPORT_LABELS[activity.sport] || "Andere";
  }
  const value = String(activity?.type || "").toLowerCase();
  if (value === "virtualride") return "Rad indoor";
  if (/ride|bike|cycling|rad|velo|bicycle/.test(value)) return "Radfahren";
  if (/run|lauf|jog/.test(value)) return "Laufen";
  if (/swim|schwimm/.test(value)) return "Schwimmen";
  if (/strength|kraft|gym|weight/.test(value)) return "Kraft";
  return "Andere";
}



function garminPerformanceSources(garmin) {
  return [garmin.has_vo2max ? "VO2max" : null, garmin.has_estimated_run_times ? "Laufprognosen" : null, garmin.has_max_hr ? "Max. Herzfrequenz" : null, garmin.has_weight ? "Gewicht" : null].filter(Boolean);
}

// Only a read that stopped early is worth a line; the windows it was split into are not.
const GARMIN_PAGINATION_LABELS = { activities: "Aktivitäten", body_battery: "Body Battery", resting_hr: "Ruheherzfrequenz", sleep: "Schlaf", hrv: "HRV", weight: "Gewicht" };

function garminPaginationDetail(garmin) {
  return Object.entries(garmin.pagination || {})
    .filter(([, value]) => value?.complete === false)
    .map(([name]) => `${GARMIN_PAGINATION_LABELS[name] || name}: unvollständig`)
    .join(" · ");
}

function garminBodyBatteryDetail(garmin) {
  const bodyBattery = garmin.morning_body_battery || {};
  const beforeSleep = Number(bodyBattery.before_sleep?.value);
  const morning = Number(bodyBattery.morning?.value);
  if (bodyBattery.status === "ready" && Number.isFinite(beforeSleep) && Number.isFinite(morning)) {
    return `Body Battery am ${dateLabel(bodyBattery.sleep_date)}: ${beforeSleep} vor dem Schlafen → ${morning} nach dem Aufwachen`;
  }
  return bodyBattery.sleep_date ? `Body Battery am ${dateLabel(bodyBattery.sleep_date)}: nicht verfügbar` : "";
}

// Pure (#2807): the sentence for a watch that has not uploaded to Garmin Connect for 24 hours or
// more, else "" (also without a known time). Fresh uploads need no sentence (athlete decision).
function deviceSyncSentence(deviceSyncedAt, now = Date.now()) {
  const at = Date.parse(deviceSyncedAt || "");
  if (!Number.isFinite(at)) return "";
  const days = Math.floor((now - at) / 86_400_000);
  if (days < 1) return "";
  const span = days === 1 ? "einem Tag" : `${days} Tagen`;
  return `Die Uhr hat seit ${span} nichts übertragen.`;
}

function garminDetailText(garmin) {
  let sourceDetail = "Noch kein Garmin-Abruf durchgeführt.";
  if (garmin.last_sync_at) {
    sourceDetail = `Letzter Abruf: ${formatTime(garmin.last_sync_at)} · ${garmin.activities || 0} Aktivitäten · Schlaf/HRV/Trainingsbereitschaft ${[garmin.has_sleep, garmin.has_hrv, garmin.has_readiness].filter(Boolean).length}/3`;
  } else if (garmin.source === "fixture") {
    sourceDetail = "Testdatei ist konfiguriert; synchronisiere sie mit dem Button.";
  }
  const performance = garminPerformanceSources(garmin);
  return [sourceDetail, performance.length ? `${performance.join("/")} aus Garmin` : "", garminBodyBatteryDetail(garmin), garminPaginationDetail(garmin)].filter(Boolean).join(" · ");
}

function renderGarminFullResync(data, garmin = data?.garmin || {}) {
  const fullResync = data?.provider_resync?.garmin || {};
  const fullRunning = Boolean(fullResync.running || state.localSync.garminFull);
  const usable = Boolean(garmin.available && garmin.configured);
  const fullButton = $("#garminFullResyncButton");
  if (fullButton) {
    renderFullResyncButton(fullButton, {
      running: fullRunning,
      blocked: !usable || Boolean(data?.garmin_sync?.running || state.localSync.garmin),
      idleLabel: "Lokale Daten neu laden",
    });
  }
  const fullStatus = $("#garminFullResyncStatus");
  if (!fullStatus || !usable) return;
  fullStatus.classList.toggle("error", Boolean(fullResync.last_error));
  let statusText = "";
  if (fullRunning && fullResync.status) statusText = fullResync.status;
  else if (fullResync.last_error) statusText = fullResync.last_error;
  else if (fullResync.last_resync_at) statusText = `Letzter vollständiger Resync: ${formatTime(fullResync.last_resync_at)}`;
  fullStatus.textContent = statusText;
  resumeFullResync(providerIds().garmin);
}

function renderUnavailableGarmin(garmin, status, detail, button) {
  const unavailable = !garmin?.available;
  if (!unavailable && garmin.configured) return false;
  // An unset card is named by its chip alone (#3936 M-03).
  status.textContent = unavailable ? "Garmin nicht verfügbar" : "";
  detail.textContent = "";
  button.disabled = true;
  return true;
}

function renderGarminKeyHint(garmin) {
  const node = $("#garminKeyHint");
  if (!node) return;
  const hint = garminKeyHint(garmin);
  node.textContent = hint.text;
  node.hidden = !hint.text;
  node.classList.toggle("error", hint.urgent);
}

function renderGarmin(garmin) {
  const status = $("#garminStatus");
  const detail = $("#garminDetail");
  const button = $("#garminSyncButton");
  if (!status || !detail || !button) return;
  renderGarminKeyHint(garmin);
  const fullResync = state.data?.provider_resync?.garmin || {};
  const fullRunning = Boolean(fullResync.running || state.localSync.garminFull);
  button.disabled = Boolean(state.data?.garmin_sync?.running || fullRunning);
  const unavailable = renderUnavailableGarmin(garmin, status, detail, button);
  renderGarminFullResync(state.data, garmin);
  if (unavailable) return;
  if (garmin.source === "fixture") status.textContent = "Lokale Garmin-Testdaten aktiv";
  else if (garmin.last_error) status.textContent = "Mit Fehlern synchronisiert";
  else status.textContent = "Optionaler Direktabruf aktiv";
  detail.textContent = garminDetailText(garmin);
  const stale = deviceSyncSentence(garmin.device_synced_at);
  if (stale) detail.append(h("br"), h("span", "device-sync-stale", stale));
}


function currentPerformanceValues(metrics, history) {
  const values = { ...metrics };
  const preferred = history?.body?.preferred_providers?.cycling_w_per_kg;
  const points = (history?.body?.windows?.["91d"]?.metrics?.cycling_w_per_kg || []).flatMap((item) => (item.points || []).filter(analysisValidPoint).map((point) => ({ ...point, source: item.source, ..."source_provider" in item ? { source_provider: item.source_provider } : {}, unit: "W/kg" }))).sort((a, b) => b.date.localeCompare(a.date) || Number(isPreferredSource(b, preferred)) - Number(isPreferredSource(a, preferred)));
  // The preferred provider's newest point wins even when another provider is newer; others only fill in when it has none.
  values.cycling_w_per_kg = (preferred === undefined ? undefined : points.find((point) => isPreferredSource(point, preferred))) || points[0];
  return values;
}

function performanceDataStatus(performance) {
  return typeof performance?.status === "string" && performance.status ? [performance.status] : [];
}

// One row per metric with a value, in the given order.
function currentPerformanceRows(metrics, items) {
  const body = h("tbody");
  for (const [key, label] of items) {
    const metric = metrics?.[key];
    if (metric?.value == null) continue;
    body.append(h("tr", { dataset: { metric: key } }, h("th", { scope: "row" }, label), h("td", null, analysisValue(metric.value, metric.unit || ""))));
  }
  return body;
}

// LT1 per sport from the performance values (#2427): measured, the adopted HRV estimate,
// own sessions at the LT1 heart rate, the top of zone 2 or a fixed share of LT2.
function withFirstThresholds(metrics, thresholds) {
  const lt1 = thresholds?.first_thresholds || {};
  const value = (number, unit) => (number == null ? undefined : { value: number, unit });
  return {
    ...metrics,
    run_lt1_pace_seconds_per_km: value(lt1.run?.pace_seconds_per_km, "s/km"),
    run_lt1_hr_bpm: value(lt1.run?.hr_bpm, "bpm"),
    cycling_lt1_watts: value(lt1.ride?.watts, "W"),
    cycling_lt1_hr_bpm: value(lt1.ride?.hr_bpm, "bpm"),
  };
}

// The local LT2 estimates beside Garmin's (#3273), each an estimate; unknown ones are left out.
function withLt2Estimates(metrics, estimates) {
  const ok = (estimate) => (estimate?.status === "ok" ? estimate : {});
  const value = (number, unit) => (number == null ? undefined : { value: number, unit, estimated: true });
  return {
    ...metrics,
    run_lt2_dfa_hr_bpm: value(ok(estimates?.run?.dfa_alpha1).heart_rate_bpm, "bpm"),
    run_lt2_dfa_pace_seconds_per_km: value(ok(estimates?.run?.dfa_alpha1).pace_seconds_per_km, "s/km"),
    run_lt2_cs_pace_seconds_per_km: value(ok(estimates?.run?.critical_speed).pace_seconds_per_km, "s/km"),
    cycling_lt2_cp_watts: value(ok(estimates?.ride?.critical_power).power_w, "W"),
  };
}

// LT1 comes from the thresholds of the same performance values as the metrics.
function renderCurrentPerformance(metrics, history, status = [], thresholds = null, lt2Estimates = null) {
  metrics = withLt2Estimates(withFirstThresholds(currentPerformanceValues(metrics, history), thresholds), lt2Estimates);
  const root = document.getElementById("currentPerformance");
  rememberAnalysisSectionState(root);
  root.replaceChildren(h("h3", null, "Aktuelle Werte"));
  for (const text of status) root.append(h("p", "empty", text));
  const groups = [
    ["Laufen", "run", [
      ["run_threshold_pace_seconds_per_km", "Anaerobe Schwelle (LT2) · Pace"],
      ["run_threshold_watts", "Anaerobe Schwelle (LT2) · Leistung"],
      ["run_threshold_hr_bpm", "Anaerobe Schwelle (LT2) · Herzfrequenz"],
      ["run_lt2_dfa_hr_bpm", "Anaerobe Schwelle (LT2) · DFA-α1 Herzfrequenz"],
      ["run_lt2_dfa_pace_seconds_per_km", "Anaerobe Schwelle (LT2) · DFA-α1 Pace"],
      ["run_lt2_cs_pace_seconds_per_km", "Anaerobe Schwelle (LT2) · CS"],
      ["run_lt1_pace_seconds_per_km", "Aerobe Schwelle (LT1) · Pace"],
      ["run_lt1_hr_bpm", "Aerobe Schwelle (LT1) · Herzfrequenz"],
      ["running_vo2max_ml_kg_min", "VO\u2082max"],
    ]],
    ["Radfahren", "ride", [
      ["cycling_ftp_watts", "Anaerobe Schwelle (LT2) · FTP"],
      ["bike_threshold_hr_bpm", "Anaerobe Schwelle (LT2) · Herzfrequenz"],
      ["cycling_lt2_cp_watts", "Anaerobe Schwelle (LT2) · CP"],
      ["cycling_w_per_kg", "Leistung pro Gewicht"],
      ["cycling_lt1_watts", "Aerobe Schwelle (LT1) · Leistung"],
      ["cycling_lt1_hr_bpm", "Aerobe Schwelle (LT1) · Herzfrequenz"],
      ["cycling_vo2max_ml_kg_min", "VO\u2082max"],
    ]],
  ];
  const grid = h("div", "current-performance-groups");
  // Laufen and Radfahren each show their own table (#2265); the caption names it for screen readers.
  for (const [sport, analysisSport, items] of groups) {
    const body = currentPerformanceRows(metrics, items);
    // A status message (e.g. a failed read) keeps the section visible.
    root.dataset[analysisSport === "run" ? "analysisEmptyRunning" : "analysisEmptyCycling"] = String(!body.childElementCount && !status.length);
    if (!body.childElementCount) {
      const none = h("p", "muted", "Noch keine aktuellen Werte.");
      none.dataset.analysisSport = analysisSport;
      grid.append(none);
      continue;
    }
    grid.append(h("table", { className: "current-performance-table", dataset: { analysisSport } }, h("caption", "sr-only", sport), body));
  }
  root.dataset.analysisEmpty = String(root.dataset.analysisEmptyRunning === "true" && root.dataset.analysisEmptyCycling === "true");
  root.append(grid);
  makeAnalysisSectionCollapsible(root, "current-performance");
}

// "Neue Schwelle" above Aktuelle Werte (#4135): one row per offer of the sport; Verwerfen dismisses them all.
function analysisThresholdOfferCard(sport, offers) {
  const rows = offers.flatMap((offer) => {
    const view = thresholdOfferView(offer);
    return [h("div", { className: "threshold-offer-line", dataset: { thresholdOffer: offer.key } }, h("b", null, `${view.label} · ${view.value}`), h("span", "zone-muted", view.brief)), ...thresholdOfferButtons([offer], { dismiss: false })];
  });
  const details = h("a", { href: "#more/profile" }, "Details in Zonen & Schwellen");
  details.addEventListener("click", (event) => openThresholdOfferDetails(event, sport));
  const [, discard] = thresholdOfferButtons(offers);
  return h("section", { className: "analysis-chart-card threshold-offer", "aria-label": "Neue Schwelle", dataset: { analysisSport: sport } },
    h("h3", null, "Neue Schwelle"), h("div", "threshold-offer-list", ...rows), h("div", "threshold-offer-foot", details, discard));
}

function renderAnalysisThresholdOffers() {
  const root = $("#analysisThresholdOffers");
  if (!root) return;
  root.replaceChildren(...["run", "ride"].map((sport) => [sport, thresholdOffersFor(sport)]).filter(([, offers]) => offers.length).map(([sport, offers]) => analysisThresholdOfferCard(sport, offers)));
  globalThis.renderAnalysisSegmentsIfActive?.();
}

// The link opens Zonen & Schwellen on the offer's sport; a modified click keeps the browser's own behaviour.
function openThresholdOfferDetails(event, sport) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button > 0) return;
  event.preventDefault();
  void AppRouter.navigate("more/profile", { historyMode: "push" }).then((shown) => {
    const section = shown && $("#zoneSettings");
    if (!section) return;
    showZoneSport(sport);
    section.open = true;
    section.scrollIntoView({ block: "start" });
  });
}

// A cold start has no performance read yet; the two cards say so instead of claiming there is no data (A-13).
function renderPerformanceLoading() {
  const current = $("#currentPerformance");
  current.dataset.analysisEmpty = "loading";
  current.dataset.analysisEmptyRunning = "loading";
  current.dataset.analysisEmptyCycling = "loading";
  current.replaceChildren(h("h3", null, "Aktuelle Werte"), createLoadingState("Werte werden geladen…"));
  const predictions = $("#performancePredictions");
  predictions.dataset.analysisEmpty = "loading";
  predictions.hidden = false;
  predictions.replaceChildren(h("h4", null, "Laufprognosen"), createLoadingState("Prognosen werden geladen…"));
}

function renderPerformance(performance, { refreshCharts = true } = {}) {
  if (refreshCharts) {
    void renderTrainingRecords({ background: true });
    renderPersonalRecovery(performance?.personal_recovery);
    renderAnalysisHistory(performance?.history);
    void renderCyclingPowerProfile();
    renderTrainingFocus(analysisTrainingFocus(performance));
  }
  // Stored values of an earlier start (the device snapshot) show at once; without any, the read is awaited.
  if (!state.loadedAreas?.has("performance") && !Object.keys(performance?.metrics || {}).length) {
    renderPerformanceLoading();
    renderAnalysisSegments(state.route);
    return;
  }
  const root = $("#performancePredictions");
  root.replaceChildren();
  const values = performance?.metrics || {};
  renderCurrentPerformance(values, performance?.history, performanceDataStatus(performance), performance?.thresholds, state.data?.lt2_estimates);
  refreshThresholdBox();
  refreshThresholdOffers();
  const predictions = racePredictionRows(values);
  root.hidden = false;
  root.append(h("h4", null, "Laufprognosen"));
  root.dataset.analysisEmpty = String(!predictions.length);
  if (!predictions.length) {
    root.append(h("p", "muted", racePredictionEmptyReason(values)));
  } else {
    const rows = predictions.map(({ label, metric, withSeconds }) => [h("th", { scope: "row" }, label), predictionTime(metric.value, withSeconds)]);
    const scroll = makeScrollRegionFocusable(h("div", "table-scroll"), "Zeiten nach Distanz");
    scroll.append(table(["Distanz", "Zeit"], rows, { className: "race-predictions-table" })); root.append(scroll);
  }
  renderAnalysisSegments(state.route);
}

// Model estimates are times on the clock, m:ss or h:mm:ss with no unit (#4194): 5 and 10 km with their seconds
// ("21:47"), the longer races rounded to the minute ("1:39:00").
function predictionTime(seconds, withSeconds = false) {
  const total = Number(seconds);
  if (withSeconds) return AppFormat.clock(total);
  return AppFormat.clock(Math.round(total / 60) * 60);
}

function racePredictionRows(metrics) {
  // 5 and 10 km keep their seconds (#2263); the longer races stay in hours and minutes.
  const distances = [["run_5k_seconds", "5 km", true], ["run_10k_seconds", "10 km", true],
    ["run_half_marathon_seconds", "Halbmarathon", false], ["run_marathon_seconds", "Marathon", false]];
  const values = metrics || {};
  return distances.filter(([key]) => values[key]?.value != null).map(([key, label, withSeconds]) => ({ label, metric: values[key], withSeconds }));
}

function racePredictionEmptyReason(metrics) {
  const values = metrics || {};
  const runningValues = ["run_threshold_pace_seconds_per_km", "running_vo2max_ml_kg_min"].some((key) => values[key]?.value != null);
  return runningValues
    ? "Noch keine Prognosen – Laufwerte sind vorhanden, aber es wurde keine Laufprognose geliefert."
    : "Noch keine Prognosen – dafür fehlen aktuelle Leistungswerte aus Garmin oder Wahoo.";
}
