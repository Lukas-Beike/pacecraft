/* Local activity view of the served activity_detail.v1 document. Provider reads require an explicit durable refresh job. */
(() => {
  let dialog;
  let generation = 0;

  function chart(times, values, label, unit, tableOnly = false) {
    if (!Array.isArray(times) || !Array.isArray(values) || times.length !== values.length) return null;
    const points = times.map((time, index) => [time, values[index]])
      .filter(([time, value]) => typeof time === "number" && typeof value === "number" && Number.isFinite(time) && Number.isFinite(value));
    if (points.length < 2) return null;
    const width = 680; const height = 220; const left = 64; const right = 12; const top = 16; const bottom = 34;
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${label}, Zeitachse in Minuten; Messwerte und Lücken sind in der Tabelle aufgeführt` });
    const min = Math.min(...points.map((point) => point[1]));
    const max = Math.max(...points.map((point) => point[1]));
    const start = points[0][0];
    const duration = Math.max(1, points.at(-1)[0] - start);
    // A near-constant series keeps an axis span of at least 5 % of its level (1 unit minimum) and stays a line.
    const level = (max + min) / 2;
    const axisSpan = Math.max(max - min, 1, Math.abs(level) * 0.05);
    const axisMax = level + axisSpan / 2;
    const y = (value) => top + (axisMax - value) / axisSpan * (height - top - bottom);
    const x = (time) => left + (time - start) / duration * (width - left - right);
    const ticks = [axisMax, level, level - axisSpan / 2];
    ticks.forEach((value) => {
      svg.append(svgNode("line", { x1: left, x2: width - right, y1: y(value), y2: y(value), class: "activity-gridline" }));
      svg.append(svgNode("text", { x: left - 8, y: y(value) + 4, "text-anchor": "end" }, AppFormat.number(value, { digits: 1 })));
    });
    const xTicks = [start, start + duration / 2, points.at(-1)[0]];
    xTicks.forEach((time) => svg.append(svgNode("text", { x: x(time), y: height - 8, "text-anchor": "middle" }, `${Math.round(time / 60)} min`)));
    let continuing = false;
    const path = times.map((time, index) => {
      const value = values[index];
      if (typeof time !== "number" || typeof value !== "number" || !Number.isFinite(time) || !Number.isFinite(value)) { continuing = false; return ""; }
      const command = continuing ? "L" : "M";
      continuing = true;
      return `${command}${x(time).toFixed(1)},${y(value).toFixed(1)}`;
    }).join(" ");
    svg.append(svgNode("path", { d: path }));
    const tableWrap = makeScrollRegionFocusable(h("div", "analysis-chart-table"), `${label} in ${unit}: Messwerte`);
    const rows = [];
    times.forEach((time, index) => {
      if (typeof time !== "number" || !Number.isFinite(time)) return;
      const value = values[index];
      const reading = typeof value === "number" && Number.isFinite(value) ? `${AppFormat.number(value, { digits: 2 })} ${unit}` : "Lücke";
      rows.push([h("th", { scope: "row" }, `${Math.floor(time / 60)}:${String(Math.floor(time % 60)).padStart(2, "0")}`), reading]);
    });
    tableWrap.append(table(["Zeit", label], rows, { caption: `${label} in ${unit}; fehlende Werte sind als Lücke markiert.` }));
    // The dialog keeps the value tables; the charts are the inline view's (#4221).
    if (tableOnly) return h("details", "activity-values", h("summary", null, `${label} (${unit}): alle ${points.length} Messwerte ansehen`), tableWrap);
    return h("section", "activity-series", h("h3", null, `${label} (${unit})`), svg,
      h("p", "muted", `${AppFormat.number(min, { digits: 1 })}–${AppFormat.number(max, { digits: 1 })} ${unit} · ${Math.round(start / 60)}–${Math.round(points.at(-1)[0] / 60)} min · ${times.length - points.length} fehlende Messpunkte`),
      h("details", "activity-values", h("summary", null, `Alle ${points.length} Messwerte ansehen`), tableWrap));
  }

  // Served metric values by key; metrics the document does not carry stay absent.
  function metricValues(payload) {
    const values = {};
    for (const [key, metric] of Object.entries(payload?.metrics || {})) {
      if (metric?.value != null) values[key] = metric.value;
    }
    return values;
  }

  // The dialog shows what the inline "Details" showed (the panel of the activity analysis, in its style) and, below it, what only
  // the dialog has: the value tables, strength sets, interval quality, heart rate drift, best power, equipment and the recordings.
  // `panelSource`: the calendar entry the panel starts from and, when there is one, the calendar's shared read.
  function render(payload, content, api, activityId, reload, notice, panelSource) {
    const { summary, read } = panelSource;
    content.replaceChildren();
    const panel = h("div", "activity-analysis-panel");
    content.append(panel);
    // The panel shows the document the dialog just loaded (also after a split, merge or undo); a refresh or retry reads it
    // fresh, through the calendar's shared read when there is one, so the card shows it too (#2478).
    const fresh = read || (() => api(`/api/activities/${encodeURIComponent(activityId)}`));
    const panelRead = (again) => (again ? fresh(true) : Promise.resolve(payload));
    void ActivityAnalysis.loadPanel(panel, summary, panelRead);
    renderSeries(payload.session || {}, payload.streams, content);
    renderStrengthSets(payload.strength_sets, content);
    renderIntervalQuality(payload, content);
    renderAerobic(payload, content);
    renderPower(payload, content);
    for (const item of payload.equipment || []) content.append(h("p", null, `Ausrüstung: ${item.name} · ${item.usage.distance_km} km${item.usage.maintenance_due ? " · persönliches Wartungsintervall erreicht" : ""}`));
    renderBundle(payload.session_bundle, api, content, reload, notice);
  }

  function recordingText(entry) {
    const parts = [entry.label];
    if (entry.native_type) parts.push(entry.native_type);
    if (entry.started_at) parts.push(formatTime(entry.started_at));
    if (Number.isFinite(entry.duration_s)) parts.push(`${Math.round(entry.duration_s / 60)} min`);
    return parts.join(" · ");
  }

  // The provider copies bundled into this session; confirm, split and merge each carry an undo.
  function renderBundle(bundle, api, content, reload, notice) {
    const members = bundle?.members || []; const candidates = bundle?.merge_candidates || [];
    if (!reload || !bundle) return;
    const section = h("section", "activity-bundle", h("h3", null, "Aufzeichnungen"));
    const status = h("p", "muted", notice?.text || ""); status.setAttribute("role", "status");
    const buttons = [];
    const action = (label, body, done) => {
      const button = h("button", { className: "secondary-button", type: "button" }, label); buttons.push(button);
      button.addEventListener("click", async () => {
        buttons.forEach((item) => { item.disabled = true; }); status.textContent = "Speichere …";
        try {
          const result = await api(`/api/activities/${encodeURIComponent(bundle.activity_id)}/session`, { method: "POST", body: JSON.stringify(body) });
          await reload({ text: done, change: result.change });
        } catch (error) { buttons.forEach((item) => { item.disabled = false; }); status.textContent = error.message; }
      });
      return button;
    };
    const list = (entries, label, body, done) => {
      const items = h("ul", "activity-bundle-list");
      for (const entry of entries) {
        const item = h("li", null, recordingText(entry));
        if (label) { item.append(" ", action(label, body(entry), done)); }
        items.append(item);
      }
      return items;
    };
    const several = members.length > 1;
    section.append(list(members, several ? "Trennen" : null, (entry) => ({ action: "split", member_id: entry.session_id }), "Aufzeichnung getrennt. "));
    if (several && !bundle.athlete_confirmed) section.append(action("Zuordnung bestätigen", { action: "confirm" }, "Zuordnung bestätigt. "));
    if (candidates.length) { section.append(h("h4", null, "Am selben Tag")); section.append(list(candidates, "Zusammenführen", (entry) => ({ action: "merge", other_id: entry.session_id }), "Aktivitäten zusammengeführt. ")); }
    if (notice?.change?.id) status.append(undoButton(api, notice.change, status, reload));
    const remove = h("button", { className: "secondary-button", type: "button" }, "Aktivität löschen"); buttons.push(remove);
    remove.addEventListener("click", async () => {
      const confirmed = await requestConfirmation("Die Aktivität wird mit allen Aufzeichnungen lokal gelöscht. Bei den Anbietern bleibt sie erhalten.", { title: "Aktivität löschen?", danger: true, confirmLabel: "Löschen" });
      if (!confirmed) return;
      buttons.forEach((item) => { item.disabled = true; }); status.textContent = "Speichere …";
      try {
        const result = await api(`/api/activities/${encodeURIComponent(bundle.activity_id)}/session`, { method: "POST", body: JSON.stringify({ action: "delete", confirmed: true }) });
        const deleted = h("p", "muted", "Aktivität gelöscht. "); deleted.setAttribute("role", "status");
        if (result.change?.id) deleted.append(undoButton(api, result.change, deleted, reload));
        content.replaceChildren(deleted); reload.deleted?.();
      } catch (error) { buttons.forEach((item) => { item.disabled = false; }); status.textContent = error.message; }
    });
    section.append(remove);
    section.append(status); content.append(section);
  }

  // Undo of a session correction restores the touched sessions, then the detail reloads.
  function undoButton(api, change, status, reload) {
    const undo = h("button", { className: "message-undo", type: "button" }, "Rückgängig");
    undo.addEventListener("click", async () => {
      undo.disabled = true;
      try {
        await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: change.id, expected_current_hash: change.after_hash }) });
        await reload({ text: "Zurückgenommen." });
      } catch (error) { undo.disabled = false; status.textContent = error.message; }
    });
    return undo;
  }

  // A session's temperature is only its activity weather, named with its source (#3036):
  // the watch sensor reads body-warmed values.
  function weatherTemperature(weather) {
    if (!Number.isFinite(weather?.temperature_c)) return null;
    const value = `${AppFormat.number(weather.temperature_c, { digits: 1 })} °C`;
    return weather.source ? `${value} (${weather.source})` : value;
  }


  const ROUTE_VISIT_PAGE = 6;
  const PACE_SPORTS = new Set(["run", "walk", "hike"]);

  // A visit's date: day and month this year, with a short year before it.
  function routeVisitDate(value) {
    const [year, month, day] = String(value || "").split("-");
    if (!day) return "–";
    return year === String(new Date().getFullYear()) ? `${day}.${month}.` : `${day}.${month}.${year}`;
  }

  function routeVisitClock(seconds) {
    const value = Number(seconds);
    if (seconds == null || !Number.isFinite(value) || value <= 0) return "–";
    const total = Math.round(value);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  function routeVisitNumber(value, digits = 0) {
    const number = Number(value);
    return value == null || !Number.isFinite(number) ? "–" : AppFormat.number(number, { digits });
  }

  // Pace in s/km for foot sports, else km/h; a missing or zero speed, or a visit of another
  // sport on the same loop, stays a gap.
  function routeVisitOutput(visit, pace) {
    const speed = Number(visit.avg_speed_mps);
    if (visit.sport && PACE_SPORTS.has(visit.sport) !== pace) return null;
    if (visit.avg_speed_mps == null || !Number.isFinite(speed) || speed <= 0) return null;
    return pace ? 1000 / speed : speed * 3.6;
  }

  // Pace (or speed) per visit over time, drawn like the Analyse charts; the table below holds the values.
  function routeVisitChart(visits, pace) {
    const points = visits.map((visit) => ({ date: visit.date, value: routeVisitOutput(visit, pace) })).filter((point) => point.date).reverse();
    if (typeof analysisChart !== "function" || points.length < 2) return null;
    const unit = pace ? "s/km" : "km/h";
    const title = pace ? "Pace je Besuch" : "Geschwindigkeit je Besuch";
    const chart = analysisChart(title, [{ label: title, legendLabel: title, unit, color: 0, cadenceDays: 1, points }], unit, points[0].date, points.at(-1).date, "Durchschnitt je Besuch.", { showLegend: false });
    // The visit table below replaces the chart's own "Werte ansehen".
    chart?.querySelector("details")?.remove();
    return chart;
  }

  // Every visit, newest first, a page at a time; the session's own visit is marked.
  function routeVisitTable(visits, pace, sessionId) {
    const body = h("tbody");
    const more = h("button", { className: "secondary-button", type: "button" }, "Ältere Besuche");
    let shown = 0;
    const page = () => {
      for (const visit of visits.slice(shown, shown + ROUTE_VISIT_PAGE)) {
        const output = routeVisitOutput(visit, pace);
        let outputText = "–";
        if (output != null) outputText = pace ? routeVisitClock(output) : routeVisitNumber(output, 1);
        const row = h("tr", null,
          h("th", { scope: "row" }, routeVisitDate(visit.date)),
          // A visit over an hour reads h:mm:ss, like the session duration.
          h("td", null, Number(visit.moving_duration_s) > 0 ? AppFormat.clock(visit.moving_duration_s) : "–"),
          h("td", null, routeVisitNumber(visit.avg_hr)),
          h("td", null, outputText),
          h("td", null, routeVisitNumber(visit.temperature_c)));
        if (visit.activity_id === sessionId) row.setAttribute("aria-current", "true");
        body.append(row);
      }
      shown += ROUTE_VISIT_PAGE;
      more.hidden = shown >= visits.length;
    };
    more.addEventListener("click", page);
    page();
    const head = h("thead", null, h("tr", null, ["Datum", "Dauer", "Herzfrequenz", pace ? "/km" : "km/h", "°C"].map((label) => h("th", { scope: "col" }, label))));
    const scroll = h("div", "table-scroll", h("table", "race-predictions-table route-visits", head, body));
    return h("details", { className: "activity-values", open: true }, h("summary", null, `Alle ${visits.length} Besuche ansehen`), scroll, more);
  }

  // The recognised route of the session: its name and visit with a rename action, then the
  // pace or speed of every visit as a chart and a table (#3763). Never a coordinate.
  function renderRoute(route, api, content, session = {}) {
    if (!route) return;
    const line = h("div", "activity-route-head");
    let label = route.label;
    const show = () => {
      const rename = h("button", { className: "secondary-button", type: "button" }, "Umbenennen");
      rename.addEventListener("click", edit);
      line.replaceChildren(h("h3", null, `${label} · ${route.repeat_index}. von ${route.repeat_count} Besuchen`), rename);
    };
    const edit = () => {
      const input = h("input", { maxLength: 60, required: true, value: label, "aria-label": "Streckenname" });
      const save = h("button", { type: "submit" }, "Speichern");
      const cancel = h("button", { className: "secondary-button", type: "button" }, "Abbrechen"); cancel.addEventListener("click", show);
      const status = h("span", "muted"); status.setAttribute("role", "status");
      const form = h("form", null, input, " ", save, " ", cancel, " ", status);
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); save.disabled = true; status.textContent = "Speichere …";
        try {
          const result = await api("/api/routes/rename", { method: "POST", body: JSON.stringify({ route_id: route.route_id, name: input.value }) });
          label = result.label; show();
        } catch (error) { status.textContent = error.message; save.disabled = false; }
      });
      line.replaceChildren(form); input.focus();
    };
    show();
    const card = h("section", "activity-route", line);
    const visits = Array.isArray(route.visits) ? route.visits : [];
    if (visits.length) {
      const pace = PACE_SPORTS.has(session.sport);
      const chart = routeVisitChart(visits, pace);
      if (chart) card.append(chart);
      card.append(routeVisitTable(visits, pace, session.activity_id));
    }
    content.append(card);
  }

  const COVERAGE_LABELS = { power_w: "Leistung", heart_rate_bpm: "Herzfrequenz", speed_mps: "Geschwindigkeit", altitude_m: "Höhe", distance_m: "Distanz", grade_pct: "Steigung", cadence_rpm: "Trittfrequenz", temperature_c: "Temperatur", moving: "Bewegung", ground_contact_time_ms: "Bodenkontaktzeit", ground_contact_balance_pct: "Bodenkontakt links", vertical_oscillation_cm: "Vertikale Oszillation", vertical_ratio_pct: "Vertikales Verhältnis", step_length_m: "Schrittlänge", respiration_rate_brpm: "Atemfrequenz", performance_condition: "Leistungszustand", stamina_137_pct: "Ausdauer A", stamina_138_pct: "Ausdauer B" };

  // Per provider: the load state and the data coverage, with readable channel names.
  function providerFacts(entry) {
    const label = entry.label || entry.provider;
    const facts = [h("p", "muted", entry.fetched_at ? `${label} · Detaildaten geladen: ${formatTime(entry.fetched_at)}` : `${label} · Detaildaten wurden noch nicht geladen.`)];
    const coverage = Object.entries(entry.coverage || {}).map(([name, value]) => `${COVERAGE_LABELS[name] || name}: ${value?.valid_points} von ${value?.points}`);
    if (coverage.length) facts.push(h("p", "muted", `Datenabdeckung ${label} · ${coverage.join(" · ")} Messwerten`));
    return facts;
  }

  // Warnings and load errors stay visible; the provenance is one muted source line with the details folded away.
  // Each contributing provider reports its own load state; provider text is shown as plain text only.
  function providerNotes(payload) {
    const detail = payload?.detail_data || {};
    const providers = detail.providers || [];
    const notes = [];
    const facts = [];
    const loaded = providers.filter((entry) => entry.fetched_at);
    if (providers.length) {
      const latest = loaded.map((entry) => entry.fetched_at).sort().at(-1);
      notes.push(h("p", "muted", loaded.length ? `Quelle: ${loaded.map((entry) => entry.label || entry.provider).join(", ")} · Stand ${formatTime(latest)}` : "Detaildaten noch nicht geladen"));
    }
    for (const entry of providers) {
      const label = entry.label || entry.provider;
      if (entry.stale) notes.push(h("p", "muted", `${label}: Die Zusammenfassung hat sich seit dem Laden geändert. Aktualisiere die Detaildaten vor einer neuen Bewertung.`));
      if (entry.load_error?.message) notes.push(h("p", "activity-analysis-error", `${label}: ${entry.load_error.message} Automatischer Ladeversuch: ${formatTime(entry.load_error.observed_at)}`));
      facts.push(...providerFacts(entry));
    }
    // The simplified-charts note changes how the charts read, so it stays outside the folded details.
    if (facts.length) {
      notes.push(h("details", "provenance-details", h("summary", null, "Herkunft im Detail"), facts));
    }
    return notes;
  }

  // One explicit detail load per provider entry the server marks refreshable.
  function refreshActions(payload, { api, isCurrent, onStatus, onLoaded, onSetup = () => {} }) {
    const actions = h("div", "dialog-actions");
    const activityId = String(payload?.session?.activity_id || "");
    for (const entry of payload?.detail_data?.providers || []) {
      const label = entry.label || entry.provider;
      if (!entry.refreshable || !activityId) {
        if (state.data?.configured?.[entry.provider] !== false) continue;
        const link = h("a", { href: "#more/connections", dataset: { connection: entry.provider } }, "Anbindung einrichten");
        link.addEventListener("click", onSetup);
        actions.append(h("p", "muted provider-setup-hint", `${label} ist nicht eingerichtet. `, link));
        continue;
      }
      const button = h("button", { className: "secondary-button", type: "button" }, `${label}: Detaildaten ${entry.fetched_at ? "aktualisieren" : "laden"}`);
      button.addEventListener("click", async () => {
        const buttons = [...actions.querySelectorAll("button")];
        buttons.forEach((item) => { item.disabled = true; });
        onStatus("Ladejob wird gestartet …");
        try {
          if (await requestDetails(activityId, { provider: entry.provider, api, isCurrent, onStatus })) { await onLoaded(); return; }
          if (isCurrent()) onStatus("Der Ladejob läuft im Hintergrund weiter.");
        } catch (error) {
          if (isCurrent()) onStatus(error.message);
        }
        buttons.forEach((item) => { item.disabled = false; });
      });
      actions.append(button);
    }
    return actions;
  }


  // Running cadence is stored per leg; foot sports show steps per minute of both legs.
  function cadenceDisplay(activity) {
    if (["run", "walk", "hike"].includes(activity?.sport)) return { label: "Schrittfrequenz", unit: "spm", factor: 2 };
    return { label: "Trittfrequenz", unit: "rpm", factor: 1 };
  }

  // Running dynamics averages (#1870): label, unit and decimals per served metric key.
  const RUNNING_DYNAMICS = [
    ["ground_contact_time_ms", "Bodenkontaktzeit", "ms", 0],
    ["ground_contact_balance_pct", "Bodenkontakt links", "%", 1],
    ["vertical_oscillation_cm", "Vertikale Oszillation", "cm", 1],
    ["vertical_ratio_pct", "Vertikales Verhältnis", "%", 1],
    ["step_length_m", "Schrittlänge", "m", 2],
  ];
  const DRIFT_METRICS = { contact_time: ["Bodenkontaktzeit", 0], vertical_oscillation: ["Vertikale Oszillation", 1], step_length: ["Schrittlänge", 2] };
  const decimal = (value, digits) => AppFormat.number(value, { digits, minDigits: digits });
  const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

  // Label and text of each running dynamics average a sensor measured; none when the device had no sensor.
  function runningDynamics(metrics) {
    return RUNNING_DYNAMICS
      .filter(([key]) => isNumber(metrics?.[key]))
      .map(([key, label, unit, digits]) => [label, `${decimal(metrics[key], digits)} ${unit}`]);
  }

  // Lines of the form drift check, led by its label when the run is flagged; none unless the check ran.
  function formDriftLines(drift) {
    if (drift?.status !== "ok") return [];
    const lines = Object.entries(drift.metrics || {})
      .filter(([name, metric]) => DRIFT_METRICS[name] && isNumber(metric?.first) && isNumber(metric?.last) && isNumber(metric?.change_percent))
      .map(([name, metric]) => {
        const [label, digits] = DRIFT_METRICS[name];
        return `${label} ${decimal(metric.first, digits)} → ${decimal(metric.last, digits)} ${metric.unit} (${AppFormat.signed(metric.change_percent, { digits: 1, minDigits: 1 })} %)`;
      });
    return drift.flagged && typeof drift.label === "string" ? [drift.label, ...lines] : lines;
  }

  function renderSeries(session, streams, content) {
    const tables = [];
    const cadence = cadenceDisplay(session);
    const labels = { power_w: ["Leistung", "W"], heart_rate_bpm: ["Herzfrequenz", "bpm"], speed_mps: ["Geschwindigkeit", "m/s"], altitude_m: ["Höhe", "m"], distance_m: ["Distanz", "m"], grade_pct: ["Steigung", "%"], cadence_rpm: [cadence.label, cadence.unit], respiration_rate_brpm: ["Atemfrequenz", "/min"], performance_condition: ["Leistungszustand", ""], stamina_137_pct: ["Ausdauer A", "%"], stamina_138_pct: ["Ausdauer B", "%"] };
    const channels = streams?.channels || {};
    for (const [key, [label, unit]] of Object.entries(labels)) {
      let values = channels[key]?.values;
      if (!Array.isArray(values)) continue;
      if (key === "cadence_rpm" && cadence.factor !== 1) values = values.map((value) => (typeof value === "number" ? value * cadence.factor : value));
      // An unofficial provider value names itself as such, in the server's words (#3374).
      const source = channels[key].source;
      const title = source?.kind === "unofficial" && source.label ? `${label} · ${source.label}` : label;
      const series = chart(streams.time_s, values, title, unit, true);
      if (series) tables.push(series);
    }
    if (tables.length) content.append(h("h4", null, "Messwerte"), ...tables);
  }




  const STRENGTH_SOURCES = { garmin_exercise_sets: "Garmin-Sätze", fit_set_messages: "FIT-Datei" };

  // "Kniebeuge – Barbell Back Squat": the category links it to the plan, the code names the exercise.
  function strengthExerciseLabel(exercise) {
    const category = AppFormat.strengthCategory(exercise.category);
    const code = typeof exercise.name === "string" && exercise.name
      ? exercise.name.toLowerCase().split("_").map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ")
      : null;
    return [category, code].filter(Boolean).join(" – ") || "Übung";
  }

  function strengthNumber(value, digits = 1) {
    const number = Number(value);
    return value == null || !Number.isFinite(number) ? "–" : AppFormat.number(number, { digits: Number.isInteger(number) ? 0 : digits });
  }

  function strengthSeconds(value) {
    const seconds = Number(value);
    if (value == null || !Number.isFinite(seconds)) return "–";
    return `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")}`;
  }

  // Reps, else the duration of a timed set.
  function strengthAmount(set) {
    if (set.reps != null) return strengthNumber(set.reps, 0);
    return strengthSeconds(set.duration_s);
  }

  function renderStrengthSets(strength, content) {
    if (!strength?.exercises?.length) return;
    content.append(h("h3", null, "Kraftsätze"));
    const sets = Number(strength.active_sets) || 0;
    content.append(h("p", "muted", [
      `${sets} ${sets === 1 ? "Satz" : "Sätze"}`,
      strength.total_reps != null ? `${strength.total_reps} Wdh.` : null,
      strength.volume_kg != null ? `${strengthNumber(strength.volume_kg)} kg Volumen` : null,
      STRENGTH_SOURCES[strength.source],
    ].filter(Boolean).join(" · ")));
    const tableWrap = makeScrollRegionFocusable(h("div", "analysis-chart-table"), "Kraftsätze");
    const grid = h("table", null, h("caption", null, "Ausgeführte Sätze je Übung"),
      h("thead", null, h("tr", null, ["Übung", "Satz", "Wdh. / Dauer", "kg", "Pause"].map((label) => h("th", { scope: "col" }, label)))));
    strength.exercises.forEach((exercise) => {
      const setList = Array.isArray(exercise.set_list) ? exercise.set_list : [];
      // One row group per exercise, so its header covers exactly its own sets.
      const body = h("tbody");
      setList.forEach((set, index) => {
        const row = h("tr");
        if (index === 0) {
          // One header per exercise spans its sets, so every set row keeps its exercise.
          row.append(h("th", { scope: "rowgroup", rowSpan: setList.length }, strengthExerciseLabel(exercise)));
        }
        row.append(h("td", null, String(index + 1)), h("td", null, strengthAmount(set)), h("td", null, strengthNumber(set.weight_kg)), h("td", null, strengthSeconds(set.rest_s)));
        body.append(row);
      });
      grid.append(body);
    });
    tableWrap.append(grid); content.append(tableWrap);
    const omitted = Number(strength.omitted_exercises) || 0;
    if (omitted) content.append(h("p", "muted", `${omitted} weitere ${omitted === 1 ? "Übung" : "Übungen"} nicht angezeigt.`));
  }

  function renderIntervalQuality(payload, content) {
    const quality = payload.analysis?.interval_quality;
    if (quality) {
      content.append(h("h3", null, "Trainingsqualität"));
      if (quality.reason) content.append(h("p", null, quality.reason));
      if (quality.steps?.length) {
        content.append(h("p", null, `${quality.aligned_steps}/${quality.planned_steps} Schritte zugeordnet · ${quality.missing_steps} fehlen`));
        const list = h("ol", "activity-laps");
        quality.steps.forEach((step) => list.append(h("li", null, step.status === "ok"
          ? `${step.target}: ${step.mean} ${step.unit} · ${step.seconds_in_target} s im Ziel · Abweichung ${AppFormat.percent(step.average_deviation_percent)} · Abdeckung ${AppFormat.percent(step.coverage * 100, { digits: 0 })}`
          : `${step.target}: ${step.reason || "Messabdeckung unzureichend"}`)));
        content.append(list);
        quality.repeat_groups?.forEach((group) => content.append(h("p", null, `${group.target}: ${group.repetitions} vergleichbare Wiederholungen · Abfall ${AppFormat.percent(group.fade_percent)}`)));
      }
      content.append(h("p", "muted", "Einzelziele verwenden ±5%; vorgegebene Bereiche bleiben unverändert. Ziele stammen aus der ersten Detailabfrage. Frühere Vorlagenänderungen sind nicht rekonstruierbar."));
    }
  }


  function renderAerobic(payload, content) {
    const aerobic = payload.analysis?.aerobic;
    if (aerobic) {
      content.append(h("h3", null, "Herzfrequenzdrift"));
      let description = aerobic.reason;
      if (aerobic.status === "ok") {
        const longSession = aerobic.long_session ? " · lange Einheit" : "";
        description = `Drift ${AppFormat.percent(aerobic.drift_percent)} · gleichmäßige Leistung pro Schlag ${aerobic.efficiency} ${aerobic.unit}${longSession}`;
      }
      content.append(h("p", null, description));
      content.append(h("p", "muted", "Lokale Methode: zehn Minuten Aufwärmen ausgeschlossen, danach zwei gleich lange Hälften bei gleichmäßiger Belastung und mindestens 85% Messabdeckung. Temperatur, Gelände und Indoor/Outdoor beeinflussen den Vergleich; keine pauschale Fitnessbewertung."));
    }
  }


  function renderPower(payload, content) {
    const power = payload.analysis?.power_profile;
    if (power) {
      content.append(h("h3", null, "Beobachtete Bestleistung"));
      for (const point of power.points || []) {
        const duration = point.duration_seconds < 60 ? `${point.duration_seconds} s` : `${point.duration_seconds / 60} min`;
        const measurement = point.watts == null ? "keine lückenlose Messung" : `${point.watts} W`;
        content.append(h("p", null, `${duration}: ${measurement}`));
      }
    }
  }

  // `backLabel` names where closing returns to; Analyse › Rekorde passes its own (#3847).
  async function open(activity, { api, showDialog, read = null, backLabel = "Zurück zum Kalender" }) {
    close(false);
    const activityId = String(activity.id ?? activity.activity_id ?? "");
    if (!activityId) return;
    const token = ++generation;
    const navigationState = { ...history.state };
    delete navigationState.activityDetail;
    history.replaceState(navigationState, "", location.href);
    history.pushState({ ...navigationState, activityDetail: activityId }, "", location.href);
    dialog = h("dialog", { className: "activity-detail-dialog", "aria-labelledby": "activityDetailTitle" });
    const title = h("h2", { id: "activityDetailTitle" }, activity.name || "Aktivitätsdetails");
    const back = h("button", { className: "secondary-button", type: "button" }, backLabel);
    back.addEventListener("click", close);
    const content = h("div");
    const status = h("p", "muted", "Lokale Daten werden geladen …");
    status.setAttribute("role", "status");
    const actions = h("div");
    dialog.append(back, title, content, status, actions);
    document.body.append(dialog);
    const currentDialog = dialog;
    currentDialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
    const returnFocus = document.activeElement;
    dialog.addEventListener("close", () => {
      if (token === generation) generation += 1;
      currentDialog.remove();
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    }, { once: true });
    showDialog(currentDialog, back);
    const isCurrent = () => token === generation;
    async function load(notice) {
      const payload = await api(`/api/activities/${encodeURIComponent(activityId)}`);
      if (!isCurrent()) return;
      render(payload, content, api, activityId, load, notice, { summary: activity, read });
      title.textContent = payload.session?.name || activity.name || "Aktivitätsdetails";
      status.textContent = "";
    }
    // A deleted activity keeps only its receipt: its provider actions go away.
    load.deleted = () => { actions.replaceChildren(); status.textContent = ""; };
    // A failed read shows its error with a retry, as the inline view did (#4221).
    const attempt = async () => {
      try { await load(); } catch (error) {
        if (!isCurrent()) return;
        status.textContent = "";
        content.replaceChildren(createErrorState(error.message, () => { status.textContent = "Lokale Daten werden geladen …"; void attempt(); }));
      }
    };
    await attempt();
  }

  // Runs the explicit detail job of one provider; resolves false when the caller is no longer current.
  async function requestDetails(activityId, { provider, api, isCurrent = () => true, onStatus = () => {} }) {
    let job = await api("/api/sync/jobs", { method: "POST", body: JSON.stringify({ provider, type: "activity_details", payload: { activity_id: activityId } }) });
    const deadline = Date.now() + 120_000;
    while (!["completed", "failed", "partial"].includes(job.status)) {
      if (!isCurrent()) return false;
      if (Date.now() >= deadline) throw new Error("Der Job läuft weiter. Öffne die Aktivität später erneut oder prüfe Mehr → Anbindungen.");
      onStatus("Detaildaten werden im Hintergrund geladen …");
      await new Promise((resolve) => setTimeout(resolve, 1500));
      if (!isCurrent()) return false;
      job = await api(`/api/sync/jobs/${encodeURIComponent(job.id)}`);
    }
    if (job.status !== "completed") throw new Error("Detaildaten konnten nicht vollständig geladen werden. Der vorherige Stand bleibt erhalten. Details stehen unter Mehr → Anbindungen.");
    return isCurrent();
  }

  function close(navigateBack = true) {
    generation += 1;
    if (dialog?.open) dialog.close();
    dialog?.remove();
    dialog = null;
    if (navigateBack && history.state?.activityDetail) history.back();
  }

  globalThis.addEventListener("popstate", () => { if (dialog) close(false); });

  globalThis.ActivityDetails = { open, close, requestDetails, cadenceDisplay, metricValues, runningDynamics, RUNNING_DYNAMICS, formDriftLines, providerNotes, refreshActions, weatherTemperature, renderRoute, strengthExerciseLabel };
})();
