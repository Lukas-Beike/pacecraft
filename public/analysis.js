
const KIND_LABELS = { bike: "Fahrrad", component: "Komponente", shoes: "Schuhe", other: "Sonstiges" };
const SPORT_LABELS = { Ride: "Rad", VirtualRide: "Indoor-Rad", WeightTraining: "Kraft", Run: "Laufen", Swim: "Schwimmen", Walk: "Gehen", ride: "Rad", run: "Laufen", swim: "Schwimmen", walk: "Gehen", hike: "Wandern", strength: "Kraft", row: "Rudern", nordic_ski: "Langlauf", alpine_ski: "Alpinski", yoga: "Yoga", other: "Andere" };
const equipmentSportLabel = (item) => (item.sport === "ride" && item.environment === "virtual" ? "Indoor-Rad" : SPORT_LABELS[item.sport] || item.sport);
const isRideEquipment = (item) => ["Ride", "VirtualRide", "ride"].includes(item.sport);
const isRunEquipment = (item) => ["Run", "run"].includes(item.sport);

function analysisValue(value, unit) {
  if (value == null || value === "" || !Number.isFinite(Number(value))) return "unbekannt";
  if (unit === "s/km") return formatPace(value);
  if (unit === "s") return formatDuration(Math.round(Number(value)));
  if (unit === "h") {
    const minutes = Math.round(Math.abs(Number(value)) * 60);
    return `${Number(value) < 0 && minutes > 0 ? "−" : ""}${AppFormat.duration(minutes * 60)}`;
  }
  if (unit === "Einheiten") return AppFormat.plural(value, "Einheit", "Einheiten");
  const suffix = unit ? ` ${unit}` : "";
  return `${analysisNumber(value, unit)}${suffix}`;
}

function analysisRatioUnit(unit) {
  return String(unit || "").endsWith("/bpm");
}

function analysisMinimumPadding(unit, high) {
  if (unit === "s/km") return 5;
  return analysisRatioUnit(unit) ? Math.abs(high) * .02 : 1;
}

function analysisCountUnit(unit) {
  return unit === "Schritte/Tag" || unit === "Stockwerke/Tag";
}

// Ratio units such as W/bpm keep three significant digits so small efficiencies are not rounded to zero.
function analysisNumber(value, unit) {
  const numeric = Number(value);
  if (analysisCountUnit(unit)) return AppFormat.number(Math.round(numeric), { digits: 0 });
  // Mass, speed and percentages always show one decimal; VO₂max keeps the source's own (#2263, #3933).
  if (["kg", "km/h", "%"].includes(unit)) return AppFormat.number(numeric, { digits: 1, minDigits: 1 });
  if (!analysisRatioUnit(unit) || !numeric) return AppFormat.number(numeric);
  return AppFormat.number(numeric, { digits: Math.max(1, 2 - Math.floor(Math.log10(Math.abs(numeric)))) });
}

function analysisCountLabel(count, singular, plural) {
  return AppFormat.plural(count, singular, plural);
}

function analysisPercent(value) {
  return AppFormat.percent(value);
}

function analysisLegendText(item, latest, unit) {
  if (!latest) return `${item.label}: keine Werte`;
  return `${item.label}: ${analysisPointValue(item, latest, unit)} · ${dateLabel(latest.observedDate || latest.date)}`;
}

function analysisPointValue(item, point, unit) {
  if (point.actual == null) return analysisValue(point.value, item.unit || unit);
  return `${analysisValue(point.actual, item.unit)} (${analysisValue(point.value, "%")})`;
}

function analysisLatestPoint(item) {
  return item.currentPoint || item.points.findLast(analysisValidPoint);
}

function analysisCoverageText(count, total, unitWord) {
  return `Daten für ${count} von ${total} ${unitWord} vorhanden.`;
}

let analysisInfoId = 0;

// The quick-action menu of the Coach still opens as a positioned popover, kept inside the viewport.
function analysisPopoverPosition(anchor, popover, viewport) {
  const margin = 12, gap = 8;
  const width = Math.min(popover.width, viewport.width - margin * 2);
  const height = Math.min(popover.height, viewport.height - margin * 2);
  const left = Math.max(margin, Math.min(anchor.left, viewport.width - width - margin));
  const below = anchor.bottom + gap;
  const above = anchor.top - gap - height;
  const fitsBelow = below + height <= viewport.height - margin;
  const top = fitsBelow || above < margin ? Math.max(margin, Math.min(below, viewport.height - height - margin)) : above;
  return { left, top };
}

// An explanation opens in place under its button and pushes the rest down, so it covers nothing and needs no
// positioning (athlete decision, #3929 A-08). Escape closes it again.
function bindAnalysisPopover(button, info) {
  info.removeAttribute("popover");
  info.hidden = true;
  info.setAttribute("role", "note");
  button.removeAttribute("popovertarget");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", info.id);
  const set = (open) => {
    info.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
  };
  button.addEventListener("click", () => set(info.hidden));
  info.addEventListener("keydown", (event) => { if (event.key === "Escape") { set(false); button.focus(); } });
  button.addEventListener("keydown", (event) => { if (event.key === "Escape" && !info.hidden) set(false); });
}

function analysisChart(title, series, unit, start, end, note, {
  sparse = false, zeroCentered = false, showLegend = true, calendarWeeks = false, caption = analysisAxisCaption(unit), domain = null,
} = {}) {
  if (!analysisHasData(series)) return null;
  const heading = h("h3", null, title);
  const section = h("section", "analysis-chart-card", heading);
  const valid = analysisValidPoint;

  // A series without a drawn line (a single reading, or none) names no legend entry and sets no axis; the table still lists it.
  const shown = series.some(analysisPlottable) ? series.filter(analysisPlottable) : series;
  // One denominator for every series of a cadence: the days or the week starts of the chart's own period.
  const periodCount = { 1: analysisTickDates(start, end, false).length, 7: analysisTickDates(start, end, true).length };
  const legend = h("ul", "analysis-chart-legend");
  series.forEach((item, index) => {
    if (!shown.includes(item)) return;
    const latest = analysisLatestPoint(item);
    const label = item.legendLabel || item.label;
    const sourceInfo = item.source ? " · Quelle: " + item.source : "";
    const info = h("div", { className: "analysis-info-tooltip", id: `analysis-info-${++analysisInfoId}`, dataset: { series: String(index) } }, `${analysisLegendText(item, latest, unit)}${sourceInfo}. ${item.explanation || note}`);
    const button = h("button", { className: "analysis-legend-info", type: "button", "aria-label": `Erklärung zu ${label}`, "aria-describedby": info.id }, label);
    bindAnalysisPopover(button, info);
    const entry = h("li", { dataset: { color: String(item.color ?? index), line: analysisLineStyle(item, index) } }, button, info);
    // Value, date and source stand once, in the first line of the explanation (#3929 A-08).
    if (item.referenceLabel) info.append(h("span", "analysis-metric-context", item.referenceLabel));
    const readings = item.points.filter(valid);
    info.append(h("span", "analysis-metric-meta", item.coverageShort || analysisCoverageText(readings.length, item.cadenceDays === 7 ? periodCount[7] : item.points.length, item.cadenceDays === 7 ? "Wochen" : "Tagen")));
    const changeText = analysisChangeText(item, latest, readings, item.unit || unit);
    if (changeText) info.append(h("span", "analysis-metric-change", changeText));
    legend.append(entry);
  });
  if (showLegend && analysisLegendRepeatsTitle(shown, title)) analysisLegendInfoToHeading(heading, legend, title);
  else if (showLegend) section.append(legend);

  if (!series.some(analysisPlottable)) {
    section.append(h("p", "empty", "Ein einzelner Messwert zeigt noch keinen Verlauf; mindestens zwei datierte Werte sind erforderlich."));
    appendAnalysisTable(section, title, series, unit, caption);
    return section;
  }
  const fewReadings = sparse && series.every((item) => item.points.filter(valid).length < 3);
  if (fewReadings) section.querySelectorAll(".analysis-info-tooltip").forEach((info) => info.append(h("p", "analysis-sparse-note", "Wenige Messungen · kein belastbarer Trend.")));
  const weekTicks = calendarWeeks && series.length > 0 && series.every((item) => item.cadenceDays === 7);
  // A week's mark sits on its week start; half a week of room keeps the first mark off the value axis.
  const scales = analysisPlotScales(shown, weekTicks ? addDateKey(start, -3) : start, end, { unit, zeroCentered, domain });
  const svg = svgNode("svg", { viewBox: `0 0 ${scales.chartWidth} 200`, role: "group", "aria-label": `${title}: datierter Verlauf. Tageswerte auswählen oder Werte ansehen öffnen.` });
  svg.append(svgNode("title", {}, `${title} · ${dateLabel(start)} bis ${dateLabel(end)}`));
  svg.append(svgNode("desc", {}, `Eigene Skala: ${caption}. Fehlende Messungen bleiben unbekannt.`));
  appendAnalysisAxes(svg, unit, scales, caption);
  appendAnalysisSeries(svg, series, unit, scales, zeroCentered, caption, weekTicks);
  appendAnalysisDateTicks(svg, start, end, scales, weekTicks ? analysisWeekTickLabel : analysisDayTickLabel);
  appendAnalysisPointInspectors(section, svg, series, unit, scales);
  section.append(svg);
  appendAnalysisDataStart(section, shown, start, end);
  appendAnalysisReferenceNotes(section, series, unit);
  appendAnalysisTable(section, title, series, unit, caption);
  return section;
}

// Data that begins in the last part of the period says so, instead of leaving the first part of the axis unexplained.
function appendAnalysisDataStart(section, series, start, end) {
  const first = series.flatMap((item) => item.points.filter(analysisValidPoint).map((point) => point.date)).sort()[0];
  if (!first || (Date.parse(first) - Date.parse(start)) / Math.max(86400000, Date.parse(end) - Date.parse(start)) <= 0.3) return;
  section.append(h("p", "muted", `Daten ab ${dateLabel(first)}.`));
}

// A single series named like the chart only repeats the heading in its legend; its info button joins the heading instead.
function analysisLegendRepeatsTitle(series, title) {
  return series.length === 1 && (series[0].legendLabel || series[0].label) === title;
}

function analysisLegendInfoToHeading(heading, legend, title) {
  const button = legend.querySelector(".analysis-legend-info");
  button.textContent = "i";
  button.className = "analysis-legend-info analysis-info-icon";
  button.setAttribute("aria-label", `${title}: Informationen`);
  heading.append(button, legend.querySelector(".analysis-info-tooltip"));
}

// A bar series is solid; a line is dashed when the series says so or by its position (see appendAnalysisSeries).
function analysisLineStyle(item, index) {
  if (item.bars) return "solid";
  return item.line || (index ? "dashed" : "solid");
}

// A body series (``edgeMeans`` null or set) names its change by the edge means only, and none while an edge
// is empty: never last minus first, the second definition (#4037). Every other series keeps "Seit".
function analysisChangeText(item, latest, readings, unit) {
  if (item.edgeMeans !== undefined) return item.edgeMeans ? analysisEdgeChangeText(item.edgeMeans, unit) : null;
  if (readings.length < 2) return null;
  const first = readings[0];
  return `Seit ${dateLabel(first.observedDate || first.date)}: ${analysisChange(Number(latest.value) - Number(first.value), unit)}`;
}

// Weight and body fat: the mean of the last days minus the mean of the first, the one definition of the
// Körper charts, the Verlauf card and the Coach (#4037).
function analysisEdgeChangeText(edges, unit) {
  const span = edges.span_days === 7 ? "Wochenmittel" : `Mittel von ${edges.span_days / 7} Wochen`;
  return `Änderung (${span}): ${analysisChange(edges.change, unit)} · Ø ${analysisValue(edges.first_mean, unit)} → Ø ${analysisValue(edges.last_mean, unit)}`;
}

// A change that rounds to zero carries no sign.
function analysisChange(delta, unit) {
  const text = unit === "s/km" ? formatPace(Math.abs(delta)) : analysisValue(delta, unit);
  if (/^[−+]?0([,:]0+)?(\s|$)/.test(text)) return text;
  // A pace is the absolute time and needs its own minus; a value carries it already.
  let sign = delta < 0 ? "" : "+";
  if (unit === "s/km") sign = delta < 0 ? "−" : "+";
  return `${sign}${text}`;
}

function appendAnalysisReferenceNotes(section, series, unit) {
  series.forEach((item, index) => {
    const info = section.querySelector(`.analysis-legend-info + .analysis-info-tooltip[data-series="${index}"]`);
    if (item.points.some((point) => point.lower != null)) info?.append(h("p", "analysis-reference-note", "Wochenmedian mit Streuung (25.–75. Perzentil) · nur vorhandene Messungen"));
    if (item.range) info?.append(h("p", "analysis-reference-note", "Mittlere Hälfte deiner früheren Werte (Quartile) · 42 Tage vor der letzten Messung" + (item.range.status === "provisional" ? " · vorläufig" : "")));
    if (item.target != null) info?.append(h("p", "analysis-reference-note", `Ziellinie: ${analysisValue(item.target, item.unit || unit)} · persönliches Schlafziel`));
    if (item.average && item.bars) {
      info?.append(h("p", "analysis-reference-note", "Gestrichelte Linie: Durchschnitt der angezeigten Werte."));
    }
  });
}

function analysisReadingDetails(point, unit) {
  let details = point.count ? ` · ${AppFormat.plural(point.count, "Messung", "Messungen")}` : "";
  if (point.weekReadings > 1) details += ` · ${point.weekReadings} Messungen in der Woche`;
  if (point.observedDate && point.observedDate !== point.date) details += ` · letzter Messwert ${dateLabel(point.observedDate)}`;
  if (point.ftp_observed_at) details += ` · ${point.power_method || "FTP"} ${dateLabel(point.ftp_observed_at)}`;
  if (point.weight_observed_at) details += ` · Gewicht ${dateLabel(point.weight_observed_at)} (${point.weight_source || "Quelle unbekannt"})`;
  if (point.lower != null) details += ` · Streuung ${analysisValue(point.lower, unit)} bis ${analysisValue(point.upper, unit)}`;
  if (point.readout) details += ` · ${point.readout}`;
  return details;
}

function analysisValidPoint(point) {
  return point?.value != null && Number.isFinite(Number(point.value));
}

// A series may lower the default two-point minimum with minPoints.
function analysisPlottable(item) {
  return item.bars || item.points.filter(analysisValidPoint).length >= (item.minPoints ?? 2);
}

function analysisHasData(series) {
  return series.some((item) => item.points.some(analysisValidPoint));
}

// A chart without any valid point is omitted without a note (#1783); its title is still collected.
function collectAnalysisChart(missing, title, ...chartArguments) {
  const chart = analysisChart(title, ...chartArguments);
  if (!chart) missing.push(title);
  return chart;
}

// One value per week or day, newest first: the last rows show, the rest opens on request (#3928).
const ANALYSIS_TABLE_ROWS = 6;

// The week's own date and the count have their columns; a later reading date is only worth naming on a daily row.
function analysisTableValue(item, point, unit, { weekly, counted }) {
  if (!analysisValidPoint(point)) return "–";
  const shown = { ...point, observedDate: weekly ? point.date : point.observedDate, ...(counted ? { count: 0, weekReadings: 0 } : {}) };
  return analysisPointValue(item, point, unit) + analysisReadingDetails(shown, item.unit || unit);
}

function appendAnalysisTable(section, title, series, unit, caption = analysisAxisCaption(unit)) {
  const scroll = makeScrollRegionFocusable(h("div", "analysis-chart-table analysis-series-table"), `${title}: Einzelwerte`);
  const weekly = series.every((item) => item.cadenceDays === 7);
  const dates = [...new Set(series.flatMap((item) => item.points.filter(analysisValidPoint).map((point) => point.date)))].sort((a, b) => b.localeCompare(a));
  const single = series.length === 1;
  const known = series.flatMap((item) => item.points.filter(analysisValidPoint));
  const counted = single && known.some((point) => point.count || point.weekReadings);
  const sourced = single && Boolean(series[0].source || known.some((point) => point.source_label));
  const columns = [weekly ? "Woche ab" : "Tag", ...(single ? ["Wert"] : series.map((item) => item.label)), ...(counted ? ["Messungen"] : []), ...(sourced ? ["Quelle"] : [])];
  const rows = dates.map((date) => {
    const points = series.map((item) => item.points.find((candidate) => candidate.date === date));
    const values = series.map((item, index) => analysisTableValue(item, points[index], unit, { weekly, counted }));
    const point = points.find(analysisValidPoint);
    const count = point?.count || point?.weekReadings;
    return [h("th", { scope: "row" }, dateLabel(date)), ...values,
      ...(counted ? [count ? String(count) : "–"] : []), ...(sourced ? [point?.source_label || series[0].source || "–"] : [])];
  });
  const grid = table(columns, rows, { caption: `${title} · ${caption} · nur vorhandene Werte` });
  const hidden = [...[...grid.children].at(-1).children].slice(ANALYSIS_TABLE_ROWS);
  hidden.forEach((row) => { row.hidden = true; });
  scroll.append(grid);
  if (hidden.length) {
    const more = h("button", { type: "button", className: "secondary-button" }, `Alle ${rows.length} ${weekly ? "Wochen" : "Tage"} anzeigen`);
    more.addEventListener("click", () => {
      hidden.forEach((row) => { row.hidden = false; });
      more.remove();
      // The button had the focus: it moves to the first row that appeared, so a keyboard user keeps their place.
      const first = hidden[0].children[0];
      first.tabIndex = -1;
      first.focus();
    });
    scroll.append(more);
  }
  section.append(h("details", null, h("summary", null, "Werte ansehen"), scroll));
}

// `domain` is a scale with fixed ends, such as 0 to 100 points: { min, max, step }.
function analysisPlotScales(series, start, end, { unit = "", zeroCentered = false, domain = null } = {}) {
  const values = series.flatMap((item) => [
    ...(analysisPlottable(item) ? item.points.filter(analysisValidPoint) : []).flatMap((point) => [Number(point.value), point.lower, point.upper].filter((v) => v != null && Number.isFinite(Number(v))).map(Number)),
    ...(item.range ? [item.range.lower, item.range.upper] : []),
    ...(item.target != null ? [item.target] : []),
  ]);
  let low = Math.min(...values), high = Math.max(...values);
  if (zeroCentered) { const extent = Math.max(Math.abs(low), Math.abs(high), 1); low = -extent; high = extent; }
  if (series.some((item) => item.bars)) low = Math.min(0, low);
  // A pulse that varies by two beats does not fill the axis: the range is at least ten beats wide.
  if (unit === "bpm" && high - low < 10) { const middle = (high + low) / 2; low = middle - 5; high = middle + 5; }
  const padding = Math.max((high - low) * .12, analysisMinimumPadding(unit, high));
  const rawStep = (high - low + padding * 2) / 4;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  let step = [1, 2, 5, 10].find((factor) => factor * magnitude >= rawStep) * magnitude;
  if (unit === "s/km") step = Math.max(5, Math.ceil(step / 5) * 5);
  if (analysisCountUnit(unit)) step = Math.max(1, step);
  if (domain) step = domain.step;
  const fitted = series.some((item) => item.bars) ? 0 : Math.floor((low - padding) / step) * step;
  const min = domain ? domain.min : fitted;
  const max = domain ? domain.max : Math.ceil((high + padding) / step) * step;
  // Wide screens place two charts per row, so the plot is drawn at the column width and keeps its text size.
  let chartWidth = 680;
  if (globalThis.innerWidth < 600) chartWidth = Math.max(220, globalThis.innerWidth - 80);
  else if (globalThis.innerWidth >= 1100) chartWidth = 460;
  const chartRight = chartWidth - 24;
  const x = (date) => 60 + ((Date.parse(date) - Date.parse(start)) / Math.max(86400000, Date.parse(end) - Date.parse(start))) * (chartRight - 60);
  const y = unit === "s/km" ? (value) => 30 + (Number(value) - min) / (max - min) * 130 : (value) => 160 - (Number(value) - min) / (max - min) * 130;
  return { min, max, step, chartWidth, chartRight, x, y };
}

function analysisAxisLabel(value, unit) {
  if (unit === "s/km") return formatPace(value).split(" ")[0];
  if (unit === "s") return formatDuration(Math.round(value));
  return analysisNumber(analysisRatioUnit(unit) ? value : Number(value.toFixed(3)), unit);
}

function analysisAxisCaption(unit) {
  if (unit === "s/km") return "min/km · schneller oben";
  return unit || "Belastungspunkte";
}

function appendAnalysisAxes(svg, unit, { min, max, step, chartRight, y }, caption = analysisAxisCaption(unit)) {
  for (let value = min; value <= max + step / 100; value += step) {
    svg.append(svgNode("line", { x1: 60, x2: chartRight, y1: y(value), y2: y(value), class: Math.abs(value) < step / 100 ? "analysis-zero-line" : "analysis-grid-line" }));
    const label = analysisAxisLabel(value, unit);
    svg.append(svgNode("text", { x: 52, y: y(value) + 4, "text-anchor": "end", class: "analysis-value-tick" }, label));
  }
  svg.append(svgNode("text", { x: 4, y: 15, class: "analysis-axis-unit" }, caption));
}

function appendAnalysisReferenceLines(svg, item, index, unit, scales, obstacles = []) {
  const { chartRight, y } = scales;
  const lineBox = (value) => ({ left: 60, right: chartRight, top: y(value) - 1.5, bottom: y(value) + 1.5 });
  const labelAttributes = { "text-anchor": "end", class: "analysis-point-value analysis-average-value", stroke: "var(--surface)", "stroke-width": 4, "paint-order": "stroke", "stroke-linejoin": "round" };
  const readings = item.average ? item.points.filter(analysisValidPoint).map((point) => Number(point.value)) : [];
  const average = readings.length ? readings.reduce((sum, value) => sum + value, 0) / readings.length : null;
  if (item.range) {
    const top = Math.min(y(item.range.lower), y(item.range.upper));
    const bottom = Math.max(y(item.range.lower), y(item.range.upper));
    svg.append(svgNode("rect", { x: 60, y: top, width: chartRight - 60, height: Math.max(1, bottom - top), class: "analysis-baseline-band" }));
    // The band is named at its right edge, above it, or below when the average's label sits there (#4025).
    const above = average == null || Math.abs(top - 4 - (y(average) - 8)) >= 14;
    const labelY = above ? top - 4 : bottom + 12;
    svg.append(svgNode("text", { x: chartRight, y: labelY, ...labelAttributes }, "Mittlere Hälfte"));
    obstacles.push({ left: chartRight - 15 * 7.5, right: chartRight, top: labelY - 15, bottom: labelY });
  }
  if (item.target != null) {
    svg.append(svgNode("line", { x1: 60, x2: chartRight, y1: y(item.target), y2: y(item.target), class: "analysis-target-line" }));
    obstacles.push(lineBox(item.target));
  }
  if (average != null) {
    const line = svgNode("line", { x1: 60, x2: chartRight, y1: y(average), y2: y(average), class: "analysis-average-line" });
    line.append(svgNode("title", {}, `${item.label}: Durchschnitt ${analysisValue(average, item.unit || unit)}`));
    svg.append(line);
    obstacles.push(lineBox(average));
    // The line carries its name, also where the heading names the average (#4025).
    const text = `\u00d8 ${analysisValue(average, item.unit || unit)}`;
    obstacles.push({ left: chartRight - text.length * 7.5, right: chartRight, top: y(average) - 20, bottom: y(average) - 5 });
    svg.append(svgNode("text", { x: chartRight, y: y(average) - 8, ...labelAttributes }, text));
  }
}

function flushAnalysisArea(svg, segment, zeroCentered, color, x, y) {
  if (zeroCentered && segment.length > 1) svg.append(svgNode("path", { d: `M${x(segment[0].date)},${y(0)} ` + segment.map((point) => `L${x(point.date)},${y(point.value)}`).join(" ") + ` L${x(segment.at(-1).date)},${y(0)} Z`, class: "analysis-form-area", "data-color": color }));
}

function analysisBarGeometry(item, point, { chartRight, x }) {
  const width = Math.min(24, (chartRight - 60) / Math.max(1, item.points.length) * .55);
  const left = item.connected ? x(point.date) - width / 2 : Math.max(60, Math.min(chartRight - width, x(point.date) - width / 2));
  return { left, width };
}

function appendAnalysisPointMark(svg, item, point, index, unit, scales, obstacles) {
  const { x, y } = scales;
  if (point.lower != null) {
    svg.append(svgNode("line", { x1: x(point.date), x2: x(point.date), y1: y(point.lower), y2: y(point.upper), class: "analysis-range-whisker", "data-color": item.color ?? index }));
    obstacles.push({ left: x(point.date) - 1, right: x(point.date) + 1, top: Math.min(y(point.lower), y(point.upper)), bottom: Math.max(y(point.lower), y(point.upper)) });
  }
  // One mark type per series (#1782): bars for per-day or per-week sums and means, dots on a line otherwise.
  if (item.bars) {
    const { left, width } = analysisBarGeometry(item, point, scales);
    const top = Math.min(y(point.value), y(0));
    const bottom = Math.max(y(point.value), y(0));
    svg.append(svgNode("rect", { x: left, y: top, width, height: bottom - top, rx: 3, class: item.connected ? "analysis-series-bar" : "recovery-sleep-bar", "data-color": item.color ?? index }));
    obstacles.push({ left, right: left + width, top, bottom });
    return;
  }
  const dot = svgNode("circle", { cx: x(point.date), cy: y(point.value), r: 2.5, "data-series": index, "data-color": item.color ?? index });
  dot.append(svgNode("title", {}, `${item.label} · ${dateLabel(point.date)}: ${analysisPointValue(item, point, unit)}`));
  svg.append(dot);
  obstacles.push({ left: x(point.date) - 3, right: x(point.date) + 3, top: y(point.value) - 3, bottom: y(point.value) + 3 });
}

// Only the latest, highest, lowest and first reading carry a value label (#1782); the tap readout shows the rest.
function analysisLabelledPoints(item) {
  const readings = item.points.filter(analysisValidPoint).filter((point) => !(item.hideZeroLabels && Number(point.value) === 0));
  if (!readings.length) return [];
  const byValue = [...readings].sort((a, b) => Number(a.value) - Number(b.value));
  return [...new Set([readings.at(-1), byValue.at(-1), byValue[0], readings[0]])];
}

// The drawn width of a 12px value label plus its 4px halo; 7px per character where no canvas measures,
// which overstates a thousands point and leaves a short bar between taller ones unlabelled (#3839).
let analysisLabelContext;
function analysisLabelWidth(text) {
  analysisLabelContext ??= globalThis.document?.createElement?.("canvas")?.getContext?.("2d") || null;
  if (!analysisLabelContext) return text.length * 7 + 4;
  analysisLabelContext.font = `12px ${getComputedStyle(document.body).fontFamily}`;
  return Math.ceil(analysisLabelContext.measureText(text).width) + 4;
}

function analysisHoursLabel(value) {
  const minutes = Math.round(Math.abs(Number(value)) * 60);
  return `${Number(value) < 0 && minutes > 0 ? "−" : ""}${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}`;
}

function analysisPointValueLabel(item, point, { index, unit, scales }) {
  // An hours value is labelled compactly as h:mm on the chart; its unit stands in the axis caption.
  const label = (item.unit || unit) === "h" ? analysisHoursLabel(point.value) : analysisValue(point.value, item.unit || unit).split(" ")[0];
  const { x, y } = scales;
  let pointX = x(point.date);
  if (item.bars) {
    const { left, width } = analysisBarGeometry(item, point, scales);
    pointX = left + width / 2;
  }
  return { label, x: pointX, width: analysisLabelWidth(label), index, bars: Boolean(item.bars), pointY: item.bars ? Math.min(y(point.value), y(0)) : y(point.value) };
}

// The closest free box for a label that still spans its point: above or below it, centred or slid sideways.
// A line label tries both slots on the side with more room before the next mark or line, away from another series.
function analysisLabelSlot(label, clear, chartWidth, obstacles) {
  const { x, width, pointY } = label;
  const lefts = [x - width / 2, x - 4, x - width + 4, chartWidth - width, 56].filter((left) => left <= x - 2 && left + width >= x + 2);
  const room = (side) => Math.min(200, ...obstacles.filter((box) => box.left <= x && box.right >= x)
    .map((box) => (side < 0 ? pointY - box.bottom : box.top - pointY)).filter((distance) => distance > 3));
  let offsets = [-10, -20, 18, 28];
  if (label.bars) offsets = [-10, -20];
  else if (room(1) > room(-1)) offsets = [18, 28, -10, -20];
  for (const offset of offsets) {
    const box = lefts.map((left) => ({ left, right: left + width, top: pointY + offset - 12, bottom: pointY + offset + 3 })).find((candidate) => clear(candidate));
    if (box) return box;
  }
  return null;
}

function analysisTextBox(x, y, text, charWidth) {
  return { left: x, right: x + String(text).length * charWidth, top: y - 12, bottom: y + 3 };
}

function analysisLineObstacles(points, { x, y }) {
  const boxes = [];
  for (let index = 1; index < points.length; index++) {
    const [x1, y1, x2, y2] = [x(points[index - 1].date), y(points[index - 1].value), x(points[index].date), y(points[index].value)];
    const steps = Math.max(1, Math.ceil(Math.hypot(x2 - x1, y2 - y1) / 3));
    for (let step = 0; step <= steps; step++) {
      const px = x1 + (x2 - x1) * step / steps;
      const py = y1 + (y2 - y1) * step / steps;
      boxes.push({ left: px - 1, right: px + 1, top: py - 1, bottom: py + 1 });
    }
  }
  return boxes;
}

function appendAnalysisSeriesPoints(svg, item, { index, unit, scales, zeroCentered, labels, obstacles }) {
  const { x, y } = scales;
  const color = item.color ?? index;
  let path = "", previous = null, segment = [], hasLinePoint = false;
  if (!analysisPlottable(item)) return path;
  item.points.forEach((point) => {
    if (!analysisValidPoint(point)) {
      flushAnalysisArea(svg, segment, zeroCentered, color, x, y);
      segment = [];
      return;
    }
    const continuous = previous && Date.parse(point.date) - Date.parse(previous.date) <= (item.cadenceDays || 1) * 86400000;
    if (!continuous) {
      flushAnalysisArea(svg, segment, zeroCentered, color, x, y);
      segment = [];
    }
    segment.push(point);
    path += `${hasLinePoint ? "L" : "M"}${x(point.date).toFixed(2)},${y(point.value).toFixed(2)} `;
    hasLinePoint = true;
    appendAnalysisPointMark(svg, item, point, index, unit, scales, obstacles);
    previous = point;
  });
  flushAnalysisArea(svg, segment, zeroCentered, color, x, y);
  analysisLabelledPoints(item).forEach((point, order) => labels.push({ ...analysisPointValueLabel(item, point, { index, unit, scales }), newest: order === 0 }));
  return path;
}

function analysisBoxesOverlap(box, other, gapX, gapY) {
  return box.left < other.right + gapX && box.right > other.left - gapX && box.top < other.bottom + gapY && box.bottom > other.top - gapY;
}

// Labels avoid marks, lines, the caption and each other and stay inside the plot; a label without a free slot
// next to its point is dropped. Every series' newest value is placed first (#3450).
function appendAnalysisPointLabels(svg, labels, obstacles, { chartWidth }) {
  const occupied = [];
  const accepted = [];
  const clear = (box) => box.left >= 56 && box.right <= chartWidth && box.top >= 2 && box.bottom <= 176
    && !obstacles.some((other) => analysisBoxesOverlap(box, other, 1, 1))
    && !occupied.some((other) => analysisBoxesOverlap(box, other, 6, 4));
  const seen = new Set();
  [...labels].sort((a, b) => Number(b.newest) - Number(a.newest)).forEach((label) => {
    const key = `${label.index}:${label.label}`;
    if (seen.has(key)) return;
    const box = analysisLabelSlot(label, clear, chartWidth, obstacles);
    if (!box) return;
    seen.add(key);
    occupied.push(box);
    accepted.push({ ...label, box });
  });
  accepted.sort((a, b) => a.index - b.index || a.x - b.x);
  accepted.forEach(({ box, index, label }) => svg.append(svgNode("text", { x: (box.left + box.right) / 2, y: box.bottom - 3, "text-anchor": "middle", class: "analysis-point-value", "data-value-series": index }, label)));
}

function appendAnalysisSeries(svg, series, unit, scales, zeroCentered, caption, weeklyCaption = false) {
  const labels = [];
  const obstacles = [analysisTextBox(4, 15, caption, 6.4)];
  // The week caption sits at the top right: no value label may overprint it.
  if (weeklyCaption) obstacles.push(analysisTextBox(scales.chartWidth - 8 - "Woche ab".length * 6.4, 15, "Woche ab", 6.4));
  // A dashed zero line beside a label reads as a minus sign (#3450).
  if (scales.min < 0 && scales.max > 0) obstacles.push({ left: 60, right: scales.chartRight, top: scales.y(0) - 1, bottom: scales.y(0) + 1 });
  series.forEach((item, index) => {
    const color = item.color ?? index;
    appendAnalysisReferenceLines(svg, item, index, unit, scales, obstacles);
    const path = appendAnalysisSeriesPoints(svg, item, { index, unit, scales, zeroCentered, labels, obstacles });
    const readings = item.points.filter(analysisValidPoint);
    if (!item.bars && readings.length >= 2) {
      svg.append(svgNode("path", { d: path, fill: "none", "data-series": index, "data-color": color, "data-line": analysisLineStyle(item, index) }));
      obstacles.push(...analysisLineObstacles(readings, scales));
    }
  });
  appendAnalysisPointLabels(svg, labels, obstacles, scales);
}

function analysisWeekTickLabel(dateKey) {
  return analysisDayTickLabel(dateKey);
}

function analysisDayTickLabel(dateKey) {
  return dateKey.slice(5).split("-").reverse().join(".");
}

// The label step in ticks: a round step (days of a week, a few weeks) when one fits, else the even step for the labels that fit.
function analysisTickStep(tickCount, maxLabels) {
  const fits = (step) => Math.floor((tickCount - 1) / step) + 1 <= maxLabels;
  return [1, 2, 3, 4, 7, 14, 28, 56, 91].find(fits) || Math.ceil((tickCount - 1) / (maxLabels - 1));
}

// Tick marks sit on every day (or every week start of a weekly chart); the first and the last tick are always labelled,
// the labels between repeat at an even step counted back from the latest tick and keep clear of the two ends (#4025).
const TICK_LABEL_GAP = 40;

function appendAnalysisDateTicks(svg, start, end, { chartWidth, x }, tickLabel = analysisDayTickLabel) {
  const weekly = tickLabel === analysisWeekTickLabel;
  const dates = analysisTickDates(start, end, weekly);
  const maxLabels = chartWidth < 600 ? 4 : 7;
  const step = analysisTickStep(dates.length, maxLabels);
  const last = dates.length - 1;
  const clearOfEnds = (index) => Math.abs(x(dates[index]) - x(dates[0])) >= TICK_LABEL_GAP && Math.abs(x(dates[index]) - x(dates[last])) >= TICK_LABEL_GAP;
  dates.forEach((date, index) => {
    const labelled = index === 0 || index === last || ((last - index) % step === 0 && clearOfEnds(index));
    if (!labelled && dates.length > 31) return;
    svg.append(svgNode("line", { x1: x(date), x2: x(date), y1: 160, y2: labelled ? 168 : 164, class: "analysis-tick-mark" }));
    // A label at the right end stays inside the drawing instead of being clipped.
    const clipped = x(date) + 18 > chartWidth;
    if (labelled) svg.append(svgNode("text", { x: clipped ? chartWidth - 2 : x(date), y: 188, "text-anchor": clipped ? "end" : "middle", class: "analysis-date-tick" }, tickLabel(date)));
  });
  // At the top right, clear of the first tick label.
  if (weekly) svg.append(svgNode("text", { x: chartWidth - 8, y: 15, "text-anchor": "end", class: "analysis-week-caption" }, "Woche ab"));
}

function analysisTickDates(start, end, weekly) {
  const dates = [];
  for (let date = start; date <= end && dates.length < 400; date = addDateKey(date, weekly ? 7 : 1)) dates.push(date);
  return dates.length ? dates : [start];
}

function analysisPointTooltip(section, marker, title, lines) {
  const tooltip = h("div", { className: "analysis-info-tooltip", id: `point-info-${++analysisInfoId}` }, h("strong", null, title), lines.map((line) => h("p", null, line)));
  tooltip.setAttribute("popover", "auto");
  tooltip.setAttribute("role", "tooltip");
  marker.setAttribute("role", "button");
  marker.setAttribute("tabindex", "0");
  marker.setAttribute("aria-label", title);
  marker.setAttribute("aria-describedby", tooltip.id);
  marker.setAttribute("aria-expanded", "false");
  marker.addEventListener("click", () => tooltip.togglePopover());
  marker.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault(); tooltip.togglePopover();
  });
  tooltip.addEventListener("toggle", (event) => {
    marker.setAttribute("aria-expanded", String(event.newState === "open"));
    if (event.newState !== "open") return;
    const rect = marker.getBoundingClientRect();
    tooltip.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - tooltip.offsetWidth - 12))}px`;
    tooltip.style.top = `${Math.max(12, Math.min(rect.bottom + 8, innerHeight - tooltip.offsetHeight - 12))}px`;
  });
  section.append(tooltip);
}

function appendAnalysisPointInspectors(section, svg, series, unit, { x, y, chartRight }) {
  const dates = [...new Set(series.flatMap((item) => item.points.map((point) => point.date)))].sort((a, b) => a.localeCompare(b));
  const hitWidth = Math.min(44, (chartRight - 60) / Math.max(1, dates.length - 1));
  const candidates = [];
  dates.forEach((date) => {
    const readings = series.map((item) => ({ item, point: item.points.find((point) => point.date === date) })).filter(({ point }) => analysisValidPoint(point));
    if (!readings.length) return;
    const marker = svgNode("g", { class: "analysis-day-marker", "data-date": date });
    marker.append(svgNode("rect", { x: Math.max(60, x(date) - hitWidth / 2), y: 26, width: Math.min(hitWidth, chartRight - Math.max(60, x(date) - hitWidth / 2)), height: 137, fill: "transparent" }));
    const lines = readings.map(({item, point}) => {
      const source = item.source ? " · " + item.source : "";
      return `${item.legendLabel || item.label}: ${analysisPointValue(item, point, unit)}${source}${analysisReadingDetails(point, item.unit || unit)}`;
    });
    analysisPointTooltip(section, marker, dateLabel(date), lines);
    const tooltip = section.lastElementChild;
    candidates.push({ date, marker, tooltip });
    marker.addEventListener("mouseenter", () => { if (matchMedia("(hover: hover)").matches && !tooltip.matches(":popover-open")) tooltip.showPopover(); });
    marker.addEventListener("mouseleave", () => { if (tooltip.matches(":popover-open") && document.activeElement !== marker) tooltip.hidePopover(); });
    marker.addEventListener("focus", () => {
      const root = section.closest(".analysis-history-charts");
      root?.querySelectorAll(".analysis-day-marker").forEach((node) => node.classList.toggle("is-selected", node.dataset.date === date));
      // The keyboard focus ring sits on the day's points, not around the tall hit column (#4025).
      svg.querySelectorAll(".analysis-focus-ring").forEach((ring) => ring.remove());
      // Only series that are drawn get a ring (Gitar, #4204).
      if (marker.matches(":focus-visible")) readings.filter(({ item }) => analysisPlottable(item)).forEach(({ point }) => svg.append(svgNode("circle", { cx: x(date), cy: y(point.value), r: 8, class: "analysis-focus-ring" })));
    });
    marker.addEventListener("blur", () => svg.querySelectorAll(".analysis-focus-ring").forEach((ring) => ring.remove()));
    svg.append(marker);
  });
  if (!candidates.length) return;
  // Roving tabindex: one tab stop per chart (the latest day); arrows move between days.
  const markers = candidates.map((candidate) => candidate.marker);
  const rove = (target) => markers.forEach((node) => node.setAttribute("tabindex", node === target ? "0" : "-1"));
  rove(markers.at(-1));
  markers.forEach((marker, index) => {
    marker.addEventListener("focus", () => rove(marker));
    marker.addEventListener("keydown", (event) => {
      const target = { ArrowLeft: Math.max(0, index - 1), ArrowRight: Math.min(markers.length - 1, index + 1), Home: 0, End: markers.length - 1 }[event.key];
      if (target === undefined) return;
      event.preventDefault();
      markers[target].focus();
    });
  });
  const overlay = svgNode("rect", { x: 60, y: 26, width: chartRight - 60, height: 137, fill: "transparent", class: "analysis-plot-hit", "aria-hidden": "true" });
  const nearest = (event) => {
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
    return candidates.reduce((best, candidate) => Math.abs(x(candidate.date) - point.x) < Math.abs(x(best.date) - point.x) ? candidate : best, candidates[0]);
  };
  overlay.addEventListener("click", (event) => {
    const candidate = nearest(event); candidate.marker.focus();
    if (!candidate.tooltip.matches(":popover-open")) candidate.tooltip.showPopover();
  });
  overlay.addEventListener("pointermove", (event) => {
    if (event.pointerType !== "mouse" || !matchMedia("(hover: hover)").matches) return;
    const candidate = nearest(event); if (!candidate.tooltip.matches(":popover-open")) candidate.tooltip.showPopover();
  });
  overlay.addEventListener("pointerleave", () => {
    candidates.forEach(({ marker, tooltip }) => { if (tooltip.matches(":popover-open") && document.activeElement !== marker) tooltip.hidePopover(); });
  });
  svg.append(overlay);
}

const analysisSectionState = new Map();

function makeAnalysisSectionCollapsible(section, key) {
  const heading = section.querySelector(":scope > h3");
  if (!heading) return section;
  const details = h("details", { className: "analysis-section-toggle", dataset: { analysisSection: key }, open: analysisSectionState.get(key) ?? true });
  const help = heading.querySelector(":scope > .analysis-legend-info");
  if (help) { help.remove(); help.classList.add("analysis-section-help"); }
  const summary = h("summary", null, heading);
  const content = h("div", "analysis-section-content");
  content.append(...section.childNodes);
  details.append(summary, content); section.append(details);
  if (help) section.append(help);
  details.addEventListener("toggle", () => analysisSectionState.set(key, details.open));
  return section;
}

function rememberAnalysisSectionState(...roots) {
  // The toggle event fires asynchronously, so a render right after a click must read the live state.
  for (const root of roots) root.querySelectorAll("details[data-analysis-section]").forEach((details) => analysisSectionState.set(details.dataset.analysisSection, details.open));
}

// What the Entwicklung of a sport draws, said once for its own sport (#3929 A-08).
function developmentNote(sport, weeks) {
  const values = sport === "Lauf" ? "letzter gültiger Wochenwert für die Schwellenpace, Wochenmedian für VO₂max" : "alle datierten FTP-Werte, Wochenmedian für VO₂max";
  return `Letzte ${weeks} Kalenderwochen einschließlich der laufenden: ${values}. Fehlende Messungen bleiben Lücken.`;
}

function analysisChartGroup(title, charts, series, note) {
  const group = h("section", "analysis-chart-card analysis-chart-stack");
  const heading = h("h3", "analysis-group-heading", h("span", "analysis-group-title", title));
  if (series?.length) {
    const help = h("button", { className: "analysis-legend-info analysis-info-icon", type: "button", "aria-label": `${title}: Informationen` }, "i");
    const info = h("div", { className: "analysis-info-tooltip", id: `group-info-${++analysisInfoId}` });
    series.forEach((item) => {
      const details = [item.source, item.coverage, item.referenceLabel].filter(Boolean).join(" · ");
      if (details) info.append(h("p", null, `${item.legendLabel || item.label}: ${details}`));
    });
    if (note) info.append(h("p", null, note));
    bindAnalysisPopover(help, info); heading.append(help);
    group.append(heading, info);
  } else group.append(heading);
  charts.forEach((chart) => {
    chart.classList.remove("analysis-chart-card"); chart.classList.add("analysis-subchart");
    const heading = chart.querySelector("h3");
    const subheading = h("h4"); subheading.append(...heading.childNodes); heading.replaceWith(subheading);
    group.append(chart);
  });
  return group;
}

// The one period table of Analyse and Ernährung (athlete decision, #3839): the key of the energy
// read's fit_scale, the days, the calendar weeks a weekly view draws, the label of every button and
// the span the cards name. 4 Wochen draw days; every longer period draws calendar weeks.
const ANALYSIS_PERIODS = [
  { key: "28d", days: 28, weeks: 4, label: "4 Wochen", span: "den letzten 4 Wochen" },
  { key: "91d", days: 91, weeks: 13, label: "3 Monate", span: "den letzten 3 Monaten" },
  { key: "182d", days: 182, weeks: 26, label: "6 Monate", span: "den letzten 6 Monaten" },
  { key: "365d", days: 365, weeks: 52, label: "12 Monate", span: "den letzten 12 Monaten" },
];
// The performance state serves up to 3 Monate; a longer period reads /api/analysis/trend-history on demand.
const BASE_PERIOD_DAYS = 91;

// Each tab starts on its remembered period; called once when the app starts, before any view renders.
function restoreAnalysisPeriods() {
  analysisHistoryPeriod = storedAnalysisPeriod("load", analysisHistoryPeriod);
  recoveryHistoryPeriod = storedAnalysisPeriod("recovery", recoveryHistoryPeriod);
  developmentPeriod = storedAnalysisPeriod("development", developmentPeriod);
  strengthHistoryPeriod = storedAnalysisPeriod("strength", strengthHistoryPeriod);
  bodyHistoryPeriod = storedAnalysisPeriod("body", bodyHistoryPeriod);
  restoreNutritionPeriod();
}

function analysisPeriod(key) {
  return ANALYSIS_PERIODS.find((item) => item.key === key) || ANALYSIS_PERIODS[0];
}

function analysisPeriodWeekly(key) {
  return analysisPeriod(key).days > 28;
}

// `trend` adds the line under the buttons that names a failed longer read; `remember` names the tab
// whose choice is kept on this device. Each text stays on one line.
function analysisPeriodControls(label, value, change, { trend = false, remember = null } = {}) {
  const controls = h("div", { className: "analysis-period-controls", "aria-label": label });
  controls.setAttribute("role", "group");
  for (const { key, label: text } of ANALYSIS_PERIODS) {
    const button = h("button", { className: "secondary-button", type: "button", "aria-pressed": String(key === value) }, text);
    button.addEventListener("click", () => {
      if (remember) rememberAnalysisPeriod(remember, key);
      // A choice retries a failed longer read.
      for (const [days, entry] of trendHistories) if (entry.failed) trendHistories.delete(days);
      change(key);
    });
    controls.append(button);
  }
  return trend ? h("div", "analysis-trend-controls", controls, trendHistoryNote(value)) : controls;
}

let analysisHistoryPeriod = "91d";
let recoveryHistoryPeriod = "28d";
// Only a period past the base read reads /api/analysis/trend-history; the default view keeps its read.
let developmentPeriod = "91d";
const trendHistories = new Map();

// The stored long read of `days`, or null while it loads; `rerender` runs once it arrives.
// A newer data version reloads it in the background while the older read stays drawn; a failed read is
// kept as failed (no automatic retry) until the athlete picks a period again.
function trendHistory(days, rerender) {
  const cached = trendHistories.get(days);
  const version = analysisReportVersionKey();
  if (cached?.failed || cached?.loading || cached?.version === version) return cached?.report || null;
  trendHistories.set(days, { ...cached, loading: true });
  requestAnalysisReport(`/api/analysis/trend-history?days=${days}`)
    .then((report) => { trendHistories.set(days, { report, version }); rerender(); })
    .catch(() => { trendHistories.set(days, { failed: true }); rerender(); });
  return cached?.report || null;
}

// The long read of a period longer than the view's own `base` days, else null.
function trendHistoryFor(period, base, rerender) {
  const { days } = analysisPeriod(period);
  return days > base ? trendHistory(days, rerender) : null;
}

// A short line under the selector when the longer read failed.
function trendHistoryNote(period) {
  return trendHistories.get(analysisPeriod(period).days)?.failed ? h("p", "muted", "Der längere Zeitraum konnte nicht geladen werden.") : null;
}

// A long read's answer redraws the charts and the training focus of its period.
function rerenderAnalysisTrend() {
  renderAnalysisHistory(state.data?.performance?.history);
  renderTrainingFocus(analysisTrainingFocus());
}

// The training focus of the load tab's period: from the state, else from its long read.
function analysisTrainingFocus(performance = state.data?.performance) {
  const periods = performance?.training_focus_periods;
  return periods?.[analysisHistoryPeriod] || trendHistories.get(analysisPeriod(analysisHistoryPeriod).days)?.report?.training_focus || periods?.["91d"] || performance?.training_focus;
}

// Report responses are reused per content version (state_versions) and in-flight requests are shared.
const analysisReportCache = new Map();

function analysisReportVersionKey() {
  const versions = state.data?.state_versions || {};
  return JSON.stringify(Object.keys(versions).sort((left, right) => left.localeCompare(right)).map((key) => [key, versions[key]]));
}

function requestAnalysisReport(path) {
  const version = analysisReportVersionKey();
  const cached = analysisReportCache.get(path);
  if (cached?.version === version) return cached.request;
  const request = api(path, { timeoutMs: DATA_LOAD_TIMEOUT_MS }).catch((error) => {
    if (analysisReportCache.get(path)?.request === request) analysisReportCache.delete(path);
    throw error;
  });
  analysisReportCache.set(path, { version, request });
  return request;
}

function invalidateAnalysisReport(path) {
  analysisReportCache.delete(path);
}

function clearAnalysisReportCache() {
  analysisReportCache.clear();
  trendHistories.clear();
}

let cyclingPowerProfileGeneration = 0;
const ALL_TIME_RETRY_MS = 3000;
// One power-profile read fills the Rekorde cards (#3826) and the sport tabs' CP/CS lines (#2265).
const PROFILE_CARDS = [["rideActivityRecords", "Einzelne Fahrt"], ["cyclingPowerProfile", "Leistungsprofil"], ["runActivityRecords", "Einzelner Lauf"], ["runningBestEfforts", "Bestwerte"]];
const CRITICAL_LINES = [["cyclingCriticalPower", "ride"], ["runningCriticalSpeed", "run"]];
const ANALYSIS_PERFORMANCE_ROUTES = new Set(["analysis", "analysis/running", "analysis/cycling"]);
// Every measured duration (athlete decision, #3826).
const CYCLING_POWER_PROFILE_DURATIONS = [[5, "5 Sekunden"], [15, "15 Sekunden"], [30, "30 Sekunden"], [60, "1 Minute"], [120, "2 Minuten"], [180, "3 Minuten"], [300, "5 Minuten"], [420, "7 Minuten"], [600, "10 Minuten"], [720, "12 Minuten"], [1200, "20 Minuten"], [1800, "30 Minuten"], [3600, "1 Stunde"]];

// The date of a record opens its activity (#3826); a record without one keeps plain text.
function recordDateCell(point, label) {
  const cell = h("td", { dataset: { label: "Datum" } });
  if (!point) return cell;
  if (!point.activity_id) { cell.append(dateLabel(point.date)); return cell; }
  const open = h("button", { type: "button", className: "link-button", "aria-label": `${label} vom ${dateLabel(point.date)} öffnen` }, dateLabel(point.date));
  open.addEventListener("click", () => void ActivityDetails.open({ activity_id: point.activity_id, name: point.name }, { api, showDialog: showAccessibleDialog, backLabel: state.route === "analysis/cycling" ? "Zurück zu Radfahren" : "Zurück zu Laufen" }));
  cell.append(open);
  return cell;
}

// Both cards show the all-time bests, each with its date (#2694).
function cyclingPowerProfilePoint(result, duration) {
  const candidates = result?.all_time?.power || [];
  return candidates.filter((item) => item.duration_seconds === duration && item.status === "ok" && item.watts != null && Number.isFinite(Number(item.watts))).sort((a, b) => Number(b.watts) - Number(a.watts))[0];
}

function cyclingPowerProfileHasValues(result) {
  return CYCLING_POWER_PROFILE_DURATIONS.some(([duration]) => cyclingPowerProfilePoint(result, duration));
}

function cyclingPowerProfileTable(result) {
  const head = h("thead", null, h("tr", null, ["Dauer", "Leistung", "Datum"].map((label) => h("th", { scope: "col" }, label))));
  const body = h("tbody");
  for (const [duration, label] of CYCLING_POWER_PROFILE_DURATIONS) {
    const point = cyclingPowerProfilePoint(result, duration);
    const cell = h("td", { title: point?.source, dataset: { label: "Leistung" } }, point && analysisValue(point.watts, "W"));
    // Indoor rides count for power, marked as such (#3826).
    if (point?.environment === "indoor") cell.append(recordSourceChip("Indoor"));
    if (!point) markValueMissing(cell);
    body.append(h("tr", { dataset: { powerDuration: String(duration) } }, h("th", { scope: "row" }, label), cell, recordDateCell(point, `Leistung ${label}`)));
  }
  const scroll = makeScrollRegionFocusable(h("div", "table-scroll"), "Leistung nach Dauer");
  scroll.append(h("table", "race-predictions-table", head, body));
  return scroll;
}

// One sport's experimental estimate: CS/D′ under the running best efforts, CP/W′ under the power profile (#2265).
// Beside an estimate it also names the missing ones; a line of only missing ones holds no value and is dropped (#4037).
function criticalPowerLine(result, sport) {
  const estimate = result?.critical_power;
  const fits = (estimate?.fits || []).filter((fit) => fit.sport === sport);
  if (!fits.some((fit) => fit.status === "ok")) return null;
  const labels = { "ride:outdoor": "Rad outdoor", "ride:indoor": "Rad indoor", "run:outdoor": "Lauf" };
  const parts = fits.map((fit) => {
    const label = labels[`${fit.sport}:${fit.environment}`] || fit.sport;
    if (fit.status !== "ok") return `${label} —`;
    const value = fit.sport === "run"
      ? `${label} CS ${AppFormat.number(fit.cs_mps, { digits: 2 })} m/s · D′ ${analysisValue(fit.d_prime_meters, "m")} (unsicher)`
      : `${label} CP ${analysisValue(fit.cp_watts, "W")} · W′ ${analysisValue(fit.w_prime_joules / 1000, "kJ")} (unsicher)`;
    const races = (fit.efforts || []).filter((effort) => effort.effort_context === "race").length;
    return races ? `${value} · ${races}× Wettkampf` : value;
  });
  const line = h("p", "analysis-reference-note");
  line.append(`Experimentell · ${sport === "run" ? "CS" : "CP"} (${estimate.window_days} Tage): ${parts.join(" · ")}`);
  line.dataset.criticalPower = "true";
  line.title = estimate.scope || "";
  return line;
}

// The Rekorde cards show the read's loading state once, or its failure with a retry.
function showProfileCardsState(error) {
  for (const [id, title] of PROFILE_CARDS) {
    const target = document.getElementById(id);
    if (!error && target.childElementCount && target.dataset.analysisEmpty !== "failed") continue;
    target.dataset.analysisEmpty = error ? "failed" : "loading";
    target.replaceChildren(h("h4", null, title), error
      ? createErrorState(error.message, () => void renderCyclingPowerProfile())
      : createLoadingState(`${title} wird geladen…`));
  }
}

// Analyse › Kraft (#3764, athlete B2): how often per week the upper body and the legs were trained over
// the chosen period, a full body session on both; sessions that decide neither are counted apart.
let strengthAnalysisGeneration = 0;
let strengthHistoryPeriod = "91d";

// One chart per body side: the sessions per week that trained it, a full body session on both.
function strengthSideChart(title, key, color, report) {
  const points = (report.weeks || []).map((week) => ({ date: week.week_start, value: week[key] }));
  const series = [{ label: title, legendLabel: title, unit: "Mal", color, bars: true, connected: true, cadenceDays: 7, average: true, averageInHeading: true, hideZeroLabels: true, points }];
  return analysisChart(title, series, "Mal", report.start, report.end, "Einheiten pro Woche; Ganzkörper zählt für Oberkörper und Beine.", { calendarWeeks: true, showLegend: false });
}

function strengthAnalysisCards(report) {
  const unknown = (report.weeks || []).reduce((total, week) => total + (week.unknown || 0), 0);
  return [
    strengthSideChart("Oberkörper", "upper", 2, report),
    strengthSideChart("Beine", "legs", 3, report),
    unknown ? h("p", "analysis-reference-note", `${AppFormat.plural(unknown, "Einheit", "Einheiten")} ohne Oberkörper- oder Beinschwerpunkt, nicht zugeordnet.`) : null,
  ].filter(Boolean);
}

async function renderStrengthAnalysis() {
  if (state.route !== "analysis/strength" || !state.data) return;
  const root = document.getElementById("strengthAnalysis");
  const generation = ++strengthAnalysisGeneration;
  if (!root.childElementCount) {
    root.dataset.analysisEmpty = "loading";
    root.replaceChildren(createLoadingState("Kraftdaten werden geladen…"));
  }
  try {
    const report = await requestAnalysisReport(`/api/analysis/strength?weeks=${analysisPeriod(strengthHistoryPeriod).weeks}`);
    if (generation !== strengthAnalysisGeneration) return;
    const controls = analysisPeriodControls("Zeitraum für Kraft", strengthHistoryPeriod, (choice) => { strengthHistoryPeriod = choice; void renderStrengthAnalysis(); }, { remember: "strength" });
    // The selector stays above the empty card, so a short period without sessions can be widened.
    document.getElementById("strengthPeriod").replaceChildren(controls);
    root.replaceChildren(...strengthAnalysisCards(report));
    root.dataset.analysisEmpty = String(!(report.weeks || []).some((week) => week.sessions));
  } catch (error) {
    if (generation !== strengthAnalysisGeneration) return;
    root.dataset.analysisEmpty = "failed";
    root.replaceChildren(createErrorState(error.message, () => void renderStrengthAnalysis()));
  }
  renderAnalysisSegmentsIfActive();
}

async function renderCyclingPowerProfile() {
  if (!ANALYSIS_PERFORMANCE_ROUTES.has(state.route) || !state.data?.performance) return;
  const generation = ++cyclingPowerProfileGeneration;
  const session = state.sessionGeneration;
  showProfileCardsState(null);
  try {
    const result = await requestAnalysisReport("/api/analysis/power-profiles");
    if (generation !== cyclingPowerProfileGeneration || session !== state.sessionGeneration) return;
    // The server reads the all-time bests in the background; the cards keep loading until then (#2694).
    const allTime = result?.all_time;
    const again = allTime?.status === "pending" || allTime?.stale === true;
    if (again || allTime?.status === "failed") invalidateAnalysisReport("/api/analysis/power-profiles");
    if (allTime?.status === "failed") throw new Error("Konnte nicht geladen werden.");
    if (again) setTimeout(() => { if (generation === cyclingPowerProfileGeneration) void renderCyclingPowerProfile(); }, ALL_TIME_RETRY_MS);
    if (allTime?.status === "pending") return;
    renderPowerProfile(result);
    renderRunningBestEfforts(result);
    renderActivityRecords(result);
    renderCriticalLines(result);
    renderAnalysisSegmentsIfActive();
  } catch (error) {
    if (generation === cyclingPowerProfileGeneration && session === state.sessionGeneration) {
      showProfileCardsState(error || new Error("Konnte nicht geladen werden."));
      renderCriticalLines(null);
      renderAnalysisSegmentsIfActive();
    }
  }
  // The trends read is slower; it follows the profile cards so it never holds them up (#2802).
  if (generation === cyclingPowerProfileGeneration && session === state.sessionGeneration) void renderAnalysisTrends();
}

// Recurring sessions, the values the Coach reads (#2802); the pace at equal heart rate is
// the Coach's only and shows as the LT1 line in "Entwicklung" (#3153).
const ANALYSIS_TRENDS_PATH = "/api/analysis/trends";
const RECURRING_CARDS = [["runningRecurringSessions", "run"], ["cyclingRecurringSessions", "ride"]];
let analysisTrendsGeneration = 0;
let renderedAnalysisTrends = null;

async function renderAnalysisTrends() {
  const generation = ++analysisTrendsGeneration;
  const session = state.sessionGeneration;
  let result;
  try {
    result = await requestAnalysisReport(ANALYSIS_TRENDS_PATH);
  } catch (error) {
    if (generation !== analysisTrendsGeneration || session !== state.sessionGeneration) return;
    // Each sport page shows one error with a retry; no earlier groups stay beside it.
    renderedAnalysisTrends = null;
    for (const [id] of RECURRING_CARDS) {
      const root = document.getElementById(id);
      root.replaceChildren(createErrorState(error.message, () => void renderAnalysisTrends()));
      root.dataset.analysisEmpty = "failed";
    }
    renderAnalysisSegmentsIfActive();
    return;
  }
  // A cached answer that is already drawn is not drawn again on each profile retry.
  if (generation !== analysisTrendsGeneration || session !== state.sessionGeneration || result === renderedAnalysisTrends) return;
  renderedAnalysisTrends = result;
  for (const [id, sport] of RECURRING_CARDS) renderRecurringSessions(document.getElementById(id), result?.recurring, sport);
  renderAnalysisSegmentsIfActive();
}

// "Garmin original speed/heart-rate streams" names its providers before "original".
function analysisSourceLabels(texts) {
  const labels = texts.flatMap((text) => String(text || "").replace(/^derived from /, "").replace(/ original .*$/, "").split(/ \/ |, /));
  return [...new Set(labels.filter(Boolean))];
}

function renderRecurringSessions(root, recurring, sport) {
  const groups = (recurring?.groups || []).filter((group) => group.sport === sport);
  root.dataset.analysisEmpty = String(!groups.length);
  root.replaceChildren(...(groups.length ? [h("h3", null, "Wiederkehrende Einheiten"), ...groups.map(recurringGroup)] : []));
}

function recurringGroup(group) {
  const observations = [...group.observations].reverse().slice(0, 6);
  const latest = observations[0];
  const repeat = latest.repeat_groups?.[0];
  const title = repeat ? `${repeat.repetitions} × ${repeat.target}` : (latest.name || `${latest.steps.length} Schritte`);
  const rows = observations.map((row) => h("tr", null, h("th", { scope: "row" }, dateLabel(row.date)), h("td", null, recurringValue(row))));
  const chips = analysisSourceLabels(observations.map((row) => row.source)).map(recordSourceChip);
  return h("section", { className: "recurring-group", dataset: { recurringGroup: group.target_sha256 } },
    h("h4", null, title, " ", ...chips), h("table", "race-predictions-table", h("tbody", null, ...rows)));
}

// The mean of the repeated work steps (or of every measured step), with the fade of the repeats.
function recurringValue(row) {
  const repeat = row.repeat_groups?.[0];
  const measured = row.steps.filter((step) => step.status === "ok" && step.mean != null && (!repeat || step.target === repeat.target));
  // Steps targeted by heart rate, power or pace are never averaged together.
  const steps = measured.filter((step) => step.unit === measured[0]?.unit);
  if (!steps.length) return "–";
  const mean = steps.reduce((sum, step) => sum + Number(step.mean), 0) / steps.length;
  const unit = steps[0].unit;
  const value = unit === "m/s" ? analysisValue(1000 / mean, "s/km") : analysisValue(mean, unit);
  return repeat ? `${value} · Abfall ${AppFormat.number(repeat.fade_percent, { digits: 1 })} %` : value;
}

function renderPowerProfile(result) {
  const root = document.getElementById("cyclingPowerProfile");
  root.replaceChildren(h("h4", null, "Leistungsprofil"));
  const hasValues = cyclingPowerProfileHasValues(result);
  if (hasValues) root.append(cyclingPowerProfileTable(result));
  else root.append(h("p", "muted", "Noch keine Leistungsdaten."));
  const coverage = result.all_time?.cache_coverage;
  if (coverage?.power_unknown_records > 0) {
    const gap = h("p", "analysis-reference-note", `Für ${coverage.power_unknown_records} von ${coverage.power_records} Radeinheiten fehlen vollständige Messreihen; die Bestwerte können höher liegen.`);
    gap.dataset.powerProfileGap = "true";
    root.append(gap);
  }
  root.dataset.analysisEmpty = String(!hasValues);
}

// The experimental CP/CS line stays in its sport tab (#3826); a failed read leaves it out, marked failed
// so the tab does not count as empty.
function renderCriticalLines(result) {
  for (const [id, sport] of CRITICAL_LINES) {
    const root = document.getElementById(id);
    const line = result && criticalPowerLine(result, sport);
    root.replaceChildren(...(line ? [line] : []));
    root.dataset.analysisEmpty = result ? String(!line) : "failed";
  }
}

// The longest and highest ride and run (#3826): outdoor rides only, every run.
const ACTIVITY_RECORD_ROWS = {
  ride: [["distance_m", "Weiteste Fahrt"], ["moving_duration_s", "Längste Fahrt"], ["elevation_gain_m", "Meiste Höhenmeter"]],
  run: [["distance_m", "Weitester Lauf"], ["moving_duration_s", "Längster Lauf"], ["elevation_gain_m", "Meiste Höhenmeter"]],
};

function activityRecordValue(record) {
  if (record.field === "distance_m") return analysisValue(record.value / 1000, "km");
  if (record.field === "elevation_gain_m") return analysisValue(record.value, "m");
  return AppFormat.clock(record.value);
}

function renderActivityRecords(result) {
  const records = result?.all_time?.activity_records || [];
  for (const [id, sport, title] of [["rideActivityRecords", "ride", "Einzelne Fahrt"], ["runActivityRecords", "run", "Einzelner Lauf"]]) {
    const root = document.getElementById(id);
    const body = h("tbody");
    let values = 0;
    for (const [field, label] of ACTIVITY_RECORD_ROWS[sport]) {
      const record = records.find((item) => item.sport === sport && item.field === field && item.status === "ok");
      const cell = h("td", { title: record?.source, dataset: { label: "Wert" } }, record && activityRecordValue(record));
      if (record) values += 1; else markValueMissing(cell, "–");
      body.append(h("tr", { dataset: { activityRecord: `${sport}:${field}` } }, h("th", { scope: "row" }, label), cell, recordDateCell(record, label)));
    }
    const head = h("thead", null, h("tr", null, ["Rekord", "Wert", "Datum"].map((label) => h("th", { scope: "col" }, label))));
    const scroll = makeScrollRegionFocusable(h("div", "table-scroll"), title);
    scroll.append(h("table", "race-predictions-table", head, body));
    root.replaceChildren(h("h4", null, title), scroll);
    root.dataset.analysisEmpty = String(!values);
  }
}

// The fastest observed time per race distance of all stored sessions (#2694); running bests
// are distances only, every row stays with a dash where no best exists (#2786). A missing own
// best takes the provider's personal record, marked with a chip of its short source label (#2806).
const RUNNING_BEST_DISTANCES = [[1000, "1 km"], [5000, "5 km"], [10000, "10 km"], [21097.5, "Halbmarathon"], [42195, "Marathon"]];

function recordSourceChip(label) {
  const chip = createStatusChip(label);
  chip.classList.add("source-chip");
  return chip;
}

function renderRunningBestEfforts(result) {
  const root = document.getElementById("runningBestEfforts");
  const points = result?.all_time?.running?.distance || [];
  root.replaceChildren(h("h4", null, "Bestwerte"));
  const body = h("tbody");
  let values = 0;
  const records = state.data?.performance?.personal_records || [];
  for (const [meters, label] of RUNNING_BEST_DISTANCES) {
    const own = points.find((item) => item.distance_meters === meters && item.seconds > 0);
    const point = own || records.find((item) => item.distance_meters === meters && item.seconds > 0);
    const cell = h("td", { title: point?.source, dataset: { label: "Zeit" } }, point && AppFormat.clock(point.seconds));
    if (point && !own) cell.append(recordSourceChip(point.label));
    if (point) values += 1; else markValueMissing(cell, "–");
    // Garmin's personal record names no activity; its date stays plain text.
    body.append(h("tr", { dataset: { runningBest: `distance:${meters}` } }, h("th", { scope: "row" }, label), cell, recordDateCell(own || (point && { date: point.date }), `Bestzeit ${label}`)));
  }
  const scroll = makeScrollRegionFocusable(h("div", "table-scroll"), "Lauf-Bestwerte");
  const head = h("thead", null, h("tr", null, ["Strecke", "Zeit", "Datum"].map((label) => h("th", { scope: "col" }, label))));
  scroll.append(h("table", "race-predictions-table", head, body)); root.append(scroll);
  root.dataset.analysisEmpty = String(!values);
}

function equipmentRouteActive() {
  return state.route === "more/equipment";
}

globalThis.matchMedia("(max-width: 599px)").addEventListener("change", () => {
  if (state.data) { renderAnalysisHistory(state.data.performance?.history); renderPersonalRecovery(state.data.performance?.personal_recovery); }
});

function performanceSeriesExplanation(key) {
  if (key === "cycling_ftp_watts") return "Alle datierten Garmin-FTP-Messungen.";
  if (key.includes("vo2max")) return "Wochenmedian aus vorhandenen Messungen.";
  return "Letzter gültiger Messwert je Woche.";
}

// LT1 beside LT2 in each threshold chart, one point per calendar week (#2693): weeks without
// three own steady sessions stay gaps; the readout names the heart rate and the origin.
function firstThresholdSeries(lt1, start, end) {
  return [["run", "Lauf", "s/km"], ["ride", "Rad", "W"]].map(([sport, label, unit]) => {
    // The server dates a week at its end and the running week at today; the chart uses the week's slot (#3450).
    const points = (lt1?.[sport] || []).filter((point) => point.date >= start && point.date <= end).map((point) => ({
      date: addDateKey(start, Math.floor((Date.parse(point.date) - Date.parse(start)) / 604800000) * 7), observedDate: point.date, value: point.value,
      readout: [point.hr_bpm == null ? null : `bei ${point.hr_bpm} bpm`, THRESHOLD_ORIGINS[point.origin]].filter(Boolean).join(" · "),
    }));
    return { label: `${label} · LT1`, legendLabel: "LT1 · aerobe Schwelle", unit, color: 1, line: "solid", cadenceDays: 7,
      explanation: "Wie der heutige LT1-Wert, je Woche mit den damaligen Schwellen und Einheiten.", currentPoint: points.findLast(analysisValidPoint), points };
  }).filter((item) => item.points.some(analysisValidPoint));
}

// The LT1 history waits for the server's background session read: read the values again
// while it is pending or stale and an analysis sport view is open (#2693).
// A failed read is tried again by the server after a pause, so it is asked again as well.
let firstThresholdRefreshTimer = null;
let firstThresholdRefreshGeneration = 0;
function scheduleFirstThresholdRefresh(status) {
  clearTimeout(firstThresholdRefreshTimer);
  const generation = ++firstThresholdRefreshGeneration;
  if (!["pending", "stale", "failed"].includes(status)) return;
  const session = state.sessionGeneration;
  firstThresholdRefreshTimer = setTimeout(async () => {
    if (!ANALYSIS_PERFORMANCE_ROUTES.has(state.route)) return;
    const result = await api("/api/performance").catch(() => null);
    // A newer render or another login owns the state by now.
    if (!result || !state.data || generation !== firstThresholdRefreshGeneration || session !== state.sessionGeneration) return;
    if (!ANALYSIS_PERFORMANCE_ROUTES.has(state.route)) return;
    state.data.performance = result.performance || result;
    renderAnalysisHistory(state.data.performance.history);
  }, ALL_TIME_RETRY_MS);
}

function renderAnalysisHistory(history) { // NOSONAR
  const root = document.querySelector("#analysisHistoryCharts");
  const loadRoot = document.querySelector("#analysisLoadCharts");
  const enduranceRoot = document.querySelector("#performanceEnduranceCharts");
  const predictions = document.getElementById("performancePredictions");
  const powerProfile = document.getElementById("cyclingCriticalPower");
  const runningBest = document.getElementById("runningCriticalSpeed");
  powerProfile.remove();
  predictions.remove();
  runningBest.remove();
  predictions.classList.remove("analysis-chart-card");
  predictions.classList.add("analysis-subchart");
  renderBodyAnalysis(history?.body);
  const openDetails = new Set([...root.querySelectorAll("details[open]"), ...loadRoot.querySelectorAll("details[open]"), ...enduranceRoot.querySelectorAll("details[open]")].map((details) => `${details.closest("section")?.querySelector("h3,h4")?.textContent}:${details.querySelector("summary")?.textContent}`));
  const periodRoot = document.getElementById("analysisLoadPeriod");
  rememberAnalysisSectionState(root, loadRoot, enduranceRoot);
  root.replaceChildren(); loadRoot.replaceChildren(); enduranceRoot.replaceChildren(); periodRoot.replaceChildren();
  if (!history?.start || !history?.end) {
    // Without a history window there are no charts, so no empty sport groups; the async tables stay attached.
    root.append(predictions, runningBest, powerProfile);
    for (const target of [root, loadRoot, enduranceRoot]) target.dataset.analysisEmpty = "true";
    Object.assign(root.dataset, { analysisEmptyRunning: "true", analysisEmptyCycling: "true" });
    renderAnalysisSegmentsIfActive();
    return;
  }
  const end = history.end;
  const weekStart = addDateKey(end, -((new Date(`${end}T12:00:00Z`).getUTCDay() + 6) % 7));
  const loadPeriod = analysisPeriod(analysisHistoryPeriod);
  const weekly = analysisPeriodWeekly(analysisHistoryPeriod);
  const start = weekly ? addDateKey(weekStart, -7 * (loadPeriod.weeks - 1)) : addDateKey(end, -(loadPeriod.days - 1));
  // 6 and 12 Monate read their own history once (#3839); until it arrives the charts keep the base read.
  const loadHistory = trendHistoryFor(analysisHistoryPeriod, BASE_PERIOD_DAYS, rerenderAnalysisTrend)?.history || history;
  periodRoot.append(analysisPeriodControls("Zeitraum für Belastung", analysisHistoryPeriod, (period) => {
    analysisHistoryPeriod = period;
    renderAnalysisHistory(history);
    renderTrainingFocus(analysisTrainingFocus());
  }, { trend: true, remember: "load" }));
  const developmentWeeks = analysisPeriod(developmentPeriod).weeks;
  const performanceStart = addDateKey(weekStart, -7 * (developmentWeeks - 1));
  const development = trendHistoryFor(developmentPeriod, BASE_PERIOD_DAYS, rerenderAnalysisTrend)?.history || history;
  const developmentMetrics = development.metrics;
  const load = loadHistory.load || { points: [] };
  const loadSeries = [{ label: "Akute Garmin-Belastung", legendLabel: "Akute Garmin-Belastung", source: load.source, unit: "", color: 0,
    cadenceDays: weekly ? 7 : 1, average: true, averageInHeading: true,
    points: weekly ? analysisWeeklyLastPoints(load.points.filter((point) => point.date >= start && point.date <= end).map((point) => ({ date: point.date, value: point.value })), start, end) : load.points.filter((point) => point.date >= start && point.date <= end).map((point) => ({ date: point.date, value: point.value })),
    currentPoint: load.points.filter((point) => point.date >= start && point.date <= end).map((point) => ({ date: point.date, value: point.value })).findLast(analysisValidPoint) }];
  const loadValues = loadSeries[0].points.filter(analysisValidPoint).map((point) => Number(point.value));
  const loadAverage = loadValues.length ? loadValues.reduce((sum, value) => sum + value, 0) / loadValues.length : null;
  const loadTitle = loadAverage == null ? "Akute Garmin-Belastung" : `Akute Garmin-Belastung \u00b7 \u00d8 ${analysisValue(loadAverage, "")}`;
  const sourcePrefix = (source) => (source ? `${source}: ` : "");
  const loadMissing = [];
  const loadChart = collectAnalysisChart(loadMissing, loadTitle, loadSeries, "", start, end, `${sourcePrefix(load.source)}gemessene akute Garmin-Belastung. Gestrichelte Linie: Durchschnitt der angezeigten Werte. Fehlende Messungen bleiben L\u00fccken.`, { calendarWeeks: weekly, caption: analysisBasisCaption("Belastungspunkte", weekly, "state") });
  if (loadChart) {
    loadChart.querySelector(".analysis-chart-legend")?.remove();
    loadRoot.append(loadChart);
  }
  const timePoints = (loadHistory.training_time?.points || []).filter((point) => point.date >= start && point.date <= end).map((point) => ({ date: point.date, value: point.value }));
  if (timePoints.some(analysisValidPoint)) {
    const timeSeries = [{ label: weekly ? "Trainingszeit pro Woche" : "Trainingszeit pro Tag", legendLabel: "Trainingszeit", source: loadHistory.training_time.source, unit: "h", color: 1, bars: true, connected: true, cadenceDays: weekly ? 7 : 1, average: true, averageInHeading: true, points: weekly ? analysisWeeklySumPoints(timePoints, start, end) : timePoints }];
    const timeValues = timeSeries[0].points.filter(analysisValidPoint).map((point) => Number(point.value));
    const timeAverage = timeValues.reduce((sum, value) => sum + value, 0) / timeValues.length;
    const timeCadence = weekly ? " pro Woche" : " pro Tag";
    const timeTitle = timeValues.length ? `Trainingszeit \u00b7 \u00d8 ${analysisValue(timeAverage, "h")}${timeCadence}` : "Trainingszeit";
    const timeChart = collectAnalysisChart(loadMissing, timeTitle, timeSeries, "h", start, end, `${sourcePrefix(loadHistory.training_time.source)}Summe der Bewegungszeit erfasster Aktivit\u00e4ten (Kraft: Gesamtzeit), jede Aktivit\u00e4t einmal. Tage und Wochen ohne erfasste Dauer bleiben L\u00fccken.`, { calendarWeeks: weekly, caption: analysisBasisCaption("h", weekly, "sum") });
    if (timeChart) {
      timeChart.querySelector(".analysis-chart-legend")?.remove();
      loadRoot.append(timeChart);
    }
  } else {
    loadMissing.push("Trainingszeit");
  }
  appendSessionRpeLoadChart(loadHistory.session_rpe_load, loadRoot, loadMissing, start, end);
  appendSmoothedLoadChart(loadHistory.smoothed_session_load, loadHistory.session_rpe_load, loadRoot, loadMissing, start, end);
  appendLocalFitnessChart(loadHistory.local_fitness_fatigue, loadRoot, loadMissing, start, end);
  appendDailyActivityGroup(loadHistory.daily_activity, loadRoot, loadMissing, start, end);
  const performanceSeries = [
    ["cycling_ftp_watts", "Rad · FTP", "W", 0],
    ["run_threshold_pace_seconds_per_km", "Lauf · Schwellenpace", "s/km", 2],
    ["cycling_vo2max_ml_kg_min", "Rad · VO₂max", "ml/kg/min", 3],
    ["running_vo2max_ml_kg_min", "Lauf · VO₂max", "ml/kg/min", 4],
  ].flatMap(([key, label, unit, color]) => (developmentMetrics?.[key] || []).filter((item) => isPreferredSource(item, history.preferred_providers?.[key])).flatMap((item) => {
    const periodPoints = (points) => (key === "cycling_ftp_watts" ? points.filter((point) => point.date >= performanceStart && point.date <= end) : analysisWeeklyPerformancePoints(points, performanceStart, end, key.includes("vo2max")));
    return [{
      label: `${label} · ${item.source}`, legendLabel: label.replace(/^Rad · /, ""), source: item.source, unit, color,
      line: "solid",
      cadenceDays: key === "cycling_ftp_watts" ? 1 : 7,
      explanation: performanceSeriesExplanation(key),
      currentPoint: item.points.filter((point) => point.date <= end).findLast(analysisValidPoint),
      points: periodPoints(item.points),
    }];
  }));
  performanceSeries.push(...firstThresholdSeries(history.first_thresholds, performanceStart, end));
  scheduleFirstThresholdRefresh(history.first_thresholds?.status);
  root.append(analysisPeriodControls("Zeitraum für Entwicklung", developmentPeriod, (period) => {
    developmentPeriod = period;
    rerenderAnalysisTrend();
  }, { trend: true, remember: "development" }));
  // Each sport's development sits in its own segment, so the group is only "Entwicklung" (#2265).
  for (const [sport, primaryUnit, analysisSport] of [["Lauf", "s/km", "run"], ["Rad", "W", "ride"]]) {
    const sportSeries = performanceSeries.filter((item) => item.label.startsWith(sport));
    const missing = [];
    const charts = [[primaryUnit, primaryUnit === "W" ? "Leistungsschwelle · FTP" : "Schwellenpace"], ["ml/kg/min", "VO₂max"]].map(([unit, metric]) => collectAnalysisChart(missing, metric, sportSeries.filter((item) => item.unit === unit), unit, performanceStart, end, "", { compactInfo: true, sparse: true, includeCoverage: false, showLegend: unit === "W" || sportSeries.filter((item) => item.unit === unit).length > 1, calendarWeeks: true })).filter(Boolean);
    if (sport === "Lauf") {
      const extra = document.createDocumentFragment();
      renderProviderMetrics({ running_tolerance: development.provider_metrics?.running_tolerance }, extra, performanceStart, end, missing);
      charts.push(...extra.children);
    }
    root.dataset[analysisSport === "run" ? "analysisEmptyRunning" : "analysisEmptyCycling"] = String(!charts.length);
    const group = analysisChartGroup("Entwicklung", charts, sportSeries, `${developmentNote(sport, developmentWeeks)}`);
    makeAnalysisSectionCollapsible(group, `development-${sport}`);
    group.dataset.analysisSport = analysisSport;
    if (sport === "Lauf") group.querySelector(".analysis-section-content").append(predictions, runningBest);
    else group.querySelector(".analysis-section-content").prepend(powerProfile);
    root.append(group);
  }
  // The endurance score rates all sports' long-term training, so it sits in Belastung (#2265);
  // readiness is charted in Erholung and the weekly training status only repeats the acute load.
  const extra = document.createDocumentFragment();
  renderProviderMetrics({ endurance_score: loadHistory.provider_metrics?.endurance_score }, extra, start, end, []);
  while (extra.firstElementChild) enduranceRoot.append(makeAnalysisSectionCollapsible(extra.firstElementChild, "endurance-score"));
  root.dataset.analysisEmpty = String(root.dataset.analysisEmptyRunning === "true" && root.dataset.analysisEmptyCycling === "true");
  loadRoot.dataset.analysisEmpty = String(!loadRoot.childElementCount);
  enduranceRoot.dataset.analysisEmpty = String(!enduranceRoot.childElementCount);
  for (const target of [root, loadRoot, enduranceRoot]) target.querySelectorAll("details:not([data-analysis-section])").forEach((details) => { details.open = openDetails.has(`${details.closest("section")?.querySelector("h3,h4")?.textContent}:${details.querySelector("summary")?.textContent}`); });
  // The new sport groups start visible: every redraw re-applies the shown tab itself, so no caller can forget it (#3824, #3828).
  renderAnalysisSegmentsIfActive();
}

function sessionRpeSourceLabel(days, rated) {
  const provider = days.reduce((sum, day) => sum + day.rpe_sources.provider, 0);
  const providerLabel = [...new Set(days.flatMap((day) => day.rpe_provider_labels || []))].sort((a, b) => a.localeCompare(b)).join(" + ") || "Anbieter";
  if (!rated) return "Athletenfeedback";
  if (provider === rated) return providerLabel;
  return provider ? `Athletenfeedback + ${providerLabel}` : "Athletenfeedback";
}

// Session-RPE load (AU) stays its own chart: it is never added to or compared with provider load.
function appendSessionRpeLoadChart(report, loadRoot, loadMissing, start, end) {
  const days = (report?.days || []).filter((day) => day.date >= start && day.date <= end);
  const rated = days.reduce((sum, day) => sum + day.rated_sessions, 0);
  if (!rated) { loadMissing.push("RPE-Belastung"); return; }
  const total = days.reduce((sum, day) => sum + day.total_sessions, 0);
  const source = sessionRpeSourceLabel(days, rated);
  const weekly = analysisPeriodWeekly(analysisHistoryPeriod);
  const points = days.map((day) => ({ date: day.date, value: day.value }));
  const series = [{ label: weekly ? "RPE-Belastung pro Woche" : "RPE-Belastung pro Tag", legendLabel: "RPE-Belastung", source, unit: "RPE-Punkte", color: 2, bars: true, connected: true, cadenceDays: weekly ? 7 : 1, points: weekly ? analysisWeeklySumPoints(points, start, end) : points }];
  const chart = analysisChart(`RPE-Belastung · ${rated} von ${total} Einheiten bewertet`, series, "RPE-Punkte", start, end, `${source}: RPE × Gesamtdauer je Einheit.`, { calendarWeeks: weekly, caption: analysisBasisCaption("RPE-Punkte", weekly, "sum") });
  if (!chart) { loadMissing.push("RPE-Belastung"); return; }
  chart.querySelector(".analysis-chart-legend")?.remove();
  loadRoot.append(chart);
}

// Smoothed session-RPE load: own AU curves, never compared with provider load; gaps stay gaps.
function appendSmoothedLoadChart(report, rpeReport, loadRoot, loadMissing, start, end) {
  const days = (report?.days || []).filter((day) => day.date >= start && day.date <= end);
  if (!days.some((day) => day.short != null || day.long != null)) { loadMissing.push("RPE-Belastung kurz und lang"); return; }
  const weekly = analysisPeriodWeekly(analysisHistoryPeriod);
  // The curves carry load from before the window, so their label reads every reported day.
  const rpeDays = (rpeReport?.days || []).filter((day) => day.date <= end);
  const source = sessionRpeSourceLabel(rpeDays, rpeDays.reduce((sum, day) => sum + day.rated_sessions, 0));
  const series = [["short", "RPE-Belastung kurz", 2], ["long", "RPE-Belastung lang", 5]].map(([key, label, color]) => {
    const points = days.map((day) => ({ date: day.date, value: day[key] }));
    return { label, legendLabel: label, source, unit: "RPE-Punkte", color, connected: true, cadenceDays: weekly ? 7 : 1,
      points: weekly ? analysisWeeklyLastPoints(points, start, end) : points, currentPoint: points.findLast(analysisValidPoint) };
  });
  const chart = collectAnalysisChart(loadMissing, "RPE-Belastung kurz und lang", series, "RPE-Punkte", start, end, "", { calendarWeeks: weekly, caption: analysisBasisCaption("RPE-Punkte", weekly, "state") });
  if (chart) loadRoot.append(chart);
}

// Local fitness, fatigue and form (card M7): TSS-scale, never mixed with Intervals.icu CTL/ATL.
function appendLocalFitnessChart(report, loadRoot, loadMissing, start, end) {
  const title = "Fitness, Ermüdung, Form";
  const days = (report?.days || []).filter((day) => day.date >= start && day.date <= end);
  if (!days.some((day) => day.fitness != null || day.fatigue != null)) { loadMissing.push(title); return; }
  const weekly = analysisPeriodWeekly(analysisHistoryPeriod);
  const series = [["fitness", "Fitness", 0], ["fatigue", "Ermüdung", 1], ["form", "Form", 3]].map(([key, label, color]) => {
    const points = days.map((day) => ({ date: day.date, value: day[key] }));
    return { label, legendLabel: label, source: "Lokal berechnet", unit: "TSS/Tag", color, connected: true, cadenceDays: weekly ? 7 : 1,
      points: weekly ? analysisWeeklyLastPoints(points, start, end) : points, currentPoint: points.findLast(analysisValidPoint) };
  });
  const unloaded = days.reduce((sum, day) => sum + (day.sessions_without_load || 0), 0);
  const unloadedNote = `${AppFormat.plural(unloaded, "Einheit", "Einheiten")} ohne TSS`;
  // Share of the visible days, so the note matches the shown period like the session count.
  const incompletePercent = Math.round(100 * days.filter((day) => day.state === "incomplete").length / days.length);
  const notes = [];
  if (unloaded) notes.push(unloadedNote);
  if (incompletePercent > 0) notes.push(`${incompletePercent} % der Tage unvollständig`);
  const chart = collectAnalysisChart(loadMissing, title, series, "TSS/Tag", start, end, notes.join(" · "), { calendarWeeks: weekly, caption: analysisBasisCaption("TSS/Tag", weekly, "state") });
  if (chart) loadRoot.append(chart);
}

// Steps and floors are whole counts per day; the weekly view plots the daily mean of each week (#1782).
function dailyActivityMetrics() {
  return [
    { key: "steps", label: "Schritte", unit: "Schritte/Tag", color: 3 },
    { key: "floors", label: "Stockwerke", unit: "Stockwerke/Tag", color: 4 },
  ];
}

// Names what one plotted value is: a daily value, a weekly sum or mean, or the state at the end of the week.
function analysisBasisCaption(unit, weekly, kind) {
  const basis = {
    state: weekly ? "Wochenstand" : "Tageswert",
    sum: weekly ? "Wochensumme" : "Tagessumme",
    mean: weekly ? "Wochenmittel" : "",
  }[kind];
  return basis ? `${unit} · ${basis}` : unit;
}

function dailyActivityTitle(label, unit, daily) {
  const values = daily.filter(analysisValidPoint).map((point) => Number(point.value));
  if (!values.length) return label;
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  return `${label} · Ø ${analysisNumber(mean, unit)} pro Tag`;
}

function appendDailyActivityGroup(activity, loadRoot, loadMissing, start, end) {
  if (!activity) return;
  const weekly = analysisPeriodWeekly(analysisHistoryPeriod);
  const charts = []; const allSeries = [];
  for (const { key, label, unit, color } of dailyActivityMetrics()) {
    const daily = (activity[key] || []).filter((point) => point.date >= start && point.date <= end).map((point) => ({ date: point.date, value: point.value }));
    const points = weekly ? analysisWeeklyMeanPoints(daily, start, end) : daily;
    const current = daily.findLast(analysisValidPoint);
    const series = [{ label: weekly ? `${label} Ø pro Tag` : label, legendLabel: label, source: activity.source, unit, color, bars: true, connected: true, cadenceDays: weekly ? 7 : 1, points, currentPoint: current }];
    const chart = collectAnalysisChart(loadMissing, dailyActivityTitle(label, unit, daily), series, unit, start, end, `${activity.source}: ${weekly ? "Tagesdurchschnitt je Woche" : "Tageswerte"}. Fehlende Tage bleiben Lücken.`, { calendarWeeks: weekly, caption: analysisBasisCaption(unit, weekly, "mean") });
    if (!chart) continue;
    chart.querySelector(".analysis-chart-legend")?.remove();
    charts.push(chart); allSeries.push(series[0]);
  }
  if (charts.length) loadRoot.append(analysisChartGroup("Alltagsaktivität", charts, allSeries));
}

function providerMetricLabel(key) {
  return { endurance_score: "Ausdauer-Score", running_tolerance: "Lauf-Belastbarkeit", training_readiness_history: "Trainingsbereitschaft", weekly_training_status: "Trainingsstatus · akute Garmin-Belastung" }[key] || key;
}

function providerMetricUnit(key) {
  return { endurance_score: "Punkte", training_readiness_history: "Punkte" }[key] || "";
}

function providerMetricValue(metric, values) {
  const fields = {
    endurance_score: ["overallScore", "enduranceScore", "score", "groupAverage"],
    running_tolerance: ["toleranceScore", "runningTolerance", "tolerance"],
  }[metric] || [];
  for (const field of fields) {
    const value = values?.[field];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

function renderProviderMetrics(metrics, root, start, end, missing, { weeklyReadiness = false } = {}) {
  Object.entries(metrics || {}).forEach(([key, item], index) => {
    const projected = key === "training_readiness_history" || key === "weekly_training_status";
    const raw = (Array.isArray(item?.points) ? item.points : []).map((point) => ({ date: point?.date, value: projected ? point?.value : providerMetricValue(key, point?.values) })).filter((point) => point.date && typeof point.value === "number" && Number.isFinite(point.value));
    if (!raw.some(analysisValidPoint)) { missing.push(providerMetricLabel(key)); return; }
    const daily = key === "training_readiness_history" && !weeklyReadiness;
    const readiness = key === "training_readiness_history";
    const inPeriod = raw.filter((point) => point.date >= start && point.date <= end);
    let points = analysisWeeklyPerformancePoints(raw, start, end, false);
    if (readiness) points = daily ? inPeriod : analysisWeeklyMeanPoints(inPeriod, start, end);
    const series = [{ label: providerMetricLabel(key), legendLabel: providerMetricLabel(key), source: item.source, unit: providerMetricUnit(key), color: index, cadenceDays: daily ? 1 : 7, points, currentPoint: raw.filter((point) => point.date <= end).findLast(analysisValidPoint) }];
    // The readiness score runs from 0 to 100; a week plots the mean of its days.
    const options = { sparse: true, showLegend: false, calendarWeeks: true, ...(readiness ? { domain: { min: 0, max: 100, step: 25 } } : {}), ...(readiness && weeklyReadiness ? { caption: analysisBasisCaption("Punkte", true, "mean") } : {}) };
    const chart = collectAnalysisChart(missing, providerMetricLabel(key), series, providerMetricUnit(key), start, end, "", options);
    if (!chart) return;
    chart.querySelector(".analysis-chart-legend")?.remove();
    root.append(chart);
  });
}

let bodyHistoryPeriod = "91d";

// The athlete's own weigh-ins (no provider, "Lokal") show beside the preferred source (#3252).
function bodySourceShown(item, preferredProvider) {
  return isPreferredSource(item, preferredProvider) || item?.source_provider === null;
}

function bodyWeeklyPoints(points, start, end) {
  return analysisWeeklyPoints(points, start, end, (readings) => {
    const latest = readings.at(-1);
    return { ...latest, weekReadings: readings.length, observedDate: latest.observed_at || latest.date };
  }, { weekReadings: 0 });
}

// A weekly view draws whole weeks: the window starts at the period's last 13, 26 or 52 weeks.
function bodyWindowStart(window, period) {
  const weeks = analysisPeriodWeekly(period) ? analysisPeriod(period).weeks : 0;
  const first = weeks ? addDateKey(window.end, -(weeks * 7 - 1)) : window.start;
  return [first, window.start].sort((a, b) => a.localeCompare(b)).at(-1);
}

function bodySeriesPoints(item, window, period = bodyHistoryPeriod) {
  const start = bodyWindowStart(window, period);
  const points = (item.points || []).filter((point) => point.date >= start && point.date <= window.end)
    .map((point) => ({ ...point, power_method: item.power_method, observedDate: point.observed_at || point.date }));
  return analysisPeriodWeekly(period) ? bodyWeeklyPoints(points, start, window.end) : points;
}

function bodyMetricChart(metric, title, unit, window, period, preferred = {}) {
  const power = metric === "cycling_w_per_kg";
  const weekly = analysisPeriodWeekly(period);
  const entries = (window.metrics?.[metric] || []).filter((item) => power || bodySourceShown(item, preferred[metric]));
  const series = entries.map((item, index) => ({
    label: power ? [item.power_method, item.source].filter(Boolean).join(" \u00b7 ") : [title, entries.length > 1 && item.source].filter(Boolean).join(" \u00b7 "),
    legendLabel: power || entries.length > 1 ? item.source : title,
    source: power ? item.source : "",
    unit,
    color: index,
    noCurrentLine: true,
    sparse: true,
    // A single measured week is still drawn as a point.
    minPoints: weekly ? 1 : 2,
    cadenceDays: weekly ? 7 : 1,
    edgeMeans: power ? undefined : item.edge_means ?? null,
    explanation: metric === "cycling_w_per_kg"
      ? "Tageswert aus FTP geteilt durch das letzte Körpergewicht der vorherigen sieben Tage."
      : "Nur gemessene Tageswerte; fehlende Tage bleiben Lücken.",
    points: bodySeriesPoints(item, window, period),
  }));
  const chart = analysisChart(title, series, unit, bodyWindowStart(window, period), window.end,
    metric === "cycling_w_per_kg"
      ? "W/kg verwendet FTP (Garmin Connect) und ein höchstens sieben Tage altes gemessenes Gewicht."
      : "Gemessene Verlaufswerte. Fehlende Messungen werden nicht ergänzt.",
    // A weekly reading sits on its rolling week's start, so the axis reads in weeks (#3450).
    { sparse: true, calendarWeeks: weekly });
  if (!chart) return null;
  chart.dataset.bodyMetric = metric;
  if (weekly) chart.querySelectorAll(".analysis-info-tooltip").forEach((info) => {
    info.append(h("p", "analysis-reference-note", `${analysisPeriod(period).weeks} rollierende Wochen: letzter Messwert je Woche.`));
  });
  // The legend tooltip names its series; a hidden series leaves no tooltip, so the position says nothing.
  if (metric === "cycling_w_per_kg") chart.querySelectorAll(".analysis-info-tooltip[data-series]").forEach((info) => {
    const item = entries[Number(info.dataset.series)];
    if (item?.power_method) info.append(h("p", "analysis-reference-note", `Methode: ${item.power_method}. FTP- und Gewichtsdatum sind in den Einzelwerten getrennt ausgewiesen.`));
  });
  return chart;
}

// The newest shown reading of a metric (``local`` only the athlete's own); on its day the
// athlete's own entry wins over a measurement, as in the current weight (#3252).
function bodyOverviewLatest(metric, body, preferred, local = false) {
  const readings = Object.values(body?.windows || {}).flatMap((window) => (window.metrics?.[metric] || [])
    .filter((item) => bodySourceShown(item, preferred[metric]))
    .flatMap((item) => (item.points || []).filter((point) => point.date >= window.start && point.date <= window.end && analysisValidPoint(point))
      // One series per metric (#3464): each day names its own source.
      .map((point) => {
        // A profile body fat is the athlete's own skinfold (#3569).
        const own = point.source_label ? ["eingetragen", "Hautfalten"].includes(point.source_label) : item.source_provider === null;
        return { ...point, local: own, source: own ? "Eingetragen" : item.source };
      })
      .filter((point) => !local || point.local)));
  readings.sort((a, b) => a.date.localeCompare(b.date) || Number(a.local) - Number(b.local)
    || (a.observed_at || a.date).localeCompare(b.observed_at || b.date));
  return readings.at(-1) || null;
}

// KFA (#3402): the newest of the folds' body fat and an entered percentage, the folds winning a
// shared day; a scale's or the profile's value is not KFA (#3336). Null while the folds load.
function bodyFoldFat(read, entered) {
  if (!read) return null;
  // A failed skinfold read still shows an entered percentage (Gitar, #3412).
  const newest = read.failed ? null : read.measurements?.[0];
  const fold = newest?.body_fat_pct == null ? null : newest;
  if (entered && !(fold && fold.date >= entered.date)) {
    const method = entered.source_label === "Hautfalten" ? "Eingetragen (Hautfalten)" : "Eingetragen";
    return { value: entered.value, note: `${method} · ${dateLabel(entered.date)}` };
  }
  if (fold) return { value: fold.body_fat_pct, note: `Eingetragen (Hautfalten) · ${dateLabel(fold.date)}` };
  return newest ? { text: "unbekannt", note: "ohne Geburtsjahr" } : null;
}

function bodyOverviewReading(metric, body, preferred) {
  if (metric === "body_fat_pct") {
    const own = bodyFoldFat(skinfoldRead, bodyOverviewLatest(metric, body, preferred, true));
    if (own) return own;
    // Nothing entered: the value the chart and the energy formulas use is shown with its source, as for the weight (#3930).
    const device = skinfoldRead ? bodyOverviewLatest(metric, body, preferred) : null;
    return device ? { value: device.value, note: `${device.source} · ${dateLabel(device.observed_at || device.date)}`, device: true } : { text: "—" };
  }
  const latest = bodyOverviewLatest(metric, body, preferred);
  return latest ? { value: latest.value, date: latest.date, note: `${latest.source} · ${dateLabel(latest.observed_at || latest.date)}` } : { text: "—" };
}

// A marked weigh-in step the server reads as probable water: a hint, no figure, only for the shown weigh-in (#4047).
const WATER_HINT_CAUSES = { salt: "Salz", carbs: "Kohlenhydrate" };

function waterHintText(hint, reading) {
  if (!hint || hint.date !== reading.date) return null;
  if (hint.direction === "down") return "Wahrscheinlich Wasser: gestern wenig Kohlenhydrate nach langer Einheit";
  const named = hint.causes.filter((cause) => WATER_HINT_CAUSES[cause]);
  if (named.length === 1) return `Wahrscheinlich Wasser: gestern ${named[0] === "carbs" ? "viele" : "viel"} ${WATER_HINT_CAUSES[named[0]]}`;
  return named.length ? "Wahrscheinlich Wasser: gestern viel Salz und Kohlenhydrate" : null;
}

function bodyOverviewRow(metric, title, unit, reading, edit) {
  return h("tr", { dataset: { metric } }, h("th", { scope: "row" }, title, reading.note && h("small", null, reading.note), reading.hint && h("small", { className: "water-hint" }, reading.hint)),
    h("td", null, reading.value == null ? reading.text : analysisValue(reading.value, unit)), h("td", null, edit));
}

// 3-site skinfolds (#3099) per the profile's sex. The read is kept until a save or a removal.
const SKINFOLD_SITES = { male: ["chest", "abdomen", "thigh"], female: ["triceps", "suprailiac", "thigh"] };
// The skinfold read's list length (backend/athlete/skinfolds.py LIST_LIMIT).
const SKINFOLD_READ_LIMIT = 20;
const SKINFOLD_LABELS = { chest: "Brust", abdomen: "Bauch", thigh: "Oberschenkel", triceps: "Trizeps", suprailiac: "Hüfte" };
// Jackson-Pollock density (a - b*S + c*S² - d*age) as backend/athlete/body_composition.py (M13).
const SKINFOLD_DENSITY = { male: [1.10938, 0.0008267, 0.0000016, 0.0002574], female: [1.0994921, 0.0009929, 0.0000023, 0.0001392] };
let skinfoldRead = null;
let skinfoldRequest = null;

// Forgets the read and the energy view built on it, so the next body render reads both again.
function resetSkinfolds() {
  skinfoldRead = null;
  resetEnergy();
}

// A failed read also re-renders, so the card shows the failure instead of loading forever.
function loadSkinfolds() {
  skinfoldRequest ||= api("/api/body/skinfolds")
    .then((read) => { skinfoldRead = read; })
    .catch(() => { skinfoldRead = { failed: true }; })
    .finally(() => {
      skinfoldRequest = null;
      renderBodyAnalysis(state.data?.performance?.history?.body);
    });
}

function skinfoldDialogSites() {
  return SKINFOLD_SITES[state.data?.profile?.sex === "female" ? "female" : "male"];
}

// Up to three readings per site; the site's value is their median (#3556).
const SKINFOLD_READINGS = 3;

function skinfoldInputs(form, site) {
  return Array.from({ length: SKINFOLD_READINGS }, (_, index) => form.elements[index ? `${site}_${index + 1}` : site]);
}

function skinfoldReadings(form, site) {
  return skinfoldInputs(form, site).map((input) => input.value.trim()).filter(Boolean);
}

function skinfoldMedian(readings) {
  const sorted = readings.map((value) => Number(value.replace(",", "."))).sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

// "Deine Messgenauigkeit: ± 0,9 %-Punkte (aus 24 Wiederholungen)" once the repeats suffice (#3556).
function skinfoldRepeatabilityText(read) {
  const found = read?.repeatability;
  if (found?.sd_pct == null) return "";
  return `Deine Messgenauigkeit: ± ${AppFormat.number(found.sd_pct, { digits: 1, minDigits: 1 })} %-Punkte (aus ${found.repeat_sites} Wiederholungen)`;
}

// Siri body fat (%) from the folds in mm, or null outside the accepted folds and ages.
function skinfoldBodyFat(sex, folds, age) {
  if (!(age >= 18 && age <= 80) || folds.some((mm) => !(mm >= 2 && mm <= 80))) return null;
  const total = folds.reduce((sum, mm) => sum + mm, 0);
  const [a, b, c, d] = SKINFOLD_DENSITY[sex];
  return Math.round((495 / (a - b * total + c * total ** 2 - d * age) - 450) * 10) / 10;
}

// Body fat (#3402): one dialog takes a percentage or the folds; a day with an entry of the
// shown mode fills it and offers its removal.
let bodyFatEntries = [];
let bodyFatMode = "pct";

function showSkinfoldResult() {
  const form = document.querySelector("#bodyFatDialogForm");
  const profile = state.data?.profile || {};
  const age = Number(form.elements.date.value.slice(0, 4)) - Number(profile.birth_year);
  const folds = skinfoldDialogSites().map((site) => skinfoldMedian(skinfoldReadings(form, site)));
  const sex = profile.sex === "female" ? "female" : "male";
  const fat = profile.birth_year ? skinfoldBodyFat(sex, folds, age) : null;
  const result = fat == null ? "" : `Ergibt ${analysisValue(fat, "%")} Körperfett`;
  document.querySelector("#bodyFatDialogResult").textContent = [result, skinfoldRepeatabilityText(skinfoldRead)].filter(Boolean).join(" · ");
}

function fillBodyFatDialog() {
  // While editing a queued entry the typed value stays and Löschen stays hidden (review on #3817).
  if (bodyFatOutboxEdit) return;
  const form = document.querySelector("#bodyFatDialogForm");
  const day = form.elements.date.value;
  const entry = bodyFatEntries.find((item) => item.date === day);
  const measured = skinfoldRead?.measurements?.find((item) => item.date === day);
  form.elements.pct.value = entry ? String(entry.pct) : "";
  const values = Object.fromEntries((measured?.folds_mm || []).map((fold) => [fold.site, fold.readings || [fold.mm]]));
  for (const site of skinfoldDialogSites()) skinfoldInputs(form, site).forEach((input, index) => { input.value = values[site]?.[index] ?? ""; });
  document.querySelector("#bodyFatDialogRemove").hidden = !(bodyFatMode === "folds" ? measured : entry);
  showSkinfoldResult();
}

// A hidden field is disabled too, so the form validates only the shown mode.
function setBodyFatMode(mode) {
  bodyFatMode = mode;
  const form = document.querySelector("#bodyFatDialogForm");
  for (const button of form.querySelectorAll("[data-body-fat-mode]")) button.setAttribute("aria-pressed", String(button.dataset.bodyFatMode === mode));
  for (const input of [form.elements.pct, ...skinfoldDialogSites().flatMap((site) => skinfoldInputs(form, site))]) {
    const shown = (input.name === "pct") === (mode === "pct");
    input.disabled = !shown;
    input.closest("label").hidden = !shown;
  }
  document.querySelector("#bodyFatDialogResult").hidden = mode !== "folds";
  fillBodyFatDialog();
}

// The queued entry the open dialog replaces (an edit from the outbox, #3817), else null.
let bodyFatOutboxEdit = null;

async function openBodyFatDialog(edit = null) {
  bindBodyFatDialog();
  bodyFatOutboxEdit = edit;
  const form = document.querySelector("#bodyFatDialogForm");
  // The fold fields sit in the dialog's own form grid, after the percentage, for the profile's sex.
  form.querySelectorAll("[data-skinfold-site]").forEach((label) => label.remove());
  // Each site takes up to three readings in one row; only the first is required (#3556).
  // The label names the first reading; the repeats carry their own name.
  const reading = (site, index) => h("input", { name: index ? `${site}_${index + 1}` : site, type: "number", min: "2", max: "80", step: "0.5", inputMode: "decimal", required: !index,
    ...(index ? { "aria-label": `${SKINFOLD_LABELS[site]}, Messung ${index + 1} (mm)` } : {}) });
  form.elements.pct.closest("label").after(...skinfoldDialogSites().map((site) => h("label", { dataset: { skinfoldSite: site } }, `${SKINFOLD_LABELS[site]} (mm)`,
    h("span", "skinfold-readings", Array.from({ length: SKINFOLD_READINGS }, (_, index) => reading(site, index))))));
  form.elements.date.value = edit?.payload.date || timezoneDateKey(state.data?.profile?.timezone, new Date());
  document.querySelector("#bodyFatDialogStatus").textContent = "";
  for (const button of form.querySelectorAll("button")) setSaving(button, false);
  // Read before opening, so a late answer never overwrites what the athlete typed.
  try { bodyFatEntries = (await api("/api/body/body-fat")).entries; } catch { bodyFatEntries = []; }
  setBodyFatMode("pct");
  // A queued percentage is edited as a percentage: no switch to folds, no removal of a stored day.
  document.querySelector("#bodyFatDialogMode").hidden = Boolean(edit);
  if (edit) {
    form.elements.pct.value = edit.payload.pct;
    document.querySelector("#bodyFatDialogRemove").hidden = true;
  }
  document.querySelector("#bodyFatDialog").showModal();
}

let bodyFatDialogBound = false;

// Bound on the first open, so loading the script touches no dialog.
function bindBodyFatDialog() {
  const form = document.querySelector("#bodyFatDialogForm");
  if (bodyFatDialogBound || !form) return;
  bodyFatDialogBound = true;
  form.elements.date.addEventListener("change", fillBodyFatDialog);
  form.addEventListener("input", (event) => { if (event.target.closest("[data-skinfold-site]")) showSkinfoldResult(); });
  for (const button of form.querySelectorAll("[data-body-fat-mode]")) button.addEventListener("click", () => setBodyFatMode(button.dataset.bodyFatMode));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const day = form.elements.date.value;
    const folds = bodyFatMode === "folds";
    const [path, payload] = folds
      ? ["/api/body/skinfolds", { date: day, folds_mm: Object.fromEntries(skinfoldDialogSites().map((site) => [site, skinfoldReadings(form, site)])) }]
      : ["/api/body/body-fat", { date: day, pct: form.elements.pct.value }];
    await sendBodyEntry("bodyFatDialog", path, payload, document.querySelector("#bodyFatDialogSave"), bodyFatReceipt(day, folds, "gespeichert"), bodyFatOutboxEdit);
  });
  const remove = document.querySelector("#bodyFatDialogRemove");
  remove.addEventListener("click", async () => {
    const day = form.elements.date.value;
    const folds = bodyFatMode === "folds";
    const [name, path] = folds ? ["Messung", "/api/body/skinfolds/remove"] : ["Körperfett", "/api/body/body-fat/remove"];
    if (!await requestConfirmation(`${name} vom ${dateLabel(day)} wird gelöscht.`, { title: `${name} löschen?`, danger: true, confirmLabel: "Löschen" })) return;
    await sendBodyEntry("bodyFatDialog", path, { date: day, confirmed: true }, remove, bodyFatReceipt(day, folds, "gelöscht"));
  });
  document.querySelector("#bodyFatDialogCancel").addEventListener("click", () => document.querySelector("#bodyFatDialog").close());
}

// The athlete's own weigh-ins (#3252): the dialog lists them to fill a day that has one.
let weightRead = null;

function fillWeightDialog() {
  // While editing a queued entry the typed value stays and Löschen stays hidden (review on #3817).
  if (weightOutboxEdit) return;
  const form = document.querySelector("#weightDialogForm");
  const entry = weightRead?.entries.find((item) => item.date === form.elements.date.value);
  form.elements.kg.value = entry ? String(entry.kg) : "";
  document.querySelector("#weightDialogRemove").hidden = !entry;
}

// The queued entry the open dialog replaces (an edit from the outbox, #3817), else null.
let weightOutboxEdit = null;

async function openWeightDialog(edit = null) {
  bindWeightDialog();
  weightOutboxEdit = edit;
  const form = document.querySelector("#weightDialogForm");
  form.elements.date.value = edit?.payload.date || timezoneDateKey(state.data?.profile?.timezone, new Date());
  document.querySelector("#weightDialogStatus").textContent = "";
  for (const button of form.querySelectorAll("button")) setSaving(button, false);
  // Read before opening, so a late answer never overwrites what the athlete typed (#3829).
  try { weightRead = await api("/api/body/weight"); } catch { weightRead = { entries: [] }; }
  fillWeightDialog();
  if (edit) {
    form.elements.kg.value = edit.payload.kg;
    document.querySelector("#weightDialogRemove").hidden = true;
  }
  document.querySelector("#weightDialog").showModal();
}

// The receipt of a body change (#3403): what was done, and the request that puts the day back as it
// was (the day's previous entry, else its removal); sendBodyEntry adds the text and the pushes.
function bodyReceipt(noun, done, undo) {
  return { noun, done, undo };
}

function weightReceipt(day, done) {
  const before = weightRead?.entries.find((item) => item.date === day);
  const undo = before ? ["/api/body/weight", { date: day, kg: before.kg }] : ["/api/body/weight/remove", { date: day, confirmed: true }];
  return bodyReceipt("Gewicht", done, undo);
}

function bodyFatReceipt(day, folds, done) {
  if (folds) {
    // Without the read the earlier measurement is unknown, so no undo is offered.
    if (!skinfoldRead?.measurements) return bodyReceipt("Messung", done, null);
    const before = skinfoldRead.measurements.find((item) => item.date === day);
    // The read lists at most 20 measurements: an older day's earlier state is unknown.
    if (!before && skinfoldRead.measurements.length >= SKINFOLD_READ_LIMIT) return bodyReceipt("Messung", done, null);
    const undo = before ? ["/api/body/skinfolds", { date: day, folds_mm: Object.fromEntries(before.folds_mm.map((fold) => [fold.site, fold.mm])) }] : ["/api/body/skinfolds/remove", { date: day, confirmed: true }];
    return bodyReceipt("Messung", done, undo);
  }
  const before = bodyFatEntries.find((item) => item.date === day);
  const undo = before ? ["/api/body/body-fat", { date: day, pct: before.pct }] : ["/api/body/body-fat/remove", { date: day, confirmed: true }];
  return bodyReceipt("Körperfett", done, undo);
}

// #4267 (option B): the save answers at once; the Garmin and Wahoo pushes run on and the receipt
// follows them through the sync poll for at most two minutes.
// A push's target key is its provider's name in lower case.
const bodyPushName = (key) => key[0].toUpperCase() + key.slice(1);
const BODY_PUSH_FOLLOW_MS = 120_000;
const GARMIN_PUSH_STATES = { running: "running", pending: "failed", needs_weight: "needs_weight", sent: "done", removed: "done", unchanged: "done" };
// How a finished Wahoo push ended (its diagnostics enum); a final refusal is not retried.
const WAHOO_PUSH_STATES = { sent: "done", unchanged: "done", scope_missing: "scope", http_4xx: "refused" };
let bodyPushFollows = [];

// Each push's state from the save's answer: running, done, failed or needs_weight; off is no push.
function bodyPushTargets(garmin, wahoo) {
  const targets = { garmin: GARMIN_PUSH_STATES[garmin], wahoo: { running: "running", pending: "failed" }[wahoo] };
  return Object.fromEntries(Object.entries(targets).filter(([, value]) => value));
}

function bodyPushText(targets, done) {
  const keys = Object.keys(targets);
  const names = (list) => list.map(bodyPushName).join(" und ");
  if (keys.some((key) => targets[key] === "running")) return `An ${names(keys)} senden…`;
  const saved = keys.filter((key) => targets[key] === "done");
  const verb = done === "gespeichert" ? "gespeichert" : "aktualisiert";
  return [
    saved.length ? `✓ In ${names(saved)} ${verb}` : "",
    ...keys.filter((key) => targets[key] === "failed").map((key) => `${bodyPushName(key)}-Sync fehlgeschlagen – wird erneut versucht.`),
    targets.garmin === "needs_weight" ? "Garmin braucht ein Gewicht am selben Tag." : "",
    targets.wahoo === "refused" ? "Wahoo-Sync fehlgeschlagen." : "",
    targets.wahoo === "scope" ? "Wahoo: Berechtigung fehlt – Wahoo neu verbinden." : "",
  ].filter(Boolean).join(" · ");
}

const bodyPushFailed = (targets) => Object.values(targets).some((value) => !["running", "done"].includes(value));

function renderBodyPushLine(line, targets, done) {
  const running = Object.values(targets).includes("running");
  line.classList.toggle("is-loading", running);
  line.replaceChildren(...(running ? [h("span", { className: "button-spinner", "aria-hidden": "true" })] : []), bodyPushText(targets, done));
}

// One followed receipt against a sync status asked for after its save: a push that stopped running
// takes its day's outcome (after a restart: still pending is failed, else done).
function settleBodyPush(follow, pushes) {
  const { garmin = {}, wahoo = {} } = pushes;
  if (follow.targets.garmin === "running" && !garmin.running?.includes(follow.day)) {
    const outcome = garmin.outcomes?.[follow.day] ?? (garmin.pending?.includes(follow.day) ? "pending" : "sent");
    follow.targets.garmin = GARMIN_PUSH_STATES[outcome];
    if (!follow.targets.garmin) delete follow.targets.garmin;
  }
  if (follow.targets.wahoo === "running" && !wahoo.running) follow.targets.wahoo = wahoo.pending ? "failed" : WAHOO_PUSH_STATES[wahoo.outcome] || "done";
}

// The sync poll's body pushes. A followed receipt ends done or failed; after two minutes its line
// turns neutral; a closed toast gets the end in a second one. A status asked for before a save
// (requestedAt) settles nothing of it. Returns whether to keep polling fast.
function followBodyPush(pushes, requestedAt = Date.now()) {
  const toastNode = document.querySelector("#toast");
  bodyPushFollows = bodyPushFollows.filter((follow) => {
    const shown = follow.line.isConnected && toastNode?.classList.contains("show");
    if (Date.now() > follow.until) {
      if (shown) { follow.line.classList.remove("is-loading"); follow.line.replaceChildren("Wird im Hintergrund weiter übertragen."); }
      return false;
    }
    if (!pushes || requestedAt < follow.since) return true;
    settleBodyPush(follow, pushes);
    if (Object.values(follow.targets).includes("running")) return true;
    const failed = bodyPushFailed(follow.targets);
    if (!shown) toast(bodyPushText(follow.targets, follow.done), failed);
    else {
      renderBodyPushLine(follow.line, follow.targets, follow.done);
      if (failed) toastNode.classList.add("error");
    }
    return false;
  });
  return bodyPushFollows.length > 0;
}

// The receipt stays in the toast with "Rückgängig" and the push line a little longer than a plain message.
function showBodyReceipt(receipt, day) {
  const targets = bodyPushTargets(receipt.garmin, receipt.wahoo);
  toast(receipt.text, bodyPushFailed(targets));
  const node = document.querySelector("#toast");
  if (receipt.undo) {
    const undo = h("button", { type: "button", className: "message-undo" }, "Rückgängig");
    undo.addEventListener("click", async () => {
      setSaving(undo, true);
      const [path, payload] = receipt.undo;
      try {
        const read = await api(path, { method: "PUT", body: JSON.stringify(payload) });
        await bodyEntryChanged();
        showBodyReceipt({ text: "Zurückgenommen.", done: "zurückgenommen", garmin: read?.remote, wahoo: read?.remote_wahoo }, day);
      } catch (error) {
        setSaving(undo, false);
        toast(error.message, true);
      }
    });
    node?.append(undo);
  }
  if (Object.keys(targets).length) {
    const line = h("span", "body-push");
    renderBodyPushLine(line, targets, receipt.done);
    node?.append(line);
    if (Object.values(targets).includes("running")) {
      const since = Date.now();
      bodyPushFollows.push({ day, targets, done: receipt.done, line, since, until: since + BODY_PUSH_FOLLOW_MS });
      scheduleSyncPoll(SYNC_POLL_ACTIVE_MS);
    }
  }
  if (!receipt.undo && !Object.keys(targets).length) return;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(hideToast, 10000);
}

// A saved or removed body value reaches the body values and the energy table on the next read.
async function bodyEntryChanged() {
  resetSkinfolds();
  renderDiaryEnergy();
  // The save stands; a failed re-read is reported outside the closed dialog.
  try {
    const result = await api("/api/performance");
    state.data.performance = result.performance || result;
    renderBodyAnalysis(state.data.performance?.history?.body);
  } catch (error) {
    toast(error.message, true);
  }
}

// The weight and the body fat save wait in the outbox when the server cannot be reached (#3761);
// a removal, a skinfold measurement and an undo do not.
const OUTBOX_BODY_ENTRIES = {
  "/api/body/weight": { kind: "weight", label: (payload) => `Gewicht ${AppFormat.number(payload.kg, { digits: 1 })} kg` },
  "/api/body/body-fat": { kind: "body-fat", label: (payload) => `Körperfett ${AppFormat.percent(payload.pct)}` },
};

async function sendBodyEntry(dialog, path, payload, button, receipt, replaces = null) {
  const status = document.querySelector(`#${dialog}Status`);
  setSaving(button, true);
  let read;
  try {
    const own = OUTBOX_BODY_ENTRIES[path];
    read = own
      ? await saveOwnEntry({ kind: own.kind, method: "PUT", path, payload, label: `${own.label(payload)} · ${dateLabel(payload.date)}`, replaces })
      : await api(path, { method: "PUT", body: JSON.stringify(payload) });
  } catch (error) {
    // The queued entry is gone meanwhile: the next save goes out as a new one (Gitar on #3830).
    if (error?.reason === "outbox_entry_gone") {
      if (dialog === "weightDialog") weightOutboxEdit = null;
      if (dialog === "bodyFatDialog") bodyFatOutboxEdit = null;
    }
    status.textContent = error.message;
    setSaving(button, false);
    return false;
  }
  document.querySelector(`#${dialog}`).close();
  setSaving(button, false);
  if (read?.outboxEntry) {
    toast(`${read.outboxEntry.label} wartet, noch nicht gesendet.`);
    return true;
  }
  await bodyEntryChanged();
  // A save names its value and day ("Gewicht 78,4 kg · 27.11. gespeichert."), a removal its kind.
  const own = OUTBOX_BODY_ENTRIES[path];
  const text = own ? `${own.label(payload)} · ${dateLabel(payload.date)} ${receipt.done}.` : `${receipt.noun} ${receipt.done}.`;
  showBodyReceipt({ ...receipt, text, garmin: read?.remote, wahoo: read?.remote_wahoo }, payload.date);
  return true;
}

let weightDialogBound = false;

function bindWeightDialog() {
  const form = document.querySelector("#weightDialogForm");
  if (weightDialogBound || !form) return;
  weightDialogBound = true;
  form.elements.date.addEventListener("change", fillWeightDialog);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const day = form.elements.date.value;
    await sendBodyEntry("weightDialog", "/api/body/weight", { date: day, kg: form.elements.kg.value }, document.querySelector("#weightDialogSave"), weightReceipt(day, "gespeichert"), weightOutboxEdit);
  });
  const remove = document.querySelector("#weightDialogRemove");
  remove.addEventListener("click", async () => {
    const day = form.elements.date.value;
    if (!await requestConfirmation(`Das Gewicht vom ${dateLabel(day)} wird gelöscht.`, { title: "Gewicht löschen?", danger: true, confirmLabel: "Löschen" })) return;
    await sendBodyEntry("weightDialog", "/api/body/weight/remove", { date: day, confirmed: true }, remove, weightReceipt(day, "gelöscht"));
  });
  document.querySelector("#weightDialogCancel").addEventListener("click", () => document.querySelector("#weightDialog").close());
}

function bodyEditButton(label, open) {
  const button = messageActionButton("edit", label);
  button.addEventListener("click", () => void open());
  return button;
}

// The energy goal (#3547) sits with the body values; the diary's "Offene Kalorien" price it.
const ENERGY_GOAL_NAMES = { lose: "Abnehmen", hold: "Halten", build: "Aufbauen" };
const ENERGY_GOAL_KCAL_LABELS = { lose: "Defizit pro Tag (kcal)", build: "Überschuss pro Tag (kcal)" };
// kcal per kg of body weight as the energy view prices it (#3618): the rule of thumb's 70 % fat at
// 9,441 and 30 % lean at 1,816 kcal/kg, or the read's blended share once the deficit card has one.
const PRIOR_KCAL_PER_KG = 7154;

function energyGoalText(profile) {
  const name = ENERGY_GOAL_NAMES[profile?.energy_goal];
  if (!name) return "nicht gesetzt";
  return profile.energy_goal === "hold" ? name : `${name}, ${profile.energy_goal_kcal} kcal/Tag`;
}

function energyGoalRow() {
  return h("tr", { id: "energyGoalRow", dataset: { metric: "energy_goal" } }, h("th", { scope: "row" }, "Energieziel"),
    h("td", null, energyGoalText(state.data?.profile)), h("td", null, bodyEditButton("Energieziel festlegen", openEnergyGoalDialog)));
}

// The kcal field names deficit or surplus, hides for "Halten", and shows its body weight per week.
function updateEnergyGoalDialog() {
  const form = document.querySelector("#energyGoalDialogForm");
  const goal = form.elements.goal.value;
  const kcal = Number(form.elements.kcal.value);
  form.elements.kcal.closest("label").hidden = goal === "hold";
  form.elements.kcal.required = goal !== "hold";
  document.querySelector("#energyGoalDialogKcalLabel").textContent = ENERGY_GOAL_KCAL_LABELS[goal] || "";
  const density = energyRead?.deficit?.kcal_per_kg || PRIOR_KCAL_PER_KG;
  const week = deficitDecimal(kcal * 7 / density);
  document.querySelector("#energyGoalDialogWeek").textContent = goal !== "hold" && kcal > 0 ? `≈ ${week} kg Körpergewicht pro Woche` : "";
}

let energyGoalDialogBound = false;

function bindEnergyGoalDialog() {
  const form = document.querySelector("#energyGoalDialogForm");
  if (energyGoalDialogBound || !form) return;
  energyGoalDialogBound = true;
  form.addEventListener("input", updateEnergyGoalDialog);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const goal = form.elements.goal.value;
    void saveEnergyGoal({ energy_goal: goal, energy_goal_kcal: goal === "hold" ? "" : form.elements.kcal.value }, document.querySelector("#energyGoalDialogSave"));
  });
  const remove = document.querySelector("#energyGoalDialogRemove");
  remove.addEventListener("click", () => void saveEnergyGoal({ energy_goal: "", energy_goal_kcal: "" }, remove));
  document.querySelector("#energyGoalDialogCancel").addEventListener("click", () => document.querySelector("#energyGoalDialog").close());
}

function openEnergyGoalDialog() {
  bindEnergyGoalDialog();
  const form = document.querySelector("#energyGoalDialogForm");
  const profile = state.data?.profile || {};
  form.elements.goal.value = ENERGY_GOAL_NAMES[profile.energy_goal] ? profile.energy_goal : "lose";
  form.elements.kcal.value = profile.energy_goal_kcal || "500";
  document.querySelector("#energyGoalDialogRemove").hidden = !ENERGY_GOAL_NAMES[profile.energy_goal];
  document.querySelector("#energyGoalDialogStatus").textContent = "";
  for (const button of form.querySelectorAll("button")) setSaving(button, false);
  updateEnergyGoalDialog();
  document.querySelector("#energyGoalDialog").showModal();
}

// The goal is a profile patch; the diary's energy read is priced again with it.
async function saveEnergyGoal(patch, button) {
  setSaving(button, true);
  try {
    const saved = await api("/api/profile", { method: "PUT", body: JSON.stringify(patch) });
    if (state.data) state.data.profile = saved;
    resetEnergy();
    document.querySelector("#energyGoalDialog").close();
    toast(patch.energy_goal ? "Energieziel gespeichert." : "Energieziel entfernt.");
    renderBodyAnalysis(state.data?.performance?.history?.body);
    renderDiaryEnergy();
  } catch (error) {
    document.querySelector("#energyGoalDialogStatus").textContent = error.message;
    setSaving(button, false);
  }
}

// "Offene Kalorien" (#3547, #3703) head the diary's energy card: the outlined source's day total
// with the goal, less what was eaten; without a goal a link to the goal row in Körper.
function energyGoalStrip(read) {
  const goal = read.goal;
  if (!goal) {
    // The dialog opens in place: the diary stays under it, and the card is priced again after the save (#3930).
    const link = h("button", { type: "button", className: "secondary-button" }, "Ziel festlegen");
    link.addEventListener("click", openEnergyGoalDialog);
    return h("p", { className: "energy-goal", dataset: { energyGoal: "none" } }, link);
  }
  if (goal.remaining_kcal == null) return h("p", { className: "energy-goal muted", dataset: { energyGoal: "pending" } }, "Noch kein Tagesverbrauch für dein Ziel");
  const kcal = (value) => `${formatWhole(value)} kcal`;
  const left = goal.remaining_kcal < 0 ? `${kcal(-goal.remaining_kcal)} über Ziel` : kcal(goal.remaining_kcal);
  const target = Math.max(goal.target_kcal, 1);
  const wording = { lose: "Defizit", build: "Überschuss" }[goal.goal];
  const aim = ["Ziel heute", formatWhole(goal.target_kcal), wording && `(${formatWhole(goal.goal_kcal)} ${wording})`].filter(Boolean).join(" ");
  return h("div", { className: "energy-goal", dataset: { energyGoal: goal.goal } },
    h("p", null, h("span", null, "Offene Kalorien"), h("strong", null, left)),
    h("progress", { max: String(target), value: String(Math.min(goal.eaten_kcal, target)), "aria-label": "Gegessen von deinem Ziel" }),
    h("ul", "energy-goal-items", h("li", null, `gegessen ${formatWhole(goal.eaten_kcal)}`), h("li", null, aim)));
}

const ENERGY_INSUFFICIENT = { intake_days: "unter 14 vollständige Ernährungstage", weighins: "unter 8 Wiegungen", weight_span: "Wiegungen über weniger als 21 Tage", logged_span: "erfasste Tage über weniger als 21 Tage" };
const ENERGY_STEP_SOURCES = { walks: "aus Gehen", height: "aus Körpergröße" };
let energyRead = null;
let energyRequest = null;
let energyGeneration = 0;
// The diary day the read holds (#3395); a picked other day reads again.
let energyReadDay = null;
let energyReadPeriod = null;

function energyDay() {
  return document.querySelector("#nutritionDate")?.value || "";
}

// A reset also drops the read in flight, so its answer from before a skinfold save is not shown (Gitar, #3180).
function resetEnergy() {
  energyRead = null;
  energyRequest = null;
  energyGeneration += 1;
}

// Only Verlauf's 6 and 12 Monate ask for `period=182d` or `period=365d`, whose own read no other request pays for (#3711).
function loadEnergy(day = energyDay(), period = null) {
  if (energyRequest) return;
  const generation = energyGeneration;
  energyReadDay = day;
  energyReadPeriod = period;
  const query = [day && `date=${day}`, period && `period=${period}`].filter(Boolean).join("&");
  energyRequest = api(query ? `/api/body/energy?${query}` : "/api/body/energy")
    .then((read) => { if (generation === energyGeneration) energyRead = read; })
    .catch(() => { if (generation === energyGeneration) energyRead = { failed: true }; })
    .finally(() => {
      if (generation !== energyGeneration) return;
      energyRequest = null;
      renderBodyAnalysis(state.data?.performance?.history?.body);
      renderDiaryEnergy();
      renderNutritionFit();
    });
}

// A band shows as its low and high end (#3478); a kcal value is never negative, so the low end stops at 0.
function energyKcalRange(value, band) {
  return [Math.max(0, value - band), value + band].map((kcal) => formatWhole(kcal));
}

// The span "low–high" on its own line under the estimate, read out as "zwischen … und … kcal".
function energyKcalSpan(value, band) {
  const [low, high] = energyKcalRange(value, band);
  return h("small", "energy-span", h("span", { "aria-hidden": "true" }, `${low}–${high}`), h("span", "sr-only", `zwischen ${low} und ${high} kcal`));
}

function energyKcal(value, band) {
  if (value == null) return "–";
  return band == null ? formatWhole(value) : h("span", null, formatWhole(value), energyKcalSpan(value, band));
}

// NEAT's inputs (#3255), so an outlier in steps or step length can be checked.
function energyNeatInputs(neat) {
  if (!neat) return "";
  const parts = [`Schritte & Treppen aus ${formatWhole(neat.steps_per_day)} Schritten/Tag, davon ${formatWhole(neat.session_steps_per_day)} in Einheiten abgezogen`];
  if (neat.step_length_m != null) parts.push(`Schrittlänge ${analysisValue(neat.step_length_m, "m")} (${ENERGY_STEP_SOURCES[neat.step_length_source] || neat.step_length_source})`);
  if (neat.floors_per_day != null) parts.push(`${analysisValue(neat.floors_per_day, "Etagen")} ohne Einheiten`);
  return parts.join(", ");
}

// The formulas' body fat: skinfold, else entered, else a device or scale (#3362, #3640).
function energyBodyText(body) {
  if (!body) return "Ohne Körperfettwert der letzten 90 Tage keine Formelwerte";
  const source = body.date ? `${body.label} vom ${dateLabel(body.date)}` : body.label;
  return `KFA ${analysisValue(body.body_fat_pct, "%")} (${source}), fettfreie Masse ${analysisValue(body.lean_mass_kg, "kg")}`;
}

// How the values come about sits behind one small "i": the basis, the formulas and NEAT's inputs
// (athlete decisions 2026-11-16, #3252 and #3255); no helper text stays under the table.
let energyInfoId = 0;
// The bias of meals flagged as estimated, per entry (#3195, M12's allowed wording), once it exists.
function energyBiasText(flagged) {
  if (flagged?.bias_pct == null) return "";
  const direction = flagged.bias_pct >= 0 ? "höher" : "niedriger";
  return `Geschätzte Mahlzeiten wirken im Mittel ${formatWhole(Math.abs(flagged.bias_pct))} % ${direction} als erfasst (± ${formatWhole(flagged.bias_band_pct)} %)`;
}

const ENERGY_DAY_NOTE = "Verbrauch des gewählten Tages, heute bis jetzt, in kcal/Tag, der Quelle, die dein Gewicht am besten trifft (Vergleich im Verlauf); Sport 0 ohne Einheit, Verdauung nur mit erfassten Mahlzeiten; Offene Kalorien = Tag gesamt − Gegessenes; das Defizit des Zeitraums steht im Verlauf";

// The same small info icon as the chart groups (Analyse › Laufen › Entwicklung, #3336).
function energyInfoButton(label, text) {
  const button = h("button", { type: "button", className: "analysis-legend-info analysis-info-icon", "aria-label": label }, "i");
  const info = h("div", { className: "analysis-info-tooltip", id: `energy-info-${++energyInfoId}` }, text);
  bindAnalysisPopover(button, info);
  return [button, info];
}

// What the numbers are, where they come from and how sure they are, one short line each (athlete decision, #3930).
function energyInfoItems(read) {
  const { basis, body } = read;
  const data = basis.insufficient ? `Datenbasis zu gering (${ENERGY_INSUFFICIENT[basis.insufficient] || basis.insufficient})` : `${basis.complete_days} von ${basis.days} Tagen vollständig`;
  const activity = [energyNeatInputs(basis?.neat), energyStandNote(read.wellness)].filter(Boolean).join(" · ");
  const uncertainty = ["Bänder nominal 95 %, ohne Fehler beim Erfassen des Essens",
    "Ruhe aus deinen Daten ist ein Rest (Gesamt − eigene Aktivität − Verdauung) und trägt deren Fehler mit",
    energyTefNote(read), energyBiasText(read.flagged)].filter(Boolean).join(" · ");
  return [["Was", ENERGY_DAY_NOTE], ["Daten", `Aus deinen Daten: ${data}`],
    ["Quelle", `Formeln: Ruhe + eigene Aktivität + Verdauung · ${energyBodyText(body)}`], ["Aktivität", activity], ["Unsicherheit", uncertainty]]
    .filter(([, text]) => text);
}

function energyInfo(read) {
  const list = h("ul", "energy-info-list", energyInfoItems(read).map(([what, text]) => h("li", null, h("strong", null, `${what}: `), text)));
  return energyInfoButton("Energieverbrauch: Informationen", list);
}

// Split A (athlete decision 2026-11-16): one stacked bar for the outlined source, a row per part.
const ENERGY_PARTS = [["resting_kcal", "resting", "Ruhe"], ["neat_kcal", "neat", "Schritte & Treppen"], ["sport_kcal", "sport", "Sport"], ["tef_kcal", "tef", "Verdauung"]];

// A strength session's MET follows its session RPE (athlete decision, #3530); the formula rows name both.
function energyStrengthNote(strength) {
  return (strength || []).map(({ rpe, met }) => {
    const rating = rpe == null ? "ohne RPE" : "RPE " + formatWhole(rpe);
    return `Kraft ${rating}, MET ${met.toLocaleString("de-DE")}`;
  }).join("; ");
}

// A formula row's learned term follows its Schritte & Treppen (#3577), "vorläufig" until the
// athlete takes over a qualified term (#3723).
const ENERGY_LEARNED = ["learned_kcal", "learned", "Übriger Alltag"];

function energyRowParts(split) {
  const [resting, neat, ...rest] = ENERGY_PARTS;
  if (split?.learned_kcal == null) return [resting, neat, ...rest];
  const [key, part, label] = ENERGY_LEARNED;
  return [resting, neat, [key, part, split.learned_provisional ? `${label} (vorläufig)` : label], ...rest];
}

// The learned term under the card (#3577): offered with "Übernehmen" until the athlete
// takes them over (a profile date, receipt and undo); afterwards the date. "Noch nicht" stores nothing.
const ENERGY_LEARNED_FORMULAS = [["katch_mcardle", "Katch-McArdle"], ["cunningham", "Cunningham"]];
let energyLearnedDeferred = false;

function energyLearnedLine(read) {
  const learned = read.learned_everyday;
  if (!learned) return null;
  const fits = ENERGY_LEARNED_FORMULAS.filter(([key]) => learned[key]);
  const head = h("strong", null, "Übriger Alltag");
  // #3723: without an estimate it is not learned and counts 0; with one, the row shows it
  // "vorläufig", so the line has nothing to add until a term qualifies (#3689).
  if (!fits.length) return h("div", { className: "energy-learned", dataset: { learned: "pending" } }, h("p", null, head, " noch nicht gelernt"));
  if (!fits.some(([key]) => learned[key].qualified)) return null;
  const windows = Math.max(...fits.map(([key]) => learned[key].windows));
  if (learned.checked) {
    return h("div", { className: "energy-learned", dataset: { learned: "checked" } }, h("p", null, head, ` übernommen am ${dateLabel(learned.checked)}`, h("br"),
      h("small", null, `aus ${windows} Fenstern, wird mit jedem Fenster genauer`)));
  }
  // A term may be negative, a reduction (#3689), so its span is not clamped at 0.
  const span = ({ kcal, band_kcal: band }) => [kcal - band, kcal + band].map((value) => formatWhole(value)).join("–");
  const values = fits.map(([key, label], index) => `${label} ${energyFitSigned(learned[key].kcal)}${index ? "" : " kcal/Tag"} (${span(learned[key])})`).join(" · ");
  const line = h("div", { className: "energy-learned", dataset: { learned: "offered" } }, h("p", null, head, ` aus ${windows} Fenstern`, h("br"), h("small", null, values)));
  if (energyLearnedDeferred) return line;
  const take = h("button", { type: "button", className: "secondary-button" }, "Übernehmen");
  const later = h("button", { type: "button", className: "secondary-button" }, "Noch nicht");
  take.addEventListener("click", () => void saveLearnedCheck(todayIso(), take));
  later.addEventListener("click", () => {
    energyLearnedDeferred = true;
    // The shown read is kept by renderDiaryEnergy; the card must be built again (Gitar).
    const slot = document.querySelector("#nutritionEnergy");
    if (slot) slot.energyShown = null;
    renderDiaryEnergy();
  });
  line.append(h("div", "actions", take, later));
  return line;
}

// The check is a profile patch with a receipt and undo; the diary's energy read is priced again with it.
async function saveLearnedCheck(day, button) {
  setSaving(button, true);
  try {
    const saved = await api("/api/profile", { method: "PUT", body: JSON.stringify({ learned_everyday_checked: day }) });
    if (state.data) state.data.profile = saved;
    resetEnergy();
    renderDiaryEnergy();
    toast(day ? "Übriger Alltag übernommen." : "Übriger Alltag zurückgenommen.");
    if (!day) return;
    const undo = h("button", { type: "button", className: "message-undo" }, "Rückgängig");
    undo.addEventListener("click", () => void saveLearnedCheck("", undo));
    document.querySelector("#toast")?.append(undo);
    clearTimeout(toast.timer);
    toast.timer = setTimeout(hideToast, 10000);
  } catch (error) {
    setSaving(button, false);
    toast(error.message, true);
  }
}

// Garmin's day summary ends early (#3530): the Schritte & Treppen come from it, so the "i" names
// the time, and that a session after it is not in it yet; the row shows the number only (#3719).
function energyStandNote(stand) {
  if (!stand?.as_of) return "";
  return `Schritte & Treppen nach dem letzten Garmin-Abruf, Stand ${stand.as_of}${stand.behind ? ", Garmin hängt hinterher" : ""}`;
}

// Digestion of kcal without macros is estimated (#3617); the "i" says so, not the row (#3719).
function energyTefNote(read) {
  return read.tef_estimated && read.outlined !== "device" ? "Verdauung geschätzt, wo Mahlzeiten keine Makros haben" : "";
}

// One part's value with the strength session's note on the formula rows (#3530).
function energyPartText(read, row, [key, part]) {
  const split = row.split;
  const strength = row.source !== "device" ? energyStrengthNote(read.strength) : "";
  const pending = row.pending_parts?.includes(part);
  const notes = { sport_kcal: strength ? ` (${strength})` : "" };
  // The learned term may be negative, a reduction (#3689).
  const value = key === "learned_kcal" ? energyFitSigned(split[key]) : energySplitValue(key, split[key], row.source, pending);
  return `${value}${notes[key] || ""}`;
}

function energySplitBar(split, parts, label) {
  // Only drawn parts spread the bar: a reduction (a negative learned term) has no segment (#3784).
  const total = parts.reduce((sum, [key]) => sum + Math.max(0, split[key] || 0), 0);
  const bar = h("div", { className: "energy-split", role: "img", "aria-label": label });
  for (const [key, part] of parts) {
    if (split[key] == null || split[key] <= 0) continue;
    const segment = h("span", { dataset: { part } });
    segment.style.width = `${(split[key] / total) * 100}%`;
    bar.append(segment);
  }
  return bar;
}

// The formula's name, the device row's label from the read (#3323).
function energySourceLine(row) {
  const name = FIT_SOURCE_NAMES[row.source] || row.label || row.source;
  return h("small", { className: "energy-source", dataset: { energySource: row.source } }, row.split.learned_kcal == null ? `Quelle: ${name}` : `Quelle: ${name} mit übrigem Alltag`);
}

// The diary's "Verbrauch" (#3703): the outlined source only, as a stacked bar and a row per part,
// then the day total with its band; the comparison of all sources is Verlauf's.
function energyUsage(read) {
  const row = read.rows.find((item) => item.source === read.outlined);
  const head = h("div", "energy-usage-head", h("h3", null, read.day_partial ? "Verbrauch heute (Stand jetzt)" : "Verbrauch"), ...energyInfo(read));
  if (!row?.split) return h("div", { className: "energy-usage" }, head, h("p", "muted", "Noch keine Quelle"));
  const parts = energyRowParts(row.split);
  const texts = parts.map((part) => energyPartText(read, row, part));
  const items = parts.map(([, part, label], index) => h("li", { dataset: { part } }, h("span", null, label), h("span", null, texts[index])));
  // A part whose data has not arrived says so in its row and draws no segment (#3784).
  const drawn = parts.filter(([, part]) => !row.pending_parts?.includes(part));
  const bar = energySplitBar(row.split, drawn, parts.map(([, , label], index) => `${label} ${texts[index]}`).join(" · "));
  const total = h("li", { className: "energy-total" }, h("span", null, "Tag gesamt"), h("span", null, energyKcal(row.total_kcal, row.total_band_kcal)));
  // Today's total with the usual steps and digestion and the planned units (#3917); it only informs.
  const expected = read.day_partial && read.expected_total_kcal != null
    ? [h("li", { className: "energy-expected" }, h("span", null, "Tag erwartet"), h("span", null, `≈ ${formatWhole(read.expected_total_kcal)}`))] : [];
  return h("div", { className: "energy-usage" }, head, bar, h("ul", "energy-parts", ...items, total, ...expected), energySourceLine(row), ...energyGuardNotes(read));
}

// Energy availability and the weight's loss rate, shown only past their line (#3917); neither blocks anything.
function energyGuardNotes({ energy_availability: availability, loss_rate: loss }) {
  const notes = [];
  if (availability?.below) notes.push(`Energieverfügbarkeit (7 Tage) ${formatWhole(availability.kcal_per_kg_ffm)} kcal/kg FFM · unter ${formatWhole(availability.floor_kcal_per_kg_ffm)}`);
  if (loss?.fast) notes.push(`Gewicht fällt ${AppFormat.number(loss.pct_per_week, { digits: 2 })} %/Woche · schneller als ${formatWhole(loss.limit_pct_per_week)} %`);
  return notes.map((text) => h("small", { className: "energy-note", dataset: { energyNote: "" } }, text));
}

// Digestion comes from the logged meals: without any it is not recorded; Garmin has no such part.
// Today a part whose input has not arrived counts 0 in Tag gesamt and says so (#3494).
function energySplitValue(key, value, source, pending = false) {
  if (pending) return "noch keine Daten";
  if (value != null) return formatWhole(value);
  return key === "tef_kcal" && source !== "device" ? "nicht erfasst" : "–";
}

// The deficit card names how its weight change split into fat and lean mass: the rule of thumb
// blended with the measurements, and their weight in it (athlete decision, #3555).
// The prior follows Forbes' curve at the current fat mass, else the rule of thumb (#3684).
function energyFatSharePrior(share) {
  return share?.prior_source === "forbes" ? "Forbes" : "Faustregel";
}

function energyFatShareRule(share) {
  const prior = `${energyFatSharePrior(share)} ${share.prior_pct} %`;
  return share.measured_pct > 0 ? `${prior}, zu ${share.measured_pct} % gemessen` : prior;
}

// Verlauf's period (athlete decisions, #3704, #3839) is a row of the shared table: the label of the button
// and of every card's title row, the days of the week read.
function verlaufPeriod(key = nutritionHistoryPeriod) {
  return analysisPeriod(key);
}

// The one title row of every Verlauf card: the title (and its "i") left, the period small and muted right.
function verlaufHead(title, period, info = []) {
  return h("div", "verlauf-head", h("h3", null, title), ...info, h("span", "verlauf-period", period));
}

// The distance of a balance per day to the energy goal, as the deficit card's (#3606): within 150 kcal
// "near", within 350 "mid", else "far"; null without a goal or a balance.
function verlaufGoalDistance(balance, goal) {
  if (!goal || balance == null) return null;
  const gap = (goal.goal === "build" ? balance : -balance) - goal.goal_kcal;
  if (Math.abs(gap) <= 150) return "near";
  return Math.abs(gap) <= 350 ? "mid" : "far";
}

// "Dein Defizit" (#3548) replaces Verlauf's 28-day own row, now for the selected period (#3704): the
// energy per day from the body stores as the hero, the goal marker, the boxes weight and body fat →
// fat → kcal per day, and a short text (the athlete asked for it); the flagged-meal rows are in its "i".
const DEFICIT_UNCERTAINTY = { fat_share: "Fettanteil (Faustregel und Messungen)", weight_scatter: "Schwankungen der Wiegungen", unstable: "Wasser im Gewicht, die Wochen widersprechen sich" };
const DEFICIT_GOAL_STATUS = { in: "✓ im Ziel", under: "unter Ziel", over: "über Ziel" };

function deficitDecimal(value) {
  return AppFormat.number(value, { digits: 1, minDigits: 1 });
}

function deficitSigned(value) {
  const sign = { 1: "+", [-1]: "−" }[Math.sign(value)] || "";
  return `${sign}${deficitDecimal(Math.abs(value))}`;
}

// The hero's 95 % span is signed, not clamped at 0: a deficit's span may reach into a surplus
// (Gitar on #3562), then it reads "−135 bis 335".
function deficitSpan(card) {
  const value = Math.abs(card.deficit_kcal);
  const [low, high] = [value - card.band_kcal, value + card.band_kcal];
  const text = (kcal) => `${kcal < 0 ? "−" : ""}${formatWhole(Math.abs(kcal))}`;
  const shown = low < 0 ? `${text(low)} bis ${text(high)}` : `${text(low)}–${text(high)}`;
  return h("small", "energy-span", h("span", { "aria-hidden": "true" }, shown), h("span", "sr-only", `zwischen ${text(low)} und ${text(high)} kcal`));
}

// The deficit's span with the estimate and, with a goal, its marker on one bar.
function deficitGoalBar(card) {
  const goal = card.goal;
  if (!goal) return [];
  const sign = goal.goal === "build" ? -1 : 1;
  const target = sign * goal.goal_kcal;
  const low = card.deficit_kcal - card.band_kcal;
  const high = card.deficit_kcal + card.band_kcal;
  const min = Math.min(low, target) - 100;
  const max = Math.max(high, target) + 100;
  const at = (value) => `${((value - min) / (max - min)) * 100}%`;
  const mark = (className, left, width) => h("span", { className, style: width ? { left, width } : { left } });
  const noun = goal.goal === "build" ? "Überschuss" : "Defizit";
  const label = goal.goal === "hold" ? "Ziel: Halten" : `Ziel: ${formatWhole(goal.goal_kcal)} kcal ${noun}`;
  return [h("div", { className: "energy-deficit-bar", "aria-hidden": "true" },
    mark("energy-deficit-band", at(low), `calc(${at(high)} - ${at(low)})`),
    mark("energy-deficit-mark", at(card.deficit_kcal)),
    mark("energy-deficit-goal", at(target))),
  h("p", { className: "energy-deficit-status", dataset: { goalStatus: goal.status } }, `${label} · ${DEFICIT_GOAL_STATUS[goal.status]}`)];
}

// A day as "07.10.", for the body fat readings' span (#3598).
function deficitDay(iso) {
  return iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.` : "";
}

// "22.10. → 18.11." for a box's span, or null without both days.
function deficitSpanDays(start, end) {
  return start && end ? `${deficitDay(start)} → ${deficitDay(end)}` : null;
}

// The Körperfett box, built like the Gewicht box (athlete decision, #3704): the change, from → to,
// and the readings' own days; only readings of the period are compared (#4037).
function deficitFatBox(card) {
  if (card.fat_pct_start == null) {
    // No reading of the period: the last one stands alone, named, not compared (#4037).
    if (card.fat_last_only && card.fat_pct_end != null) return ["–", `${deficitDecimal(card.fat_pct_end)} %`, `Letzte Messung ${deficitDay(card.fat_date_end)}`];
    return ["–", card.fat_pct_end == null ? "keine Messung" : "nur eine Messung", card.fat_date_end ? `vom ${deficitDay(card.fat_date_end)}` : null];
  }
  const change = Math.round((card.fat_pct_end - card.fat_pct_start) * 10) / 10;
  return [`${deficitSigned(change)} %-Pkt.`, `${deficitDecimal(card.fat_pct_start)} → ${deficitDecimal(card.fat_pct_end)} %`, deficitSpanDays(card.fat_date_start, card.fat_date_end)];
}

// The weight's end is the trend line's; it says "aktuell" when it differs from the chart's last
// weigh-in at the shown precision.
function deficitBoxes(card) {
  // The fitted change, which the fat and kcal boxes take too, not the rounded ends' (#3618).
  const change = card.weight_change_kg;
  // Edge means (the first and last 7 days' weigh-ins, 14 for 3 and 6 months) are marked "Ø" (#3739).
  const mean = card.edge_weighins ? "Ø " : "";
  const current = !mean && card.weight_last_kg != null && deficitDecimal(card.weight_last_kg) !== deficitDecimal(card.weight_end_kg) ? " (aktuell)" : "";
  const box = (kind, title, value, ...notes) => h("li", { dataset: { deficitBox: kind } }, h("small", null, title), h("strong", null, value), ...notes.filter(Boolean).map((note) => h("small", null, note)));
  return h("ol", "energy-deficit-flow",
    box("weight", "Gewicht", `${deficitSigned(change)} kg`, `${mean}${deficitDecimal(card.weight_start_kg)} → ${mean}${deficitDecimal(card.weight_end_kg)} kg${current}`, deficitSpanDays(card.weight_date_start, card.weight_date_end)),
    box("body_fat", "Körperfett", ...deficitFatBox(card)),
    box("fat", "davon Fett", `≈ ${deficitDecimal(Math.abs(card.fat_change_kg))} kg`, card.fat_share ? `≈ ${card.fat_share.share_pct} % · ${energyFatShareRule(card.fat_share)}` : null),
    box("kcal", "pro Tag", `≈ ${formatWhole(Math.abs(card.deficit_kcal))} kcal`, `${formatWhole(Math.abs(card.stored_kcal))} kcal ÷ ${card.span_days} Tage`));
}

// The athlete's words: what changed, what it means per day, and the real expenditure.
// The two facts the boxes lack (athlete decision, #3606): the real expenditure, with the device's
// beside it, and the main source of the uncertainty.
function deficitText(card, deviceLabel) {
  const device = card.device_total_kcal == null ? "" : ` (${deviceLabel || "Gerät"}: ${formatWhole(card.device_total_kcal)})`;
  // The weigh-ins carry the card; the expenditure also needs logged meals ("Nur Waage", #3603).
  // An unstable window is an estimate, not the real expenditure (#3614).
  const kind = card.uncertainty === "unstable" ? "Geschätzter" : "Echter";
  const spent = card.total_kcal == null ? "Echter Verbrauch: braucht erfasste Mahlzeiten." : `${kind} Verbrauch: ≈ ${formatWhole(card.total_kcal)} kcal am Tag${device}.`;
  // The fat share names its prior's source as the box does (#3684).
  let cause = DEFICIT_UNCERTAINTY[card.uncertainty] || card.uncertainty;
  if (card.uncertainty === "fat_share") cause = `Fettanteil (${energyFatSharePrior(card.fat_share)} und Messungen)`;
  return [spent, `Unsicher vor allem: ${cause}.`];
}

// Without logged meals, the intake from weight and activity (#3604): an estimate, never "gegessen"
// as logged, with its span and the last logged window it was calibrated on, or why there is none.
const ESTIMATE_MISSING = {
  calibration: "in den letzten 12 Wochen keine 4 Wochen mit genug erfassten Tagen und Wiegungen",
  calibration_age: "letzte Erfassung zu lange her",
  weight_change: "Gewicht seitdem zu stark verändert",
  activity_change: "Training seitdem zu stark verändert",
  body: "Körperfett fehlt",
  activity: "Aktivität fehlt",
};

function deficitIntake(estimate) {
  if (!estimate) return null;
  const since = estimate.calibration_end ? ` (erfasst bis ${deficitDay(estimate.calibration_end)})` : "";
  if (estimate.kcal == null) return `Geschätzte Aufnahme: keine Schätzung, ${ESTIMATE_MISSING[estimate.missing] || estimate.missing}${since}.`;
  const [low, high] = [estimate.kcal - estimate.band_kcal, estimate.kcal + estimate.band_kcal];
  return `Geschätzte Aufnahme aus Gewicht und Aktivität: ≈ ${formatWhole(estimate.kcal)} kcal am Tag (${formatWhole(low)}–${formatWhole(high)})${since}.`;
}

// The card of Verlauf's selected period (#3704); the flagged-meal rows of the latest 28 days sit
// behind its "i".
function energyDeficitCard(read, key) {
  const card = read?.fit_scale?.[key]?.deficit;
  if (!card) return null;
  const surplus = card.deficit_kcal < 0;
  const deviceLabel = read.rows?.find((row) => row.source === "device")?.label;
  const flagged = energyFlaggedText(read.flagged);
  const [spent, unsure] = deficitText(card, deviceLabel);
  const intake = deficitIntake(read.estimated_intake);
  // The goal's distance class colours the number and the mark (#3606); without a goal they keep theirs.
  const dataset = { energy: "", energyDeficit: card.method, ...(card.goal ? { goalDistance: card.goal.distance } : {}) };
  return h("section", { className: "analysis-chart-card", dataset },
    verlaufHead("Defizit aus Gewicht und Körperfett", verlaufPeriod(key).label, flagged ? energyInfoButton("Verbrauch: Informationen", flagged) : []),
    h("p", "energy-deficit-hero", h("strong", null, `≈ ${formatWhole(Math.abs(card.deficit_kcal))} kcal`),
      h("small", null, surplus ? "pro Tag in die Speicher" : "pro Tag aus den Reserven"),
      deficitSpan(card)),
    ...deficitGoalBar(card), deficitBoxes(card),
    h("p", { className: "energy-deficit-text" }, spent),
    intake ? h("p", { className: "energy-deficit-text", dataset: { estimatedIntake: read.estimated_intake.missing || "" } }, intake) : null,
    h("small", "energy-deficit-uncertainty", unsure));
}

// "2.700 (2.500–2.900) kcal" for the text of an "i".
function energyKcalText(total) {
  const [low, high] = energyKcalRange(total.total_kcal, total.total_band_kcal ?? 0);
  return total.total_band_kcal == null ? `${formatWhole(total.total_kcal)} kcal` : `${formatWhole(total.total_kcal)} kcal (${low}–${high})`;
}

// With meals flagged as estimated, the own total without them (only clean windows, #3187 item 14)
// and with the learned correction of the latest 28 days (athlete decision on #3100) sit behind the
// deficit card's "i" (#3704); "" without such meals.
function energyFlaggedText(flagged) {
  if ((flagged?.days ?? 0) <= 0) return "";
  const unflagged = flagged.unflagged;
  // The clean window is an older one than the 28 days beside it, so it names its last day (#3618).
  const label = unflagged?.end ? `ohne geschätzte Tage (bis ${deficitDay(unflagged.end)})` : "ohne geschätzte Tage";
  // Until the bias is narrow enough and seen in independent windows it is not learned (#3616).
  // A learned bias without the latest window's total (too few logged days) shows "–" (Gitar on #3634).
  let corrected = flagged.bias_pct == null ? "noch nicht gelernt" : "–";
  if (flagged.corrected) corrected = energyKcalText(flagged.corrected);
  const clean = unflagged ? energyKcalText(unflagged) : "kein sauberer Zeitraum";
  return `Verbrauch, Ø 28 Tage, ${label}: ${clean} · mit gelernter Korrektur: ${corrected}`;
}

// The read's loading or failure card, or null once it is there; the first call starts the read.
// The diary asks for its day (#3395); Körper's fit does not depend on the day (`null`), so it takes
// any read there is and never shows a diary day's note (Gitar #3423).
// While Verlauf shows 6 or 12 Monate, every read asks for them, so a diary day's read serves both and
// neither resets the other's (Gitar, #3712).
function longPeriodShown() {
  return document.querySelector("#nutritionFitScale") && verlaufPeriod().days > BASE_PERIOD_DAYS ? verlaufPeriod().key : null;
}

function energyPending(day = null, period = null) {
  if (day && day > timezoneDateKey(state.data?.profile?.timezone, new Date())) {
    return h("section", "analysis-chart-card", h("p", "empty", "Tag liegt in der Zukunft"));
  }
  if (energyRead && day !== null && energyReadDay !== day) resetEnergy();
  if (energyRead && period && energyReadPeriod !== period) resetEnergy();
  if (!energyRead) {
    loadEnergy(day ?? "", period ?? longPeriodShown());
    return h("section", "analysis-chart-card", createLoadingState("Energieverbrauch wird geladen…"));
  }
  return energyRead.failed ? h("section", "analysis-chart-card", h("p", "empty", "Energieverbrauch konnte nicht geladen werden.")) : null;
}

// The card sits in the diary in place of the former Garmin expenditure card (#3252).
function energyDiaryCards() {
  const pending = energyPending(energyDay());
  if (pending) return [pending];
  return [h("section", { className: "analysis-chart-card", dataset: { energy: "" } }, energyGoalStrip(energyRead), energyUsage(energyRead), energyLearnedLine(energyRead))];
}

// "Welche Quelle trifft dein Gewicht?" (A2, athlete decision on #3397): one kcal/day scale for
// Verlauf's period; the own estimate as a mark with its green band, each source as a dot labelled
// with its value and signed distance, nearest first, under a verdict and one note line.
const FIT_SOURCE_NAMES = { katch_mcardle: "Katch-McArdle", cunningham: "Cunningham" };
const FIT_COLORS = { katch_mcardle: 1, cunningham: 2, device: 3 };

function energyFitSources(scale) {
  const own = scale.own_kcal;
  return scale.sources.filter((item) => item.total_kcal != null).map((item) => ({
    ...item,
    name: item.label || FIT_SOURCE_NAMES[item.source] || item.source,
    diff: own == null ? null : item.total_kcal - own,
  })).sort((a, b) => (a.diff == null ? a.total_kcal - b.total_kcal : Math.abs(a.diff) - Math.abs(b.diff)));
}

function energyFitSigned(diff) {
  // An equal value reads "0", so no "±" remains in the energy views (#3478).
  let sign = "";
  if (diff > 0) sign = "+";
  else if (diff < 0) sign = "−";
  return `${sign}${formatWhole(Math.abs(diff))}`;
}

// The weigh-ins' span the sources average over (#3613), else the period the own value covers.
function energyFitSpan(scale) {
  if (scale.span_start && scale.span_end) return `Vom ${deficitDay(scale.span_start)} bis ${deficitDay(scale.span_end)}`;
  const period = ANALYSIS_PERIODS.find((item) => item.days === scale.own_days);
  return `In ${period?.span ?? "den letzten " + scale.own_days + " Tagen"}`;
}

// The verdict names the own value and the server's one outlined source with its distance in this
// period (athlete decision, #3613); certain only by the 12-week rule (#3187).
function energyFitVerdict(read, scale, sources) {
  // The sentence stands only where nothing is drawn; beside a drawn scale it would contradict it (#3930).
  if (scale.own_kcal == null) return sources.length ? null : h("p", { className: "energy-fit-verdict" }, "Für einen Vergleich fehlen noch genug vollständig erfasste Tage und Wiegungen.");
  const parts = [`${energyFitSpan(scale)} verbrauchst du rund `, h("b", null, `${formatWhole(scale.own_kcal)} kcal am Tag`), "."];
  const named = sources.find((item) => item.source === read.outlined);
  if (named) {
    const more = named.diff >= 0 ? "mehr" : "weniger";
    parts.push(read.best === named.source
      ? [" Am besten trifft ", h("b", null, named.name), ` dein Gewicht, mit ${formatWhole(Math.abs(named.diff))} kcal ${more}.`]
      : [" Am besten bisher: ", h("b", null, named.name), ` (${energyFitSigned(named.diff)}) – noch nicht sicher.`]);
  }
  return h("p", { className: "energy-fit-verdict" }, ...parts.flat());
}

function energyFitDomain(scale, sources) {
  const values = sources.flatMap((item) => (item.learned_total_kcal == null ? [item.total_kcal] : [item.total_kcal, item.learned_total_kcal]));
  if (scale.own_kcal != null) values.push(scale.own_kcal - (scale.own_band_kcal || 0), scale.own_kcal + (scale.own_band_kcal || 0));
  const low = Math.floor(Math.min(...values) / 500) * 500;
  const high = Math.max(low + 500, Math.ceil(Math.max(...values) / 500) * 500);
  // The lane shares the row with the name and the value, so a tick every 1,000 keeps them apart.
  const step = (high - low) / 500 > 3 ? 1000 : 500;
  return { low, high, step, at: (value) => ((value - low) / (high - low)) * 100 };
}

// One positioned element of a lane; the position is set through the style object (CSP).
function energyFitMark(className, left, width = null, color = null) {
  const node = h("span", color ? { className, dataset: { color: String(color) } } : className);
  node.style.left = `${left.toFixed(1)}%`;
  if (width != null) node.style.width = `${width.toFixed(1)}%`;
  return node;
}

function energyFitLane(domain, scale, faint) {
  const lane = h("div", "energy-fit-lane");
  if (scale.own_kcal != null && scale.own_band_kcal != null) {
    const band = energyFitMark(`energy-fit-band${faint ? " is-faint" : ""}`, domain.at(scale.own_kcal - scale.own_band_kcal), domain.at(scale.own_kcal + scale.own_band_kcal) - domain.at(scale.own_kcal - scale.own_band_kcal));
    lane.append(band);
  }
  return lane;
}

function energyFitAxis(domain) {
  const axis = h("div", { className: "energy-fit-axis", "aria-hidden": "true" });
  for (let value = domain.low; value <= domain.high; value += domain.step) {
    const tick = energyFitMark("energy-fit-tick", domain.at(value));
    tick.textContent = formatWhole(value);
    axis.append(tick);
  }
  return axis;
}

function energyFitOwnRow(domain, scale) {
  const lane = energyFitLane(domain, scale, false);
  const value = h("span", "energy-fit-value", formatWhole(scale.own_kcal));
  if (scale.own_band_kcal != null) value.append(energyKcalSpan(scale.own_kcal, scale.own_band_kcal));
  lane.append(energyFitMark("energy-fit-own", domain.at(scale.own_kcal)));
  return h("li", { className: "energy-fit-row", dataset: { fitSource: "own" } }, h("span", "energy-fit-name", "Deine Daten"), lane, value);
}

function energyFitLabel(scale, total) {
  return scale.own_kcal == null ? formatWhole(total) : `${formatWhole(total)} (${energyFitSigned(total - scale.own_kcal)})`;
}

// A source row: its name left, one lane with its dot, "value (signed distance)" right; inside marks
// a distance within the own band. A formula row with a learned everyday adds a ring at its total
// with it, linked to the dot, and "mit Alltag ≈ …" under its value (#3723).
function energyFitSourceRow(domain, scale, item) {
  const color = FIT_COLORS[item.source];
  const lane = energyFitLane(domain, scale, true);
  const value = h("span", "energy-fit-value", energyFitLabel(scale, item.total_kcal));
  const learned = item.learned_total_kcal;
  // A term of 0 would put the ring over the dot, so only the line names it (review, #3723).
  const ring = learned != null && learned !== item.total_kcal;
  if (learned != null) value.append(h("span", { className: "energy-span", dataset: { fitLearned: "" } }, `mit Alltag ≈ ${energyFitLabel(scale, learned)}`));
  if (ring) {
    const [from, to] = [domain.at(item.total_kcal), domain.at(learned)];
    lane.append(energyFitMark("energy-fit-link", Math.min(from, to), Math.abs(to - from), color));
  }
  lane.append(energyFitMark("energy-fit-point", domain.at(item.total_kcal), null, color));
  if (ring) lane.append(energyFitMark("energy-fit-learned", domain.at(learned), null, color));
  const inside = item.diff != null && scale.own_band_kcal != null && Math.abs(item.diff) <= scale.own_band_kcal;
  return h("li", { className: "energy-fit-row", dataset: { fitSource: item.source, inside: String(inside) } }, h("span", { className: "energy-fit-name", dataset: { color: String(color) } }, item.name), lane, value);
}

// The legend under the rows once a ring is shown; "(vorläufig)" until every shown term qualifies.
function energyFitLegend(sources) {
  const learned = sources.filter((item) => item.learned_total_kcal != null);
  if (!learned.length) return null;
  const ring = learned.every((item) => item.learned_qualified) ? "mit Alltag" : "mit Alltag (vorläufig)";
  return h("div", { className: "energy-fit-legend", dataset: { energyFitLegend: "" } },
    h("span", null, h("i", "is-full"), "Formel allein"), h("span", null, h("i", "is-ring"), ring));
}

function energyFitNoteText(scale, key) {
  // The green band belongs to the own value: without one it is not drawn and not explained (#3930).
  const band = scale.own_kcal == null ? "" : " Grünes Band: so genau kennt die App deinen echten Verbrauch – liegt eine Quelle darin, trifft sie.";
  return `kcal pro Tag, Ø ${verlaufPeriod(key).label}.${band} ${scale.weeks_with_data} von ${scale.weeks} Wochen mit Daten.`;
}

function energyFitScale(read, key) {
  const scale = read?.fit_scale?.[key];
  const card = h("section", { className: "analysis-chart-card energy-fit", dataset: { energyFit: key } }, verlaufHead("Welche Quelle trifft dein Gewicht?", verlaufPeriod(key).label));
  if (!scale) return card;
  const sources = energyFitSources(scale);
  const verdict = energyFitVerdict(read, scale, sources);
  if (verdict) card.append(verdict);
  if (sources.length || scale.own_kcal != null) {
    const domain = energyFitDomain(scale, sources);
    const rows = sources.map((item) => energyFitSourceRow(domain, scale, item));
    if (scale.own_kcal != null) rows.unshift(energyFitOwnRow(domain, scale));
    card.append(energyFitAxis(domain), h("ul", "energy-fit-rows", rows), ...[energyFitLegend(sources)].filter(Boolean));
  }
  card.append(h("p", { className: "analysis-reference-note", dataset: { energyFitNote: "" } }, energyFitNoteText(scale, key)));
  return card;
}

// Verlauf shows the deficit card of the selected period (#3606, #3704), then the scale above
// "Tagesverlauf" (#3548); any loaded read serves both, as neither depends on the diary's day. The
// Bilanz takes its goal from the same read, so it is drawn again with it.
function renderNutritionFit() {
  const slot = document.querySelector("#nutritionFitScale");
  if (!slot) return;
  const key = verlaufPeriod().key;
  const pending = energyPending(null, longPeriodShown());
  slot.replaceChildren(...(pending ? [pending] : [energyDeficitCard(energyRead, key), energyFitScale(energyRead, key)].filter(Boolean)));
  if (nutritionWeekShown) renderNutritionGoalParts(nutritionWeekShown);
}

function renderDiaryEnergy() {
  const totals = document.querySelector("#nutritionTotals");
  if (!totals) return;
  let slot = document.querySelector("#nutritionEnergy");
  if (!slot) {
    slot = h("div", { id: "nutritionEnergy" });
    totals.after(slot);
  }
  // A diary render with the read already shown keeps the table, so an open "i" stays open (#3379).
  // Another diary day needs its own read (#3395), so only the same day's read is kept.
  if (energyRead && slot.energyShown === energyRead && energyReadDay === energyDay()) return;
  slot.replaceChildren(...energyDiaryCards());
  slot.energyShown = energyRead;
}

function renderBodyAnalysis(body) {
  const root = document.getElementById("bodyAnalysisCharts");
  if (!root) return;
  const openDetails = new Set([...root.querySelectorAll("details[open]")].map((item) => item.querySelector("summary")?.textContent));
  root.replaceChildren();
  if (!skinfoldRead) loadSkinfolds();
  const metrics = [
    ["weight_kg", "Gewicht", "kg"],
    ["body_fat_pct", "Körperfett", "%"],
  ];
  const preferred = body?.preferred_providers || {};
  const weight = bodyOverviewReading("weight_kg", body, preferred);
  // The weight's water hint comes with the energy read; Körper starts it when no view has (#4047).
  if (!energyRead) loadEnergy("");
  weight.hint = waterHintText(energyRead?.water_hint, weight);
  const fat = bodyOverviewReading("body_fat_pct", body, preferred);
  const fatRow = bodyOverviewRow("body_fat_pct", "Körperfett", "%", fat, bodyEditButton("Körperfett eintragen", openBodyFatDialog));
  fatRow.addEventListener("click", (event) => { if (!event.target.closest("button")) void openBodyFatDialog(); });
  const rows = [bodyOverviewRow("weight_kg", "Gewicht", "kg", weight, bodyEditButton("Gewicht eintragen", openWeightDialog)), fatRow];
  // The fat-free mass stays derived from the two values, without a pencil (#3402).
  if (weight.value != null && fat.value != null && !fat.device) rows.push(bodyOverviewRow("lean_mass_kg", "Fettfreie Masse", "kg", { value: weight.value * (1 - fat.value / 100) }, null));
  rows.push(energyGoalRow());
  root.append(h("section", "analysis-chart-card", h("table", { className: "current-performance-table", dataset: { bodyOverview: "" } }, h("caption", null, "Körperwerte"), h("tbody", null, rows))));
  const key = analysisPeriod(bodyHistoryPeriod).key;
  // 6 and 12 Monate read their own body window once; until it arrives the 3 Monate stay drawn.
  const long = trendHistoryFor(bodyHistoryPeriod, BASE_PERIOD_DAYS, () => renderBodyAnalysis(body))?.history?.body;
  const window = long?.windows?.[key] || body?.windows?.[key] || body?.windows?.["91d"];
  root.append(analysisPeriodControls("Zeitraum für Körper", bodyHistoryPeriod, (period) => {
    bodyHistoryPeriod = period;
    renderBodyAnalysis(body);
  }, { trend: true, remember: "body" }));
  if (!window?.start || !window?.end) {
    root.append(h("p", "empty", "Noch keine Körper-Historie verfügbar."));
    return;
  }
  for (const [metric, title, unit] of metrics) {
    const chart = bodyMetricChart(metric, title, unit, window, bodyHistoryPeriod, preferred);
    if (chart) root.append(chart);
  }
  root.querySelectorAll("details").forEach((details) => { details.open = openDetails.has(details.querySelector("summary")?.textContent); });
}

let trainingRecordsGeneration = 0;
let equipmentTab = "bike";

// `background` is a refresh nobody asked for (a state load, a route render): it waits while the athlete
// has an equipment form open, so typed values are not lost (#2879).
async function renderTrainingRecords({ background = false } = {}) { // NOSONAR
  if (!state.data || !equipmentRouteActive()) return;
  const editing = [...document.querySelectorAll("#equipmentItems .equipment-form:not([hidden])")].some((form) => form.checkVisibility?.() ?? form.offsetParent !== null);
  if (background && editing) return;
  const generation = ++trainingRecordsGeneration;
  const session = state.sessionGeneration;
  const gear = document.getElementById("equipmentItems");
  const heading = () => h("h3", null, "Ausrüstung und Wartung");
  // Only a section without a rendered list shows loading and failure; a failed refresh keeps the list.
  const rendered = () => Boolean(gear.querySelector(".equipment-tabs"));
  if (!rendered()) gear.replaceChildren(heading(), createLoadingState("Ausrüstung wird geladen…"));
  try {
    const result = await requestAnalysisReport("/api/analysis/training-records");
    if (generation !== trainingRecordsGeneration || session !== state.sessionGeneration) return;
    const openDetails = new Set([...gear.querySelectorAll("details[open]")].map((details) => details.dataset.key));
    // A background refresh of the same tab and session keeps the maintenance receipt and its undo.
    const receipt = gear.querySelector(":scope > .maintenance-receipt");
    const keep = receipt?.dataset.tab === equipmentTab && receipt.dataset.session === String(session);
    gear.replaceChildren(heading(), ...(keep ? [receipt] : []));
    const equipment = result.equipment || {};
    // The notice reuses this read, so it does not read the same state a second time (#2809).
    updateStatusSources({ equipment: equipmentStatusItem(equipment) });
    equipmentNoticeKey = equipmentNoticeState();
    const items = equipment.garmin_items || [];
    const tabs = h("div", { className: "segmented-control equipment-tabs", "aria-label": "Ausrüstung nach Sportart" });
    tabs.setAttribute("role", "tablist");
    const localItems = equipment.items || [];
    const parents = new Map(localItems.map((item) => [item.id, item.name]));
    const card = (place) => ({ item, source }) => equipmentCard(item, source, parents, place);
    const archive = (key, entries) => {
      return h("details", { className: "training-focus-details equipment-archive", dataset: { key: `archive:${key}` } }, h("summary", null, `Ausgemustert (${entries.length})`), entries.map(card(key)));
    };
    const isArchivedEquipment = (item) => item.status === "archived";
    const activeLocalItems = localItems.filter((item) => !isArchivedEquipment(item));
    equipmentFormBikes = activeLocalItems.filter((item) => item.kind === "bike" && !item.parent_id);
    const archivedLocalItems = localItems.filter(isArchivedEquipment);
    const activeGarminItems = items.filter((item) => item.status !== "archived");
    const archivedGarminItems = items.filter((item) => item.status === "archived");
    const bikeItems = activeLocalItems.filter((item) => isRideEquipment(item) || item.kind === "bike" || item.kind === "component");
    const runItems = activeLocalItems.filter((item) => isRunEquipment(item) || item.kind === "shoes");
    const archivedBikeItems = archivedLocalItems.filter((item) => isRideEquipment(item) || item.kind === "bike" || item.kind === "component");
    const archivedRunItems = archivedLocalItems.filter((item) => isRunEquipment(item) || item.kind === "shoes");
    const uncategorizedItems = activeLocalItems.filter((item) => !bikeItems.includes(item) && !runItems.includes(item));
    const archivedUncategorizedItems = archivedLocalItems.filter((item) => !archivedBikeItems.includes(item) && !archivedRunItems.includes(item));
    // "+ Hinzufügen" sits right of the heading and adds to the shown tab; its form opens in that tab (#4129 M-06).
    const [addButton, addForm] = equipmentAddControl(equipmentTab);
    gear.querySelector(":scope > h3").replaceWith(h("div", "equipment-head", heading(), addButton));
    const groups = {
      bike: { label: "Fahrrad", items: bikeItems },
      run: { label: "Laufschuhe", items: runItems },
    };
    const garminBikeItems = activeGarminItems.filter((item) => /bike|cycl|component|rad/i.test(`${item.kind} ${item.name}`));
    const garminRunItems = activeGarminItems.filter((item) => /shoe|run|lauf/i.test(`${item.kind} ${item.name}`));
    const archivedGarminBikeItems = archivedGarminItems.filter((item) => /bike|cycl|component|rad/i.test(`${item.kind} ${item.name}`));
    const archivedGarminRunItems = archivedGarminItems.filter((item) => /shoe|run|lauf/i.test(`${item.kind} ${item.name}`));
    const uncategorizedGarminItems = activeGarminItems.filter((item) => !garminBikeItems.includes(item) && !garminRunItems.includes(item));
    const archivedUncategorizedGarminItems = archivedGarminItems.filter((item) => !archivedGarminBikeItems.includes(item) && !archivedGarminRunItems.includes(item));
    const garminGroups = {
      bike: garminBikeItems,
      run: garminRunItems,
    };
    const archivedGroups = {
      bike: [...archivedBikeItems.map((item) => ({ item, source: "local" })), ...archivedGarminBikeItems.map((item) => ({ item, source: "provider" }))],
      run: [...archivedRunItems.map((item) => ({ item, source: "local" })), ...archivedGarminRunItems.map((item) => ({ item, source: "provider" }))],
    };
    for (const key of ["bike", "run"]) {
      const tab = h("button", { type: "button", id: `equipmentTab-${key}`, "aria-controls": `equipmentPanel-${key}` }, groups[key].label);
      tab.setAttribute("role", "tab"); tabs.append(tab);
      const panel = h("div", { className: "equipment-tab-panel", id: `equipmentPanel-${key}`, "aria-labelledby": tab.id, tabIndex: 0, hidden: equipmentTab !== key });
      panel.setAttribute("role", "tabpanel");
      if (key === equipmentTab) panel.append(addForm);
      for (const item of groups[key].items) panel.append(card(key)({ item, source: "local" }));
      for (const item of garminGroups[key]) panel.append(card(key)({ item, source: "provider" }));
      // The defaults choose among items, so they wait until there is one (#3936 G-13); they follow the cards as a flat section.
      if (groups[key].items.length) panel.append(equipmentRuleTable(key, equipment.rules, groups[key].items));
      if (archivedGroups[key].length) panel.append(archive(key, archivedGroups[key]));
      if (!groups[key].items.length && !garminGroups[key].length && !archivedGroups[key].length) {
        panel.append(h("p", "muted", key === "bike" ? "Noch keine Fahrräder oder Komponenten erfasst." : "Noch keine Laufschuhe erfasst."));
      }
      gear.append(panel);
      tab.setAttribute("aria-selected", String(equipmentTab === key));
      tab.tabIndex = equipmentTab === key ? 0 : -1;
      tab.classList.toggle("active", equipmentTab === key);
      tab.addEventListener("click", () => { equipmentTab = key; void renderTrainingRecords(); });
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault(); equipmentTab = equipmentTab === "bike" ? "run" : "bike";
        void renderTrainingRecords().then(() => document.getElementById(`equipmentTab-${equipmentTab}`)?.focus());
      });
    }
    if (uncategorizedItems.length || uncategorizedGarminItems.length || archivedUncategorizedItems.length || archivedUncategorizedGarminItems.length) {
      const other = h("section", "equipment-uncategorized");
      other.append(h("h4", null, "Weitere Ausrüstung"));
      for (const item of uncategorizedItems) other.append(card("other")({ item, source: "local" }));
      for (const item of uncategorizedGarminItems) other.append(card("other")({ item, source: "provider" }));
      if (archivedUncategorizedItems.length || archivedUncategorizedGarminItems.length) {
        other.append(archive("other", [
          ...archivedUncategorizedItems.map((item) => ({ item, source: "local" })),
          ...archivedUncategorizedGarminItems.map((item) => ({ item, source: "provider" })),
        ]));
      }
      gear.append(other);
    }
    // A kept receipt stays right under the heading row, the tabs follow it.
    (gear.querySelector(":scope > .maintenance-receipt") || gear.querySelector(".equipment-head")).after(tabs);
    gear.querySelectorAll("details").forEach((details) => { details.open = openDetails.has(details.dataset.key); });
    if (equipment.garmin_synced_at) gear.append(h("p", "muted", `Garmin \u00b7 Stand ${formatTime(equipment.garmin_synced_at)}${equipment.garmin_freshness === "stale" ? " \u00b7 letzter erfolgreicher Abruf" : ""}`));
  } catch (error) {
    if (generation === trainingRecordsGeneration && session === state.sessionGeneration) {
      if (!rendered()) gear.replaceChildren(heading(), createErrorState(error?.message, () => void renderTrainingRecords()));
    }
  }
}

// Containers whose data decides whether a segment shows its one empty card (#1783). A container
// without a marker, still loading or failed counts as not empty; period controls stay visible.
// A container shared by both sports names the marker of the sport's part (#2265).
const ANALYSIS_SEGMENT_CONTENT = {
  running: [["currentPerformance", "analysisEmptyRunning"], ["analysisHistoryCharts", "analysisEmptyRunning"], ["performancePredictions"], ["runningCriticalSpeed"], ["runningRecurringSessions"], ["runActivityRecords"], ["runningBestEfforts"]],
  cycling: [["currentPerformance", "analysisEmptyCycling"], ["analysisHistoryCharts", "analysisEmptyCycling"], ["cyclingCriticalPower"], ["cyclingRecurringSessions"], ["rideActivityRecords"], ["cyclingPowerProfile"]],
  load: [["sessionPerformance"], ["analysisLoadCharts"], ["trainingZoneCharts"], ["performanceEnduranceCharts"]],
  recovery: [["personalRecovery"]],
  strength: [["strengthAnalysis"]],
};
const ANALYSIS_EMPTY_TITLES = { running: "Noch keine Laufdaten", cycling: "Noch keine Raddaten", load: "Noch keine Belastungsdaten", recovery: "Noch keine Erholungsdaten", strength: "Noch keine Kraftdaten" };
const ANALYSIS_SEGMENT_LABELS = { running: "Laufen", cycling: "Radfahren", strength: "Kraft", load: "Belastung", recovery: "Erholung" };
const ANALYSIS_SEGMENT_SPORTS = { running: "run", cycling: "ride" };

function analysisSegmentEmpty(segment) {
  return ANALYSIS_SEGMENT_CONTENT[segment].every(([id, marker = "analysisEmpty"]) => document.getElementById(id)?.dataset[marker] === "true");
}

function analysisDataSourceConnected(data = state.data) {
  return Boolean(data?.garmin?.configured || data?.wahoo?.connected);
}

function analysisSegmentPeriod(segment) {
  if (segment === "load") return analysisHistoryPeriod;
  if (segment === "strength") return strengthHistoryPeriod;
  return segment === "recovery" ? recoveryHistoryPeriod : null;
}

function analysisEmptyCard(segment) {
  let action;
  let cause;
  const connected = analysisDataSourceConnected();
  if (connected && analysisSegmentPeriod(segment) === "28d") {
    // Data may exist outside the short window, so the one action widens the period.
    cause = "Keine Daten in den letzten 4 Wochen.";
    action = h("button", { className: "push-button", type: "button" }, "3 Monate anzeigen");
    action.addEventListener("click", () => {
      const buttons = { load: "#analysisLoadPeriod button", strength: "#strengthPeriod button" };
      const controls = document.querySelectorAll(buttons[segment] || "#personalRecovery .analysis-period-controls button");
      [...controls].find((button) => button.textContent === "3 Monate")?.click();
    });
  } else if (connected) {
    cause = "Noch keine Daten abgeglichen.";
    action = h("button", { className: "push-button", type: "button" }, "Jetzt abgleichen");
    action.addEventListener("click", () => void analysisRefresh());
  } else {
    cause = "Garmin oder Wahoo ist nicht verbunden.";
    action = h("a", { className: "push-button", href: "#more/connections" }, "Anbindung einrichten");
  }
  return createEmptyState(ANALYSIS_EMPTY_TITLES[segment], cause, action);
}

function renderAnalysisEmptyState(segment) {
  const root = document.getElementById("analysisEmptyState");
  const empty = analysisSegmentEmpty(segment);
  const key = empty ? `${segment}:${analysisDataSourceConnected()}:${analysisSegmentPeriod(segment)}` : "";
  if (root.dataset.segment !== key) {
    root.dataset.segment = key;
    root.replaceChildren(...(empty ? [analysisEmptyCard(segment)] : []));
  }
  root.hidden = !empty;
  document.querySelectorAll("#personalRecovery .analysis-trend-controls").forEach((controls) => { controls.hidden = analysisPeriodIdle(empty); });
  return empty;
}

// Without a connected source the period buttons have nothing to change, so they step aside (#3936 G-13).
function analysisPeriodIdle(empty) {
  return empty && !analysisDataSourceConnected();
}

function renderAnalysisSegmentsIfActive() {
  if (String(state.route || "").startsWith("analysis")) renderAnalysisSegments(state.route);
}

function renderAnalysisSegments(route = state.route) {
  const segment = { "analysis/load": "load", "analysis/recovery": "recovery", "analysis/cycling": "cycling", "analysis/strength": "strength" }[route] || "running";
  const empty = renderAnalysisEmptyState(segment);
  document.getElementById("analysisRefreshButton").setAttribute("aria-label", `Analyse aktualisieren: ${ANALYSIS_SEGMENT_LABELS[segment]}`);
  const sport = empty ? null : ANALYSIS_SEGMENT_SPORTS[segment];
  const load = segment === "load" && !empty;
  document.getElementById("analysisHistoryCharts").hidden = !sport;
  document.getElementById("currentPerformance").hidden = !sport;
  // Laufen and Radfahren share the containers; each part names its sport.
  document.querySelectorAll("#dataPanel [data-analysis-sport]").forEach((part) => { part.hidden = part.dataset.analysisSport !== sport || !part.hasChildNodes(); });
  const endurance = document.getElementById("performanceEnduranceCharts");
  endurance.hidden = !load || !endurance.childElementCount;
  document.getElementById("analysisLoadPeriod").hidden = segment !== "load" || analysisPeriodIdle(empty);
  document.getElementById("sessionPerformance").hidden = !load;
  document.getElementById("trainingZoneCharts").hidden = !load;
  document.getElementById("analysisLoadCharts").hidden = !load;
  document.getElementById("personalRecovery").hidden = segment !== "recovery";
  document.getElementById("strengthPeriod").hidden = segment !== "strength" || analysisPeriodIdle(empty);
  document.getElementById("strengthAnalysis").hidden = segment !== "strength" || empty;
  document.querySelectorAll("[data-analysis-segment]").forEach((link) => {
    const active = link.dataset.analysisSegment === segment;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
}

const analysisRefresh = AppViewRefresh.create({
  api,
  waitForJob: (...args) => waitForSyncJob(...args),
  getRoute: () => state.route,
  getRequest: (route) => {
    // Laufen and Radfahren read the same performance values; the week review reads the sessions.
    const area = { running: "performance", cycling: "performance", week: "sessions", strength: "sessions" }[route.split("/")[1]] || route.split("/")[1] || "performance";
    const days = analysisRefreshDays(area);
    return { path: "/api/analysis/refresh", body: { area, ...(days ? { days } : {}) } };
  },
  getViewKey: analysisRefreshViewKey,
  reload: async (route) => {
    const result = await api("/api/performance");
    if (state.route !== route || !state.data) return;
    state.data.performance = result.performance || result;
    const performance = state.data.performance;
    clearAnalysisReportCache();
    if (route === "analysis/recovery") renderPersonalRecovery(performance.personal_recovery);
    else if (route === "analysis/strength") void renderStrengthAnalysis();
    else {
      renderAnalysisHistory(performance.history);
      if (route === "analysis/load") renderTrainingFocus(analysisTrainingFocus(performance));
      else {
        renderPerformance(performance, { refreshCharts: false });
        // The cleared report cache is read again for the power profile and the running best efforts.
        void renderCyclingPowerProfile();
      }
    }
  },
  button: document.getElementById("analysisRefreshButton"),
  status: document.getElementById("analysisRefreshStatus"),
});

function analysisRefreshDays(area) {
  // The provider read covers 4 Wochen or 3 Monate; a longer period refreshes the 3 Monate.
  if (area === "load") return analysisPeriodWeekly(analysisHistoryPeriod) ? 91 : 28;
  if (area === "recovery") return analysisPeriodWeekly(recoveryHistoryPeriod) ? 91 : 28;
  return null;
}

function analysisRefreshViewKey(route) {
  if (route === "analysis/load") return analysisHistoryPeriod;
  if (route === "analysis/recovery") return recoveryHistoryPeriod;
  return null;
}
document.getElementById("analysisRefreshButton").addEventListener("click", () => void analysisRefresh());

function selectedRecoveryBaselines(report) {
  const preferred = report?.preferred_providers || {};
  return ["sleep", "hrv", "resting_hr"].flatMap((metric) => {
    const candidates = (report?.baselines || []).filter((item) => item.metric === metric);
    candidates.sort((a, b) => String(b.observed_at).localeCompare(String(a.observed_at))
      || Number(isPreferredSource(b, preferred[metric])) - Number(isPreferredSource(a, preferred[metric]))
      || b.history.length - a.history.length);
    return candidates.slice(0, 1);
  });
}

// The one line under a recovery value: the sleep average, the seven-day mean's verdict, or the night's own range.
function recoveryReference({ metric, item, range, target, unit, position, rollingOk }) {
  if (metric === "sleep") return "Durchschnitt der angezeigten Werte";
  return rollingOk ? recoveryRollingNote(item.rolling) : recoveryReferenceLabel(item, range, target, unit, position);
}

function recoveryReferenceLabel(item, range, target, unit, position) {
  if (range) {
    const provisional = item.status === "provisional" ? " · vorläufig" : "";
    return `${position || "Bereich deiner früheren Werte"} (${analysisValue(range.lower, unit)}–${analysisValue(range.upper, unit)})${provisional}`;
  }
  if (target != null) return `Persönliches Schlafziel: ${analysisValue(target, unit)}`;
  return item.reason || "Noch kein Vergleich verfügbar";
}

// M2 rolling-lnrmssd-v1 (#1772): seven-day mean of ln(RMSSD), back-transformed to ms.
// The caller ends the note with a full stop, so a reason drops its own.
function recoveryRollingNote(rolling) {
  if (!["ok", "provisional"].includes(rolling.status)) return `7-Tage-Mittel: ${String(rolling.reason || "kein Vergleich").replace(/\.$/, "")}`;
  const position = { below: "unter dem Bereich deiner früheren Werte", within: "im Bereich deiner früheren Werte", above: "über dem Bereich deiner früheren Werte" }[rolling.position] || "neben dem Bereich deiner früheren Werte";
  const range = `${analysisValue(rolling.lower_ms, "ms")}–${analysisValue(rolling.upper_ms, "ms")}`;
  const spread = rolling.cv_pct == null ? "" : ` · Schwankung ${analysisValue(rolling.cv_pct, "%")}`;
  const provisional = rolling.status === "provisional" ? " · vorläufig" : "";
  return `7-Tage-Mittel ${analysisValue(rolling.mean_ms, "ms")} (${rolling.window_nights} Nächte) · ${position} (${range})${spread}${provisional}`;
}

// Daily values on `dates`, or the mean of each calendar week when `weekly`; missing values stay gaps.
function recoveryPeriodPoints(history, dates, today, weekly) {
  if (weekly) return analysisWeeklyMeanPoints(history.filter((point) => point.date >= dates[0] && point.date <= today), dates[0], today, 3);
  const values = new Map(history.filter((point) => point.date <= today).map((point) => [point.date, point.value]));
  return dates.map((date) => ({ date, value: values.get(date) ?? null }));
}

function renderRecoveryCharts(report, root) {
  const metrics = [["sleep", "Schlafdauer", "h", 0], ["hrv", "HRV", "ms", 2], ["resting_hr", "Ruheherzfrequenz", "bpm", 3]];
  // The base read's baselines hold 84 days: 3 Monate and more read their own history once (#3839);
  // until it arrives the charts keep the base read.
  const long = trendHistoryFor(recoveryHistoryPeriod, 28, () => renderPersonalRecovery(report))?.personal_recovery;
  const baselines = selectedRecoveryBaselines(long || report);
  const today = report?.as_of || timezoneDateKey(state.data?.profile?.timezone, new Date());
  const period = analysisPeriod(recoveryHistoryPeriod);
  const weekly = analysisPeriodWeekly(recoveryHistoryPeriod);
  // Whole calendar weeks, so 12 Monate end on 52 weeks and every week is dated at its Monday.
  const weekStart = addDateKey(today, -((new Date(`${today}T12:00:00Z`).getUTCDay() + 6) % 7));
  const start = weekly ? addDateKey(weekStart, -7 * (period.weeks - 1)) : addDateKey(today, 1 - period.days);
  const dayCount = Math.round((Date.parse(today) - Date.parse(start)) / 86400000) + 1;
  const dates = Array.from({ length: dayCount }, (_, index) => addDateKey(start, index));
  const end = today;
  const series = metrics.map(([metric, title, unit, color]) => {
    const item = baselines.find((candidate) => candidate.metric === metric);
    if (!item) return { label: title, legendLabel: title, unit, color, points: dates.map((date) => ({ date, value: null })) };
    const points = recoveryPeriodPoints(item.history, dates, today, weekly);
    const readings = item.history.filter((point) => point.date >= start && point.date <= today);
    const expectedDays = Math.max(0, Math.round((Date.parse(today) - Date.parse(start)) / 86400000) + 1);
    const range = metric !== "sleep" && ["ok", "provisional"].includes(item.status) && Number.isFinite(item.lower) && Number.isFinite(item.upper) ? { lower: item.lower, upper: item.upper, status: item.status } : null;
    const target = null;
    const position = { below: "Unter dem Bereich deiner früheren Werte", within: "Im Bereich deiner früheren Werte", above: "Über dem Bereich deiner früheren Werte" }[item.position];
    const measurement = metric === "hrv" ? " · " + item.measurement : "";
    const rangeDescription = range ? " Vergleich mit " + item.nights + " früheren Nächten." : "";
    const changed = item.context_changed_on ? ` Messkontext geändert am ${dateLabel(item.context_changed_on)}.` : "";
    const continuity = `${changed}${item.continuity === "unknown" ? " Messkontext unbekannt." : ""}`;
    // With a seven-day mean the card gives one verdict, the mean's; the single night's range stays out of it (#3929 A-08).
    const rollingOk = ["ok", "provisional"].includes(item.rolling?.status);
    const rolling = item.rolling && !rollingOk ? ` ${recoveryRollingNote(item.rolling)}.` : "";
    return { label: `${title} · ${item.source}${measurement}`, legendLabel: title, source: item.source, unit, color,
      bars: metric === "sleep", average: true, averageInHeading: true, cadenceDays: weekly ? 7 : 1, range, target,
      currentPoint: points.some(analysisValidPoint) ? null : item.history.findLast((point) => point.date <= today && analysisValidPoint(point)),
      referenceLabel: recoveryReference({ metric, item, range, target, unit, position, rollingOk }),
      coverageShort: analysisCoverageText(readings.length, expectedDays, "Tagen"),
      coverage: `${analysisCoverageText(readings.length, expectedDays, "Tagen")}${rangeDescription}${continuity}${rolling}`, points };
  });
  const missing = [];
  const charts = series.map((item) => {
    const values = item.points.filter(analysisValidPoint).map((point) => Number(point.value));
    const average = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
    const title = average == null ? item.legendLabel : `${item.legendLabel} · Ø ${analysisValue(average, item.unit)}`;
    return collectAnalysisChart(missing, title, [item], item.unit, start, end, "", { showLegend: false, calendarWeeks: weekly, ...(weekly ? { caption: analysisBasisCaption(item.unit, true, "mean") } : {}) });
  }).filter(Boolean);
  const readiness = state.data?.performance?.history?.provider_metrics?.training_readiness_history;
  if (readiness) {
    const readinessCharts = document.createDocumentFragment();
    renderProviderMetrics({ training_readiness_history: readiness }, readinessCharts, start, end, missing, { weeklyReadiness: weekly });
    charts.unshift(...readinessCharts.children);
  }
  root.append(analysisPeriodControls("Zeitraum für Erholung", recoveryHistoryPeriod, (choice) => { recoveryHistoryPeriod = choice; renderPersonalRecovery(report); }, { trend: true, remember: "recovery" }));
  root.dataset.analysisEmpty = String(!charts.length);
  if (!charts.length) return;
  const group = analysisChartGroup(`Erholung · Letzte ${period.label}`, charts, series);
  root.append(group);
}

function renderPersonalRecovery(report) {
  const root = document.getElementById("personalRecovery");
  root.replaceChildren();
  renderRecoveryCharts(report, root);
  // Missing recovery values often mean the watch has not uploaded (#2807).
  const garmin = state.data?.garmin;
  const stale = garmin?.configured ? deviceSyncSentence(garmin.device_synced_at) : "";
  if (stale) root.prepend(h("p", "device-sync-stale", stale));
  renderAnalysisSegments(state.route);
}

function trainingFocusPeriodLabel(report) {
  const days = Math.round((Date.parse(report.end) - Date.parse(report.start)) / 86400000) + 1;
  const period = ANALYSIS_PERIODS.find((item) => item.days === days);
  return period ? `Letzte ${period.label}` : `Letzte ${days} Tage`;
}

function renderTrainingFocus(report) {
  const root = document.getElementById("sessionPerformance");
  const zones = document.getElementById("trainingZoneCharts");
  zones.replaceChildren();
  root.replaceChildren(h("h3", null, "Trainingsfokus"));
  // Unclassified sessions keep the card, which explains why they are not counted.
  root.dataset.analysisEmpty = String(!report?.classified_sessions && !report?.unclassified_sessions);
  zones.dataset.analysisEmpty = "true";
  if (!report) { root.append(h("p", "muted", "Noch keine Trainingsdaten vorhanden.")); renderAnalysisSegmentsIfActive(); return; }

  const categories = [["low_aerobic", "Leicht aerob"], ["high_aerobic", "Hoch aerob"], ["anaerobic", "Anaerob"]];
  const info = h("button", { className: "analysis-legend-info analysis-info-icon", type: "button", "aria-label": "Trainingsfokus: Informationen" }, "i");
  const explanation = h("div", { className: "analysis-info-tooltip", id: `focus-info-${++analysisInfoId}` }, `${trainingFocusPeriodLabel(report)}: ${dateLabel(report.start)} bis ${dateLabel(report.end)}. ${report.coverage?.known_sessions ?? 0} erfasste Garmin-Einheiten im Zeitraum${report.coverage?.observed_start && report.coverage?.observed_end ? ` · erfasste Daten ${dateLabel(report.coverage.observed_start)} bis ${dateLabel(report.coverage.observed_end)}` : ""}. Aufgezeichnete Garmin-Belastung nach der Hauptwirkung der Einheit (Trainingseffekt). Keine aus Zonen abgeleitete Einteilung und nicht Garmins separat berechneter Belastungsfokus. ${AppFormat.plural(report.unclassified_sessions || 0, "Einheit", "Einheiten")} ohne bekannte Wirkung oder Garmin-Belastung ${report.unclassified_sessions === 1 ? "bleibt" : "bleiben"} ausgeschlossen.`); // NOSONAR
  bindAnalysisPopover(info, explanation); root.firstChild.append(info); root.firstChild.after(explanation);
  appendTrainingFocusShare(report, categories, root);
  appendTrainingFocusZones(report, zones);
  zones.dataset.analysisEmpty = String(!zones.childElementCount);
  renderAnalysisSegmentsIfActive();
}

// One collapsible card per item (#2302): name, status chip, one line and the lifetime bar when
// closed; usage, maintenance, Garmin state and the action when open. Garmin-only items carry a tag.
function equipmentCard(item, source, parents, place) {
  const local = source === "local";
  const distance = local ? item.usage?.distance_km : item.distance_km;
  const life = equipmentLifetime(item.lifetime, distance, local ? item.lifetime_target_km : item.goal_km);
  const [kind, label] = equipmentState(item, local, life);
  // The provider's name comes from the bootstrap labels, never from a literal here.
  const garmin = state.data?.provider_labels?.[providerIds().garmin] || "";
  const card = h("details", { className: "connection-card equipment-card", dataset: { key: `${place}:${source}:${item.id}`, state: kind } });
  const summary = h("summary");
  summary.append(h("strong", null, item.name));
  if (!local && garmin) summary.append(h("span", "equipment-source-tag", garmin));
  summary.append(h("span", `connection-chip is-${kind}`, label));
  // #2358: the percent is read once, followed by its meaning (and an exceeded target the
  // chip does not name); the bar below is decorative.
  const meaning = `Lebensdauer${life.over && kind !== "error" ? ", Ziel überschritten" : ""}`;
  const percent = life.percent == null ? null : [analysisPercent(life.percent), " ", h("span", "sr-only", meaning)];
  const line = [equipmentParent(item, local, parents), Number.isFinite(Number(distance ?? Number.NaN)) ? analysisValue(distance, "km") : "", percent].filter(Boolean);
  if (line.length) summary.append(h("span", "equipment-card-line", ...line.flatMap((part, index) => [...(index ? [" · "] : []), ...[part].flat()])));
  if (life.percent != null) summary.append(equipmentBar(life));
  const body = h("div", "connection-card-body");
  body.append(equipmentSection("Nutzung", equipmentUsage(item, local, life)));
  if (local) body.append(equipmentSection("Wartung", equipmentMaintenance(item)));
  const provider = equipmentGarmin(item, local, garmin);
  if (provider) body.append(provider);
  if (local && item.status !== "archived") {
    const actions = h("div", "connection-actions");
    const form = equipmentForm(place, item);
    const edit = h("button", { className: "secondary-button", type: "button", "aria-expanded": "false" }, "Bearbeiten");
    edit.addEventListener("click", () => {
      form.hidden = !form.hidden;
      edit.setAttribute("aria-expanded", String(!form.hidden));
    });
    const retire = h("button", { className: "secondary-button danger-button", type: "button" }, "Ausmustern");
    retire.addEventListener("click", () => void retireEquipment(item, retire));
    actions.append(maintenanceButton(item), edit, retire);
    body.append(actions, form);
  }
  card.append(summary, body);
  return card;
}

// Garmin's status words in German; a stored "Garmin | active" keeps only what follows the source.
function equipmentGarminStatusText(value) {
  const word = String(value || "").split("|").pop().trim();
  if (!word) return "";
  return normalizeEquipmentStatus(word) === "archived" ? "ausgemustert" : "aktiv";
}

// Garmin's status of the item and the conflict with the local status; none for a local item Garmin does not know.
function equipmentGarmin(item, local, title) {
  const garminStatus = String(item.garmin_status || "").trim();
  if (!garminStatus && local) return null;
  const conflict = local && normalizeEquipmentStatus(garminStatus) !== item.status;
  const coach = item.status === "archived" ? "archiviert" : "aktiv";
  return equipmentSection(title, [
    h("p", null, equipmentGarminStatusText(garminStatus) || "Unbekannter Garmin-Status"),
    conflict ? h("p", "muted", `Statuskonflikt · Coach: ${coach} · Garmin: ${equipmentGarminStatusText(garminStatus)}`) : null,
  ]);
}

// The slim lifetime bar of a closed card; its colour follows the card's state. The card
// line already says the percent and the chip the state, so the bar stays out of the tree.
function equipmentBar(life) {
  return h("progress", { className: "connection-sync-progress-bar", max: 100, value: life.progress, "aria-hidden": "true" });
}

// The parent bike of a component, or that it is still open; empty for other items.
function equipmentParent(item, local, parents) {
  if (!local || item.kind !== "component") return "";
  return item.parent_pending ? "Fahrrad offen" : parents.get(item.parent_id);
}

function equipmentSection(title, nodes) {
  return h("div", "connection-card-status", h("h4", null, title), nodes.filter(Boolean));
}

function equipmentUsage(item, local, life) {
  const usage = item.usage || {};
  const sport = item.sport_pending ? "Sportart offen" : equipmentSportLabel(item);
  const status = item.status === "archived" ? "ausgemustert" : "aktiv";
  const facts = local ? [KIND_LABELS[item.kind] || item.kind, sport, status] : [KIND_LABELS[item.kind] || item.kind];
  return [
    h("p", null, facts.filter(Boolean).join(" · ")),
    local ? h("p", null, `${analysisValue(usage.distance_km, "km")} · ${analysisValue(usage.hours, "h")} · ${analysisCountLabel(usage.assigned_sessions, "zugeordnete Einheit", "zugeordnete Einheiten")}`) : null,
    !local && item.sessions != null ? h("p", null, analysisCountLabel(item.sessions, "Einheit", "Einheiten")) : null,
    h("p", life.percent == null ? "muted" : null, life.text),
  ];
}

function equipmentMaintenance(item) {
  const usage = item.usage || {};
  const history = h("ul");
  for (const event of item.maintenance || []) history.append(h("li", null, `${dateLabel(event.date)} · ${event.notes || "Wartung erfasst"}`));
  const since = Number.isFinite(Number(usage.maintenance_distance_km ?? Number.NaN)) || Number.isFinite(Number(usage.maintenance_hours ?? Number.NaN));
  const due = new Map([[true, "Wartung fällig"], [false, "Wartung nicht fällig"]]).get(usage.maintenance_due) || "Wartungsstand unklar";
  // Due and "none recorded" are one fact, not two lines that read as a contradiction (#3936 M-06).
  return [
    since ? h("p", null, `Seit letzter Wartung: ${analysisValue(usage.maintenance_distance_km, "km")} · ${analysisValue(usage.maintenance_hours, "h")}`) : null,
    h("p", null, history.childElementCount ? due : `${due} · noch keine Wartung erfasst`),
    usage.maintenance_coverage === "partial" ? h("p", "muted", "Nutzung unvollständig bekannt.") : null,
    history.childElementCount ? history : null,
  ];
}

// Pure: the closed card's chip as [kind, text]; the kinds reuse the connection chip colours.
function equipmentState(item, local, life) {
  if (item.status === "archived") return ["off", "Ausgemustert"];
  if (life.over) return ["error", "Ziel \u00fcberschritten"];
  if (local && item.usage?.maintenance_due === true) return ["unchecked", "Wartung f\u00e4llig"];
  if (local && item.usage?.maintenance_due === false) return ["ok", "Wartung ok"];
  return life.percent == null ? ["off", "Ziel unbekannt"] : ["ok", "Ziel ok"];
}

// Pure (#2809): one status-strip notice naming the active items whose lifetime target is exceeded
// or whose maintenance is due, by the rules of the equipment cards; exceeded targets come first.
function equipmentStatusItem(equipment) {
  const due = [
    ...(equipment?.items || []).map((item) => [item, true, item.usage?.distance_km, item.lifetime_target_km]),
    ...(equipment?.garmin_items || []).map((item) => [item, false, item.distance_km, item.goal_km]),
  ].map(([item, local, distance, target]) => [item.name, ...equipmentState(item, local, equipmentLifetime(item.lifetime, distance, target))])
    .filter(([, kind]) => kind === "error" || kind === "unchecked")
    .sort(([, left], [, right]) => (left === "error" ? 0 : 1) - (right === "error" ? 0 : 1));
  if (!due.length) return null;
  const [[name, , label]] = due;
  return { level: "warning", title: `${name}: ${label}`, detail: due.length > 1 ? `+${due.length - 1}` : "", action: { href: "#more/equipment", label: "Ansehen" } };
}

let equipmentNoticeKey = null;

// The state an equipment read belongs to: the session and the activity and Garmin versions.
function equipmentNoticeState() {
  const versions = state.data?.state_versions || {};
  return JSON.stringify([state.sessionGeneration, versions.activities, versions.garmin]);
}

// The equipment read takes seconds on a long history (#2809), so the notice reads it, through the cache the
// Ausrüstung view shares, only when the browser is idle on a visible page and only once per session and
// activity or Garmin state; a chat turn does not read it again. A failed read keeps the last notice and
// waits for the next state; a hidden page leaves the read to the next status render. The device snapshot
// shown before the fresh state reads nothing, and without an idle callback (Safari) the read waits 2 s.
function refreshEquipmentNotice() {
  if (!state.data || state.replaceDataOnLoad) return;
  const session = state.sessionGeneration;
  const key = equipmentNoticeState();
  if (key === equipmentNoticeKey) return;
  equipmentNoticeKey = key;
  const idle = globalThis.requestIdleCallback || ((callback) => setTimeout(callback, 2000));
  // An idle callback can fire while the view's own reads are still running; the read waits them out (#2966).
  idle(() => globalThis.AppApi.settled().then(() => {
    if (session !== state.sessionGeneration || !state.data) return;
    if (document.visibilityState === "hidden") {
      equipmentNoticeKey = null;
      return;
    }
    requestAnalysisReport("/api/analysis/training-records").then((result) => {
      if (session === state.sessionGeneration) updateStatusSources({ equipment: equipmentStatusItem(result?.equipment) });
    }).catch(() => {});
  }));
}

// A Coach turn that changed equipment makes the next status render read it again (#2809).
function invalidateEquipmentNotice() {
  equipmentNoticeKey = null;
  invalidateAnalysisReport("/api/analysis/training-records");
}

// The athlete's own equipment editor (#2879): create in a tab, edit or retire a local item. Every save
// shows its receipt with undo; retiring is confirmed here and again by the server ("confirmed").
let equipmentFormBikes = [];
const EQUIPMENT_FORM_CHOICES = {
  bike: { sports: [["ride/outdoor", "Radfahren"], ["ride/indoor", "Rad indoor"]], kinds: [["bike", "Fahrrad"], ["component", "Komponente"], ["other", "Sonstiges"]] },
  run: { sports: [["run/outdoor", "Laufen"]], kinds: [["shoes", "Schuh"], ["other", "Sonstiges"]] },
};
const EQUIPMENT_SAVED_FIELDS = ["name", "sport", "environment", "kind", "status", "parent_id", "start_date", "initial_distance_km", "initial_hours", "maintenance_km", "maintenance_hours", "lifetime_target_km"];

// Default equipment per kind of session (#3014): one select per kind, counting from today on. Earlier
// sessions without an own choice change only after the preview is confirmed, with a receipt and undo.
const EQUIPMENT_RULE_ROWS = {
  run: [["run:intervals", "Intervalle"], ["run:long", "Langer Lauf"], ["run:easy", "Locker"], ["run:race", "Wettkampf"]],
  bike: [["ride:indoor", "Drinnen"], ["ride:outdoor", "Draußen"]],
};

// A kind's items as the server pairs them: indoor rides take indoor or virtual bikes, the rest the others.
function equipmentRuleItems(kind, items) {
  const indoor = (item) => ["indoor", "virtual"].includes(item.environment);
  return items.filter((item) => !item.parent_id && item.kind !== "component" && !item.sport_pending && !item.parent_pending
    && (!kind.startsWith("ride:") || indoor(item) === (kind === "ride:indoor")));
}

function equipmentRuleTable(key, rules, items) {
  const status = h("div", { className: "equipment-rule-status", "aria-live": "polite" });
  const body = h("tbody");
  for (const [kind, label] of EQUIPMENT_RULE_ROWS[key]) {
    const rule = { revision: 0, ...rules?.[kind] };
    const select = h("select", { "aria-label": `Standard ${label}` },
      h("option", { value: "", selected: !rule.equipment_id }, "–"),
      equipmentRuleItems(kind, items).map((item) => h("option", { value: item.id, selected: item.id === rule.equipment_id }, item.name)));
    select.addEventListener("change", () => void saveEquipmentRule(status, kind, select, rule));
    body.append(h("tr", { dataset: { ruleKind: kind } }, h("th", { scope: "row" }, label), h("td", null, select)));
  }
  return h("section", { className: "equipment-rules", "aria-label": "Standard-Ausrüstung" },
    h("h4", null, "Standard-Ausrüstung"), h("table", "race-predictions-table", body), status);
}

async function saveEquipmentRule(status, kind, select, rule) {
  select.disabled = true;
  try {
    const result = await api("/api/equipment/rules", { method: "POST", body: JSON.stringify({ kind, equipment_id: select.value || null, expected_revision: rule.revision }) });
    rule.revision = result.rule.revision;
    rule.equipment_id = select.value || null;
    equipmentRuleChanged();
    const preview = select.value ? await api(`/api/equipment/rules/preview?kind=${encodeURIComponent(kind)}`) : null;
    status.replaceChildren(...equipmentRulePreview(status, kind, preview?.count || 0));
  } catch (error) {
    // A refused save leaves the stored default shown.
    select.value = rule.equipment_id || "";
    if (!isSessionEndedError(error)) status.replaceChildren(error.message);
  } finally {
    select.disabled = false;
  }
}

// A rule changes what sessions count; the calendar reads its units again and the notice follows.
function equipmentRuleChanged() {
  invalidateAnalysisReport("/api/analysis/training-records");
  if (typeof plannedSessionReads !== "undefined") plannedSessionReads.clear();
  globalThis.invalidateEquipmentNotice?.();
}

function equipmentRulePreview(status, kind, count) {
  if (!count) return ["Gespeichert."];
  const apply = h("button", { type: "button", className: "secondary-button" }, `Auf ${count} anwenden`);
  const skip = h("button", { type: "button", className: "secondary-button" }, "Nur künftig");
  skip.addEventListener("click", () => status.replaceChildren("Gespeichert."));
  apply.addEventListener("click", async () => {
    setSaving(apply, true);
    try {
      const result = await api("/api/equipment/rules/apply", { method: "POST", body: JSON.stringify({ kind, confirmed: true }) });
      equipmentRuleChanged();
      status.replaceChildren(...equipmentRuleReceipt(status, result));
    } catch (error) {
      setSaving(apply, false);
      if (!isSessionEndedError(error)) status.replaceChildren(error.message);
    }
  });
  return [h("p", null, `Gespeichert. Auch ${countLabel(count, "frühere Einheit", "frühere Einheiten")} ohne eigene Wahl?`), apply, skip];
}

function equipmentRuleReceipt(status, result) {
  const text = `Auf ${countLabel(result.count, "Einheit", "Einheiten")} angewendet. `;
  if (!result.change?.id) return [text];
  const undo = h("button", { className: "message-undo", type: "button" }, "Rückgängig");
  undo.addEventListener("click", async () => {
    setSaving(undo, true);
    try {
      await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: result.change.id, expected_current_hash: result.change.after_hash }) });
      equipmentRuleChanged();
      status.replaceChildren("Zurückgenommen.");
    } catch (error) {
      setSaving(undo, false);
      if (!isSessionEndedError(error)) status.replaceChildren(error.message);
    }
  });
  return [text, undo];
}

// The button and the form of the shown tab: the button toggles the form.
function equipmentAddControl(tab) {
  const form = equipmentForm(tab, null);
  const add = h("button", { className: "push-button equipment-add", type: "button", "aria-expanded": "false" }, "+ Hinzufügen");
  add.addEventListener("click", () => {
    form.hidden = !form.hidden;
    add.setAttribute("aria-expanded", String(!form.hidden));
  });
  return [add, h("div", "equipment-add-row", form)];
}

function equipmentForm(tab, item) {
  const choices = EQUIPMENT_FORM_CHOICES[tab] || EQUIPMENT_FORM_CHOICES.bike;
  const select = (name, options, value) => h("select", { name, required: true }, options.map(([key, label]) => h("option", { value: key, selected: key === value }, label)));
  const number = (name, value, required = false) => h("input", { type: "number", name, min: "0", step: "any", inputMode: "decimal", required, value: value ?? (required ? "0" : "") });
  const pair = item ? `${item.sport}/${item.environment}` : choices.sports[0][0];
  const sports = choices.sports.some(([key]) => key === pair) ? choices.sports : [...choices.sports, [pair, activitySportLabel(item)]];
  const kinds = !item || choices.kinds.some(([key]) => key === item.kind) ? choices.kinds : [...choices.kinds, [item.kind, item.kind]];
  // The current parent stays selectable even when it is no longer an active bike.
  const bikes = equipmentFormBikes.filter((bike) => bike.id !== item?.id).map((bike) => [bike.id, bike.name]);
  const parents = item?.parent_id && !bikes.some(([id]) => id === item.parent_id) ? [[item.parent_id, "Bisheriges Rad"], ...bikes] : bikes;
  const parentSelect = select("parent_id", parents, item?.parent_id);
  parentSelect.required = false;
  const parent = h("label", null, "Gehört zu", parentSelect);
  const kind = select("kind", kinds, item?.kind || kinds[0][0]);
  // A hidden or empty parent field never blocks the form: it is disabled, so validation skips it.
  const showParent = () => { parent.hidden = kind.value !== "component"; parentSelect.disabled = parent.hidden; };
  kind.addEventListener("change", showParent);
  showParent();
  const error = h("p", { className: "error", role: "alert" });
  const form = h("form", { className: "equipment-form", hidden: true },
    h("label", null, "Name", h("input", { name: "name", required: true, maxLength: 200, value: item?.name || "" })),
    h("label", null, "Sportart", select("sport", sports, pair)),
    h("label", null, "Art", kind), parent,
    h("label", null, "Im Einsatz seit", h("input", { type: "date", name: "start_date", required: true, value: item?.start_date || timezoneDateKey(state.data?.profile?.timezone, new Date()) })),
    h("label", null, "Bisherige Strecke (km)", number("initial_distance_km", item?.initial_distance_km, true)),
    h("label", null, "Bisherige Zeit (h)", number("initial_hours", item?.initial_hours, true)),
    h("label", null, "Lebensdauer-Ziel (km)", number("lifetime_target_km", item?.lifetime_target_km)),
    h("label", null, "Wartung alle (km)", number("maintenance_km", item?.maintenance_km)),
    h("label", null, "Wartung alle (h)", number("maintenance_hours", item?.maintenance_hours)),
    error, h("button", { className: "secondary-button", type: "submit" }, "Speichern"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector("button[type='submit']");
    if (submit.disabled) return;
    if (kind.value === "component" && !parents.length) {
      error.textContent = "Zuerst ein Fahrrad anlegen.";
      return;
    }
    const data = new FormData(form);
    const text = (name) => { const value = data.get(name); return typeof value === "string" ? value : ""; };
    const optional = (name) => (text(name) === "" ? null : Number(text(name)));
    const [sport, environment] = text("sport").split("/");
    const component = text("kind") === "component";
    error.textContent = "";
    // One tap, one save: a second tap while the first runs would create the item twice.
    setSaving(submit, true);
    const saved = await saveEquipment({
      ...(item ? { id: item.id, expected_revision: item.revision } : {}),
      name: text("name").trim(), sport, environment, kind: text("kind"),
      // Always explicit: an omitted status would save as active.
      status: item?.status || "active",
      parent_id: component ? text("parent_id") || null : null,
      start_date: text("start_date"),
      initial_distance_km: Number(text("initial_distance_km") || 0), initial_hours: Number(text("initial_hours") || 0),
      lifetime_target_km: optional("lifetime_target_km"), maintenance_km: optional("maintenance_km"), maintenance_hours: optional("maintenance_hours"),
    }, "Ausrüstung gespeichert. ", error);
    if (!saved) setSaving(submit, false);
  });
  return form;
}

// Retiring acts at once and the receipt line offers "Rückgängig" (athlete decision, #3934 G-03).
async function retireEquipment(item, button) {
  setSaving(button, true);
  const saved = Object.fromEntries(EQUIPMENT_SAVED_FIELDS.filter((key) => key in item).map((key) => [key, item[key]]));
  const ok = await saveEquipment({ ...saved, initial_distance_km: item.initial_distance_km ?? 0, initial_hours: item.initial_hours ?? 0, id: item.id, expected_revision: item.revision, status: "archived", confirmed: true }, "Ausgemustert. ");
  if (!ok) setSaving(button, false);
}

async function saveEquipment(payload, message, errorNode = null) {
  try {
    const result = await api("/api/equipment", { method: "POST", body: JSON.stringify(payload) });
    invalidateAnalysisReport("/api/analysis/training-records");
    await renderTrainingRecords();
    showMaintenanceReceipt(result?.change, message);
    return true;
  } catch (error) {
    if (isSessionEndedError(error)) return false;
    if (errorNode) errorNode.textContent = error.message;
    else showMaintenanceReceipt(null, `${error.message} `);
    return false;
  }
}

function maintenanceButton(item) {
  const button = h("button", { className: "secondary-button", type: "button" }, "Wartung erledigt");
  button.addEventListener("click", async () => {
    setSaving(button, true);
    try {
      const result = await api("/api/equipment/maintenance", { method: "POST", body: JSON.stringify({ equipment_id: item.id, date: timezoneDateKey(state.data?.profile?.timezone, new Date()) }) });
      // Maintenance does not advance state_versions, so drop the cached response even if the athlete has left the route.
      invalidateAnalysisReport("/api/analysis/training-records");
      await renderTrainingRecords();
      showMaintenanceReceipt(result?.change);
    } catch (error) {
      setSaving(button, false);
      if (!isSessionEndedError(error)) button.textContent = error.message || "Wartung fehlgeschlagen";
    }
  });
  return button;
}

function showMaintenanceReceipt(change, message = "Wartung erfasst. ") {
  const status = h("p", { className: "maintenance-receipt", dataset: { tab: equipmentTab, session: String(state.sessionGeneration) } }, message);
  status.setAttribute("role", "status");
  const gear = document.getElementById("equipmentItems");
  gear?.querySelector(":scope > .maintenance-receipt")?.remove();
  gear?.querySelector(":scope > :is(.equipment-head, h3)")?.after(status);
  if (!change?.id) return;
  const undo = h("button", { className: "message-undo", type: "button" }, "Rückgängig");
  undo.addEventListener("click", async () => {
    setSaving(undo, true);
    try {
      await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: change.id, expected_current_hash: change.after_hash }) });
      invalidateAnalysisReport("/api/analysis/training-records");
      await renderTrainingRecords();
      showMaintenanceReceipt(null, "Zurückgenommen.");
    } catch (error) {
      setSaving(undo, false);
      status.firstChild.textContent = `${error.message} `;
    }
  });
  status.append(undo);
}

function normalizeEquipmentStatus(value) {
  return /retir|archiv|inactive|inaktiv|ausgemustert/i.test(value || "") ? "archived" : "active";
}

// Pure: lifetime text, percent and bar value; percent stays null while usage or target is unknown.
function equipmentLifetime(lifetime, usageKm, targetKm) {
  const usage = lifetime?.usage_km ?? usageKm;
  const target = lifetime?.target_km ?? targetKm;
  const knownTarget = Number.isFinite(target) && target > 0;
  if (!Number.isFinite(usage) || usage < 0) {
    return { percent: null, text: knownTarget ? `Nutzung unbekannt \u00b7 Ziel ${analysisValue(target, "km")}` : "Nutzung und Lebensdauerziel unbekannt" };
  }
  if (!knownTarget) return { percent: null, text: `${analysisValue(usage, "km")} Nutzung \u00b7 Lebensdauerziel unbekannt` };
  const percent = Number.isFinite(lifetime?.percent) ? lifetime.percent : Math.round(usage / target * 1000) / 10;
  const over = percent > 100;
  return {
    percent, over,
    progress: Math.min(100, Math.max(0, Number.isFinite(lifetime?.progress_percent) ? lifetime.progress_percent : percent)),
    text: `${analysisValue(usage, "km")} von ${analysisValue(target, "km")} \u00b7 ${analysisPercent(percent)}${over ? " \u00b7 Ziel \u00fcberschritten" : ""}`,
  };
}

// One bucket per seven days from `start`, the last one cut at `end`. `summarize` turns a week's valid
// readings into the fields of its point; a week without any is a gap (value null) carrying `gap`.
// Each point is dated at its week's first day, so the unfinished week keeps its own slot (#3450).
function analysisWeeklyPoints(points, start, end, summarize, gap = {}) {
  const weeks = [];
  for (let weekStart = start; weekStart <= end; weekStart = addDateKey(weekStart, 7)) {
    const last = addDateKey(weekStart, 6);
    const readings = points.filter((point) => point.date >= weekStart && point.date <= last && point.date <= end && analysisValidPoint(point));
    weeks.push(readings.length ? { ...summarize(readings), date: weekStart } : { date: weekStart, value: null, ...gap });
  }
  return weeks;
}

function analysisWeeklyPerformancePoints(points, start, end, median = false) {
  return analysisWeeklyPoints(points, start, end, (readings) => {
    readings.sort((a, b) => a.date.localeCompare(b.date));
    const latest = readings.at(-1);
    const values = readings.map((point) => Number(point.value)).sort((a, b) => a - b);
    const middle = Math.floor(values.length / 2);
    let value = latest.value;
    if (median) value = values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
    return { value, observedDate: latest.date, ...(median ? { count: readings.length } : {}) };
  });
}

function analysisWeeklySumPoints(points, start, end) {
  return analysisWeeklyPoints(points, start, end, (known) => ({
    value: Math.round(known.reduce((sum, point) => sum + Number(point.value), 0) * 10) / 10, observedDate: known.at(-1).date,
  }));
}

// `digits` keeps the mean as exact as its display needs: sleep hours are shown to the minute, steps to one decimal.
function analysisWeeklyMeanPoints(points, start, end, digits = 1) {
  const scale = 10 ** digits;
  return analysisWeeklyPoints(points, start, end, (known) => ({
    value: Math.round(known.reduce((sum, point) => sum + Number(point.value), 0) / known.length * scale) / scale, observedDate: known.at(-1).date, count: known.length,
  }));
}

function analysisWeeklyLastPoints(points, start, end) {
  return analysisWeeklyPoints(points, start, end, (known) => ({ ...known.at(-1), observedDate: known.at(-1).date }));
}

function trainingFocusDistribution(title, readings, unit, info) {
  const section = h("section", "analysis-chart-card training-focus-distribution");
  const help = h("button", { className: "analysis-legend-info analysis-info-icon", type: "button", "aria-label": `${title}: Informationen` }, "i");
  const tooltip = h("div", { className: "analysis-info-tooltip", id: `focus-info-${++analysisInfoId}` }, info);
  tooltip.setAttribute("popover", "auto"); tooltip.setAttribute("role", "tooltip");
  help.setAttribute("popovertarget", tooltip.id); section.append(h("h4", null, title, help), tooltip);
  const total = readings.reduce((sum, item) => sum + item.value, 0);
  for (const item of readings) {
    const bar = h("progress", { max: Math.max(total, 1), value: item.value, "aria-label": `${item.label}: ${Math.round(total ? item.value / total * 100 : 0)} Prozent` });
    section.append(h("div", { className: "training-focus-row", dataset: { zone: item.label } }, h("span", null, item.label), bar, h("span", null, `${Math.round(total ? item.value / total * 100 : 0)} % · ${unit === "seconds" ? formatDuration(item.value) : formatWhole(item.value)}`)));
  }
  return section;
}

function appendTrainingFocusShare(report, categories, root) {
  if (report.classified_sessions) {
    const total = categories.reduce((sum, [key]) => sum + report.categories[key].load, 0);
    const bar = h("div", { className: "training-focus-share", "aria-label": "Anteile leicht aerob, hoch aerob und anaerob an der bekannten Belastung" });
    bar.setAttribute("role", "img");
    const legend = h("ul", "training-focus-legend");
    for (const [index, [key, label]] of categories.entries()) {
      const percent = total ? report.categories[key].load / total * 100 : 0;
      const part = h("span", { dataset: { color: String(index) } }); part.style.width = `${percent}%`; bar.append(part);
      legend.append(h("li", { dataset: { color: String(index) } }, h("strong", null, `${Math.round(percent)} %`), h("span", null, label)));
    }
    root.append(bar, legend);
  } else root.append(h("p", "muted", "Noch keine Garmin-Einheiten mit auswertbarer Trainingswirkung vorhanden."));
}

function trainingFocusZoneSeconds(items) {
  const seconds = new Map();
  for (const item of items) for (const [zone, value] of Object.entries(item.seconds)) seconds.set(zone, (seconds.get(zone) || 0) + value);
  // Highest zone on top, as in the session detail (#1782).
  return [...seconds.entries()].sort(([a], [b]) => Number(b.slice(1)) - Number(a.slice(1))).map(([label, value]) => ({label, value}));
}

function trainingFocusZoneInfo(report, model, items) {
  const period = trainingFocusPeriodLabel(report).toLowerCase();
  const source = [...new Set(items.map((item) => item.source).filter(Boolean))].join(", ");
  // Each zone model is summed on its own (#1743); a session can appear for HF and power.
  const basis = model === "provider" ? "Zonen des Geräts, für Einheiten ohne lokale Zonenzeiten" : "Lokale Zonen am Tag der Einheit, aus den Aufzeichnungen gezählt";
  return `${basis} (${period}). Quelle: ${source || "unbekannt"}.`;
}

function appendTrainingFocusZones(report, details) {
  const zones = report.zones || [];
  // Pace zones are counted for runs only and follow power (#2808).
  for (const [sensor, name] of [["heart_rate", "Herzfrequenzzonen"], ["power", "Leistungszonen"], ["pace", "Pace-Zonen"]]) {
    const matching = zones.filter((item) => item.sensor === sensor);
    if (!matching.some((item) => item.measured_seconds > 0)) continue;
    for (const model of ["local", "provider"]) {
      const items = matching.filter((item) => (item.zone_model || "provider") === model);
      const readings = trainingFocusZoneSeconds(items);
      if (!readings.some((item) => item.value > 0)) continue;
      const title = model === "provider" ? `${name} (Gerät)` : name;
      details.append(trainingFocusDistribution(title, readings, "seconds", trainingFocusZoneInfo(report, model, items)));
    }
  }
}
