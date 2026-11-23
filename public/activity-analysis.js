/* One session's details in its calendar card (#2258). Charts follow the Garmin Connect app: one filled area per channel, average line and tap readout. */
(() => {
  const WIDTH = 340; const HEIGHT = 150; const LEFT = 40; const RIGHT = 8; const TOP = 10; const BOTTOM = 22;
  // The latest load of each panel; an older answer for the same panel is dropped.
  const panelLoads = new WeakMap();
  const panelReads = new WeakMap();

  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const numberText = (value, digits = 0) => AppFormat.number(value, { digits }) ?? "—";
  const clock = (seconds) => {
    const total = Math.round(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const rest = String(total % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
  };

  // The calendar entry and the loaded document both carry the session's neutral sport.
  function sportKind(activity) {
    const sport = activity?.sport;
    if (sport === "swim") return "swim";
    if (["run", "walk", "hike"].includes(sport)) return "run";
    return "other";
  }

  // Pace sports show min/km (or min/100 m); others km/h. Stops below 0.3 m/s become gaps.
  function speedChannel(activity) {
    const kind = sportKind(activity);
    if (kind === "other") return { label: "Geschwindigkeit", unit: "km/h", convert: (v) => v * 3.6, format: (v) => AppFormat.number(v, { digits: 1, minDigits: 1 }), color: "speed", floorAtZero: true };
    const meters = kind === "swim" ? 100 : 1000;
    return { label: "Pace", unit: kind === "swim" ? "min/100 m" : "min/km", convert: (v) => (v >= 0.3 ? meters / v : null), format: clock, invert: true, color: "speed" };
  }

  function cadenceChannel(activity, metrics) {
    const { label, unit, factor } = ActivityDetails.cadenceDisplay(activity);
    const convert = (value) => value * factor;
    return { key: "cadence_rpm", label, unit, convert, color: "cadence", format: (v) => numberText(v), floorAtZero: true, average: finite(metrics.avg_cadence) ? convert(metrics.avg_cadence) : null, max: finite(metrics.max_cadence) ? convert(metrics.max_cadence) : null };
  }

  function channels(activity, metrics, streams) {
    const speed = speedChannel(activity);
    return [
      { key: "heart_rate_bpm", label: "Herzfrequenz", unit: "bpm", color: "heart", format: (v) => numberText(v), floorAtZero: true, average: metrics.avg_hr, max: metrics.max_hr },
      { ...speed, key: "speed_mps", average: finite(metrics.avg_speed_mps) ? speed.convert(metrics.avg_speed_mps) : null, max: finite(metrics.max_speed_mps) ? speed.convert(metrics.max_speed_mps) : null },
      { key: "power_w", label: "Leistung", unit: "W", color: "power", format: (v) => numberText(v), floorAtZero: true, average: metrics.avg_power, max: metrics.max_power_w },
      cadenceChannel(activity, metrics),
      { key: "respiration_rate_brpm", label: "Atemfrequenz", unit: "/min", color: "respiration", format: (v) => numberText(v), floorAtZero: true },
      // Garmin's undocumented values (#3374), labelled by the source the server sends; the
      // stamina fields keep neutral names: the probe found no fixed order between them (#3374).
      { key: "performance_condition", label: "Leistungszustand", unit: "", color: "dynamics", format: (v) => `${v > 0 ? "+" : ""}${numberText(v)}` },
      { key: "stamina_137_pct", label: "Ausdauer A", unit: "%", color: "dynamics", format: (v) => numberText(v), floorAtZero: true },
      { key: "stamina_138_pct", label: "Ausdauer B", unit: "%", color: "dynamics", format: (v) => numberText(v), floorAtZero: true },
      { key: "altitude_m", label: "Höhe", unit: "m", color: "elevation", format: (v) => numberText(v), elevation: true },
      // Running dynamics per second, named like their averages in the values (#2258).
      ...ActivityDetails.RUNNING_DYNAMICS.map(([key, label, unit, digits]) => ({ key, label, unit, color: "dynamics", format: (v) => numberText(v, digits), average: metrics[key] })),
    ].filter((channel) => Array.isArray(streams.channels?.[channel.key]?.values));
  }

  function percentile(sorted, share) {
    return sorted[Math.min(sorted.length - 1, Math.max(0, Math.round(share * (sorted.length - 1))))];
  }

  function channelValue(channel, value) {
    if (!finite(value)) return null;
    return channel.convert ? channel.convert(value) : value;
  }

  // Robust range: single sensor spikes must not flatten the curve.
  function chartRange(sorted, channel) {
    let low = channel.elevation ? sorted[0] : percentile(sorted, 0.01);
    let high = channel.elevation ? sorted.at(-1) : percentile(sorted, 0.99);
    // A near-constant series would zoom into a solid block: the axis keeps a span of at least 5 % of the level (1 unit minimum).
    const minimumSpan = Math.max(1, Math.abs(low + high) / 2 * 0.05);
    const flat = high - low < minimumSpan;
    if (flat) { const middle = (low + high) / 2; low = middle - minimumSpan / 2; high = middle + minimumSpan / 2; }
    const padding = (high - low) * 0.08;
    // Only non-negative channels clamp at zero; elevation and pace may go below it.
    low = channel.floorAtZero ? Math.max(0, low - padding) : low - padding;
    return { low, high: high + padding, flat };
  }

  function chartSummary(channel, sorted, valid) {
    if (channel.elevation) return `Min ${channel.format(sorted[0])} · Max ${channel.format(sorted.at(-1))} ${channel.unit}`;
    const bestShare = channel.invert ? 0.01 : 0.99;
    const best = finite(channel.max) ? channel.max : percentile(sorted, bestShare);
    const bestLabel = channel.invert ? "Beste" : "Max";
    return `Ø ${channel.format(chartAverage(channel, valid))} · ${bestLabel} ${channel.format(best)} ${channel.unit}`;
  }

  // The provider's values win; otherwise the plotted mean and 1st/99th percentile ignore single GPS spikes.
  function chartAverage(channel, valid) {
    if (finite(channel.average)) return channel.average;
    return valid.reduce((sum, [, value]) => sum + value, 0) / valid.length;
  }

  function chartPaths(points, scale) {
    let line = ""; let area = ""; let segment = [];
    const flush = () => {
      if (segment.length > 1) {
        const edges = segment.map(([px, py]) => `L${px},${py}`).join(" ");
        area += `M${segment[0][0]},${scale.baseline} ${edges} L${segment.at(-1)[0]},${scale.baseline} Z `;
      }
      segment = [];
    };
    for (const [time, value] of points) {
      if (!finite(value)) { flush(); continue; }
      const px = scale.x(time).toFixed(1); const py = scale.y(value).toFixed(1);
      line += `${segment.length ? "L" : "M"}${px},${py} `;
      segment.push([px, py]);
    }
    flush();
    return { line, area };
  }

  function appendAxes(svg, channel, scale) {
    const { low, high, start, span } = scale;
    const ticks = [low + (high - low) * 0.1, (low + high) / 2, high - (high - low) * 0.1];
    for (const value of ticks) {
      svg.append(svgNode("line", { x1: LEFT, x2: WIDTH - RIGHT, y1: scale.y(value), y2: scale.y(value), class: "activity-analysis-grid" }));
      svg.append(svgNode("text", { x: LEFT - 6, y: scale.y(value) + 4, "text-anchor": "end" }, channel.format(value)));
    }
    [0, 0.5, 1].forEach((fraction) => {
      svg.append(svgNode("text", { x: scale.x(start + span * fraction), y: HEIGHT - 6, "text-anchor": ["start", "middle", "end"][fraction * 2] }, clock(span * fraction)));
    });
  }

  // Tap or drag shows the nearest reading, like the Garmin chart scrubber.
  function attachScrubber(svg, channel, scale, valid, readout) {
    const cursor = svgNode("line", { y1: TOP, y2: scale.baseline, class: "activity-analysis-cursor", visibility: "hidden" });
    const dot = svgNode("circle", { r: 3.5, class: "activity-analysis-dot", visibility: "hidden" });
    svg.append(cursor, dot);
    const inspect = (event) => {
      const box = svg.getBoundingClientRect();
      const time = scale.start + ((event.clientX - box.left) / box.width * WIDTH - LEFT) / (WIDTH - LEFT - RIGHT) * scale.span;
      const nearest = valid.reduce((best, point) => (Math.abs(point[0] - time) < Math.abs(best[0] - time) ? point : best), valid[0]);
      cursor.setAttribute("x1", scale.x(nearest[0])); cursor.setAttribute("x2", scale.x(nearest[0]));
      dot.setAttribute("cx", scale.x(nearest[0])); dot.setAttribute("cy", scale.y(nearest[1]));
      cursor.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");
      readout.textContent = `${clock(nearest[0] - scale.start)} · ${channel.format(nearest[1])} ${channel.unit}`;
    };
    svg.addEventListener("pointerdown", inspect);
    svg.addEventListener("pointermove", (event) => { if (event.buttons || event.pointerType === "mouse") inspect(event); });
    // Touch keeps the last reading visible after lifting the finger.
    svg.addEventListener("pointerleave", (event) => {
      if (event.pointerType !== "mouse") return;
      cursor.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); readout.textContent = "";
    });
  }

  // A planned step's target band over its window; an open bound reaches the chart's edge (#2830).
  function appendTargets(svg, channel, scale, targets) {
    for (const step of targets) {
      const high = step.high == null ? null : channelValue(channel, step.high);
      const low = channelValue(channel, step.low);
      const top = high == null ? TOP : scale.y(high);
      const bottom = low == null ? scale.baseline : scale.y(low);
      const left = scale.x(Math.max(step.start_s, scale.start)); const right = scale.x(Math.min(step.end_s, scale.start + scale.span));
      if (right <= left) continue;
      svg.append(svgNode("rect", { x: left, width: right - left, y: Math.min(top, bottom), height: Math.max(1, Math.abs(bottom - top)), class: "activity-analysis-target" }));
    }
  }

  function chart(streams, channel, targets = []) {
    const times = streams.time_s;
    const raw = streams.channels[channel.key].values;
    if (!Array.isArray(times) || times.length !== raw.length) return null;
    const points = times.map((time, index) => [time, channelValue(channel, raw[index])]).filter(([time]) => finite(time));
    const valid = points.filter(([, value]) => finite(value));
    if (valid.length < 2) return null;
    const sorted = valid.map(([, value]) => value).sort((a, b) => a - b);
    const { low, high, flat } = chartRange(sorted, channel);
    const start = points[0][0];
    const span = Math.max(1, points.at(-1)[0] - start);
    const share = (value) => (Math.min(high, Math.max(low, value)) - low) / (high - low);
    const scale = {
      low, high, start, span, baseline: HEIGHT - BOTTOM,
      x: (time) => LEFT + (time - start) / span * (WIDTH - LEFT - RIGHT),
      y: (value) => TOP + (channel.invert ? share(value) : 1 - share(value)) * (HEIGHT - TOP - BOTTOM),
    };

    const readout = h("span", "activity-analysis-readout");
    const summaryText = chartSummary(channel, sorted, valid);
    // An unofficial provider value names itself as such, in the server's words (#3374).
    const source = streams.channels[channel.key].source;
    const unofficial = source?.kind === "unofficial" && source.label ? createStatusChip(source.label) : null;
    unofficial?.classList.add("source-chip");
    const header = h("div", "activity-analysis-chart-header", h("h4", null, channel.label, unofficial), h("p", "activity-analysis-chart-stats", h("span", null, summaryText), readout));

    const svg = svgNode("svg", { viewBox: `0 0 ${WIDTH} ${HEIGHT}`, role: "img", "aria-label": `${channel.label}: ${summaryText}, über ${clock(span)}` });
    appendAxes(svg, channel, scale);
    appendTargets(svg, channel, scale, targets);
    const { line, area } = chartPaths(points, scale);
    // A flat series is a line only; a filled area would read as a solid block.
    if (!flat) svg.append(svgNode("path", { d: area, class: "activity-analysis-area" }));
    svg.append(svgNode("path", { d: line, class: "activity-analysis-line" }));
    if (!channel.elevation) {
      const average = scale.y(chartAverage(channel, valid));
      svg.append(svgNode("line", { x1: LEFT, x2: WIDTH - RIGHT, y1: average, y2: average, class: "activity-analysis-average" }));
    }
    attachScrubber(svg, channel, scale, valid, readout);
    return h("section", `activity-analysis-chart is-${channel.color}`, header, svg);
  }

  const measure = (value, unit) => {
    if (!finite(value)) return null;
    return unit ? `${numberText(value)} ${unit}` : numberText(value);
  };
  const duration = (value) => (finite(value) ? clock(value) : null);
  function efficiencyText(metrics) {
    if (!finite(metrics.efficiency_factor)) return null;
    const value = numberText(metrics.efficiency_factor, 2);
    return metrics.efficiency_factor_unit ? `${value} ${metrics.efficiency_factor_unit}` : value;
  }

  function speedText(speed, value) {
    const converted = finite(value) ? speed.convert(value) : null;
    return converted == null ? null : speed.format(converted);
  }

  // When the document cannot be read, the calendar entry's summary values stand in under the document's metric keys.
  function rowMetrics(row) {
    const keys = ["distance_m", "moving_duration_s", "elapsed_duration_s", "avg_speed_mps", "max_speed_mps", "avg_hr", "max_hr", "avg_power", "normalized_power", "elevation_gain_m", "intensity", "efficiency_factor", "efficiency_factor_unit", "variability_index", "work_kj", "decoupling_pct", "energy_kj", "energy_kcal", "ground_contact_time_ms", "ground_contact_balance_pct", "vertical_oscillation_cm", "vertical_ratio_pct", "step_length_m"];
    const metrics = {};
    for (const key of keys) if (row?.[key] != null) metrics[key] = row[key];
    // The calendar entry names its load like planned units do (training_load); the document calls it load.
    if (row?.training_load != null) metrics.load = row.training_load;
    return metrics;
  }

  // Strength leads with the total time, the moving time follows (#2317).
  function sessionTimes(activity, metrics) {
    const moving = duration(metrics.moving_duration_s ?? metrics.duration_s);
    const total = duration(metrics.elapsed_duration_s);
    // Two times that read the same are one time (#3932 K-04).
    if (moving && moving === total) return [["Zeit", moving]];
    const times = [["Bewegungszeit", moving], ["Gesamtzeit", total]];
    return activity?.sport === "strength" ? times.reverse() : times;
  }

  // Running values move to their own section when it is shown (#2829).
  function overview(activity, metrics, dynamicsShown = false, weather = null) {
    const speed = speedChannel(activity);
    const averageSpeed = speedText(speed, metrics.avg_speed_mps);
    const cadence = cadenceChannel(activity, metrics);
    const values = [
      ["Distanz", AppFormat.distance(metrics.distance_m)],
      ...sessionTimes(activity, metrics),
      [speed.invert ? "Ø Pace" : "Ø Geschwindigkeit", averageSpeed && `${averageSpeed} ${speed.unit}`],
      ["Ø Herzfrequenz", measure(metrics.avg_hr, "bpm")],
      ["Max. Herzfrequenz", measure(metrics.max_hr, "bpm")],
      ["Ø Leistung", measure(metrics.avg_power, "W")],
      ["Normalized Power", measure(metrics.normalized_power, "W")],
      ["Max. Leistung", measure(metrics.max_power_w, "W")],
      [`Ø ${cadence.label}`, dynamicsShown ? null : measure(cadence.average, cadence.unit)],
      ["Anstieg", measure(metrics.elevation_gain_m, "m")],
      ["Temperatur", ActivityDetails.weatherTemperature(weather)],
      ["Kalorien", measure(metrics.energy_kcal ?? metrics.calories_kcal, "kcal")],
      // Local session metrics (card M8): descriptive values, no verdict.
      ["Arbeit", measure(metrics.work_kj ?? metrics.energy_kj, "kJ")],
      ["Effizienzfaktor", efficiencyText(metrics)],
      ["Variabilitätsindex", finite(metrics.variability_index) ? numberText(metrics.variability_index, 2) : null],
      ["Decoupling", finite(metrics.decoupling_pct) ? AppFormat.percent(metrics.decoupling_pct) : null],
      ["Belastung", measure(metrics.load)],
      // Intensity arrives as a fraction or in percent; the calendar helper normalizes both.
      ["Intensität", calendarIntensityLabel(metrics.intensity)],
      ...(dynamicsShown ? [] : ActivityDetails.runningDynamics(metrics)),
    ].filter(([, value]) => value);
    if (!values.length) return null;
    return h("dl", "activity-analysis-stats", values.map(([label, value]) => h("div", null, h("dt", null, label), h("dd", null, value))));
  }

  function zones(title, zone) {
    const items = (zone?.buckets || []).filter((bucket) => finite(bucket.seconds) && bucket.seconds >= 0)
      .map((bucket) => ({ id: String(bucket.label || `Z${bucket.index}`), secs: bucket.seconds }));
    const total = items.reduce((sum, item) => sum + item.secs, 0);
    if (!total) return null;
    const rows = h("ol");
    for (const item of [...items].reverse()) {
      const share = item.secs / total;
      const bar = h("span", "activity-analysis-zone-bar");
      bar.style.setProperty("--zone-share", `${Math.round(share * 1000) / 10}%`);
      rows.append(h("li", { dataset: { zone: item.id.match(/^Z\d/)?.[0] || "" } }, h("span", "activity-analysis-zone-label", item.id.slice(0, 20)), bar, h("span", "activity-analysis-zone-value", `${clock(item.secs)} · ${Math.round(share * 100)} %`)));
    }
    // Derived zones use the zone settings of the session day, as the reports (#1743).
    return h("section", "activity-analysis-zones", h("h4", null, title), rows, h("p", "muted", `Zonen laut ${zone.source?.label || "Anbieter"} am Tag der Einheit.`));
  }

  function laps(activity, lapSet) {
    const items = lapSet?.laps || [];
    if (!items.length) return null;
    const speed = speedChannel(activity);
    const seconds = (lap) => lap.moving_duration_s ?? lap.end_offset_s - lap.start_offset_s;
    const columns = [
      ["Zeit", (lap) => (finite(seconds(lap)) ? clock(seconds(lap)) : null)],
      ["Distanz", (lap) => AppFormat.distance(lap.distance_m)],
      [speed.invert ? "Pace" : "km/h", (lap) => speedText(speed, lap.avg_speed_mps)],
      // Garmin's own grade-adjusted lap speed, shown like the pace beside it.
      ["GAP Garmin", (lap) => speedText(speed, lap.avg_grade_adjusted_speed_mps)],
      ["Ø W", (lap) => (finite(lap.avg_power) ? numberText(lap.avg_power) : null)],
      ["Ø HF", (lap) => (finite(lap.avg_hr) ? numberText(lap.avg_hr) : null)],
      ["Anstieg", (lap) => (finite(lap.elevation_gain_m) ? `${numberText(lap.elevation_gain_m)} m` : null)],
    ].filter(([, read]) => items.some((lap) => read(lap) != null));
    if (!columns.length) return null;
    const wrap = makeScrollRegionFocusable(h("div", "analysis-chart-table"), "Runden dieser Einheit");
    const rows = items.map((lap, index) => [h("th", { scope: "row" }, String(index + 1)), ...columns.map(([, read]) => read(lap) ?? "—")]);
    wrap.append(table(["#", ...columns.map(([label]) => label)], rows));
    return h("section", "activity-analysis-laps", h("h4", null, "Runden"), wrap);
  }

  // Planned steps against the session (#2830): score, step strip, one row per step and zone times.
  const STEP_ROLES = { warmup: "Einlaufen", recovery: "Pause", cooldown: "Auslaufen" };
  const ZONE_TITLES = { speed: "Zeit je Pace-Zone", heartrate: "Zeit je Herzfrequenzzone", watts: "Zeit je Leistungszone" };
  const TARGET_CHANNELS = { speed: "speed_mps", heartrate: "heart_rate_bpm", watts: "power_w" };

  function boundsText([low, high], unit, format = numberText) {
    if (low == null && high == null) return null;
    if (high == null) return `ab ${format(low)} ${unit}`;
    if (low == null || low === 0) return `bis ${format(high)} ${unit}`;
    return `${format(low)}–${format(high)} ${unit}`;
  }

  function stepTarget(step) {
    const pace = step.target_pace_s_per_km;
    if (pace) {
      const [fastest, slowest] = pace;
      if (fastest == null) return slowest == null ? null : `schneller als ${clock(slowest)}/km`;
      return slowest == null ? `langsamer als ${clock(fastest)}/km` : `${clock(fastest)}–${clock(slowest)}/km`;
    }
    if (step.target_bpm) return boundsText(step.target_bpm, "bpm");
    if (step.target_watts) return boundsText(step.target_watts, "W");
    return step.target_kmh ? boundsText(step.target_kmh, "km/h", (value) => numberText(value, 1)) : null;
  }

  function stepActual(step) {
    if (!finite(step.actual)) return null;
    if (step.target_pace_s_per_km) return `Ø ${clock(step.actual)}`;
    if (step.target_kmh) return `Ø ${numberText(step.actual, 1)} km/h`;
    return `Ø ${numberText(step.actual)} ${step.target_watts ? "W" : "bpm"}`;
  }

  const stepSeconds = (step) => (finite(step.end_s) && finite(step.start_s) ? step.end_s - step.start_s : step.planned_duration_s);

  function adherenceZones(zones) {
    const rows = (zones?.zones || []).filter((zone) => zone.planned_s > 0 || zone.actual_s > 0);
    if (zones?.status !== "ok" || !rows.length) return null;
    const most = Math.max(...rows.flatMap((zone) => [zone.planned_s, zone.actual_s]));
    const width = (seconds) => `${Math.round(seconds / most * 1000) / 10}%`;
    const minutes = (seconds) => `${Math.round(seconds / 60)}′`;
    const list = h("ol");
    for (const zone of rows) {
      const plan = h("span", "activity-adherence-plan"); plan.style.width = width(zone.planned_s);
      if (!zone.planned_s) plan.style.visibility = "hidden";
      const actual = h("span", "activity-adherence-actual"); actual.style.width = width(zone.actual_s);
      list.append(h("li", { dataset: { zone: `Z${zone.zone}` } }, h("span", "activity-analysis-zone-label", `Z${zone.zone}`), h("span", "activity-adherence-pair", plan, actual),
        h("span", { className: "activity-analysis-zone-value", "aria-label": `Soll ${minutes(zone.planned_s)}, Ist ${minutes(zone.actual_s)}` }, `${minutes(zone.planned_s)} / ${minutes(zone.actual_s)}`)));
    }
    return h("section", "activity-analysis-zones activity-adherence-zones", h("h4", null, ZONE_TITLES[zones.metric] || "Zeit je Zone"),
      h("p", "activity-adherence-legend", h("span", "is-plan", "Soll"), h("span", "is-actual", "Ist")), list);
  }

  function adherence(result) {
    if (result?.status !== "ok" || !result.steps?.length) return null;
    const header = h("div", "activity-adherence-header", h("h4", null, "Soll/Ist"));
    if (finite(result.score_pct)) header.append(h("span", { className: "activity-adherence-score", dataset: { band: result.score_band } }, `Zieltreue ${AppFormat.percent(result.score_pct, { digits: 0 })}`));
    const strip = h("div", { className: "activity-adherence-strip", "aria-hidden": "true" });
    const list = h("ol", "activity-adherence-steps");
    for (const step of result.steps) {
      const seconds = stepSeconds(step);
      const segment = h("i", { dataset: { band: step.band || "none" } });
      segment.style.flexGrow = String(finite(seconds) ? Math.max(seconds, 1) : 1);
      strip.append(segment);
      const text = [STEP_ROLES[step.role], stepTarget(step), finite(seconds) ? clock(seconds) : null, stepActual(step)].filter(Boolean).join(" · ");
      const share = finite(step.time_in_target_pct) ? AppFormat.percent(step.time_in_target_pct, { digits: 0 }) : "–";
      list.append(h("li", { dataset: { band: step.band || "none" } }, h("span", null, String(step.step)), h("span", null, text), h("span", "activity-adherence-share", share)));
    }
    return h("section", "activity-adherence", header, strip, list, adherenceZones(result.zones));
  }

  // The steps whose target is measured on this chart's channel and that have a window.
  function chartTargets(result, channel) {
    if (result?.status !== "ok") return [];
    return (result.steps || []).filter((step) => TARGET_CHANNELS[step.metric] === channel.key && finite(step.start_s) && finite(step.end_s) && finite(step.low));
  }

  function renderPanel(panel, summary, payload, token) {
    panel.replaceChildren();
    // The document's session wins; the calendar entry stands in until it loads.
    const session = payload?.session || summary;
    const activity = { sport: session.sport, environment: session.environment };
    const metrics = payload ? ActivityDetails.metricValues(payload) : rowMetrics(summary);
    if (payload?.metrics?.efficiency_factor?.unit) metrics.efficiency_factor_unit = payload.metrics.efficiency_factor.unit;
    const dynamics = runningDynamicsSection(activity, metrics, payload && { panel, token, activityId: payload.session?.activity_id });
    const planned = payload?.planned_adherence;
    panel.append(overview(activity, metrics, Boolean(dynamics), payload?.weather));
    ActivityDetails.renderRoute(payload?.route, api, panel, session);
    panel.append(...[adherence(planned), dynamics, providerEffect(payload?.training_effect)].filter(Boolean));
    const driftLines = ActivityDetails.formDriftLines(payload?.analysis?.form_drift);
    if (driftLines.length) panel.append(h("div", null, driftLines.map((line) => h("p", null, line))));
    // The running dynamics share the time axis of the other channels.
    const streams = payload?.streams && { ...payload.streams, channels: { ...payload.streams.channels, ...payload.running_dynamics_streams } };
    const charts = streams ? channels(activity, metrics, streams).map((channel) => chart(streams, channel, chartTargets(planned, channel))).filter(Boolean) : [];
    if (charts.length) panel.append(h("div", "activity-analysis-charts", charts));
    for (const section of [zones("Zeit in Herzfrequenzzonen", payload?.zones?.heart_rate), zones("Zeit in Leistungszonen", payload?.zones?.power), laps(activity, payload?.laps)]) {
      if (section) panel.append(section);
    }
    if (!payload) return;
    if (streams && !charts.length) panel.append(h("p", "muted", "Keine darstellbaren Messreihen"));
    panel.append(...ActivityDetails.providerNotes(payload));
    appendRefresh(panel, summary, payload, token);
  }

  // The provider's own session values (#2831): shown as reported and labelled with their source, never derived.
  const EFFECT_BENEFITS = {
    RECOVERY: "Erholung", AEROBIC_BASE: "Basis", LOW_AEROBIC: "Aerob niedrig", TEMPO: "Tempo", THRESHOLD: "Schwelle",
    LACTATE_THRESHOLD: "Laktatschwelle", VO2MAX: "VO2max", VO2_MAX: "VO2max", HIGH_AEROBIC: "Aerob hoch",
    ANAEROBIC_CAPACITY: "Anaerobe Kapazität", SPEED: "Sprint", ANAEROBIC: "Anaerob", NO_BENEFIT: "Kein Nutzen",
  };
  // Training effect runs from 0 to 5 and always shows one decimal, as Garmin shows it.
  const EFFECT_SCALE = 5;
  const effectText = (value) => AppFormat.number(value, { digits: 1, minDigits: 1 });
  // An unknown category reads as words, not as an upper-case code.
  const effectBenefit = (code) => EFFECT_BENEFITS[code] || String(code).toLowerCase().replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());

  // `effect` (0 to 5) adds a bar on the training-effect scale.
  function effectTile(label, text, effect = null) {
    const tile = h("div", null, h("dt", null, label), h("dd", null, text));
    if (finite(effect)) {
      const bar = h("span", { className: "activity-effect-bar", "aria-hidden": "true" });
      bar.style.setProperty("--effect-share", `${Math.round(Math.min(1, Math.max(0, effect / EFFECT_SCALE)) * 100)}%`);
      tile.append(bar);
    }
    return tile;
  }

  function providerEffect(effect) {
    if (!effect) return null;
    const tiles = [
      finite(effect.aerobic_training_effect) && effectTile("Trainingseffekt aerob", effectText(effect.aerobic_training_effect), effect.aerobic_training_effect),
      finite(effect.anaerobic_training_effect) && effectTile("Trainingseffekt anaerob", effectText(effect.anaerobic_training_effect), effect.anaerobic_training_effect),
      effect.training_effect_label && effectTile("Hauptnutzen", effectBenefit(effect.training_effect_label)),
      finite(effect.training_effect_load) && effectTile("Belastung", numberText(effect.training_effect_load)),
    ].filter(Boolean);
    if (!tiles.length) return null;
    const source = String(effect.source || "");
    // Titled after the provider, whatever the casing of its label ("garmin" without a label).
    const provider = source.split(" ")[0];
    const title = provider ? `${provider[0].toUpperCase()}${provider.slice(1)}-Werte` : "Werte des Anbieters";
    return h("section", "activity-effect", h("div", "activity-effect-header", h("h4", null, title), source && h("span", "activity-effect-source", source)),
      h("dl", "activity-analysis-stats", tiles));
  }

  // Running dynamics against the athlete's own runs at the same pace (#2829): each value with its
  // verdict and Garmin's colour band, then a table per pace band of the run.
  const DYNAMICS_VERDICTS = { better: "besser als sonst", usual: "üblich", worse: "schlechter als sonst", higher: "länger als sonst", lower: "kürzer als sonst", too_few: "zu wenig Vergleich" };
  const DYNAMICS_SHORT = { better: "besser", usual: "üblich", worse: "schlechter", higher: "länger", lower: "kürzer" };
  const REFERENCE_COLOURS = { purple: "lila", blue: "blau", green: "grün", orange: "orange", red: "rot" };
  const DYNAMICS_TABLE = [["ground_contact_time_ms", "Kontakt", 0], ["cadence_spm", "Frequenz", 0], ["vertical_oscillation_cm", "Vertikal", 1]];
  const DYNAMICS_RETRY_MS = 10_000;
  const DYNAMICS_RETRIES = 6;

  function dynamicsValues(activity, metrics) {
    const cadence = cadenceChannel(activity, metrics);
    const values = ActivityDetails.RUNNING_DYNAMICS.filter(([key]) => finite(metrics[key])).map(([key, label, unit, digits]) => [key, label, `${numberText(metrics[key], digits)} ${unit}`]);
    if (finite(cadence.average)) values.splice(1, 0, ["cadence_spm", cadence.label, `${numberText(cadence.average)} ${cadence.unit}`]);
    return values;
  }

  // `load` (the loaded document's panel, token and session id) starts the rating read.
  function runningDynamicsSection(activity, metrics, load) {
    if (sportKind(activity) !== "run") return null;
    const values = dynamicsValues(activity, metrics);
    if (!values.some(([key]) => key !== "cadence_spm")) return null;
    const tiles = h("dl", "activity-analysis-stats activity-dynamics-values");
    for (const [key, label, text] of values) tiles.append(h("div", { className: "activity-dynamics-value", dataset: { metric: key } }, h("dt", null, label), h("dd", null, text), h("span", "activity-dynamics-rating")));
    const section = h("section", "activity-dynamics", h("h4", null, "Laufdynamik"), tiles);
    if (load?.activityId) void loadDynamicsRating(load.panel, section, String(load.activityId), load.token);
    return section;
  }

  function dynamicsChip(verdict, short = false) {
    const text = (short ? DYNAMICS_SHORT : DYNAMICS_VERDICTS)[verdict];
    return text ? h("span", { className: "activity-dynamics-chip", dataset: { verdict } }, text) : null;
  }

  function dynamicsBands(bands) {
    const rows = bands.map((band) => [
      h("th", { scope: "row" }, `${clock(band.band_s_per_km)}–${clock(band.band_s_per_km + 15)}`),
      `${Math.round(band.seconds / 60)}′`,
      ...DYNAMICS_TABLE.map(([key, , digits]) => {
        const item = band.metrics?.[key];
        return item ? h("span", null, `${numberText(item.value, digits)} `, dynamicsChip(item.verdict, true)) : "—";
      }),
    ]);
    const wrap = makeScrollRegionFocusable(h("div", "analysis-chart-table"), "Laufdynamik je Tempo-Bereich");
    wrap.append(table(["Tempo", "Zeit", ...DYNAMICS_TABLE.map(([, label]) => label)], rows));
    return h("section", "activity-dynamics-bands", h("h4", null, "Je Tempo-Bereich"), wrap);
  }

  function renderDynamicsRating(section, rating) {
    for (const tile of section.querySelectorAll(".activity-dynamics-value")) {
      const item = rating.metrics?.[tile.dataset.metric];
      const slot = tile.querySelector(".activity-dynamics-rating");
      slot.replaceChildren(...[
        item && dynamicsChip(item.verdict),
        item?.reference_band && h("span", { className: "activity-dynamics-reference", dataset: { band: item.reference_band }, title: `${rating.garmin_source}: ${REFERENCE_COLOURS[item.reference_band]}` }, "Garmin-Einordnung"),
      ].filter(Boolean));
    }
    section.querySelector(".activity-dynamics-bands")?.remove();
    if (rating.bands?.length) section.append(dynamicsBands(rating.bands));
  }

  // The rating reads every stored run in the server's background build; a pending read asks again.
  async function loadDynamicsRating(panel, section, activityId, token, attempt = 0) {
    const current = () => panelLoads.get(panel) === token && section.isConnected;
    let result;
    try {
      result = await api(`/api/analysis/running-dynamics?activity_id=${encodeURIComponent(activityId)}`, { timeoutMs: DATA_LOAD_TIMEOUT_MS });
    } catch {
      return;
    }
    if (!current()) return;
    // Pending, or an older build that does not hold the run yet: ask again.
    const waiting = result?.status === "pending" || (result?.status === "ok" && (result.stale || !result.session));
    if (waiting && attempt < DYNAMICS_RETRIES) {
      setTimeout(() => { if (current()) void loadDynamicsRating(panel, section, activityId, token, attempt + 1); }, DYNAMICS_RETRY_MS);
      return;
    }
    if (result?.status === "ok" && result.session?.status === "ok") renderDynamicsRating(section, result.session);
  }

  // The newest activities are preloaded after each provider refresh; older ones load through these actions.
  function appendRefresh(panel, summary, payload, token) {
    const status = h("p", "muted", ""); status.setAttribute("role", "status");
    const isCurrent = () => panelLoads.get(panel) === token && panel.isConnected;
    panel.append(ActivityDetails.refreshActions(payload, {
      api, isCurrent, onStatus: (text) => { status.textContent = text; }, onLoaded: () => loadPanel(panel, summary, undefined, true),
    }), status);
  }

  // `read(fresh)` is the calendar card's shared read of the same document; a retry or refresh reads it fresh.
  async function loadPanel(panel, summary, read = panelReads.get(panel), fresh = false) {
    if (read) panelReads.set(panel, read);
    const token = {};
    panelLoads.set(panel, token);
    const current = () => panelLoads.get(panel) === token && panel.isConnected;
    panel.replaceChildren(createLoadingState("Details werden geladen …"));
    let payload;
    try {
      payload = await (read ? read(fresh) : api(`/api/activities/${encodeURIComponent(String(summary.activity_id || summary.id))}`, { timeoutMs: DATA_LOAD_TIMEOUT_MS }));
    } catch (error) {
      if (!current()) return;
      // The card's summary values still render when the detail document cannot be read.
      renderPanel(panel, summary, null, token);
      panel.prepend(createErrorState(error.message, () => void loadPanel(panel, summary, read, true)));
      return;
    }
    if (current()) renderPanel(panel, summary, payload, token);
  }

  globalThis.ActivityAnalysis = { loadPanel };
})();
