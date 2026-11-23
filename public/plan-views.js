function plannedAirValues(weather) {
  const air = weather.air_quality && typeof weather.air_quality === "object" ? weather.air_quality : {};
  const aqi = calendarMetricNumber(air.european_aqi_max);
  const category = air.european_aqi_category ? " (" + air.european_aqi_category + ")" : "";
  return [
    aqi ? `AQI ${aqi}${category}` : "",
    calendarMetricNumber(air.uv_index_max) ? `UV ${calendarMetricNumber(air.uv_index_max)}` : "",
    plannedPollenLabel(weather.pollen),
  ].filter(Boolean);
}

// One explicit plan_push job, carrying the hashes the entries were shown with.
async function runPlanPush(provider, entries) {
  const job = await api("/api/sync/jobs", { method: "POST", body: JSON.stringify({ provider, type: "plan_push", payload: { entries } }) });
  return globalThis.waitForSyncJob(job.id);
}

// The job entry that removes one listed leftover of a deleted or replaced unit:
// a Garmin workout or Wahoo plan by a remove entry, an Intervals.icu event by the hidden unit's own entry.
function leftoverEntry(provider, item, ids) {
  return provider !== ids.intervals ? { library_workout_id: item.id, remove: true } : { library_workout_id: item.id, expected_payload_hash: item.hash };
}

// What a week's sync delivered per provider: Garmin gets structured workouts.
function plannedWeekSyncSummary(done, ids, providerLabel) {
  const totals = new Map();
  for (const job of done) {
    const item = totals.get(job.provider) || { send: 0, remove: 0 };
    item[job.kind === "remove" ? "remove" : "send"] += job.entries.length;
    totals.set(job.provider, item);
  }
  const reached = [...totals].map(([provider, item]) => {
    const verb = provider === ids.garmin ? "strukturiert" : "gesendet";
    const sent = item.send ? `${item.send} ${verb}` : "";
    const removed = item.remove ? `${item.remove} alte entfernt` : "";
    return `${providerLabel(provider)}: ${[sent, removed].filter(Boolean).join(", ")}`;
  });
  // A strength unit without a Garmin workout says why the watch did not get it.
  const blocked = new Set(done.flatMap((job) => job.units).map(plannedGarminBlockedText).filter(Boolean));
  return [...reached, ...blocked].join(" · ");
}

// Controls sit in the card summary; a click must not toggle the card.
// Before a rebuild: remember which cards are open and which control has focus.
function rememberPlannedEntryState(root) {
  openPlannedEntries = new Set([...root.querySelectorAll(".planned-entry[data-entry-key]")].filter((card) => card.open).map((card) => card.dataset.entryKey));
  for (const key of plannedForceOpen) openPlannedEntries.add(key);
  plannedForceOpen.clear();
  for (const key of plannedFocusStatuses.keys()) if (!openPlannedEntries.has(key)) plannedFocusStatuses.delete(key);
  for (const key of plannedMoveReceipts.keys()) if (!openPlannedEntries.has(key)) plannedMoveReceipts.delete(key);
  const active = document.activeElement;
  const card = active?.closest?.(".planned-entry[data-entry-key]");
  if (!card || !root.contains(card)) return null;
  return { key: card.dataset.entryKey, name: active.matches("select") ? active.name : "" };
}

function restorePlannedEntryFocus(root, focused) {
  if (!focused) return;
  const card = [...root.querySelectorAll(".planned-entry[data-entry-key]")].find((node) => node.dataset.entryKey === focused.key);
  const target = focused.name ? card?.querySelector(`select[name="${focused.name}"]`) : null;
  (target || card?.querySelector("summary"))?.focus({ preventScroll: true });
}

// The unit's description, or a placeholder when the card body would stay empty.
function appendPlannedDescription(details, text) {
  if (!text && details.childElementCount) return;
  details.append(h("p", "planned-description", text || "Keine weiteren Details hinterlegt."));
}

// The planned steps as a list (#2447, mockup B): zone mark, duration or distance, zone, and the target
// range of the zones in effect on the unit's day right-aligned; a step without a range shows none.
// A step test names its steps (#3833): warm-up, the numbered stages, cool-down.
function plannedStepList(profile, stepTest = false) {
  const segments = profile?.source === "planned" && Array.isArray(profile.segments) ? profile.segments : [];
  if (!segments.length || segments.length > 200) return null;
  const axis = profile.axis === "distance" ? "distance" : "duration";
  const stepName = (index) => {
    if (!stepTest) return null;
    if (index === 0) return "Einlaufen";
    return index === segments.length - 1 ? "Auslaufen" : `Stufe ${index}`;
  };
  const rows = segments.map((segment, index) => {
    const zone = Number.isInteger(segment.zone) && segment.zone >= 1 && segment.zone <= 7 ? segment.zone : null;
    // The zone text is the written zone target ("Z1–Z2"); a percent or watt target shows its own text instead.
    const label = String(segment.label || "");
    const written = /^Z([1-7])(?:\s*-\s*Z([1-7]))?/.exec(label);
    const zoneText = written ? [written[1], written[2]].filter(Boolean).map((value) => `Z${value}`).join("–") : null;
    const range = plannedStepRange(segment.range) || (written ? "" : label.slice(0, 40));
    return h("li", { dataset: { zone: zone || "unknown" } },
      h("span", { className: "planned-step-mark", "aria-hidden": "true" }),
      h("span", "planned-step-text", [plannedStepExtent(Number(segment[axis]), axis), stepName(index) || zoneText].filter(Boolean).join(" · ")),
      h("span", "planned-step-range", range));
  });
  return h("ol", { className: "planned-steps", "aria-label": "Ablauf" }, rows);
}

// A step's exact length: "30 s", "1:30 min", "55 min", "1:10 h"; "400 m", "1,25 km".
const STEP_KM_FORMAT = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2 });
function plannedStepExtent(size, axis) {
  if (axis === "distance") return size < 1000 ? `${Math.round(size)} m` : `${STEP_KM_FORMAT.format(size / 1000)} km`;
  if (size < 60) return `${Math.round(size)} s`;
  return size < 3600 && Math.round(size) % 60 ? `${AppFormat.clock(size)} min` : AppFormat.duration(size);
}

// "110–128 bpm", "≤ 109 bpm" for the lowest zone, "≥ 155 bpm" for the open top; pace in min/km, slower end first.
function plannedStepRange(range) {
  if (!range || !["bpm", "W", "s/km"].includes(range.unit)) return "";
  const pace = range.unit === "s/km";
  const text = (value) => (pace ? AppFormat.clock(value) : String(value));
  const unit = pace ? "/km" : range.unit;
  const { min, max } = range;
  if (Number.isFinite(min) && Number.isFinite(max)) return min === max ? `${text(min)} ${unit}` : `${text(min)}–${text(max)} ${unit}`;
  // Pace counts down with intensity: the lowest zone is slower than its limit, the top one faster.
  if (Number.isFinite(max)) return `${pace ? "≥" : "≤"} ${text(max)} ${unit}`;
  if (Number.isFinite(min)) return `${pace ? "≤" : "≥"} ${text(min)} ${unit}`;
  return "";
}

// The description without its step lines ("- 10m Z2 HR") and repeat headers ("4x").
function plannedDescriptionText(text) {
  // As the workout parser reads them: a step starts with "-", a repeat header ends with "4x".
  const step = /^\s*-/;
  const repeat = /\b\d+x\s*$/i;
  return String(text || "").split("\n").filter((line) => !step.test(line) && !repeat.test(line)).join("\n").trim();
}

function stopSummaryToggle(event) {
  event.preventDefault();
  event.stopPropagation();
}

// A request kind sends the prompt as that quick action, on the planning model (#2744); without a
// planning key the button is off like the other planning entry points.
function plannedCoachButton(label, prompt, className = "secondary-button planned-conflict-button", requestKind = null, purpose = null) {
  const button = h("button", { type: "button", className, dataset: requestKind ? { requestKind } : undefined }, label);
  // Several hand-over buttons of one card share the label; the name starts with it and says what each is for.
  if (purpose) button.setAttribute("aria-label", `${label}: ${purpose}`);
  button.disabled = Boolean(requestKind) && !coachConfigured(null, requestKind);
  button.addEventListener("click", (event) => { stopSummaryToggle(event); void globalThis.askCoach?.(prompt, { requestKind }); });
  return button;
}

function plannedGarminBlockedText(entry) {
  const reason = entry?.push_blocked && Object.values(entry.push_blocked)[0];
  if (reason === "no_structure") return "Keine Kraftstruktur – nicht an Garmin gesendet";
  if (reason === "unmapped") return "Übung ohne Garmin-Zuordnung – nicht an Garmin gesendet";
  return "";
}

// The Personal API key alone cannot write workouts: a unit Garmin would wait for needs the SDK login.
function plannedGarminSdkRequired(entry, garmin) {
  return garmin?.configured === true && garmin.can_write_workouts === false && entry?.push_pending?.[providerIds().garmin] === true;
}

// Providers this planned unit still needs an explicit push to. Past, done,
// in-flight and unconfigured targets get none.
function plannedPushTargets(entry, dateKey, todayKey, { configured = {}, completed = false } = {}) {
  const pending = entry?.push_pending;
  if (!entry?.local_id || !entry.payload_hash || entry.is_completed_activity || completed) return [];
  if (!pending || typeof pending !== "object" || !dateKey || dateKey < todayKey) return [];
  const targets = Object.keys(pending).filter((provider) => pending[provider] === true && configured[provider] === true);
  return targets.sort((a, b) => a.localeCompare(b));
}

const PLAN_PUSH_MAX_ENTRIES = 28;

// The week's replacing sync as plan_push jobs, Intervals.icu last: per provider the week's units
// still to send, then the leftovers the app created there for that week (linked ids only).
function plannedWeekSyncJobs(units, leftovers, todayKey, configured, ids) {
  const jobs = [];
  for (const provider of [ids.garmin, ids.wahoo, ids.intervals]) {
    if (!provider || configured[provider] !== true) continue;
    const sends = units
      .filter(({ entry, dateKey, completed }) => plannedPushTargets(entry, dateKey, todayKey, { configured, completed }).includes(provider))
      .map(({ entry }) => ({ unit: entry, entry: { library_workout_id: entry.local_id, expected_payload_hash: entry.payload_hash } }));
    const removals = (leftovers[provider] || []).map((item) => ({ item, entry: leftoverEntry(provider, item, ids) }));
    for (const [kind, parts] of [["send", sends], ["remove", removals]]) {
      for (let index = 0; index < parts.length; index += PLAN_PUSH_MAX_ENTRIES) {
        const chunk = parts.slice(index, index + PLAN_PUSH_MAX_ENTRIES);
        jobs.push({
          provider,
          kind,
          entries: chunk.map((part) => part.entry),
          units: chunk.map((part) => part.unit).filter(Boolean),
          items: chunk.map((part) => part.item).filter(Boolean),
        });
      }
    }
  }
  return jobs;
}

// Run a week's jobs one after another; a failed job never stops the others. The message names
// the failures first, then what arrived where.
async function runPlannedWeekSync(jobs, ids, providerLabel) {
  const failures = [];
  const done = [];
  for (const job of jobs) {
    const name = providerLabel(job.provider);
    try {
      const outcome = await runPlanPush(job.provider, job.entries);
      if (outcome.status === "completed") {
        done.push(job);
        continue;
      }
      // A partial job still delivered its completed entries; name those as well.
      const reached = new Set((outcome.items || []).filter((item) => item.status === "completed").map((item) => item.item_key));
      const entries = job.entries.filter((entry) => reached.has(entry.library_workout_id));
      if (entries.length) done.push({ ...job, entries, units: job.units.filter((unit) => reached.has(unit.local_id)) });
      failures.push(`${name}: ${job.kind === "remove" ? "Alte Einträge nicht entfernt: " : ""}${globalThis.providerJobErrorDetail?.(outcome) || "Synchronisierung fehlgeschlagen"}`);
    } catch (error) {
      failures.push(`${name}: ${error.message}`);
    }
  }
  const summary = plannedWeekSyncSummary(done, ids, providerLabel);
  return { failed: failures.length > 0, message: [...failures, summary].filter(Boolean).join(" · ") || "Synchronisiert" };
}

// A leftover belongs to the week of its day; an undated or earlier one to the current week and a
// later one to the last shown week, so every leftover stays reachable from a week button.
function plannedLeftoverWeek(item, currentWeekKey, lastWeekKey, weekStart) {
  const day = String(item?.date || "").slice(0, 10);
  const week = day ? weekStart(day) : "";
  if (!week || week < currentWeekKey) return currentWeekKey;
  if (week > lastWeekKey) return lastWeekKey;
  return week;
}

const CALENDAR_MARKERS = [
  { key: "no_training", name: "NO_TRAINING", label: "Kein Training" },
  { key: "no_intensity", name: "NO_INTENSITY", label: "Keine Intensität" },
  { key: "short_only", name: "SHORT_ONLY", label: "Nur kurze Einheiten" },
];
// Mirrors backend/calendar/markers.py::has_marker: "[" or "(" opens, "]" or ")" closes,
// the words may be separated by spaces, underscores or hyphens (or none), and case is ignored.
const MARKER_WORD_GAP = String.raw`[\s_-]*`;
const calendarMarkerSource = (name) => String.raw`[\[(]\s*${name.replaceAll("_", MARKER_WORD_GAP)}\s*[\])]`;
const CALENDAR_MARKER_PATTERN = new RegExp(
  CALENDAR_MARKERS.map(({ name }) => calendarMarkerSource(name)).join("|"),
  "gi",
);
// The day heading flags rain, UV, air and pollen only when notable; all figures stay in the weather symbol's tooltip.
const PLANNED_RAIN_FLAG_MIN_PERCENT = 50;
const PLANNED_UV_FLAG_MIN = 6;
const PLANNED_AQI_FLAG_MIN = 60; // European AQI "schlecht" and worse
const PLANNED_CAMS_POLLEN_FLAG_MIN = 10; // grains/m³; DWD levels count from 1
const PLANNED_DAY_STATUS_LABELS = new Map([["rest", "Ruhetag"], ["pause", "Trainingspause"]]);

function externalCalendarDetail(calendar) {
  if (calendar.last_error) return "Fehler bei letzter Aktualisierung";
  const skipped = Number(calendar.skipped_events) || 0;
  return skipped ? `${skipped} Termine übersprungen` : "";
}

function stripCalendarMarkers(text) {
  return String(text || "").replace(CALENDAR_MARKER_PATTERN, "").replace(/\s{2,}/g, " ").trim();
}

function calendarMarkerKeys(source) {
  if (!source || typeof source !== "object") return [];
  const name = String(source.name || "");
  return CALENDAR_MARKERS
    .filter(({ key, name: markerName }) => Boolean(source[key]) || new RegExp(calendarMarkerSource(markerName), "i").test(name))
    .map(({ key }) => key);
}

function plannedDayStatusLabel(dayStatus) {
  return PLANNED_DAY_STATUS_LABELS.get(dayStatus) || null;
}

// A marker conflict names what the day's appointment allows; other labels stay as the server wrote them (#3932 K-05).
const MARKER_CONFLICT_TEXTS = {
  no_training: "Ein Termin an diesem Tag erlaubt kein Training",
  no_intensity: "Ein Termin an diesem Tag erlaubt keine Intensität",
  short_only: "Ein Termin an diesem Tag erlaubt nur kurze Einheiten",
};

function plannedConflictTexts(conflicts) {
  return [...new Set((Array.isArray(conflicts) ? conflicts : [])
    .map((conflict) => MARKER_CONFLICT_TEXTS[conflict?.code] || String(conflict?.label || "").trim())
    .filter(Boolean))];
}

function plannedConflictLabels(conflicts) {
  return [...new Set((Array.isArray(conflicts) ? conflicts : [])
    .map((conflict) => String(conflict?.label || "").trim())
    .filter(Boolean))];
}

// Day context is plain text lines in one neutral style; conflicts belong to the planned session itself.
function plannedDayNoteSpecs(dayContext, competitions, dateKey, hasSession = false) {
  const context = dayContext && typeof dayContext === "object" ? dayContext : {};
  const appointments = (Array.isArray(context.appointments) ? context.appointments : [])
    .filter((event) => event && (event.training_relevant !== false || calendarMarkerKeys(event).length > 0));
  const specs = (Array.isArray(competitions) ? competitions : [])
    .filter((competition) => String(competition?.event_date || "").slice(0, 10) === dateKey)
    .map((competition) => ({
      kind: "competition",
      icon: "🏁",
      text: `${competition.priority || "B"}-Wettkampf: ${String(competition.name || "").trim() || "Wettkampf"}`,
    }));
  // A rest day is only planned while no session stands on it, whatever the sport filter hides.
  const dayStatus = plannedDayStatusLabel(context.day_status);
  if (dayStatus && !(hasSession && context.day_status === "rest")) specs.push({ kind: "day-status", icon: "", text: context.day_status === "rest" ? "Ruhetag geplant" : dayStatus });
  const markerKeys = new Set(appointments.concat([context]).flatMap((source) => calendarMarkerKeys(source)));
  const calendarParts = [
    ...appointments.map(plannedAppointmentLabel).filter(Boolean),
    ...CALENDAR_MARKERS.filter(({ key }) => markerKeys.has(key)).map(({ label }) => label),
  ];
  if (calendarParts.length) specs.push({ kind: "calendar", icon: "📅", text: calendarParts.join(" · ") });
  return specs;
}

function plannedAppointmentLabel(event) {
  if (!event || typeof event !== "object") return "";
  const name = stripCalendarMarkers(event.name) || "Trainingstermin";
  if (event.all_day) return `${name} · ganztägig`;
  const time = /(?:T|\s)(\d{2}:\d{2})/.exec(String(event.start_local || ""));
  return time ? `${name} · ${time[1]}` : name;
}

// Garmin names an activity "<place> - <workout>"; the calendar shows it without the place.
// Only the athlete's own place (the profile's weather location) counts as one, so a workout
// name such as "Sweet Spot - 3 x 12 min" stays whole; the stored name never changes.
const CALENDAR_NAME_PREFIX = /^(.+?) [-–—] (\S.*)$/u;

// The calendar shows a fixed range: one week back, four ahead (#3914).
const CALENDAR_WEEKS = Object.freeze({ past: 1, future: 4 });

function calendarDisplayName(name, place = "") {
  const text = String(name || "").trim();
  const match = CALENDAR_NAME_PREFIX.exec(text);
  const city = String(place || "").split(",")[0].trim().toLocaleLowerCase("de");
  return match && city && match[1].trim().toLocaleLowerCase("de") === city ? match[2] : text;
}

function calendarActualActivity(entry) {
  if (!entry || typeof entry !== "object") return null;
  if (entry.is_completed_activity) return entry;
  const actual = entry.compliance?.actual_activity;
  return actual && typeof actual === "object" ? actual : null;
}

// Canonical sports that normally record a distance; only these count as "ohne Distanz" when the distance is missing.
const DISTANCE_SPORTS = new Set(["ride", "run", "walk", "hike", "swim", "nordic_ski", "row"]);

function calendarMetricNumber(value, suffix = "") {
  if (value == null || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return `${AppFormat.number(number, { digits: Number.isInteger(number) ? 0 : 1 })}${suffix}`;
}

// DWD levels (0–3 index) or Open-Meteo/CAMS concentrations become one short pollen line.
function plannedPollenLabel(pollen) {
  if (!pollen || typeof pollen !== "object") return "";
  const source = pollen.source === "DWD" ? pollen.levels : pollen.concentrations;
  if (!source || typeof source !== "object") return "";
  const entries = Object.entries(source)
    .filter(([, value]) => value != null && String(value) !== "0" && String(value) !== "")
    .map(([name, value]) => `${name} ${value}`);
  return entries.length ? `Pollen: ${entries.join(", ")}` : "Pollen: keine Belastung";
}

// Highest pollen entry: DWD hazard levels such as "1-2" rank by their upper bound, CAMS concentrations by value.
function plannedPollenPeak(pollen) {
  if (!pollen || typeof pollen !== "object") return null;
  const dwd = pollen.source === "DWD";
  const source = dwd ? pollen.levels : pollen.concentrations;
  if (!source || typeof source !== "object") return null;
  let peak = null;
  for (const [name, value] of Object.entries(source)) {
    const rank = dwd ? Number(String(value).split(/[-–]/).pop()) : Number(value);
    if (Number.isFinite(rank) && (!peak || rank > peak.rank)) peak = { name, value, rank };
  }
  const minRank = dwd ? 1 : PLANNED_CAMS_POLLEN_FLAG_MIN;
  return peak && peak.rank >= minRank ? peak : null;
}

// Compact day-heading flags; an ordinary day yields none.
function plannedWeatherFlags(weather) {
  if (!weather || typeof weather !== "object") return [];
  const flags = [];
  const number = (value) => (value == null || value === "" ? Number.NaN : Number(value));
  const rain = number(weather.precipitation_probability_max);
  if (rain >= PLANNED_RAIN_FLAG_MIN_PERCENT) flags.push({ text: `💧 ${calendarMetricNumber(rain, " %")}`, label: `${calendarMetricNumber(rain)} Prozent Regenwahrscheinlichkeit` });
  const air = weather.air_quality && typeof weather.air_quality === "object" ? weather.air_quality : {};
  const uv = number(air.uv_index_max);
  if (uv >= PLANNED_UV_FLAG_MIN) flags.push({ text: `☀ UV ${calendarMetricNumber(uv)}`, label: `UV-Index ${calendarMetricNumber(uv)}` });
  const aqi = number(air.european_aqi_max);
  if (aqi >= PLANNED_AQI_FLAG_MIN) flags.push({ text: `AQI ${calendarMetricNumber(aqi)}`, label: `Luftqualität, Europäischer AQI ${calendarMetricNumber(aqi)}${air.european_aqi_category ? " (" + air.european_aqi_category + ")" : ""}` });
  const pollen = plannedPollenPeak(weather.pollen);
  if (pollen) flags.push({ text: `🌿 ${pollen.value}`, label: `Pollen: ${pollen.name} ${pollen.value}` });
  return flags;
}

// Every activity with a positive distance adds to the total; only distance sports without one count as missing.
function calendarWeekDistance(activities) {
  const measured = activities.filter((entry) => Number(entry.distance_m) > 0);
  const unmeasured = activities.filter((entry) => {
    const distance = Number(entry.distance_m);
    return distance <= 0 || Number.isNaN(distance);
  });
  const missing = unmeasured.filter((entry) => DISTANCE_SPORTS.has(String(entry.sport || "").toLowerCase())).length;
  if (!measured.length && !missing) return null;
  const total = measured.length
    ? AppFormat.distance(measured.reduce((sum, entry) => sum + Number(entry.distance_m), 0))
    : "–";
  return { value: total, note: missing ? `${countLabel(missing, "Aktivität", "Aktivitäten")} ohne Distanz` : null };
}

function countLabel(count, one, many) {
  return AppFormat.plural(count, one, many);
}

// Load sums only the sessions that carry a value and names the rest; no value at all hides the cell.
// A planned load (`all`) is one value of the whole week or none: a partial sum would read as the week's plan.
function calendarWeekLoad(items, { all = false } = {}) {
  const known = items.filter((item) => item.training_load != null && item.training_load !== "" && Number.isFinite(Number(item.training_load)));
  if (!known.length) return null;
  const missing = items.length - known.length;
  if (all && missing) return { value: "–", note: `${countLabel(missing, "Einheit", "Einheiten")} ohne Belastung` };
  return {
    // Whole points, as the week review shows the week's load (#4061).
    value: AppFormat.number(known.reduce((sum, item) => sum + Math.max(0, Number(item.training_load)), 0), { digits: 0 }),
    note: missing ? `${countLabel(missing, "Einheit", "Einheiten")} ohne Belastung` : null,
  };
}

// The week summary uses one duration format, hours and minutes.
function calendarClock(seconds) {
  const minutes = Math.round(Math.max(0, Number(seconds) || 0) / 60);
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}`;
}

function calendarHours(seconds) {
  return `${calendarClock(seconds)} h`;
}

function plannedDurationSeconds(entry) {
  return Math.max(0, Number(entry.duration_minutes ? Number(entry.duration_minutes) * 60 : entry.planned_duration_s) || 0);
}

const CALENDAR_SPORT_ICONS = { Radfahren: "🚴", "Rad indoor": "🚴", Laufen: "🏃", Schwimmen: "🏊", Kraft: "🏋" };

function calendarRoundedNumber(value, suffix) {
  if (value == null || value === "") return null;
  return calendarMetricNumber(Math.round(Number(value)), suffix);
}

// The intensity factor as a fraction; stored values above 2 are percent.
function calendarIntensityFactor(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return null;
  return AppFormat.number(number <= 2 ? number : number / 100, { digits: 2 });
}

// The average speed, else distance over moving time.
function calendarSpeedLabel(activity) {
  let speed = Number(activity?.avg_speed_mps);
  if (!Number.isFinite(speed) || speed <= 0) speed = Number(activity?.distance_m) / Number(activity?.moving_duration_s);
  return Number.isFinite(speed) && speed > 0 ? AppFormat.speed(speed * 3.6) : null;
}

function calendarIntensityLabel(value) {
  if (value == null || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return null;
  const percent = number > 0 && number <= 2 ? number * 100 : number;
  return `${Math.round(percent)} %`;
}

function calendarStartTime(value) {
  const match = /(?:T|\s)(\d{2}:\d{2})/.exec(String(value || ""));
  return match ? match[1] : null;
}

function plannedUnitStartTime(value) {
  // Date-only planned units are stored as midnight; that is not a start time.
  const time = calendarStartTime(value);
  return time === "00:00" ? null : time;
}

function appendCalendarFact(root, label, value) {
  if (value == null || value === "") return;
  root.append(h("span", null, h("strong", null, label), ` ${value}`));
}

function calendarBadge({ kind, icon, text, title }) {
  const badge = h("span", `planned-badge is-${kind}`, icon ? h("span", { "aria-hidden": "true" }, icon) : null, text);
  if (title) badge.title = title;
  return badge;
}

function calendarBadgeRow(specs) {
  return h("div", "planned-badges", specs.map(calendarBadge));
}

function competitionSportLabel(sport, environment) {
  if (sport === "ride" && environment === "virtual") return "Rad indoor";
  return ({ Cycling: "Radfahren", Ride: "Radfahren", ride: "Radfahren", VirtualRide: "Rad indoor", Running: "Laufen", Run: "Laufen", run: "Laufen", Swim: "Schwimmen", swim: "Schwimmen", Strength: "Kraft", strength: "Kraft", other: "Andere" })[sport] || sport || "–";
}

function competitionDate(competition, todayKey) {
  return AppFormat.relativeDay(competition.event_date, todayKey) !== null;
}

function competitionDistance(value) {
  if (value == null || String(value).trim() === "") return "Distanz unbekannt";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  if (numeric < 0) return "Distanz unbekannt";
  return numeric === 0 ? "0 km" : AppFormat.distance(numeric);
}

function competitionResult(result) {
  if (!result || typeof result !== "object") return null;
  const speed = Number(result.avg_speed_mps);
  const power = Number(result.avg_power);
  const hasSpeed = Number.isFinite(speed) && speed > 0;
  let effort = null;
  if (result.sport === "ride") {
    if (Number.isFinite(power) && power > 0) effort = `${Math.round(power)} W`;
    else if (hasSpeed) effort = AppFormat.speed(speed * 3.6);
  } else if (hasSpeed) {
    effort = result.sport === "swim" ? `${AppFormat.clock(100 / speed)} /100 m` : AppFormat.pace(1000 / speed);
  }
  const time = result.duration_s == null ? null : AppFormat.clock(result.duration_s);
  const value = [time, AppFormat.distance(result.distance_m), effort].filter(Boolean).join(" · ");
  if (!value) return null;
  return result.source ? `${value} (${result.source})` : value;
}

function competitionFact(labelText, value) {
  return h("div", null, h("span", null, labelText), h("strong", null, value || "–"));
}

function calendarEntryStatus(entry, dateKey, todayKey) {
  if (calendarActualActivity(entry)) return "completed";
  if (entry?.compliance?.status === "missed") return "missed";
  return dateKey === todayKey ? "today" : "planned";
}

function calendarStatusLabel(entry, dateKey, todayKey) {
  const status = calendarEntryStatus(entry, dateKey, todayKey);
  if (status === "completed") return entry.is_completed_activity ? "✓ Ungeplant" : "✓ Absolviert";
  if (status === "missed") return "✕ Nicht absolviert";
  return status === "today" ? "Heute geplant" : "Geplant";
}

function calendarRpeLabel(value) {
  if (value == null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 && number <= 10 ? calendarMetricNumber(number) : null;
}

// Structured complaints (#3766): the fixed body areas and sides in the athlete's words.
const COMPLAINT_AREA_LABELS = Object.freeze({ foot: "Fuß", ankle: "Sprunggelenk", achilles: "Achillessehne", calf: "Wade", shin: "Schienbein", knee: "Knie", thigh_front: "Oberschenkel vorne", thigh_back: "Oberschenkel hinten", hip: "Hüfte", groin: "Leiste", lower_back: "unterer Rücken", upper_back_neck: "oberer Rücken/Nacken", shoulder: "Schulter", arm_elbow: "Arm/Ellbogen", hand_wrist: "Hand/Handgelenk", other: "Sonstiges" });
const COMPLAINT_SIDE_LABELS = Object.freeze({ left: "links", right: "rechts", both: "beidseitig", none: "" });

function complaintLabel(item) {
  return [COMPLAINT_AREA_LABELS[item.area] || item.area, COMPLAINT_SIDE_LABELS[item.side] ?? ""].filter(Boolean).join(" ");
}

function plannedDayNotes(dayContext, competitions, dateKey, hasSession = false) {
  const notes = h("div", "planned-day-notes");
  for (const { kind, icon, text } of plannedDayNoteSpecs(dayContext, competitions, dateKey, hasSession)) {
    notes.append(h("p", `planned-day-context planned-day-${kind}`,
      icon ? h("span", { "aria-hidden": "true" }, `${icon} `) : null,
      kind === "calendar" ? h("span", "sr-only", "Kalender: ") : null,
      text));
  }
  const checkin = dayContext.checkin && typeof dayContext.checkin === "object" ? dayContext.checkin : {};
  const illness = String(checkin.illness || "").trim();
  const pain = String(checkin.pain || "").trim();
  const complaints = (Array.isArray(checkin.complaints) ? checkin.complaints : []).map((item) => `${complaintLabel(item)} ${item.severity}/10`);
  // Area, side and severity come before the athlete's own words (#3766).
  const complaintText = complaints.length ? `Beschwerden: ${[...complaints, pain].filter(Boolean).join(" · ")}` : "";
  if (illness || pain || complaints.length) {
    notes.append(h("p", "planned-day-context planned-day-health", [
      illness ? `Krankheit: ${illness}` : "",
      complaintText || (pain ? `Verletzung/Beschwerden: ${pain}` : ""),
    ].filter(Boolean).join(" · ")));
  }
  const hint = infectionHintNote(dayContext.infection_hint, dateKey, reportsComplaint(illness));
  if (hint) notes.append(hint);
  return notes;
}

// The athlete's answer to the hint (#3821): "Mir geht's gut" hides it for that day, "Ich bin krank"
// records illness for today like a check-in, with its receipt and undo. Neither is queued offline:
// both are answers for today, so a failed request shows its message.
const infectionHintDismissed = new Set();
const infectionHintReceipts = new Map();
// The symptoms of the illness answer: ids as in `SYMPTOM_LABELS` in backend/athlete/checkins.py.
const INFECTION_SYMPTOMS = Object.freeze([["schnupfen", "Schnupfen"], ["halsschmerzen", "Halsschmerzen"], ["husten", "Husten"], ["kopfschmerzen", "Kopfschmerzen"], ["fieber", "Fieber"], ["gliederschmerzen", "Gliederschmerzen"], ["muedigkeit", "Müdigkeit/Abgeschlagenheit"], ["brust_atemnot", "Druck auf der Brust/Atemnot"], ["magen_darm", "Magen-Darm"]]);

// A "keine"/"nein" answer reports no complaint, as `reports_complaint` in backend/athlete/checkins.py.
const NO_COMPLAINT_ANSWERS = new Set(["", "-", "keine", "kein", "nein", "nichts", "gesund", "none", "no", "n/a"]);
function reportsComplaint(text) {
  let answer = String(text || "").trim().toLowerCase();
  while (answer && ".!,;:".includes(answer[0])) answer = answer.slice(1);
  while (answer && ".!,;:".includes(answer.at(-1))) answer = answer.slice(0, -1);
  return !NO_COMPLAINT_ANSWERS.has(answer.trim());
}

function infectionHintNote(hint, dateKey, ill) {
  const text = infectionHintText(hint);
  if (!text || infectionHintDismissed.has(dateKey)) return null;
  const status = h("p", { className: "planned-move-status", role: "status" });
  const showReceipt = (receipt) => receipt && status.replaceChildren(receipt.text, ...(receipt.change?.id ? plannedMoveUndo(dateKey, receipt, status, infectionHintReceipts) : []));
  showReceipt(infectionHintReceipts.get(dateKey));
  const note = h("div", { className: "planned-day-context planned-day-health", dataset: { infectionHint: "" } }, h("p", null, text));
  const well = h("button", { type: "button", className: "secondary-button" }, "Mir geht's gut");
  const sick = h("button", { type: "button" }, "Ich bin krank");
  const confirm = h("button", { type: "button" }, "Bestätigen");
  const inputs = INFECTION_SYMPTOMS.map(([id]) => h("input", { type: "checkbox", value: id }));
  const buttons = [well, sick, confirm, ...inputs];
  const busy = (value) => { for (const button of buttons) button.disabled = value; };
  const answer = async (path, done, extra = {}) => {
    busy(true);
    try {
      await done(await api(path, { method: "POST", body: JSON.stringify({ date: dateKey, ...extra }) }));
    } catch (error) {
      busy(false);
      if (!isSessionEndedError(error)) status.replaceChildren(error.message);
    }
  };
  well.addEventListener("click", () => answer("/api/infection-hint/well", () => {
    infectionHintDismissed.add(dateKey);
    note.remove();
    announcePlanChange("Infekt-Hinweis für heute ausgeblendet");
  }));
  confirm.addEventListener("click", () => answer("/api/infection-hint/ill", async (result) => {
    const receipt = { text: "Krankheit für heute gespeichert.", change: result.change };
    infectionHintReceipts.set(dateKey, receipt);
    showReceipt(receipt);
    await globalThis.load?.();
  }, { symptoms: inputs.filter((input) => input.checked).map((input) => input.value) }));
  const actions = h("div", "coach-action-card-actions infection-hint-actions", well, sick);
  // "Ich bin krank" expands the card into the symptom chips; "Bestätigen" saves the illness with them.
  sick.addEventListener("click", () => {
    const chips = INFECTION_SYMPTOMS.map(([, label], index) => h("label", "chip", inputs[index], h("span", null, label)));
    actions.replaceChildren(h("div", "chip-group", ...chips), confirm);
    inputs[0].focus();
  });
  // Once today reports illness the question is answered; the receipt keeps its undo.
  if (!ill) note.append(actions);
  if (status.textContent || !ill) note.append(status);
  return note;
}

// M14 (#3762): last night's values that left the usual range; never a block or a diagnosis.
// Only the values (athlete decision): the limits and the source stay with the card and the Coach.
function infectionHintText(hint) {
  if (hint?.status !== "hint" || !Array.isArray(hint.signals)) return "";
  const unit = (value) => (value === "brpm" ? "/min" : ` ${value}`);
  const values = hint.signals.filter((signal) => signal.unusual).map((signal) => `${signal.label} ${calendarMetricNumber(signal.value)}${unit(signal.unit)} (üblich ${calendarMetricNumber(signal.lower)}–${calendarMetricNumber(signal.upper)})`);
  return values.length ? `Infekt-Hinweis: ${values.join(" · ")}` : "";
}

function plannedEntryDurationLabel(actual, entry) {
  if (actual) return AppFormat.duration(AppFormat.sessionSeconds(actual));
  if (entry.duration_minutes) return AppFormat.duration(Number(entry.duration_minutes) * 60);
  return AppFormat.duration(entry.planned_duration_s);
}

// The planned name with its category when they differ, else the category.
function plannedStrengthName(exercise) {
  const category = AppFormat.strengthCategory(exercise.category);
  const planned = typeof exercise.name === "string" && exercise.name ? exercise.name : null;
  if (!planned) return category || "Übung";
  return category && category !== planned ? `${planned} (${category})` : planned;
}

function plannedStrengthRows(strength) {
  if (!strength?.exercises?.length) return null;
  return h("ul", "planned-strength", strength.exercises.map((exercise) => {
    const load = Number(exercise.planned_kg) > 0 ? ` @ ${calendarMetricNumber(exercise.planned_kg)} kg` : "";
    const plan = `${exercise.planned_sets}×${exercise.planned_reps}${load}`;
    const name = plannedStrengthName(exercise);
    // A unit without recorded reps names none ("Ist – Wdh." says nothing), a plan without weight no "0 kg" (#3932 K-16).
    const reps = (exercise.reps || []).map((value) => value ?? "?").join("/");
    const done = reps ? ` · Ist ${reps} Wdh.` : "";
    if (exercise.status !== "matched") return h("li", null, `${name}: nicht zugeordnet · Plan ${plan}`);
    return h("li", null, `${name}: ${calendarMetricNumber(exercise.completed_sets)}/${exercise.planned_sets} Sätze erreicht${done} · Plan ${plan}`);
  }));
}

function appendPlannedCalendarComparison(details, entry, actual) {
  if (!actual || entry.is_completed_activity) return;
  const plannedDuration = entry.duration_minutes ? Number(entry.duration_minutes) * 60 : entry.planned_duration_s;
  // The actual values and the share of the plan are shown once, above and in the status.
  const planLine = h("p", null, `Plan: ${[
    entry.name,
    AppFormat.duration(plannedDuration),
    entry.training_load != null ? `Belastung ${calendarMetricNumber(entry.training_load)}` : null,
  ].filter(Boolean).join(" · ")}`);
  details.append(h("div", "planned-comparison", planLine, plannedStrengthRows(entry.compliance?.strength_sets)));
}

// "Zusätzlich" only means something next to a plan, so an extra activity in a week without one shows no status.
function appendPlannedEntryStatus(cardSummary, entry, status, restDayLabel, dateKey, todayKey, weekHasPlan) {
  if (status === "planned" || status === "today") return;
  if (entry.is_completed_activity && !weekHasPlan) return;
  if (cardSummary.textContent) cardSummary.append(" · ");
  const statusText = h("span", { className: "planned-entry-status", textContent: status === "skipped" ? `Entfallen (${restDayLabel})` : calendarStatusLabel(entry, dateKey, todayKey) });
  const percentage = calendarMetricNumber(entry.compliance?.percentage);
  const basis = { training_load: "Belastung", duration: "Dauer", strength_sets: "Sätze" }[entry.compliance?.basis];
  if (status === "completed" && percentage != null && basis) {
    statusText.textContent += ` · ${percentage} %`;
    statusText.title = `Anteil am Plan (${basis})`;
    statusText.setAttribute("aria-label", `${statusText.textContent.replace(" %", " Prozent")} des Plans · ${basis}`);
  }
  cardSummary.append(statusText);
}

function adaptiveForecastNote(forecast, formatTime) {
  if (!forecast?.fetched_at) return "";
  return `Wetterprognose Stand ${formatTime(forecast.fetched_at)}${forecast.stale ? " (veraltet)" : ""}`;
}

function adaptiveIllnessPending(data, preview) {
  const illness = String(data.local_feedback?.today?.illness || "").trim();
  const previewIllness = String(preview?.illness_pause?.illness || "").trim();
  const needsForecast = Boolean(illness && (!preview?.illness_pause || previewIllness !== illness || !preview.illness_pause?.approved));
  const pendingPause = Boolean(preview?.status === "preview" && preview?.illness_pause && !preview.illness_pause.approved);
  return needsForecast || pendingPause;
}

function adaptiveCaption(illnessPending, count, returnPending = false) {
  if (illnessPending) return "Krankheit gemeldet: Sportpause prognostizieren und bestätigen.";
  if (returnPending && !count) return "Rückkehr nach Krankheit: nächste Stufe bestätigen.";
  if (count === 1) return "Ein zukünftiger Entwurf braucht eine Anpassung.";
  return `${count} zukünftige Entwürfe brauchen eine Anpassung.`;
}

// Completed activities carry a canonical sport and environment; planned units keep their type.
function calendarSportType(item) {
  if (!item || item.type || !item.sport) return item;
  const indoor = item.sport === "ride" && ["indoor", "virtual"].includes(item.environment);
  return { type: indoor ? "VirtualRide" : item.sport };
}

const FOCUS_LABELS = {
  recovery: "Regeneration", easy: "Locker", endurance: "Grundlage", long: "Lang", tempo: "Tempo", threshold: "Schwelle", sweet_spot: "Sweet Spot",
  vo2max: "VO2max", anaerobic: "Anaerob", race: "Wettkampf", test: "Test",
  legs: "Beine", upper: "Oberkörper", full_body: "Ganzkörper", core: "Rumpf", plyometrics: "Sprungkraft", mobility: "Mobilität",
};
const STRENGTH_INTENSITY_LABELS = { light: "Leicht", moderate: "Mittel", heavy: "Schwer" };
const FOCUS_OPTIONS = {
  run: ["recovery", "easy", "endurance", "long", "tempo", "threshold", "vo2max", "anaerobic", "race", "test"],
  ride: ["recovery", "easy", "endurance", "long", "tempo", "threshold", "sweet_spot", "vo2max", "anaerobic", "race", "test"],
  strength: ["legs", "upper", "full_body", "core", "plyometrics", "mobility"],
};

function focusSport(entry) {
  const sport = String(entry?.sport || "").toLowerCase();
  return FOCUS_OPTIONS[sport] ? sport : null;
}

function focusChipSpec(entry) {
  const label = FOCUS_LABELS[entry?.session_focus];
  if (!label) return null;
  const intensityLabel = STRENGTH_INTENSITY_LABELS[entry.strength_intensity];
  const intensity = intensityLabel && entry.intensity_origin === "derived" ? `≈${intensityLabel}` : intensityLabel;
  const derived = entry.focus_origin === "derived";
  // "≈" says the value is read from the unit's content, not set by the athlete (#3932 K-06).
  return { kind: derived ? "focus-derived" : "focus", text: [derived ? "≈" : null, label, intensity].filter(Boolean).join(" "), derived, title: derived ? "≈ vom Inhalt der Einheit abgeleitet" : null };
}

const HR_CAP_SOURCE_LABELS = { default: "Standard", measured: "gemessen", estimated: "DFA-α1" };

function hrCapChipSpec(entry) {
  const percent = Number(entry?.hr_cap?.percent_lthr);
  const source = HR_CAP_SOURCE_LABELS[entry?.hr_cap?.source];
  if (!Number.isFinite(percent) || !source) return null;
  const high = entry?.hr_cap_bpm?.high;
  const bpm = typeof high === "number" ? high : Number.NaN;
  const parts = [`≤ ${percent} % LSHF`, Number.isFinite(bpm) ? `${bpm} bpm` : null, source];
  return { kind: "focus", text: parts.filter(Boolean).join(" · "), derived: false };
}

function appendFocusChip(root, entry) {
  const chip = focusChipSpec(entry);
  const cap = hrCapChipSpec(entry);
  if (!chip && !cap) return;
  const row = calendarBadgeRow([chip, cap].filter(Boolean));
  row.dataset.focusChip = chip?.derived ? "derived" : "set";
  if (chip?.derived) row.firstElementChild.setAttribute("aria-label", `${chip.text} (abgeleitet)`);
  root.append(row);
}

// A unit is identified by its local id, else by provider id and day; the open
// cards and the "Gespeichert" status survive the re-render of a calendar reload.
function plannedEntryKey(entry, dateKey) {
  if (entry?.local_id) return `local:${entry.local_id}`;
  const id = entry?.id ?? entry?.activity_id;
  return id ? `id:${id}@${dateKey}` : "";
}

let openPlannedEntries = new Set();
// Cards the next rebuild opens although they are not shown yet: a unit just saved with its receipt (#3950).
const plannedForceOpen = new Set();
// Calendar cards whose "Details" are open, so a re-render keeps them open (#2258).
const openActivityDetails = new Set();
// One read of a session's detail document per calendar data, shared by the zone bar and "Details" (#2478).
const plannedSessionReads = new Map();

// `fresh` (a retry or a detail refresh in "Details") replaces the shared read, so a rebuild shows the new document.
function plannedSessionRead(activityId, fresh = false) {
  let read = fresh ? null : plannedSessionReads.get(activityId);
  if (!read) {
    read = api(`/api/activities/${encodeURIComponent(activityId)}`, { timeoutMs: DATA_LOAD_TIMEOUT_MS });
    // A failed read is not shared: the next opening or retry reads again.
    read.catch(() => { if (plannedSessionReads.get(activityId) === read) plannedSessionReads.delete(activityId); });
    plannedSessionReads.set(activityId, read);
  }
  return read;
}

function completedSessionIds(calendar) {
  return new Set(calendar.map((item) => calendarActualActivity(item)).filter(Boolean).map((actual) => String(actual.activity_id || actual.id)));
}
const plannedFocusStatuses = new Map();
// The receipt of a moved unit by card key ({ text, change, from }); the open card shows it after the re-render.
const plannedMoveReceipts = new Map();
// The receipt of a deleted unit by its day (#3027): the day shows it once the card is gone.
const plannedDeleteReceipts = new Map();
const MOVE_DAY_FORMAT = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
const MOVE_WEEKDAY_FORMAT = new Intl.DateTimeFormat("de-DE", { weekday: "short" });

// The summary chip follows the saved focus without waiting for the next reload.
function refreshFocusChip(card, entry) {
  const holder = h("span");
  appendFocusChip(holder, entry);
  const row = card.querySelector("summary [data-focus-chip]");
  if (row) row.replaceWith(...holder.childNodes);
  else card.querySelector("summary .planned-session-line")?.append(...holder.childNodes);
}

function focusSelect(label, name, options, selected) {
  const select = h("select", { name }, [["", "–"], ...options].map(([value, text]) => h("option", { value, selected: value === (selected || "") }, text)));
  return { field: h("label", "planned-focus-field", label, select), select };
}

// A planned unit's load; a completed one shows its values in the facts.
function plannedSessionMetrics(entry) {
  const load = calendarMetricNumber(entry.training_load);
  if (load == null) return null;
  return h("span", "planned-session-metrics", `Belastung ${load}`);
}

function profileSegmentExtent(item, axis) {
  return Number.isFinite(item[axis]) && item[axis] > 0 ? item[axis] : 0;
}

function profileExtent(profile) {
  const axis = profile.axis === "distance" ? "distance" : "duration";
  return profile.segments.reduce((sum, item) => sum + profileSegmentExtent(item, axis), 0);
}

// The open card whose body is shown: not inside a collapsed group.
function shownPlannedEntry(root) {
  return [...(root?.querySelectorAll(".planned-entry[open]") || [])].find((card) => !card.parentElement.closest("details:not([open])")) || null;
}

// The detail pane's status: as on the card, but always named.
function plannedDetailStatus(entry, dateKey, todayKey, restDayLabel, weekHasPlan) {
  if (restDayLabel) return `Entfallen (${restDayLabel})`;
  if (entry.is_completed_activity && !weekHasPlan) return "✓ Absolviert";
  return calendarStatusLabel(entry, dateKey, todayKey);
}

const DETAIL_DATE_FORMAT = new Intl.DateTimeFormat("de-DE", { weekday: "long", day: "2-digit", month: "2-digit" });

// Extra activities that look the same (title, sport, duration, distance, no profile) collapse into one row.
function plannedEntryGroups(dayEntries) {
  const groups = [];
  for (const entry of dayEntries) {
    const key = entry.is_completed_activity && !entry.workout_profile
      ? JSON.stringify([entry.name || "", entry.type || entry.sport || "", entry.environment || "", AppFormat.sessionSeconds(entry) ?? null, Number(entry.distance_m) > 0 ? Number(entry.distance_m) : null])
      : null;
    const previous = groups.at(-1);
    if (key && previous?.key === key) previous.entries.push(entry);
    else groups.push({ key, entries: [entry] });
  }
  return groups;
}

function appendWeekMetricCell(grid, cell) {
  grid.append(h("strong", null, cell?.value ?? "–", cell?.note ? h("small", null, cell.note) : null));
}

function plannedZoneLegend(days) {
  const zones = [...new Set([...days.querySelectorAll(".calendar-workout-profile [data-zone]")].map((node) => node.dataset.zone))]
    .filter((zone) => zone !== "unknown")
    .sort((left, right) => Number(left) - Number(right));
  if (!zones.length) return null;
  return h("p", "planned-zone-legend", zones.map((zone) => h("span", null, h("i", { dataset: { zone }, "aria-hidden": "true" }), `Z${zone}`)));
}

// Fixed grid with one column each for planned and completed values; a row appears only when it has a value.
function plannedWeekMetrics(planned, actual) {
  const columns = [planned.length ? "planned" : null, actual.length ? "actual" : null].filter(Boolean);
  if (!columns.length) return null;
  const totals = h("span", { className: "planned-week-metrics", dataset: { columns: String(columns.length) } });
  const plannedSeconds = planned.reduce((total, entry) => total + plannedDurationSeconds(entry), 0);
  const actualSeconds = actual.reduce((total, entry) => total + Math.max(0, Number(AppFormat.sessionSeconds(entry)) || 0), 0);
  const plannedDistance = planned.reduce((total, entry) => total + Math.max(0, Number(entry.distance) || 0), 0);
  const rows = [
    ["Dauer", { planned: plannedSeconds > 0 ? { value: calendarHours(plannedSeconds) } : null, actual: { value: calendarHours(actualSeconds) } }],
    ["Distanz", { planned: plannedDistance > 0 ? { value: AppFormat.distance(plannedDistance) } : null, actual: calendarWeekDistance(actual) }],
    ["Belastung", { planned: calendarWeekLoad(planned, { all: true }), actual: calendarWeekLoad(actual) }],
  ];
  totals.append(h("span"));
  for (const column of columns) totals.append(h("span", "planned-week-column", column === "planned" ? "Geplant" : "Absolviert"));
  for (const [label, cells] of rows) {
    if (!columns.some((column) => cells[column])) continue;
    totals.append(h("span", null, label));
    for (const column of columns) appendWeekMetricCell(totals, cells[column]);
  }
  return totals.childElementCount > columns.length + 1 ? totals : null;
}

// The lists are re-rendered as a whole; only the outcome of a user action is announced.
function announcePlanChange(message) {
  const region = $("#planListStatus");
  if (region) region.textContent = message;
}

function renderAdaptivePlanning(data) {
  const planning = data.planning || {};
  const preview = planning.latest_replan;
  const changes = Array.isArray(preview?.changes) ? preview.changes : [];
  const illnessPending = adaptiveIllnessPending(data, preview);
  const required = Boolean(planning.needs_replan || illnessPending);
  const caption = adaptiveCaption(illnessPending, Number(planning.replan_changes || changes.length), Boolean(planning.illness_return_pending));
  const weatherChanged = changes.some((change) => Array.isArray(change?.blocking_triggers) && change.blocking_triggers.includes("weather"));
  const forecastNote = weatherChanged ? adaptiveForecastNote(preview?.weather_forecast, formatTime) : "";
  const coachNotice = $("#coachAdaptivePlanningNotice");
  if (coachNotice) {
    coachNotice.hidden = !required;
    const detail = coachNotice.querySelector("small");
    if (detail) detail.textContent = required ? [caption, forecastNote ? `${forecastNote}.` : "", "Bitte den Coach um die Anpassung."].filter(Boolean).join(" ") : "";
  }
}

function renderExternalCalendar(data) {
  const calendar = data.external_calendar || {};
  renderUndecidedNavCount();
  const syncButton = $("#externalCalendarSyncButton");
  setConnectionChip("#externalCalendarConnectionStatus", connectionKind({ configured: calendar.configured, error: Boolean(calendar.last_error) }));
  const detail = $("#externalCalendarConnectionDetail");
  if (detail) detail.textContent = externalCalendarDetail(calendar);
  if (syncButton) {
    syncButton.disabled = Boolean(calendar.running || state.localSync.externalCalendar);
    syncButton.textContent = calendar.running || state.localSync.externalCalendar ? "Synchronisierung läuft…" : "Synchronisieren";
  }
}


function planWeekStart(dateKey) {
  const value = dateFromKey(dateKey);
  if (Number.isNaN(value.valueOf())) return "";
  const weekday = value.getDay();
  value.setDate(value.getDate() - (weekday === 0 ? 6 : weekday - 1));
  return localDateKey(value);
}

function planWeekLabel(weekStartKey) {
  const start = dateFromKey(weekStartKey);
  const end = dateFromKey(addDateKey(weekStartKey, 6));
  if (Number.isNaN(start.valueOf()) || Number.isNaN(end.valueOf())) return weekStartKey;
  const startLabel = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit" }).format(start);
  const endLabel = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" }).format(end);
  return `${startLabel} – ${endLabel}`;
}

function plannedWeatherLabel(weather) {
  if (!weather || typeof weather !== "object") return "";
  const hasForecast = weather.condition || weather.weather_code != null
    || weather.temperature_min != null || weather.temperature_max != null;
  if (!hasForecast) return "";
  const temperatures = [weather.temperature_min, weather.temperature_max]
    .filter((value) => value != null && value !== "" && Number.isFinite(Number(value)))
    .map((value) => weatherNumber(value, "°"));
  return [weatherIconFor(weather), temperatures.join(" / ")].filter(Boolean).join(" ");
}

// Rain, wind and air details stay out of the day card; they are the tooltip/accessible name of the small weather symbol.
function plannedWeatherDetails(weather) {
  const directionValue = calendarMetricNumber(weather.wind_direction_dominant);
  const direction = directionValue != null && Number(weather.wind_direction_dominant) >= 0 && Number(weather.wind_direction_dominant) <= 360
    ? weatherDirection(weather.wind_direction_dominant) : "";
  const peakTime = /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(weather.rain_peak_time || "") ? weather.rain_peak_time : "";
  const values = [
    calendarMetricNumber(weather.precipitation_probability_max, ` % Regen${peakTime ? " (max. " + peakTime + " Uhr)" : ""}`),
    calendarMetricNumber(weather.wind_speed_max, ` km/h Wind${direction ? " " + direction : ""}`),
    calendarMetricNumber(weather.wind_gusts_max, " km/h Böen"),
  ].filter(Boolean);
  if (peakTime && weather.precipitation_probability_max == null) values.unshift(`Regen am ehesten ${peakTime} Uhr`);
  return [
    weather.archived_forecast ? "Gespeicherte Wettervorhersage" : "Wettervorhersage",
    weather.condition,
    values.join(" · "),
    plannedAirValues(weather).join(" · "),
  ].filter(Boolean);
}

// Without a location the hint's link opens the profile at that field (#3932 K-03).
function openWeatherLocationField(event) {
  // A modified click keeps the browser's own behaviour (new tab, new window).
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button > 0) return;
  event.preventDefault();
  void AppRouter.navigate("more/profile", { historyMode: "push" }).then((shown) => {
    const field = shown && document.querySelector('#profileForm [name="weather_location"]');
    if (!field) return;
    field.closest("details")?.setAttribute("open", "");
    field.focus();
  });
}

function plannedWeatherHint() {
  if (state.data?.weather?.configured) return h("p", "planned-weather-hint", "Für die kommenden Tage liegt noch keine Wettervorhersage vor.");
  const link = h("a", { className: "secondary-button", href: "#more/profile" }, "Wetterort angeben");
  link.addEventListener("click", openWeatherLocationField);
  return h("p", "planned-weather-hint", "Wetter fehlt: Im Profil ist kein Wetterort hinterlegt. ", link);
}

function plannedWeekSummary(weekKey, weekEndKey, weekEntries, compliance, todayKey) {
  const plannedEntryCount = weekEntries.filter((entry) => !entry.is_completed_activity).length;
  const plannedUnits = Number.isFinite(Number(compliance?.planned_units))
    ? Number(compliance.planned_units)
    : plannedEntryCount;
  const completedUnits = Number.isFinite(Number(compliance?.completed_units))
    ? Number(compliance.completed_units)
    : 0;
  const isPast = weekEndKey < todayKey;
  const units = isPast || completedUnits > 0
    ? `${plannedUnits} geplant · ${completedUnits} absolviert`
    : `${plannedUnits} geplant`;
  if (compliance?.basis !== "training_load" || compliance.planned_value == null) return units;
  // One load figure: the done part of the plan once a unit is done, else the plan (#3932 K-13).
  const planned = formatWhole(compliance.planned_value);
  const load = isPast || completedUnits > 0 ? `${formatWhole(compliance.actual_value || 0)} von ${planned}` : planned;
  return `${units} · Belastung ${load}`;
}

// The pace of a run is the stored average speed, as the Details show it, else distance over moving time (#3932 K-04).
function calendarPaceLabel(activity) {
  if (activitySportLabel(calendarSportType(activity)) !== "Laufen") return null;
  const speed = Number(activity?.avg_speed_mps);
  if (speed > 0) return formatPace(1000 / speed);
  const duration = Number(activity?.moving_duration_s);
  const distance = Number(activity?.distance_m);
  return duration > 0 && distance > 0 ? formatPace(duration / (distance / 1000)) : null;
}

function calendarCountLabel(entries, todayKey) {
  const counts = { completed: 0, planned: 0, missed: 0 };
  entries.forEach((entry) => {
    const status = calendarEntryStatus(entry, plannedEventDate(entry), todayKey);
    if (status === "completed") counts.completed += 1;
    else if (status === "missed") counts.missed += 1;
    else counts.planned += 1;
  });
  return [
    counts.completed ? `${counts.completed} abgeschlossen` : "",
    counts.planned ? `${counts.planned} geplant` : "",
    counts.missed ? `${counts.missed} nicht absolviert` : "",
  ].filter(Boolean).join(" · ");
}

// Entering the calendar shows the current week in the week view and scrolls to today.
function focusPlannedToday() {
  if (!state.plannedTodayFocusPending || !state.loadedAreas.has("plan")) return false;
  if (AppRouter.baseRoute(state.route) !== "plan" || AppRouter.planSegmentFromRoute(state.route) !== "overview") return false;
  const currentWeekKey = planWeekStart(timezoneDateKey(state.data?.profile?.timezone, new Date()));
  if (selectedWeekKey !== currentWeekKey || calendarView !== "week") {
    selectedWeekKey = currentWeekKey;
    calendarView = "week";
    selectedDayKey = "";
    renderPlanned(renderedCalendar);
  }
  const today = $("#plannedCalendar")?.querySelector(".planned-day.is-today");
  if (!today) return false;
  state.plannedTodayFocusPending = false;
  today.scrollIntoView({ block: "start", behavior: "auto" });
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (AppRouter.baseRoute(state.route) === "plan" && today.isConnected) today.scrollIntoView({ block: "start", behavior: "auto" });
  }));
  return true;
}

// Each value once; duration and distance stay in the card's row.
// The values a session shows, per sport (athlete decision #2258); a value without data is left out.
function actualCalendarFacts(actual) {
  const rpe = calendarRpeLabel(actual.rpe);
  const values = {
    Belastung: calendarMetricNumber(actual.training_load),
    RPE: rpe != null ? `${rpe}/10` : null,
    Intensität: calendarIntensityLabel(actual.intensity),
    "Ø Herzfrequenz": calendarMetricNumber(actual.avg_hr, " bpm"),
    "Ø Leistung": calendarMetricNumber(actual.avg_power, " W"),
    Pace: calendarPaceLabel(actual),
    Anstieg: calendarMetricNumber(actual.elevation_gain_m, " m"),
  };
  const sport = activitySportLabel(calendarSportType(actual));
  if (sport === "Laufen") {
    return [
      ["Pace", values.Pace], ["Ø Herzfrequenz", values["Ø Herzfrequenz"]],
      // Runs store the cadence per leg; the step frequency counts both legs.
      ["Schrittfrequenz", Number(actual.avg_cadence) > 0 ? calendarRoundedNumber(Number(actual.avg_cadence) * 2, " spm") : null],
      ["Ø Leistung", values["Ø Leistung"]], ["Belastung", values.Belastung], ["RPE", values.RPE],
      ["Bodenkontaktzeit", calendarRoundedNumber(actual.ground_contact_time_ms, " ms")],
      ["Anstieg", values.Anstieg],
    ];
  }
  if (sport === "Radfahren" || sport === "Rad indoor") {
    return [
      ["Normalized Power", calendarMetricNumber(actual.normalized_power, " W")], ["Ø Leistung", values["Ø Leistung"]],
      ["Intensitätsfaktor", calendarIntensityFactor(actual.intensity)], ["Ø Herzfrequenz", values["Ø Herzfrequenz"]],
      ["Trittfrequenz", calendarRoundedNumber(actual.avg_cadence, " rpm")],
      ["Belastung", values.Belastung], ["RPE", values.RPE],
      ["Ø Geschwindigkeit", calendarSpeedLabel(actual)],
      ["Anstieg", values.Anstieg],
    ];
  }
  return Object.entries(values);
}

// Each value once; duration and distance stay in the card's row.
function appendActualCalendarDetails(details, actual) {
  if (!actual) return;
  const facts = h("div", "planned-actual-facts");
  for (const [label, value] of actualCalendarFacts(actual)) appendCalendarFact(facts, label, value);
  if (facts.childElementCount) details.append(facts);
}

// One compact row: sport icon, title, sport/time/status line, duration and distance.
// What the duration of a row is, for its tooltip: a session's moving time (strength: its total time), else the plan (#3932 K-04).
function durationTitle(actual) {
  if (!actual) return "Geplante Dauer";
  return calendarSportType(actual) === "strength" ? "Gesamtzeit" : "Bewegungszeit";
}

function plannedSessionHeader(entry, actual, title, appendStatus, slim = false) {
  const displayed = actual || entry;
  const sportLabel = activitySportLabel(calendarSportType(displayed));
  const icon = h("span", "planned-session-icon", CALENDAR_SPORT_ICONS[sportLabel] || "•");
  // A slim row's line has no sport word, so its icon names the sport.
  if (slim) {
    icon.setAttribute("role", "img");
    icon.setAttribute("aria-label", sportLabel);
  } else icon.setAttribute("aria-hidden", "true");
  // A done or missed unit's icon names the sport; its line keeps the time and the status.
  const line = h("span", { className: "planned-session-line", textContent: [slim ? null : sportLabel, actual ? calendarStartTime(actual.started_at_local) : plannedUnitStartTime(entry.starts_at_local)].filter(Boolean).join(" · ") });
  appendStatus(line);
  const shownDistance = actual ? actual.distance_m : entry.distance;
  const value = h("span", "planned-session-value",
    h("strong", { title: durationTitle(actual) }, plannedEntryDurationLabel(actual, entry) || "–"),
    shownDistance > 0 ? h("span", null, distanceLabel(shownDistance)) : null);
  return h("span", "planned-session-header", icon, h("strong", "planned-session-title", title), line, value);
}

function calendarWorkoutProfile(profile) {
  const segments = profile?.segments;
  if (!Array.isArray(segments) || !segments.length || segments.length > 1000) return null;
  const axis = profile.axis === "distance" ? "distance" : "duration";
  const extent = (item) => profileSegmentExtent(item, axis);
  const total = profileExtent(profile);
  const values = segments.flatMap((item) => [item.value, item.end_value]).filter((value) => value != null && Number.isFinite(value) && value >= 0);
  if (!total || !values.length) return null;
  const top = Math.max(...values, 1) * 1.1;
  const baseLabel = profile.source === "recorded" ? "Aufgezeichnetes Belastungsprofil" : "Geplantes Intervallprofil";
  const label = profile.estimated ? `${baseLabel} (Zonen aus FTP oder Schwellenpuls)` : baseLabel;
  const svg = svgNode("svg", { viewBox: "0 0 300 56", role: "img", "aria-label": label, class: "calendar-workout-profile", preserveAspectRatio: "none" });
  svg.append(svgNode("title", {}, `${label} · ${axis === "distance" ? "Distanzachse" : "Zeitachse"} · ${profile.unit || ""}`));
  svg.append(svgNode("line", { x1: 0, x2: 300, y1: 54, y2: 54, class: "calendar-profile-baseline" }));
  let offset = 0;
  for (const item of segments) {
    const size = extent(item);
    if (!size) continue;
    const x = offset / total * 300, width = size / total * 300;
    offset += size;
    const shape = calendarProfileShape(item, x, width, top);
    if (!shape) continue;
    shape.append(svgNode("title", {}, `${axis === "distance" ? distanceLabel(size) : formatDuration(size)} · ${item.label || ""}`));
    svg.append(shape);
  }
  return svg;
}

function calendarProfileShape(item, x, width, top) {
  if (item.value == null || item.end_value == null || !Number.isFinite(item.value) || !Number.isFinite(item.end_value)) return null;
  const left = 54 - Math.max(0, item.value) / top * 50;
  const right = 54 - Math.max(0, item.end_value) / top * 50;
  const zone = Number.isInteger(item.zone) && item.zone >= 1 && item.zone <= 7 ? item.zone : "unknown";
  // A hairline gap keeps neighbouring intervals apart without hiding a short one.
  const gap = Math.min(0.6, width / 4);
  const start = x + gap / 2, end = x + width - gap / 2;
  return svgNode("polygon", { points: `${start},54 ${start},${left} ${end},${right} ${end},54`, "data-zone": zone });
}

// The detail view's larger profile with its axis from start to the whole duration or distance.
function plannedDetailProfile(profile) {
  const svg = calendarWorkoutProfile(profile);
  if (!svg) return null;
  svg.classList.add("is-detail");
  const total = profileExtent(profile);
  const end = profile.axis === "distance" ? distanceLabel(total) : AppFormat.duration(total);
  const axis = h("div", { className: "planned-detail-axis", "aria-hidden": "true" }, ["0", end].map((text) => h("span", null, text)));
  return h("div", "planned-detail-profile", svg, axis);
}

// Title, day and status of the selected unit; on wide screens it heads the detail pane.
function plannedDetailHead(entry, actual, title, dateKey, status, statusLabel, weather) {
  return h("div", "planned-detail-head",
    h("strong", "planned-detail-title", title),
    h("span", "planned-detail-line", [
      DETAIL_DATE_FORMAT.format(dateFromKey(dateKey)),
      actual ? calendarStartTime(actual.started_at_local) : plannedUnitStartTime(entry.starts_at_local),
      activitySportLabel(calendarSportType(actual || entry)),
      plannedWeatherLabel(weather),
    ].filter(Boolean).join(" · ")),
    // The library template the unit comes from (#2379).
    entry?.template_name ? h("span", "planned-detail-line planned-detail-template", `Vorlage: ${entry.template_name}`) : null,
    h("span", `planned-detail-status is-${status}`, statusLabel));
}

function plannedFocusForm(entry, dateKey) {
  const sport = focusSport(entry);
  if (!sport || !entry.local_id || entry.is_completed_activity) return null;
  const isSet = entry.focus_origin === "set";
  const form = h("div", "planned-focus-form");
  const focus = focusSelect("Art der Einheit", "session_focus", FOCUS_OPTIONS[sport].map((value) => [value, FOCUS_LABELS[value]]), isSet ? entry.session_focus : "");
  const parts = [focus];
  if (sport === "strength") parts.push(focusSelect("Intensität", "strength_intensity", Object.entries(STRENGTH_INTENSITY_LABELS), entry.intensity_origin === "set" ? entry.strength_intensity : ""));
  const statusKey = plannedEntryKey(entry, dateKey);
  const status = h("span", { className: "planned-focus-status", textContent: plannedFocusStatuses.get(statusKey) || "" });
  status.setAttribute("role", "status");
  const previous = parts.map((part) => part.select.value);
  const save = async () => {
    status.textContent = "";
    plannedFocusStatuses.delete(statusKey);
    try {
      const body = { session_focus: focus.select.value || null, strength_intensity: parts[1]?.select.value || null };
      await api(`/api/planned-units/${encodeURIComponent(entry.local_id)}/focus`, { method: "POST", body: JSON.stringify(body) });
      previous.splice(0, previous.length, ...parts.map((part) => part.select.value));
      status.textContent = "Gespeichert";
      if (statusKey) plannedFocusStatuses.set(statusKey, "Gespeichert");
      const card = form.closest(".planned-entry");
      if (card) {
        const saved = { ...entry, session_focus: focus.select.value || entry.session_focus, focus_origin: focus.select.value ? "set" : entry.focus_origin };
        if (parts[1]?.select.value) Object.assign(saved, { strength_intensity: parts[1].select.value, intensity_origin: "set" });
        refreshFocusChip(card, saved);
      }
      return true;
    } catch (error) {
      parts.forEach((part, index) => { part.select.value = previous[index]; });
      status.textContent = error.message;
      return false;
    }
  };
  for (const part of parts) part.select.addEventListener("change", save);
  form.append(...parts.map((part) => part.field), status);
  // A derived value is offered next to its empty field; one tap sets it.
  const derived = [
    [focus, entry.focus_origin === "derived" ? entry.session_focus : "", FOCUS_LABELS],
    [parts[1], entry.intensity_origin === "derived" ? entry.strength_intensity : "", STRENGTH_INTENSITY_LABELS],
  ];
  for (const [part, value, labels] of derived) {
    if (!part || !value || !labels[value]) continue;
    const chip = h("button", { type: "button", className: "planned-suggestion-chip" }, `Als Art setzen: ${labels[value]}`);
    chip.addEventListener("click", async () => {
      part.select.value = value;
      if (await save()) chip.remove();
    });
    part.field.after(chip);
  }
  return form;
}

// Leftovers of every provider, deduplicated, keyed by the week whose button removes them.
function plannedLeftoversByWeek(trainingCalendar, currentWeekKey, lastWeekKey) {
  const byWeek = new Map();
  for (const [provider, item] of plannedLeftoverItems(trainingCalendar)) {
    const week = plannedLeftoverWeek(item, currentWeekKey, lastWeekKey, planWeekStart);
    const lists = byWeek.get(week) || {};
    lists[provider] = [...(lists[provider] || []), item];
    byWeek.set(week, lists);
  }
  return byWeek;
}

// Each configured provider's leftovers once, as [provider, item] pairs.
function plannedLeftoverItems(trainingCalendar) {
  const ids = providerIds();
  const configured = plannedProviderConfiguration();
  const providers = new Set([ids.garmin, ids.intervals, ids.wahoo].filter((provider) => configured[provider] === true));
  const found = new Map();
  for (const entry of Array.isArray(trainingCalendar) ? trainingCalendar : []) {
    for (const [provider, items] of Object.entries(entry?.push_cleanup || {})) {
      if (!providers.has(provider) || !Array.isArray(items)) continue;
      for (const item of items) if (item?.id) found.set(provider + ":" + item.id, [provider, item]);
    }
  }
  return [...found.values()];
}

function plannedProviderConfiguration() {
  const ids = providerIds();
  return { [ids.intervals]: Boolean(state.data?.configured?.[ids.intervals]), [ids.garmin]: Boolean(state.data?.garmin?.configured) && state.data.garmin.can_write_workouts !== false, [ids.wahoo]: Boolean(state.data?.wahoo?.connected) };
}

// The week's sync jobs from its calendar entries and leftovers; none when nothing waits. Each job
// carries the hashes the units were shown with, so a unit edited meanwhile is refused instead of sent.
function plannedWeekJobs(weekEntries, leftovers, todayKey) {
  const units = weekEntries
    .filter((entry) => !entry.is_completed_activity)
    .map((entry) => ({ entry, dateKey: plannedEventDate(entry), completed: Boolean(calendarActualActivity(entry)) }));
  return plannedWeekSyncJobs(units, leftovers || {}, todayKey, plannedProviderConfiguration(), providerIds());
}

// One button per week: it sends every unit of the week a provider still waits for and removes
// what the app left there for that week; its tooltip names what it removes.
function plannedWeekSyncButton(weekKey, weekEntries, leftovers, todayKey) {
  const ids = providerIds();
  const jobs = plannedWeekJobs(weekEntries, leftovers, todayKey);
  if (!jobs.length) return null;
  state.plannedSyncInFlight ||= new Set();
  const key = "week:" + weekKey;
  const button = h("button", { type: "button", className: "secondary-button planned-sync-button planned-week-sync-button", "aria-label": `Woche ${planWeekLabel(weekKey)} synchronisieren` }, "Woche synchronisieren");
  const removed = jobs.flatMap((job) => job.items).map((item) => [item.name, item.date ? dateLabel(item.date) : ""].filter(Boolean).join(" "));
  if (removed.length) button.title = `Wird entfernt: ${removed.join(", ")}`;
  button.disabled = state.plannedSyncInFlight.has(key);
  button.addEventListener("click", async () => {
    if (state.plannedSyncInFlight.has(key)) return;
    state.plannedSyncInFlight.add(key);
    button.disabled = true;
    button.classList.add("busy");
    const outcome = await runPlannedWeekSync(jobs, ids, coachProviderLabel);
    state.plannedSyncInFlight.delete(key);
    globalThis.toast?.(outcome.message, outcome.failed);
    button.disabled = false;
    button.classList.remove("busy");
    await globalThis.load?.();
  });
  return button;
}

// One conflict component per planned session; its single action hands the conflict to the Coach.
function plannedConflict(entry, dateKey) {
  const labels = plannedConflictLabels(entry.conflicts);
  if (!labels.length) return null;
  const texts = plannedConflictTexts(entry.conflicts);
  const button = h("button", { type: "button", className: "secondary-button planned-conflict-button" }, "Mit Coach besprechen");
  button.setAttribute("aria-label", "Mit Coach besprechen: Konflikt");
  button.addEventListener("click", (event) => {
    // The button sits in the card summary; a click must not toggle the card.
    event.preventDefault();
    event.stopPropagation();
    const session = [activitySportLabel(calendarSportType(entry)), plannedEntryDurationLabel(null, entry)].filter(Boolean).join(", ");
    void globalThis.askCoach?.(`Die geplante Einheit am ${dateLabel(dateKey)} (${session}) kollidiert mit: ${labels.join(", ")}. Schlage eine Anpassung vor.`);
  });
  return h("span", "planned-conflict", h("span", null, `⚠ ${texts.join(" · ")}`), button);
}

// Why a strength or run unit cannot reach the watch, with the action that fixes it:
// the Coach structures a free-text unit; an unmatched exercise takes one of
// the catalogue suggestions or goes to the Coach.
function plannedGarminBlocked(entry, dateKey, todayKey, actual) {
  const reason = entry.push_blocked?.[providerIds().garmin] || (plannedGarminSdkRequired(entry, state.data?.garmin) ? "sdk_required" : "");
  if (!reason || actual || dateKey < todayKey || !entry.local_id || entry.is_completed_activity) return null;
  const block = h("span", "planned-conflict planned-garmin-blocked");
  const unit = `„${entry.name || "Krafteinheit"}“ am ${dateLabel(dateKey)}`;
  if (reason === "sdk_required") {
    block.append(h("span", null, "Garmin-Anmeldung nötig"));
    return block;
  }
  if (reason !== "unmapped") {
    block.append(h("span", null, plannedGarminBlockedText(entry)), plannedCoachButton("Mit Coach besprechen", `Strukturiere die Krafteinheit ${unit} für die Uhr: Übungen in Reihenfolge mit Sätzen, Wiederholungen, Last und Garmin-Übung. Frag nach, was fehlt.`, "secondary-button planned-conflict-button", null, "Kraftstruktur"));
    return block;
  }
  const unmapped = Array.isArray(entry.push_unmapped) ? entry.push_unmapped : [];
  block.append(...unmapped.map((exercise) => plannedUnmappedExercise(entry, exercise, unit)));
  return block.childElementCount ? block : null;
}

// One unmatched exercise: its catalogue suggestions, each a one-tap pick
// that is remembered for the exercise name, and "Andere…" for the Coach.
function plannedUnmappedExercise(entry, exercise, unit) {
  const row = h("span", "planned-garmin-exercise", h("span", null, `Keine Garmin-Übung: ${exercise.name}`));
  for (const suggestion of Array.isArray(exercise.suggestions) ? exercise.suggestions : []) {
    const chip = h("button", { type: "button", className: "planned-suggestion-chip" }, suggestion);
    chip.addEventListener("click", async (event) => {
      stopSummaryToggle(event);
      chip.disabled = true;
      try {
        await api(`/api/planned-units/${encodeURIComponent(entry.local_id)}/garmin-exercise`, { method: "POST", body: JSON.stringify({ exercise_index: exercise.index, garmin_exercise: suggestion, expected_payload_hash: entry.payload_hash }) });
        await globalThis.load?.();
        announcePlanChange(`Garmin-Übung „${suggestion}“ zugeordnet`);
      } catch (error) {
        chip.disabled = false;
        globalThis.toast?.(error.message, true);
      }
    });
    row.append(chip);
  }
  row.append(plannedCoachButton("Andere…", `Ordne in der Krafteinheit ${unit} der Übung „${exercise.name}“ die passende Garmin-Übung zu. Frag mich mit Vorschlägen aus dem Katalog.`));
  return row;
}

// "Details" opens the session's charts, zones, laps and all values in the card (#2258);
// the Coach analyses it in one athlete-started turn; "Feedback" opens the rating and session dialog.
// The Coach message names the session by date, sport, time, duration and its local id; the stored
// name never becomes Coach input.
function plannedActivityActions(actual, dateKey, title, entryKey) {
  const panel = h("div", "activity-analysis-panel");
  const more = h("details", "planned-activity-more", h("summary", null, "Details"), panel);
  // A re-render (sync, reload) keeps an opened "Details" open and loads it again.
  more.addEventListener("toggle", () => {
    if (entryKey && more.open) openActivityDetails.add(entryKey);
    else if (entryKey) openActivityDetails.delete(entryKey);
    if (more.open && !panel.childElementCount) void globalThis.ActivityAnalysis.loadPanel(panel, actual, id && ((fresh) => plannedSessionRead(id, fresh)));
  });
  const id = String(actual.activity_id || actual.id || "");
  more.open = Boolean(entryKey && openActivityDetails.has(entryKey));
  const prompt = `Analysiere meine Einheit vom ${dateLabel(dateKey)} (${[
    activitySportLabel(calendarSportType(actual)),
    calendarStartTime(actual.started_at_local),
    plannedEntryDurationLabel(actual, actual),
    /^[\w:.-]{1,80}$/.test(id) ? `ID ${id}` : null,
  ].filter(Boolean).join(", ")}).`;
  const coach = plannedCoachButton("Mit Coach besprechen", prompt, "secondary-button", "session_analysis");
  const feedback = h("button", { type: "button", className: "secondary-button planned-activity-feedback" }, "Feedback");
  feedback.addEventListener("click", () => globalThis.ActivityDetails.open({ ...actual, name: title }, { api, showDialog: showAccessibleDialog }));
  return [more, h("div", "planned-activity-actions", coach, feedback)];
}

// "Verschieben" (#2194): the next seven days from today with the unit's own day dimmed, and "…" for a
// later date. One tap moves the local unit; its open card follows it to the new week with the receipt.
function plannedUnitActions(entry, dateKey, todayKey) {
  if (!entry.local_id || entry.is_completed_activity) return [];
  const coach = plannedDiscussButton(entry, dateKey);
  if (!entry.payload_hash) return [h("div", "planned-activity-actions", coach)];
  const key = plannedEntryKey(entry, dateKey);
  const toggle = h("button", { type: "button", className: "secondary-button", "aria-expanded": "false" }, "Verschieben");
  const days = plannedDayStrip(todayKey, dateKey, "Neuer Tag", (day) => void move(day));
  const status = h("p", { className: "planned-move-status", role: "status" });
  toggle.addEventListener("click", () => {
    days.hidden = !days.hidden;
    toggle.setAttribute("aria-expanded", String(!days.hidden));
    if (!days.hidden && entry.local_id) void markBlockedDays(days, `/api/planned-units/${encodeURIComponent(entry.local_id)}`);
  });
  const move = async (target) => {
    if (!target || target === dateKey || (plannedLastDateKey && target > plannedLastDateKey)) return;
    const buttons = [...days.querySelectorAll("button, input")];
    buttons.forEach((item) => { item.disabled = true; });
    status.replaceChildren();
    try {
      const result = await api(`/api/planned-units/${encodeURIComponent(entry.local_id)}/date`, { method: "POST", body: JSON.stringify({ date: target, expected_payload_hash: entry.payload_hash }) });
      plannedMoveReceipts.set(key, { text: `Verschoben auf ${MOVE_DAY_FORMAT.format(dateFromKey(target))}`, change: result.change, from: dateKey });
      showPlannedDay(target);
      await globalThis.load?.();
      announcePlanChange(`Verschoben auf ${MOVE_DAY_FORMAT.format(dateFromKey(target))}`);
    } catch (error) {
      buttons.forEach((item) => { item.disabled = item.dataset.day === dateKey || item.classList.contains("is-blocked"); });
      status.textContent = error.message;
    }
  };
  const receipt = plannedMoveReceipts.get(key);
  if (receipt) status.append(receipt.text, ...(receipt.change?.id ? plannedMoveUndo(key, receipt, status) : []));
  const [remove, confirm] = plannedDeleteControls(entry, dateKey, key);
  const edit = h("button", { type: "button", className: "secondary-button" }, "Bearbeiten");
  edit.addEventListener("click", () => openUnitEditor({ kind: "unit", entry, onSaved: plannedUnitSaved }));
  const [archive, archiveNote] = plannedArchiveControls(entry, dateKey);
  return [h("div", "planned-activity-actions", edit, toggle, coach, archive, remove), confirm, archiveNote, days, status];
}

// "Bearbeiten" and "Archivieren" (#3950): the unit editor over the Coach's change batch; the card shows the receipt
// with "Rückgängig" and the calendar jumps to the unit's day. A new unit is planned from the Bibliothek ("Einplanen").
async function plannedUnitSaved(result, editor) {
  const key = `local:${result.local_id}`;
  const text = `Gespeichert: ${editor.name.trim()}`;
  plannedMoveReceipts.set(key, { text, change: result.change, from: String(editor.entry.date).slice(0, 10) });
  plannedForceOpen.add(key);
  showPlannedDay(editor.date);
  await globalThis.load?.();
  announcePlanChange(text);
}

// "Archivieren" takes the unit out of the calendar at once; the day's receipt offers "Rückgängig".
function plannedArchiveControls(entry, dateKey) {
  const name = entry.name || "Trainingseinheit";
  const [archive, note] = inCardDelete(async () => {
    let result;
    try {
      result = await api(`/api/planned-units/${encodeURIComponent(entry.local_id)}/archive`, { method: "POST", body: JSON.stringify({ confirmed: true, expected_payload_hash: entry.payload_hash }) });
    } catch (error) {
      // A changed unit reloads, so the card names the reason and the next try sends the current hash.
      if (error.status === 409) {
        plannedMoveReceipts.set(plannedEntryKey(entry, dateKey), { text: error.message });
        await globalThis.load?.();
      }
      throw error;
    }
    plannedDeleteReceipts.set(String(entry.local_id), { day: dateKey, text: `Archiviert: ${name}`, change: result.change, from: dateKey });
    await globalThis.load?.();
    announcePlanChange(`Archiviert: ${name}`);
  });
  archive.textContent = "Archivieren";
  archive.classList.remove("danger-button");
  return [archive, note];
}

// The strip asks the server which of its days a move would be refused for and marks them with what is in the
// way, so no day is offered that fails after the tap (#4060). A failed read leaves the strip as it was.
async function markBlockedDays(days, path) {
  const buttons = [...days.querySelectorAll("button[data-day]")];
  if (!buttons.length) return;
  try {
    const dates = buttons.map((button) => button.dataset.day).join(",");
    const { blocked } = await api(`${path}/blocked-days?dates=${dates}`);
    for (const button of buttons) {
      // Every read starts from the day's own label, so a reopened strip never stacks marks or keeps a freed day blocked.
      button.dataset.baseLabel ??= button.getAttribute("aria-label") || "";
      const names = blocked?.[button.dataset.day];
      if (names?.length) {
        button.disabled = true;
        button.classList.add("is-blocked");
        button.title = `Belegt: ${names.join(", ")}`;
        button.setAttribute("aria-label", `${button.dataset.baseLabel}, belegt: ${names.join(", ")}`);
      } else if (button.classList.contains("is-blocked")) {
        button.disabled = false;
        button.classList.remove("is-blocked");
        button.removeAttribute("title");
        button.setAttribute("aria-label", button.dataset.baseLabel);
      }
    }
  } catch { /* the move itself still answers with its message */ }
}

// The next seven days from today and "…" for a later date, inside the shown calendar (#2194, #3028).
// `skip` is dimmed; `pick` gets the chosen day.
function plannedDayStrip(todayKey, skip, label, pick) {
  const days = h("div", { className: "planned-move-days", role: "group", "aria-label": label, hidden: true });
  for (let offset = 0; offset < 7; offset += 1) {
    const day = addDateKey(todayKey, offset);
    if (plannedLastDateKey && day > plannedLastDateKey) break;
    const date = dateFromKey(day);
    const button = h("button", { type: "button", className: "planned-move-day", disabled: day === skip, dataset: { day }, "aria-label": DETAIL_DATE_FORMAT.format(date) },
      h("span", null, MOVE_WEEKDAY_FORMAT.format(date)), h("b", null, String(date.getDate())));
    button.addEventListener("click", () => pick(day));
    days.append(button);
  }
  // The system date picker opens from "…"; without showPicker the field itself is shown.
  const picker = h("input", { type: "date", className: "planned-move-picker", min: todayKey, max: plannedLastDateKey || null, tabIndex: -1, "aria-label": "Anderes Datum" });
  picker.addEventListener("change", () => pick(picker.value));
  const later = h("button", { type: "button", className: "planned-move-day", "aria-label": "Anderes Datum" }, "…");
  later.addEventListener("click", () => {
    try { picker.showPicker(); } catch { picker.classList.add("is-shown"); picker.tabIndex = 0; picker.focus(); }
  });
  days.append(later, picker);
  return days;
}

// "Löschen" acts at once (athlete decision, #3934 G-03): the card's undo line at its place takes the
// question's place. `remove` runs on the tap and its error shows beside the button.
function inCardDelete(remove) {
  const button = h("button", { type: "button", className: "secondary-button danger-button" }, "Löschen");
  const failure = h("p", { className: "planned-delete-note", role: "status" });
  button.addEventListener("click", async () => {
    button.disabled = true;
    failure.textContent = "";
    try {
      await remove();
    } catch (error) {
      button.disabled = false;
      failure.textContent = error.message;
    }
  });
  return [button, failure];
}

// A planned unit goes through the Coach's delete path; a copy at a provider goes with the next explicit sync.
function plannedDeleteControls(entry, dateKey, key) {
  const copies = (Array.isArray(entry.remote_copies) ? entry.remote_copies : []).map((provider) => state.data?.provider_labels?.[provider]).filter(Boolean);
  const name = entry.name || "Trainingseinheit";
  const copyNote = copies.length ? ` · ${copies.join(", ")}: weg beim nächsten Sync` : "";
  return inCardDelete(async () => {
    try {
      const result = await api(`/api/planned-units/${encodeURIComponent(entry.local_id)}/delete`, { method: "POST", body: JSON.stringify({ confirmed: true, expected_payload_hash: entry.payload_hash }) });
      plannedDeleteReceipts.set(String(entry.local_id), { day: dateKey, text: `Gelöscht: ${name}${copyNote}`, change: result.change, from: dateKey });
    } catch (error) {
      // A changed unit reloads, so the card names the reason and the next try sends the current hash.
      if (error.status === 409) {
        plannedMoveReceipts.set(key, { text: error.message });
        await globalThis.load?.();
      }
      throw error;
    }
    await globalThis.load?.();
    announcePlanChange(`Gelöscht: ${name}`);
  });
}

// "Mit Coach besprechen" (#2193) opens the Coach with the unit named by date, sport, start, duration and
// local id in the message field; the athlete adds the question and sends it. The stored name is never Coach input.
function plannedDiscussButton(entry, dateKey) {
  const id = String(entry.local_id);
  const reference = [
    activitySportLabel(calendarSportType(entry)),
    plannedUnitStartTime(entry.starts_at_local),
    plannedEntryDurationLabel(null, entry),
    /^[\w:.-]{1,80}$/.test(id) ? `ID ${id}` : null,
  ].filter(Boolean).join(", ");
  const button = h("button", { type: "button", className: "secondary-button" }, "Mit Coach besprechen");
  button.addEventListener("click", () => void globalThis.askCoach?.(`Zur geplanten Einheit am ${dateLabel(dateKey)} (${reference}): `));
  return button;
}

// "Rückgängig" puts the unit back on its earlier day, or back at all, and shows that week again.
function plannedMoveUndo(key, receipt, status, receipts = plannedMoveReceipts) {
  const undo = h("button", { type: "button", className: "message-undo" }, "Rückgängig");
  undo.addEventListener("click", async () => {
    undo.disabled = true;
    try {
      await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: receipt.change.id, expected_current_hash: receipt.change.after_hash }) });
      receipts.set(key, { day: receipt.day, text: "Zurückgenommen." });
      if (receipt.from) showPlannedDay(receipt.from);
      await globalThis.load?.();
    } catch (error) {
      undo.disabled = false;
      status.firstChild.textContent = `${error.message} `;
    }
  });
  return [" ", undo];
}

function showPlannedDay(dateKey) {
  selectedWeekKey = planWeekStart(dateKey);
  if (calendarView === "month") selectedDayKey = dateKey;
}

// One slim bar of the session's time per zone with every zone's minutes below and the zone kind and source:
// rides use power zones when they have them, every other sport heart-rate zones. No zones, no bar.
// The running step test (#3833): LT1 and LT2 of the completed run from its DFA-α1 windows. Only a
// good value offers "Übernehmen", through the existing adoption paths; nothing is adopted on its own.
const STEP_TEST_PROTOCOL = "run_step_test";
const STEP_TEST_PRECONDITION = "Nur mit Brustgurt und eingeschalteter HRV-Aufzeichnung auf der Uhr. Ausgeruht laufen, nicht nach einem harten Tag und nicht krank. Laufband (1 % Steigung) oder Bahn im Modus Bahnlauf; alle Tests am selben Ort.";
const STEP_TEST_ROWS = [["lt1", "LT1", "Erste Schwelle (LT1) · DFA-α1 0,75"], ["lt2", "LT2", "Anaerobe Schwelle (LT2) · DFA-α1 0,5"]];
const STEP_TEST_QUALITY = { good: "gut", low: "niedrig" };
const STEP_TEST_PERCENT = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });

// Pure: the rows, notes and provenance line of one step-test result.
function stepTestView(result) {
  if (result?.status !== "ok") {
    return { rows: [], notes: ["Keine RR-Intervalle: Brustgurt mit eingeschalteter HRV-Aufzeichnung nötig."], provenance: "" };
  }
  const rows = STEP_TEST_ROWS.map(([key, short, label]) => {
    const value = result[key] || {};
    if (value.status !== "ok") return { key, label, value: value.status === "not_reached" ? "nicht erreicht" : "nicht bestimmbar", quality: "", adopt: null };
    const pace = Number.isFinite(value.pace_seconds_per_km) ? ` · ${AppFormat.clock(value.pace_seconds_per_km)} /km` : "";
    return { key, short, label, value: `${value.heart_rate_bpm} bpm${pace}`, quality: STEP_TEST_QUALITY[value.quality] || "", adopt: value.quality === "good" ? value : null };
  });
  const notes = STEP_TEST_ROWS.filter(([key]) => result[key]?.status === "not_reached").map(([key, short]) => `${short} nicht erreicht: ${result[key].reason}`);
  const provenance = ["Brustgurt", `${result.analysable_stages} von ${result.stages} Stufen auswertbar`, `${STEP_TEST_PERCENT.format(result.artifact_share_pct)} % Artefakte`, result.source].join(" · ");
  return { rows, notes, provenance };
}

// Only a completed step-test unit gets the block, once per opened card.
async function appendPlannedStepTest(details, activityId, entry) {
  if (!activityId || entry.test_protocol !== STEP_TEST_PROTOCOL || details.querySelector(".planned-step-test")) return;
  const box = h("div", "planned-step-test");
  (details.querySelector(".planned-zone-time") || details.querySelector(".planned-actual-facts") || details.lastElementChild)?.after(box);
  if (!box.isConnected) details.append(box);
  let result;
  try {
    result = (await plannedSessionRead(activityId))?.step_test;
  } catch {
    box.remove();
    return;
  }
  if (!result) {
    box.remove();
    return;
  }
  const view = stepTestView(result);
  const rows = view.rows.map((row) => {
    const action = h("td");
    if (row.adopt) {
      const button = h("button", { type: "button", className: "secondary-button" }, "Übernehmen");
      button.addEventListener("click", () => { void adoptStepTestValue(activityId, row, button); });
      action.append(button);
    }
    return h("tr", null, h("th", { scope: "row" }, row.label), h("td", null, row.value), h("td", null, row.quality), action);
  });
  box.replaceChildren(
    rows.length ? h("table", "current-performance-table", h("tbody", null, rows)) : "",
    ...view.notes.map((note) => h("p", "planned-day-context planned-day-health", note)),
    view.provenance ? h("p", "planned-description", view.provenance) : "");
}

// LT1 goes to the profile's first threshold, LT2 to the run zones (heart rate and pace, chosen one by one).
async function adoptStepTestValue(activityId, row, button) {
  if (row.key === "lt2") {
    const adopt = lt2AdoptValues("step_test", row.adopt);
    await adoptLt2("run", { source: "step_test", sport: "run", activity_id: activityId, name: "Stufentest", adopt }, button);
    return;
  }
  const profile = state.data?.profile || {};
  const replacesMeasured = Boolean(profile.first_threshold_hr) && profile.first_threshold_origin === "measured";
  if (replacesMeasured && !(await requestConfirmation("Der gemessene Wert wird durch den Wert aus dem Stufentest ersetzt.", { title: "Gemessenen Wert ersetzen?", confirmLabel: "Ersetzen" }))) return;
  button.disabled = true;
  try {
    const result = await api("/api/profile/first-threshold/adopt", { method: "POST", body: JSON.stringify({ step_test: activityId, heart_rate_bpm: row.adopt.heart_rate_bpm, ...(replacesMeasured ? { replace_measured: true } : {}) }) });
    applyAdoptedFirstThreshold(result);
    invalidateContextPreview();
    toast(result.adopted === false ? "Wert aus dem Stufentest ist bereits übernommen" : "Erste Schwelle übernommen");
    await load();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function appendPlannedZoneTime(details, activityId, ride) {
  const box = h("div", "planned-zone-time");
  const facts = details.querySelector(".planned-actual-facts");
  if (facts) facts.after(box);
  else details.querySelector(":scope > .planned-activity-more")?.before(box);
  if (!box.isConnected) details.append(box);
  let zones;
  try {
    zones = (await plannedSessionRead(activityId))?.zones;
  } catch {
    box.remove();
    return;
  }
  const power = ride && plannedZoneBuckets(zones?.power).some((item) => item.seconds > 0) ? zones.power : null;
  const zone = power || zones?.heart_rate;
  const buckets = plannedZoneBuckets(zone);
  if (!buckets.some((item) => item.seconds > 0)) {
    box.remove();
    return;
  }
  const kind = power ? "Leistungszonen" : "Herzfrequenzzonen";
  const minutes = (seconds) => `${Math.round(seconds / 60)}′`;
  const spoken = buckets.map((item) => `${item.name} ${Math.round(item.seconds / 60)} min`).join(", ");
  const bar = h("div", { className: "planned-zone-time-bar", role: "img", "aria-label": `Zeit in ${kind}: ${spoken}` },
    buckets.filter((item) => item.seconds > 0).map((item) => {
      const part = h("i", { dataset: { zone: item.zone } });
      part.style.setProperty("flex-grow", String(item.seconds));
      return part;
    }));
  const legend = h("div", { className: "planned-zone-time-legend", "aria-hidden": "true" }, buckets.filter((item) => item.seconds >= 30).map((item) => h("span", null, `${item.name} ${minutes(item.seconds)}`)));
  box.append(bar, legend, h("p", "planned-zone-time-source", [kind, zone.source?.label].filter(Boolean).join(" · ")));
}

function plannedZoneBuckets(zone) {
  return (zone?.buckets || [])
    .filter((bucket) => Number.isFinite(bucket.seconds) && bucket.seconds >= 0)
    .map((bucket) => {
      const name = String(bucket.label || `Z${bucket.index}`).slice(0, 12);
      return { name, seconds: bucket.seconds, zone: /^Z(\d)/.exec(name)?.[1] || "" };
    });
}

// Built when the card first opens, as most cards never are: the larger profile and, for a session,
// the time in zones from the session read of "Details" (#2195).
// Drinking and carbohydrates of a planned run or ride of at least an hour (#2804).
function plannedFuellingQuery(entry, dateKey) {
  const sport = String(entry?.sport || "").toLowerCase();
  const seconds = plannedDurationSeconds(entry);
  if (!["run", "ride"].includes(sport) || seconds < 3600) return null;
  return new URLSearchParams({ sport, environment: String(entry.environment || ""), date: dateKey, duration_s: String(Math.round(seconds)) }).toString();
}

// Litres keep two decimals, so a small hourly intake is not shown as zero.
const FUELLING_LITRES = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2 });

function plannedFuellingRows(drink) {
  const litres = (value) => `${FUELLING_LITRES.format(value)} l`;
  const sodium = Array.isArray(drink.sodium_g_per_l) ? `${RACE_PLAN_KM.format(drink.sodium_g_per_l[0])}–${RACE_PLAN_KM.format(drink.sodium_g_per_l[1])} g/l` : null;
  const sodiumRow = sodium ? ["Natrium", sodium] : null;
  if (Array.isArray(drink.generic_l_per_h)) return [["Pro Stunde", `${FUELLING_LITRES.format(drink.generic_l_per_h[0])}–${litres(drink.generic_l_per_h[1])}`], sodiumRow];
  // A loss within 2 % of the weight needs no drink: say so with the loss, never "0 l" (#3012).
  if (drink.within_loss_share) return [["Pro Stunde", "nicht nötig"], drink.loss_l == null ? null : ["Verlust", `${litres(drink.loss_l)} · unter 2 % des Gewichts`]];
  if (drink.l_per_h == null) return [["Pro Stunde", `bis ${litres(drink.max_l_per_h)}`], sodiumRow];
  // An hourly share that rounds to nothing is left out; the total still shows (#3012).
  return [drink.l_per_h > 0 ? ["Pro Stunde", litres(drink.l_per_h)] : null, ["Gesamt", litres(drink.total_l)], sodiumRow];
}

function plannedFuellingBlock(plan) {
  const rate = plan.sweat_rate;
  const band = rate?.similar && Array.isArray(rate.temperature_c) ? ` bei ${rate.temperature_c[0]}–${rate.temperature_c[1]} °C` : "";
  const sessions = AppFormat.plural(rate?.sessions, "Einheit", "Einheiten");
  const basis = rate ? `Schweißrate ${FUELLING_LITRES.format(rate.l_per_h)} l/h · ${sessions}${band}` : "Startwert ohne eigene Daten";
  const rows = (items) => h("div", "race-plan-rows", items.filter(Boolean).map(([label, value]) => h("div", null, h("span", null, label), h("strong", null, value))));
  return [
    h("h4", null, "Verpflegung"),
    racePlanHeading("Trinken", basis),
    rows(plannedFuellingRows(plan.drinking || {})),
    racePlanHeading("Kohlenhydrate", plan.carbohydrate_source),
    rows([["Pro Stunde", racePlanDuring(plan.carbohydrates) || "–"]]),
  ];
}

// One fuelling read per query and calendar data, cleared with the session reads in renderPlanned
// (#2991). A read started while the view still loads waits out its reads instead of joining their
// burst (as #2979); one a newer render or another session replaced meanwhile is dropped unsent.
const plannedFuellingReads = new Map();
const STALE_FUELLING_READ = new Error("replaced fuelling read");

function plannedFuellingRead(query) {
  let read = plannedFuellingReads.get(query);
  if (!read) {
    const session = state.sessionGeneration;
    const viewLoading = !state.initialStateLoaded || Boolean(state.loadPromise);
    read = (viewLoading ? globalThis.AppApi.settled() : Promise.resolve()).then(() => {
      if (session !== state.sessionGeneration || plannedFuellingReads.get(query) !== read) throw STALE_FUELLING_READ;
      return api(`/api/nutrition/session-fuelling?${query}`, { timeoutMs: DATA_LOAD_TIMEOUT_MS });
    });
    read.catch(() => { if (plannedFuellingReads.get(query) === read) plannedFuellingReads.delete(query); });
    plannedFuellingReads.set(query, read);
  }
  return read;
}

function loadPlannedFuelling(section, query) {
  if (section.dataset.state === "loading" || section.dataset.state === "ready") return;
  section.dataset.state = "loading";
  const status = h("output", null, "Verpflegung wird berechnet …");
  section.replaceChildren(h("h4", null, "Verpflegung"), status);
  const read = plannedFuellingRead(query);
  read.then((plan) => {
    section.dataset.state = "ready";
    if (plan?.status === "ready") section.replaceChildren(...plannedFuellingBlock(plan));
    // Keep the empty section marked ready, so reopening does not read again.
    else section.hidden = true;
  }).catch(() => {
    // A plan that could not be shown is not kept either, so "Erneut laden" reads again.
    if (plannedFuellingReads.get(query) === read) plannedFuellingReads.delete(query);
    section.dataset.state = "failed";
    const retry = h("button", { type: "button", className: "secondary-button" }, "Erneut laden");
    retry.addEventListener("click", () => loadPlannedFuelling(section, query));
    status.textContent = "Verpflegung konnte nicht geladen werden.";
    section.append(retry);
  });
}

function appendOpenedPlannedParts(details, head, entry, actual) {
  if (!details.querySelector(".planned-detail-profile")) {
    const detailProfile = plannedDetailProfile(actual?.workout_profile || entry.workout_profile);
    if (detailProfile) head.after(detailProfile);
  }
  const upcoming = details.dataset.dateKey >= timezoneDateKey(state.data?.profile?.timezone, new Date());
  const fuellingQuery = actual || !upcoming ? null : plannedFuellingQuery(entry, details.dataset.dateKey);
  if (fuellingQuery) {
    let section = details.querySelector(".planned-fuelling");
    if (!section) {
      section = h("section", "race-plan planned-fuelling");
      const steps = details.querySelector(".planned-steps");
      if (steps) steps.after(section);
      else details.append(section);
    }
    loadPlannedFuelling(section, fuellingQuery);
  }
  const activityId = actual ? String(actual.activity_id || actual.id || "") : "";
  if (activityId && !details.querySelector(".planned-zone-time")) {
    void appendPlannedZoneTime(details, activityId, ["Radfahren", "Rad indoor"].includes(activitySportLabel(calendarSportType(actual))));
  }
  void appendPlannedStepTest(details, activityId, entry);
  void appendPlannedEquipment(details, head, entry, actual);
}

// The equipment of a unit (#2879, #3011): a chip in the head line, there as soon as the card opens, names the
// item that counts (the own choice, else the planned choice, a rule or Garmin's link with its source) or the
// suggestion. Tapping it opens one select of the items the unit can take and "Keiner"; only "Übernehmen"
// saves, with a receipt and undo. Planned runs and rides take a choice too; nothing reaches a provider.
const EQUIPMENT_LABELS = { Laufen: ["Schuh", "\u{1F45F}"], Radfahren: ["Rad", "\u{1F6B2}"], "Rad indoor": ["Rad", "\u{1F6B2}"] };
const EQUIPMENT_SOURCE_LABELS = { planned: "Plan", rule: "Regel" };

function plannedEquipmentTarget(entry, actual) {
  const activityId = actual ? String(actual.activity_id || actual.id || "") : "";
  if (activityId) return { activityId, body: { activity_id: activityId } };
  if (actual || !entry?.local_id || entry.is_completed_activity) return null;
  return { unitId: String(entry.local_id), body: { planned_unit_id: String(entry.local_id) } };
}

async function plannedEquipmentChoice(target) {
  try {
    if (target.activityId) return (await plannedSessionRead(target.activityId))?.equipment_choice || null;
    return await api(`/api/equipment/planned?unit_id=${encodeURIComponent(target.unitId)}`);
  } catch {
    return null;
  }
}

function plannedEquipmentSourceLabel(source) {
  if (source === "garmin_gear") return state.data?.provider_labels?.[providerIds().garmin] || "";
  return EQUIPMENT_SOURCE_LABELS[source] || "";
}

// What a unit's choice shows: the own choice, else the effective item with its source, else the suggestion.
function plannedEquipmentShown(choice) {
  const own = choice.explicit ? choice.assigned || "" : null;
  const named = (id) => (id === "" ? "Keiner" : choice.options.find((item) => item.id === id)?.name);
  // A planned "Keiner" carried to the session is "Keiner" with its source.
  const carried = choice.effective?.equipment_id === null ? { ...choice.effective, equipment_id: "" } : choice.effective;
  const effective = own === null && named(carried?.equipment_id) ? carried : null;
  const current = own ?? effective?.equipment_id ?? null;
  const preset = current ?? choice.suggested ?? null;
  let tag = "";
  if (effective) tag = plannedEquipmentSourceLabel(effective.source);
  else if (own === null && preset !== null) tag = "Vorschlag";
  const shown = preset === null ? "–" : named(preset) || "Nicht mehr verfügbar";
  return { own, current, preset, shown, tag };
}

async function appendPlannedEquipment(details, head, entry, actual, receipt = null) {
  const target = plannedEquipmentTarget(entry, actual);
  const sport = activitySportLabel(calendarSportType(actual || entry));
  if (!target || (target.unitId && !EQUIPMENT_LABELS[sport]) || details.querySelector(".planned-equipment-chip")) return;
  const [label, icon] = EQUIPMENT_LABELS[sport] || ["Ausrüstung", "\u2699"];
  const chip = h("button", { type: "button", className: "status-chip planned-equipment-chip", "aria-expanded": "false", "aria-label": label }, `${icon} …`);
  // Narrow screens hide the head (the summary is the card's head), so the chip leads the opened body there.
  const line = h("div", "planned-equipment-line", chip);
  // After a save or undo the editor stays closed: the chip names the choice, the receipt follows it (#3151).
  const row = h("div", { className: "planned-equipment", hidden: true });
  head.after(line, row);
  const choice = await plannedEquipmentChoice(target);
  if (!choice || (!choice.options?.length && !choice.explicit)) {
    line.remove();
    row.remove();
    // The receipt stays even when the unit cannot be read again.
    if (receipt) plannedEquipmentReceipt(details, head, entry, actual, receipt);
    return;
  }
  const { own, current, preset, shown, tag } = plannedEquipmentShown(choice);
  const select = h("select", { "aria-label": label },
    preset === null ? h("option", { value: "-", selected: true }, "–") : null,
    // An own choice that can no longer be assigned (retired, another sport) stays shown as it is.
    own && shown === "Nicht mehr verfügbar" ? h("option", { value: own, selected: true, disabled: true }, shown) : null,
    choice.options.map((item) => h("option", { value: item.id, selected: item.id === preset }, item.name)),
    h("option", { value: "", selected: preset === "" }, "Keiner"));
  const suffix = tag ? ` · ${tag}` : "";
  chip.textContent = `${icon} ${shown}${suffix} ▾`;
  chip.setAttribute("aria-label", [`${label}: ${shown}`, tag].filter(Boolean).join(", "));
  const apply = h("button", { type: "button", className: "secondary-button", hidden: true }, "Übernehmen");
  const status = h("p", { className: "planned-equipment-status", role: "status" });
  const sync = () => { apply.hidden = select.value === "-" || select.value === current; };
  select.addEventListener("change", sync);
  chip.addEventListener("click", () => {
    row.hidden = !row.hidden;
    chip.setAttribute("aria-expanded", String(!row.hidden));
    if (!row.hidden) select.focus();
  });
  apply.addEventListener("click", async () => {
    apply.disabled = true;
    try {
      const result = await api("/api/equipment/assignment", { method: "POST", body: JSON.stringify({ ...target.body, equipment_id: select.value || null }) });
      plannedEquipmentChanged(target);
      plannedEquipmentRedraw(details, head, entry, actual, { change: result?.change, message: "Gespeichert. " });
    } catch (error) {
      apply.disabled = false;
      if (!isSessionEndedError(error)) status.textContent = error.message;
    }
  });
  row.replaceChildren(h("label", "planned-equipment-field", h("span", null, label), select), apply, status);
  sync();
  if (receipt) plannedEquipmentReceipt(details, head, entry, actual, receipt);
}

// An assignment changes usage and the equipment notice; the next opening reads the unit again.
function plannedEquipmentChanged(target) {
  if (target.activityId) plannedSessionReads.delete(target.activityId);
  globalThis.invalidateEquipmentNotice?.();
}

function plannedEquipmentRedraw(details, head, entry, actual, receipt) {
  details.querySelector(".planned-equipment-line")?.remove();
  details.querySelector(".planned-equipment")?.remove();
  details.querySelector(".planned-equipment-receipt")?.remove();
  void appendPlannedEquipment(details, head, entry, actual, receipt);
}

// The receipt line sits under the chip and the closed editor, outside the editor.
function plannedEquipmentReceipt(details, head, entry, actual, { change, message }) {
  const status = h("p", { className: "planned-equipment-status planned-equipment-receipt", role: "status" }, message);
  (details.querySelector(".planned-equipment") || details.querySelector(".planned-equipment-line") || head).after(status);
  if (!change?.id) return;
  const undo = h("button", { className: "message-undo", type: "button" }, "Rückgängig");
  undo.addEventListener("click", async () => {
    undo.disabled = true;
    try {
      await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: change.id, expected_current_hash: change.after_hash }) });
      plannedEquipmentChanged(plannedEquipmentTarget(entry, actual));
      plannedEquipmentRedraw(details, head, entry, actual, { change: null, message: "Zurückgenommen." });
    } catch (error) {
      undo.disabled = false;
      if (!isSessionEndedError(error)) status.firstChild.textContent = `${error.message} `;
    }
  });
  status.append(undo);
}

// A planned unit's body: its load, its steps (#2447) and its actions; returns the step list, if any.
function appendPlannedUnitBody(details, entry, dateKey, todayKey) {
  const metrics = plannedSessionMetrics(entry);
  if (metrics) details.append(metrics);
  const stepTest = entry.test_protocol === STEP_TEST_PROTOCOL;
  if (stepTest) details.append(h("p", { className: "planned-day-context planned-day-health", dataset: { stepTestNote: "" } }, STEP_TEST_PRECONDITION));
  const steps = plannedStepList(entry.workout_profile, stepTest);
  if (steps) details.append(steps);
  details.append(...plannedUnitActions(entry, dateKey, todayKey));
  return steps;
}

function renderPlannedEntry(entry, dateKey, todayKey, dayContext = {}, weekHasPlan = true) {
  const actual = calendarActualActivity(entry);
  const baseStatus = calendarEntryStatus(entry, dateKey, todayKey);
  const restDayLabel = baseStatus === "missed" ? plannedDayStatusLabel(dayContext.day_status) : null;
  const status = restDayLabel ? "skipped" : baseStatus;
  // One open card at a time: on wide screens its body is the detail pane beside the week.
  const card = h("details", { className: `planned-entry is-${status}`, name: "planned-entry" });
  const entryKey = plannedEntryKey(entry, dateKey);
  if (entryKey) card.dataset.entryKey = entryKey;
  card.open = openPlannedEntries.has(entryKey);
  const displayed = actual || entry;
  card.dataset.sport = activitySportLabel(calendarSportType(displayed));
  const meta = h("span", "planned-meta", [
    activitySportLabel(calendarSportType(displayed)),
    actual ? calendarStartTime(actual.started_at_local) : plannedUnitStartTime(entry.starts_at_local),
    plannedEntryDurationLabel(actual, entry),
  ].filter(Boolean).join(" · "));
  const title = (actual?.name ? calendarDisplayName(actual.name, state.data?.profile?.weather_location) : entry.name) || "Trainingseinheit";
  // Done and missed units are one slim row; only units still to come keep the zone bar and chips.
  const slim = ["completed", "missed", "skipped"].includes(status);
  // The sport line carries the chips and, only for a unit still to send, a compact sync button.
  const cardSummary = h("summary", null,
    plannedSessionHeader(entry, actual, title, (line) => {
      appendPlannedEntryStatus(line, entry, status, restDayLabel, dateKey, todayKey, weekHasPlan);
      if (!slim) appendFocusChip(line, entry);
    }, slim),
    meta,
    slim ? null : calendarWorkoutProfile(entry.workout_profile),
    plannedConflict(entry, dateKey),
    plannedGarminBlocked(entry, dateKey, todayKey, actual));
  const head = plannedDetailHead(entry, actual, title, dateKey, status, plannedDetailStatus(entry, dateKey, todayKey, restDayLabel, weekHasPlan), plannedDayWeather(dayContext, dateKey));
  const details = h("div", { className: "planned-entry-details", dataset: { dateKey } }, head);
  card.addEventListener("toggle", () => {
    if (card.open) appendOpenedPlannedParts(details, head, entry, actual);
  });
  const steps = actual ? null : appendPlannedUnitBody(details, entry, dateKey, todayKey);
  appendActualCalendarDetails(details, actual);
  if (actual && (actual.id || actual.activity_id)) details.append(...plannedActivityActions(actual, dateKey, title, entryKey));
  appendPlannedCalendarComparison(details, entry, actual);
  const focusForm = plannedFocusForm(entry, dateKey);
  if (focusForm) details.append(focusForm);
  // With the step list, the description keeps only its free text; the stored text is unchanged (#2447).
  appendPlannedDescription(details, steps ? plannedDescriptionText(entry.description) : entry.description);
  card.append(cardSummary, details);
  return card;
}

// Personal calendar events (#2620): a band of chips above the day's units. Event text is untrusted
// and only ever set as text. The decision is local; it changes planning, never a remote calendar.
const EVENT_IMPACT_LABELS = new Map([["block", "Blockiert"], ["short", "Kürzer"], ["easy", "Locker"], ["none", "Egal"], ["open", "Offen"]]);
const EVENT_DECISIONS = ["block", "short", "easy", "none"];

// The local days an event covers, from its start day to the day of its last minute.
function plannedEventDays(event) {
  const start = String(event?.start_local || event?.event_date || "").slice(0, 10);
  const end = String(event?.end_local || "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start)) return [];
  const endsAtMidnight = /T00:00(?::00)?/.test(end.slice(10, 19));
  let last = /^\d{4}-\d{2}-\d{2}/.test(end) ? end.slice(0, 10) : start;
  if (endsAtMidnight && last > start) last = addDateKey(last, -1);
  const days = [];
  for (let day = start; day <= last && days.length < 60; day = addDateKey(day, 1)) days.push(day);
  return days;
}

function plannedEventsByDate(todayKey) {
  const events = Array.isArray(state.data?.external_calendar?.events) ? state.data.external_calendar.events : [];
  const byDate = new Map();
  for (const event of events) {
    if (!event?.uid) continue;
    for (const day of plannedEventDays(event)) {
      if (day < todayKey) continue;
      if (!byDate.has(day)) byDate.set(day, []);
      byDate.get(day).push(event);
    }
  }
  return byDate;
}

function plannedNoteContext(dayContext, dayEvents) {
  if (!dayEvents.length) return dayContext;
  const shown = new Set(dayEvents.map((event) => `${event.name}|${event.start_local}`));
  const appointments = (Array.isArray(dayContext.appointments) ? dayContext.appointments : [])
    .filter((item) => !shown.has(`${item?.name}|${item?.start_local}`));
  const flag = (key) => appointments.some((item) => item?.[key]);
  return { ...dayContext, appointments, no_training: flag("no_training"), no_intensity: flag("no_intensity"), short_only: flag("short_only") };
}

function plannedEventTime(event) {
  if (event.all_day) return "ganztägig";
  const time = (value) => /(?:T|\s)(\d{2}:\d{2})/.exec(String(value || ""))?.[1] || "";
  return [time(event.start_local), time(event.end_local)].filter(Boolean).join("–");
}

function plannedEventImpact(event) {
  return EVENT_IMPACT_LABELS.has(event.impact) ? event.impact : "open";
}

function plannedEventBand(events) {
  const band = h("div", { className: "planned-event-band", "aria-label": "Termine" });
  band.setAttribute("role", "group");
  for (const event of events) {
    const impact = plannedEventImpact(event);
    const name = stripCalendarMarkers(event.name) || "Termin";
    const time = plannedEventTime(event);
    const chip = h("button", {
      type: "button", className: "planned-event-chip", dataset: { impact, uid: event.uid, start: event.start_local || "" },
      // The name starts with what the chip shows, in its order (WCAG 2.5.3).
      "aria-label": [[time, name].filter(Boolean).join(" "), EVENT_IMPACT_LABELS.get(impact)].filter(Boolean).join(", "),
    }, impact === "open" ? h("span", { className: "planned-event-dot", "aria-hidden": "true" }) : null,
    time ? h("small", null, time) : null, time ? " " : null, h("span", "planned-event-name", name));
    chip.addEventListener("click", () => openPlannedEventSheet(event));
    band.append(chip);
  }
  return band;
}

// One sheet for every event: a bottom sheet on phones, a side panel on wide screens (styles.css).
function openPlannedEventSheet(event, { queue = false } = {}) {
  const previous = document.querySelector(".planned-event-sheet");
  previous?.close();
  previous?.remove();
  const name = stripCalendarMarkers(event.name) || "Termin";
  const dateLabel = DETAIL_DATE_FORMAT.format(dateFromKey(String(event.start_local || event.event_date).slice(0, 10)));
  const dialog = h("dialog", { className: "planned-event-sheet", "aria-labelledby": "plannedEventTitle" });
  const close = h("button", { type: "button", className: "icon-button planned-event-close", "aria-label": "Schließen" }, "✕");
  close.addEventListener("click", () => { if (!dialog.dataset.pending) dialog.close(); });
  dialog.addEventListener("cancel", (cancel) => { if (dialog.dataset.pending) cancel.preventDefault(); });
  let series = Boolean(event.recurring) && event.decision_scope !== "event";
  const scope = event.recurring ? h("div", { className: "planned-event-scope", "aria-label": "Gilt für" }) : null;
  const status = h("p", "planned-event-status");
  status.setAttribute("role", "status");
  const choices = h("div", { className: "planned-event-choices", "aria-label": "Einordnung" });
  choices.setAttribute("role", "group");
  const current = event.decision_scope ? plannedEventImpact(event) : "";
  for (const impact of EVENT_DECISIONS) {
    const button = h("button", { type: "button", dataset: { impact }, "aria-pressed": String(impact === current) }, EVENT_IMPACT_LABELS.get(impact));
    button.addEventListener("click", () => decidePlannedEvent(event, impact, series, dialog, status, queue));
    choices.append(button);
  }
  if (scope) {
    scope.setAttribute("role", "group");
    for (const [value, label] of [[false, "Nur dieser Termin"], [true, "Alle Wiederholungen"]]) {
      const button = h("button", { type: "button", "aria-pressed": String(series === value) }, label);
      button.addEventListener("click", () => {
        series = value;
        for (const item of scope.children) item.setAttribute("aria-pressed", String(item === button));
      });
      scope.append(button);
    }
  }
  dialog.append(...[h("div", "planned-event-head", h("h3", { id: "plannedEventTitle" }, name), close),
    h("p", "planned-event-meta", [dateLabel, plannedEventTime(event)].filter(Boolean).join(" · ")), choices, scope, status].filter(Boolean));
  dialog.addEventListener("click", (click) => {
    const box = dialog.getBoundingClientRect();
    const outside = click.clientX < box.left || click.clientX > box.right || click.clientY < box.top || click.clientY > box.bottom;
    if (click.target === dialog && outside && !dialog.dataset.pending) dialog.close();
  });
  dialog.addEventListener("close", () => { dialog.remove(); restoreDialogFocus(dialog); }, { once: true });
  document.body.append(dialog);
  showAccessibleDialog(dialog, choices.querySelector("[aria-pressed='true']") || choices.firstElementChild);
}

// The notice (#2621) covers events without a decision from today through 14 days ahead; the
// server count is the source; the decision response carries the new count.
const UNDECIDED_NOTICE_DAYS = 14;

function undecidedPlannedEvents() {
  const todayKey = timezoneDateKey(state.data?.profile?.timezone, new Date());
  const lastKey = addDateKey(todayKey, UNDECIDED_NOTICE_DAYS);
  const events = Array.isArray(state.data?.external_calendar?.events) ? state.data.external_calendar.events : [];
  return events.filter((event) => event?.uid && plannedEventImpact(event) === "open"
    && String(event.start_local || "").slice(0, 10) <= lastKey && String(event.end_local || event.start_local || "") > `${todayKey}T00:00:00`);
}

function undecidedPlannedCount() {
  const count = Number(state.data?.external_calendar?.undecided_upcoming);
  return Number.isFinite(count) && count > 0 ? count : 0;
}

function undecidedNoticeText(count) {
  const todayKey = timezoneDateKey(state.data?.profile?.timezone, new Date());
  const until = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit" }).format(dateFromKey(addDateKey(todayKey, UNDECIDED_NOTICE_DAYS)));
  return `${count} ${count === 1 ? "neuer Termin" : "neue Termine"} bis ${until}`;
}

function plannedUndecidedBanner() {
  const count = undecidedPlannedCount();
  const first = undecidedPlannedEvents()[0];
  if (!count) return null;
  const action = first ? h("button", { type: "button" }, "Einordnen") : null;
  action?.addEventListener("click", () => openPlannedEventSheet(first, { queue: true }));
  return h("div", "planned-undecided-banner", h("span", { className: "planned-event-dot", "aria-hidden": "true" }), h("span", null, undecidedNoticeText(count)), action);
}

// The Kalender navigation items carry the same count.
// #3384: what the running week's sync button would act on (Monday to Sunday), by the button's
// own jobs: each unit it sends counts once, whatever the providers, and each leftover it removes.
function runningWeekSyncCount(trainingCalendar) {
  const todayKey = timezoneDateKey(state.data?.profile?.timezone, new Date());
  const weekKey = planWeekStart(todayKey);
  const lastDay = addDateKey(weekKey, 6);
  const futureWeeks = CALENDAR_WEEKS.future;
  const entries = (Array.isArray(trainingCalendar) ? trainingCalendar : [])
    .filter((item) => item && !item.archived && !item.local_deleted)
    .filter((item) => { const day = plannedEventDate(item); return day && day >= weekKey && day <= lastDay; });
  const leftovers = plannedLeftoversByWeek(trainingCalendar, weekKey, addDateKey(weekKey, 7 * futureWeeks)).get(weekKey);
  const jobs = plannedWeekJobs(entries, leftovers, todayKey);
  const units = new Set(jobs.flatMap((job) => job.units.map((unit) => unit.local_id)));
  const removals = new Set(jobs.flatMap((job) => job.items.map((item) => job.provider + ":" + item.id)));
  return units.size + removals.size;
}

function weekSyncNoticeText(count) {
  return `${AppFormat.plural(count, "Einheit", "Einheiten")} zu synchronisieren`;
}

// One badge for both: undecided events plus the running week's units to sync (#3384).
function renderUndecidedNavCount() {
  const undecided = undecidedPlannedCount();
  const sync = runningWeekSyncCount(renderedCalendar);
  const count = undecided + sync;
  const parts = [undecided ? undecidedNoticeText(undecided) : "", sync ? weekSyncNoticeText(sync) : ""].filter(Boolean);
  for (const link of document.querySelectorAll('.nav-item[data-route="plan"]')) {
    let badge = link.querySelector("b.nav-count");
    if (!badge) {
      badge = h("b", { className: "nav-count", "aria-hidden": "true" });
      // The space keeps the visible text "Kalender 1" and so inside the name that starts with it (WCAG 2.5.3).
      link.append(" ", badge);
    }
    badge.textContent = count ? String(count) : "";
    link.setAttribute("aria-label", [count ? `Kalender ${count}` : "Kalender", ...parts].join(", "));
  }
}

async function decidePlannedEvent(event, impact, series, dialog, status, queue = false) {
  const choices = [...dialog.querySelectorAll(".planned-event-choices button")];
  if (choices.some((button) => button.disabled)) return;
  let result;
  status.textContent = "";
  choices.forEach((button) => { button.disabled = true; });
  dialog.dataset.pending = "true";
  try {
    result = await api("/api/external-calendar/decisions", {
      method: "POST",
      body: JSON.stringify({ uid: event.uid, occurrence_start: series ? null : event.start_local, impact }),
    });
  } catch (error) {
    status.textContent = error.message;
    choices.forEach((button) => { button.disabled = false; });
    return;
  } finally {
    delete dialog.dataset.pending;
  }
  scheduleStateEventRefresh(["plan"]);
  for (const item of state.data?.external_calendar?.events || []) {
    if (item.uid !== event.uid || (!series && item.start_local !== event.start_local)) continue;
    Object.assign(item, { impact, decision_scope: series ? "series" : "event" });
  }
  state.data.external_calendar.undecided_upcoming = Math.max(0, Number(result?.undecided_upcoming) || 0);
  dialog.close();
  renderPlanned(renderedCalendar);
  renderUndecidedNavCount();
  const next = queue ? undecidedPlannedEvents()[0] : null;
  if (next && undecidedPlannedCount()) {
    openPlannedEventSheet(next, { queue: true });
    return;
  }
  $("#plannedCalendar")?.querySelector(`.planned-event-chip[data-uid="${CSS.escape(event.uid)}"][data-start="${CSS.escape(event.start_local || "")}"]`)?.focus();
}

function plannedDayWeather(dayContext, dateKey) {
  if (dayContext.weather) return dayContext.weather;
  const days = state.data?.weather?.days;
  return Array.isArray(days) ? days.find((item) => item?.date === dateKey) : null;
}

// The timeline's date column: weekday over the day of the month; today's day is filled.
function plannedDayDate(day, dateKey) {
  const date = dateFromKey(dateKey);
  const title = h("h5", { className: "planned-day-date", id: `planned-day-${dateKey}` },
    h("span", null, new Intl.DateTimeFormat("de-DE", { weekday: "short" }).format(date)),
    h("time", { dateTime: dateKey }, String(date.getDate())));
  day.setAttribute("aria-labelledby", title.id);
  return title;
}

// "Heute", the weather and its flags above the day's units; none when nothing applies.
function plannedDayHeading(weather, dateKey, todayKey) {
  const heading = h("div", "planned-day-heading", dateKey === todayKey ? h("span", "planned-today-label", "Heute") : null);
  const weatherLabel = plannedWeatherLabel(weather);
  if (weatherLabel) {
    const title = [...plannedWeatherDetails(weather), weatherLabel].join("\n");
    heading.append(h("span", { className: "planned-day-weather", title, "aria-label": title.replaceAll("\n", ". ") }, weatherLabel));
    for (const flag of plannedWeatherFlags(weather)) heading.append(h("span", { className: "planned-day-flag", "aria-label": flag.label }, flag.text));
  }
  return heading.childElementCount ? heading : null;
}

function renderPlannedGroup(entries, dateKey, todayKey, dayContext, weekHasPlan) {
  const first = entries[0];
  // A group holding an open unit (one just saved) stays open.
  const open = entries.some((entry) => openPlannedEntries.has(plannedEntryKey(entry, dateKey)));
  return h("details", { className: "planned-group", open, dataset: { sport: activitySportLabel(calendarSportType(first)) } },
    h("summary", null, plannedSessionHeader(first, first, `${entries.length}× ${calendarDisplayName(first.name, state.data?.profile?.weather_location) || "Trainingseinheit"}`, () => {}, true)),
    h("div", "planned-group-items", entries.map((entry) => renderPlannedEntry(entry, dateKey, todayKey, dayContext, weekHasPlan))));
}

function renderPlannedDay(view, dateKey, weekHasPlan) {
  const { eventsByDate, planningContextByDate, todayKey } = view;
  const dayEntries = eventsByDate.get(dateKey) || [];
  const dayContext = planningContextByDate.get(dateKey) || {};
  const dayEvents = view.calendarEventsByDate?.get(dateKey) || [];
  const day = h("section", { className: `planned-day${dateKey === todayKey ? " is-today" : ""}`, dataset: { date: dateKey } });
  const heading = plannedDayHeading(plannedDayWeather(dayContext, dateKey), dateKey, todayKey);
  // The band names the day's events and their impact, so the calendar note line leaves them out.
  const noteContext = plannedNoteContext(dayContext, dayEvents);
  const daySessions = (view.allEventsByDate?.get(dateKey) || []).length;
  const hiddenByFilter = daySessions > dayEntries.length;
  const notes = plannedDayNotes(noteContext, state.data?.competitions, dateKey, daySessions > 0);
  let emptyText = dateKey < todayKey ? "Keine Aktivität" : "Keine Einheit geplant";
  if (hiddenByFilter) emptyText = "Keine Einheit dieser Sportart";
  const deleted = [...plannedDeleteReceipts].filter(([, receipt]) => receipt.day === dateKey).map(([id, receipt]) => {
    const status = h("p", { className: "planned-move-status", role: "status" }, receipt.text);
    if (receipt.change?.id) status.append(...plannedMoveUndo(id, receipt, status, plannedDeleteReceipts));
    return status;
  });
  const content = h("div", "planned-day-content", heading, ...deleted, notes.childElementCount ? notes : null,
    dayEvents.length ? plannedEventBand(dayEvents) : null,
    dayEntries.length ? null : h("p", "planned-day-empty", emptyText));
  for (const group of plannedEntryGroups(dayEntries)) {
    content.append(group.entries.length > 1
      ? renderPlannedGroup(group.entries, dateKey, todayKey, dayContext, weekHasPlan)
      : renderPlannedEntry(group.entries[0], dateKey, todayKey, dayContext, weekHasPlan));
  }
  day.append(plannedDayDate(day, dateKey), content);
  return day;
}

// ISO 8601 week number: the week holding the year's first Thursday is week 1.
function isoWeekNumber(weekKey) {
  const thursday = dateFromKey(addDateKey(weekKey, 3));
  const firstWeek = planWeekStart(localDateKey(new Date(thursday.getFullYear(), 0, 4)));
  return 1 + Math.round((dateFromKey(weekKey) - dateFromKey(firstWeek)) / (7 * 86_400_000));
}

// "Woche | Monat": the view is a switch only; the calendar always opens on the week.
function plannedViewSwitch() {
  const group = h("div", { className: "planned-view-switch", "aria-label": "Ansicht" });
  group.setAttribute("role", "group");
  for (const [value, label] of [["week", "Woche"], ["month", "Monat"]]) {
    const button = h("button", { type: "button", dataset: { view: value }, "aria-pressed": String(calendarView === value) }, label);
    button.addEventListener("click", () => {
      if (calendarView === value) return;
      // The athlete's choice wins over a still pending jump to today.
      state.plannedTodayFocusPending = false;
      calendarView = value;
      selectedDayKey = "";
      renderPlanned(renderedCalendar);
      $("#plannedCalendar")?.querySelector(`.planned-view-switch [data-view="${value}"]`)?.focus();
    });
    group.append(button);
  }
  return group;
}

// Sport filter (#2810): it hides the units of other sports and their figures, never calendar
// appointments; the choice is a per-device convenience and never reaches the server.
const CALENDAR_SPORT_KEY = "pacecraft-calendar-sport";
// Chip label, then the sport labels (activitySportLabel) the chip shows.
const CALENDAR_SPORTS = { all: ["Alle", []], run: ["Lauf", ["Laufen"]], ride: ["Rad", ["Radfahren", "Rad indoor"]], strength: ["Kraft", ["Kraft"]], other: ["Andere", null] };

function readCalendarSport() {
  try {
    const value = localStorage.getItem(CALENDAR_SPORT_KEY);
    return Object.hasOwn(CALENDAR_SPORTS, value) ? value : "all";
  } catch { return "all"; }
}

function calendarSportShown(entry, sport = calendarSport) {
  if (sport === "all") return true;
  const label = activitySportLabel(calendarSportType(calendarActualActivity(entry) || entry));
  // "Andere" holds every sport without a filter of its own: swims, walks and sessions without a type (#3936 G-10).
  if (sport === "other") return !Object.values(CALENDAR_SPORTS).some(([, labels]) => labels?.includes(label));
  return CALENDAR_SPORTS[sport][1].includes(label);
}

function plannedSportFilter() {
  const group = h("div", { className: "chip-group planned-sport-filter", "aria-label": "Sportart" });
  group.setAttribute("role", "radiogroup");
  for (const [value, [label]] of Object.entries(CALENDAR_SPORTS)) {
    const input = h("input", { type: "radio", name: "plannedSport", value, checked: calendarSport === value });
    input.addEventListener("change", () => {
      calendarSport = value;
      try { localStorage.setItem(CALENDAR_SPORT_KEY, value); } catch { }
      renderPlanned(renderedCalendar);
      $("#plannedCalendar")?.querySelector(`.planned-sport-filter input[value="${value}"]`)?.focus();
    });
    group.append(h("label", "chip", input, h("span", null, label)));
  }
  return group;
}

function monthStartKey(dateKey, offset = 0) {
  const date = dateFromKey(`${dateKey.slice(0, 8)}01`);
  date.setMonth(date.getMonth() + offset);
  return localDateKey(date);
}

// In the month view the selected day also sets the week shown below (renderPlanned).
function selectPlannedDay(dateKey) {
  selectedDayKey = dateKey;
  renderPlanned(renderedCalendar);
}

// One day of the month grid: its number and one mark per unit (a dot, on wide screens its duration).
function plannedMonthDay(view, dateKey, monthKey) {
  const { eventsByDate, firstWeekKey, lastDateKey, planningContextByDate, todayKey } = view;
  const entries = eventsByDate.get(dateKey) || [];
  const dayContext = planningContextByDate.get(dateKey) || {};
  const button = h("button", { type: "button", className: "planned-month-day", dataset: { date: dateKey }, "aria-pressed": String(dateKey === view.dayKey), disabled: dateKey < firstWeekKey || dateKey > lastDateKey });
  button.classList.toggle("is-outside", dateKey.slice(0, 7) !== monthKey.slice(0, 7));
  button.classList.toggle("is-today", dateKey === todayKey);
  const number = h("span", "planned-month-number", String(dateFromKey(dateKey).getDate()));
  const marks = h("span", "planned-month-marks");
  for (const entry of entries) {
    const actual = calendarActualActivity(entry);
    const status = calendarEntryStatus(entry, dateKey, todayKey);
    const seconds = actual ? Number(AppFormat.sessionSeconds(actual)) : plannedDurationSeconds(entry);
    // As on the card: a missed unit on a rest or pause day counts as skipped.
    // h:mm fits a day cell; the full label stays on the card.
    marks.append(h("span", {
      className: `planned-month-mark is-${status === "missed" && plannedDayStatusLabel(dayContext.day_status) ? "skipped" : status}`,
      dataset: { sport: activitySportLabel(calendarSportType(actual || entry)) },
    }, seconds >= 30 ? calendarClock(seconds) : "•"));
  }
  button.append(number, marks);
  const blocked = (view.calendarEventsByDate?.get(dateKey) || []).some((event) => event.impact === "block");
  button.classList.toggle("has-blocked-event", blocked);
  const units = entries.length ? countLabel(entries.length, "Einheit", "Einheiten") : "keine Einheit";
  // The visible number and marks are decoration for the name, which the hidden text spells out (WCAG 2.5.3).
  number.setAttribute("aria-hidden", "true");
  marks.setAttribute("aria-hidden", "true");
  button.append(h("span", "sr-only", `${DETAIL_DATE_FORMAT.format(dateFromKey(dateKey))}: ${units}${blocked ? ", Termin blockiert Training" : ""}`));
  button.addEventListener("click", () => {
    state.plannedTodayFocusPending = false;
    selectPlannedDay(dateKey);
    const root = $("#plannedCalendar");
    root?.querySelector(`.planned-month-day[data-date="${dateKey}"]`)?.focus();
    if (!wideCalendar?.matches) return;
    const card = root?.querySelector(`.planned-day[data-date="${dateKey}"] .planned-entry`);
    const group = card?.closest(".planned-group");
    if (group) group.open = true;
    if (card) card.open = true;
    else {
      const shown = shownPlannedEntry(root);
      if (shown) shown.open = false;
    }
  });
  return button;
}

// The month of the selected day: weeks Monday to Sunday with the ISO week and its planned time.
function renderPlannedMonth(view) {
  const { firstWeekKey, lastDateKey, todayKey } = view;
  const monthKey = monthStartKey(view.dayKey);
  const nextMonthKey = monthStartKey(monthKey, 1);
  const month = h("section", { className: "planned-month", "aria-labelledby": "plannedMonthTitle" });
  const title = h("h4", { id: "plannedMonthTitle" }, new Intl.DateTimeFormat("de-DE", { month: "long", year: "numeric" }).format(dateFromKey(monthKey)));
  // A month step selects today when it lies in that month, else the month's first loaded day.
  const stepTo = (key) => (todayKey.startsWith(key.slice(0, 7)) ? todayKey : [key, firstWeekKey].sort((left, right) => left.localeCompare(right)).at(-1));
  const heading = h("header", "planned-week-heading", title,
    plannedStep(-1, "Vorheriger Monat", addDateKey(monthKey, -1) >= firstWeekKey ? () => selectPlannedDay(stepTo(monthStartKey(monthKey, -1))) : null, "month"),
    plannedStep(1, "Nächster Monat", nextMonthKey <= lastDateKey ? () => selectPlannedDay(stepTo(nextMonthKey)) : null, "month"),
    plannedViewSwitch(), plannedSportFilter());
  const names = h("div", { className: "planned-month-row planned-month-names", "aria-hidden": "true" }, ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So", "KW"].map((name) => h("span", null, name)));
  const grid = h("div", "planned-month-grid", names);
  for (let weekKey = planWeekStart(monthKey); weekKey < nextMonthKey; weekKey = addDateKey(weekKey, 7)) grid.append(plannedMonthRow(view, weekKey, monthKey));
  month.append(heading, grid, plannedMonthTotals(view, monthKey, nextMonthKey), plannedMonthLegend());
  return month;
}

// Pure: the time of the days given, done for a recorded session and planned for a unit still ahead (a missed one counts for neither).
function plannedTimes(view, dateKeys) {
  const total = { done: 0, planned: 0 };
  for (const dateKey of dateKeys) {
    for (const entry of view.eventsByDate.get(dateKey) || []) {
      const actual = calendarActualActivity(entry);
      if (actual) total.done += Number(AppFormat.sessionSeconds(actual)) || 0;
      else if (calendarEntryStatus(entry, dateKey, view.todayKey) !== "missed") total.planned += plannedDurationSeconds(entry);
    }
  }
  return total;
}

// "Monat: 18:20 h absolviert · 3:00 h geplant" for the days of the shown month (#3932 K-07).
function plannedMonthTotals(view, monthKey, nextMonthKey) {
  const days = [];
  for (let day = monthKey; day < nextMonthKey; day = addDateKey(day, 1)) days.push(day);
  const { done, planned } = plannedTimes(view, days);
  const parts = [done > 0 ? `${calendarHours(done)} absolviert` : null, planned > 0 ? `${calendarHours(planned)} geplant` : null].filter(Boolean);
  return h("p", "planned-month-totals", parts.length ? `Monat: ${parts.join(" · ")}` : "Monat: keine Einheiten");
}

// The marks in a day cell, named once under the grid.
function plannedMonthLegend() {
  const mark = (status, text) => h("span", null, h("i", `planned-month-mark is-${status}`), text);
  return h("p", "planned-month-legend", mark("completed", "absolviert"), mark("planned", "geplant"), mark("missed", "verpasst"), mark("skipped", "entfallen"),
    h("span", null, h("i", "planned-month-blocked"), "Termin blockiert Training"));
}

// One week of the month grid: seven days, then the ISO week with its planned time.
function plannedMonthRow(view, weekKey, monthKey) {
  const row = h("div", `planned-month-row${weekKey === planWeekStart(view.todayKey) ? " is-current" : ""}`);
  for (let offset = 0; offset < 7; offset += 1) row.append(plannedMonthDay(view, addDateKey(weekKey, offset), monthKey));
  // The week's time is what was done plus what is still planned, so past weeks show theirs too.
  const times = plannedTimes(view, Array.from({ length: 7 }, (_, offset) => addDateKey(weekKey, offset)));
  const seconds = times.done + times.planned;
  row.append(h("span", "planned-month-kw", [`KW ${isoWeekNumber(weekKey)}`, seconds > 0 ? calendarHours(seconds) : ""].filter(Boolean).join(" · ")));
  return row;
}

// A step button; after the step, focus stays on it or on the other one of its pair.
function plannedStep(step, label, onStep, pair) {
  const button = h("button", { type: "button", className: "icon-button planned-week-step", dataset: { step: String(step), pair }, "aria-label": label, disabled: !onStep }, step < 0 ? "\u2039" : "\u203a");
  button.addEventListener("click", () => {
    state.plannedTodayFocusPending = false;
    onStep();
    const steps = [...($("#plannedCalendar")?.querySelectorAll(`.planned-week-step[data-pair="${pair}"]:not(:disabled)`) || [])];
    (steps.find((item) => item.dataset.step === String(step)) || steps[0])?.focus();
  });
  return button;
}

function plannedWeekStep(step, target) {
  const button = plannedStep(step, step < 0 ? "Vorherige Woche" : "Nächste Woche", target ? () => {
    selectedWeekKey = target;
    if (calendarView === "month") selectedDayKey = target;
    renderPlanned(renderedCalendar);
  } : null, "week");
  button.dataset.weekStep = String(step);
  return button;
}


function renderPlannedWeek(view) {
  const { allEventsByDate, eventsByDate, firstWeekKey, lastWeekKey, todayKey, weekKey } = view;
  const weekOf = (byDate) => Array.from({ length: 7 }, (_, offset) => byDate.get(addDateKey(weekKey, offset)) || []).flat();
  const weekEntries = weekOf(eventsByDate);
  // The week sync builds its jobs, remote removals included, from every unit of the week, whatever the sport filter.
  const allWeekEntries = weekOf(allEventsByDate);
  const week = h("section", { className: "planned-week", dataset: { weekKey }, "aria-labelledby": "plannedWeekTitle" });
  const heading = h("header", "planned-week-heading",
    h("h4", { id: "plannedWeekTitle" }, planWeekLabel(weekKey)),
    h("span", "planned-week-number", `KW ${isoWeekNumber(weekKey)}`),
    plannedWeekStep(-1, weekKey > firstWeekKey ? addDateKey(weekKey, -7) : ""),
    plannedWeekStep(1, weekKey < lastWeekKey ? addDateKey(weekKey, 7) : ""),
    calendarView === "week" ? plannedViewSwitch() : null,
    calendarView === "week" ? plannedSportFilter() : null,
    h("span", "planned-week-summary", [calendarSport === "all" ? "" : `Gefiltert: ${CALENDAR_SPORTS[calendarSport][0]} · `, calendarCountLabel(weekEntries, todayKey) || "Keine Einheiten"]));
  const planned = weekEntries.filter((entry) => !entry.is_completed_activity);
  const actual = weekEntries.map(calendarActualActivity).filter(Boolean);
  const totals = plannedWeekMetrics(planned, actual);
  if (totals) heading.append(totals);
  const syncButton = plannedWeekSyncButton(weekKey, allWeekEntries, view.leftoversByWeek?.get(weekKey), todayKey);
  if (syncButton) heading.append(h("div", "planned-week-sync", syncButton));
  week.append(heading);
  const days = h("div", "planned-week-days");
  // "Zusätzlich" depends on the whole week's plan, not on the shown sport.
  const weekHasPlan = allWeekEntries.some((entry) => !entry.is_completed_activity);
  for (let offset = 0; offset < 7; offset += 1) days.append(renderPlannedDay(view, addDateKey(weekKey, offset), weekHasPlan));
  week.append(days);
  const legend = plannedZoneLegend(days);
  if (legend) week.append(legend);
  // The plan line and the review count the whole week, so a sport filter leaves both out.
  const closing = calendarSport === "all" ? plannedWeekClosing(view, weekEntries, weekHasPlan) : null;
  if (closing) week.append(closing);
  return week;
}

// The week's last line: the review of a past or running week the server reviews, whose Plan tile
// carries the compliance (#3298), else the plan line, which also stands in when the review fails.
function plannedWeekClosing(view, weekEntries, weekHasPlan) {
  const { todayKey, weekKey, weeklyCompliance } = view;
  const extra = weekEntries.filter((entry) => entry.is_completed_activity).length;
  const planned = weekEntries.length - extra;
  let planLine = null;
  if (weekEntries.length) {
    const summary = plannedWeekSummary(weekKey, addDateKey(weekKey, 6), weekEntries, weeklyCompliance.get(weekKey), todayKey);
    planLine = h("p", "planned-week-totals", extra && planned ? `${summary} · +${extra} zusätzlich` : summary);
  }
  const currentWeekKey = planWeekStart(todayKey);
  if (weekKey > currentWeekKey || weekKey < addDateKey(currentWeekKey, -7 * WEEK_REVIEW_WEEKS_BACK)) return planLine;
  return plannedWeekReview(weekKey, todayKey, weekHasPlan ? extra : 0, planLine);
}

// The week review under the selected past or running week (#3298, layout B): computed on the
// server from the stored summaries without a model (#2805); a future week has none. A tile
// without data is left out.
let weekReviewGeneration = 0;
// The server reviews this week and the 51 before it (week_review.REVIEW_WEEKS_BACK).
const WEEK_REVIEW_WEEKS_BACK = 51;
const weekReviewFallbacks = new WeakMap();
const WEEK_REVIEW_SHORT_DAY = new Intl.DateTimeFormat("de-DE", { weekday: "short", timeZone: "UTC" });
const WEEK_REVIEW_REASONS = { highest_load: "höchste Belastung", race: "Wettkampf", missed: "verpasst" };
const WEEK_REVIEW_POSITIONS = { below: "unter Normalbereich", within: "im Normalbereich", above: "über Normalbereich" };
const WEEK_REVIEW_SCORES = [["motivation", "Motivation"], ["stress", "Stress"], ["soreness", "Muskelkater"]];

const weekReviewDay = (key) => WEEK_REVIEW_SHORT_DAY.format(new Date(`${key}T00:00:00Z`)).replace(".", "");

// Hours and minutes of a duration, as h:mm.
function weekReviewHours(seconds) {
  const minutes = Math.round(Math.abs(seconds) / 60);
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}`;
}

function weekReviewChange(text, up) {
  return h("em", { className: "week-review-change", dataset: { direction: up ? "up" : "down" } }, text);
}

function weekReviewPercent(value, before) {
  if (!Number.isFinite(value) || !Number.isFinite(before) || before <= 0) return null;
  const percent = Math.round((value / before - 1) * 100);
  return weekReviewChange(`${percent >= 0 ? "+" : "−"}${Math.abs(percent)} % ggü. Vorwoche`, percent >= 0);
}

function weekReviewTile(label, value, change = null, note = "") {
  return h("div", "week-review-tile", h("small", null, label), h("b", null, value), change, note ? h("small", "week-review-note", note) : null);
}

function weekReviewPlanTile(plan) {
  if (!plan?.planned) return null;
  const missed = (plan.missed_units || []).map((unit) => `${weekReviewDay(unit.date)} ${unit.name || "Einheit"}`);
  const note = [missed.length ? `verpasst: ${missed.join(", ")}` : null, plan.open ? `${plan.open} noch offen` : null].filter(Boolean).join(" · ");
  return weekReviewTile("Plan", `${plan.completed} von ${plan.planned}`, null, note);
}

// A running week has no full total yet, so time and load show no change against the week before.
function weekReviewLoadTiles(load, running, extra) {
  const now = load?.this_week || {};
  const before = load?.week_before || {};
  if (!now.sessions) return [];
  return [
    weekReviewTile("Einheiten", String(now.sessions), null, extra > 0 ? `davon ${extra} zusätzlich` : ""),
    weekReviewTile("Zeit", `${weekReviewHours(now.duration_s || 0)} h`, !running && weekReviewPercent(now.duration_s, before.duration_s)),
    Number.isFinite(now.training_load) ? weekReviewTile("Belastung", AppFormat.number(now.training_load, { digits: 0 }), !running && weekReviewPercent(now.training_load, before.training_load)) : null,
  ];
}

function weekReviewRecoveryTiles(recovery) {
  const tiles = [];
  const sleep = recovery?.sleep;
  if (sleep) {
    const minutes = Number.isFinite(sleep.week_before) ? Math.round((sleep.mean - sleep.week_before) * 60) : 0;
    const sign = minutes > 0 ? "+" : "−";
    const change = minutes ? weekReviewChange(`${sign}${weekReviewHours(minutes * 60)} ggü. Vorwoche`, minutes > 0) : null;
    tiles.push(weekReviewTile("Schlaf Ø", `${weekReviewHours(sleep.mean * 3600)} h`, change, sleep.source));
  }
  for (const [key, label] of [["resting_hr", "Ruhepuls Ø"], ["hrv", "HRV Ø"]]) {
    const item = recovery?.[key];
    if (item) tiles.push(weekReviewTile(label, `${AppFormat.number(item.mean, { digits: 0 })} ${item.unit}`, null, [WEEK_REVIEW_POSITIONS[item.position], item.source].filter(Boolean).join(" · ")));
  }
  return tiles;
}

// One line per body area and side (athlete: layout A, #3766): its days and its severity,
// a worsening as a red change.
function weekReviewComplaintTile(checkins) {
  const lines = (checkins.complaints || []).map((item) => {
    const days = `${item.days} ${item.days === 1 ? "Tag" : "Tage"}`;
    const change = item.days > 1 && item.last_severity !== item.first_severity
      ? weekReviewChange(`${item.first_severity} → ${item.last_severity}`, item.last_severity < item.first_severity)
      : `${item.last_severity}/10`;
    return h("li", null, `${complaintLabel(item)} · ${days} · Stärke `, change);
  });
  // Pain days told only in words keep their count.
  const textOnly = checkins.pain_text_days || 0;
  if (textOnly > 0) lines.push(h("li", null, `${textOnly} ${textOnly === 1 ? "Tag" : "Tage"} Schmerz (Text)`));
  if (checkins.illness_days) lines.push(h("li", null, `${checkins.illness_days} ${checkins.illness_days === 1 ? "Tag" : "Tage"} krank`));
  return h("div", "week-review-tile week-review-wide", h("small", null, "Beschwerden"), h("ul", null, lines));
}

function weekReviewCheckinTiles(checkins) {
  if (!checkins?.entered) return [];
  const scores = WEEK_REVIEW_SCORES.filter(([key]) => Number.isFinite(checkins.scores?.[key])).map(([key, label]) => `${label} ${AppFormat.number(checkins.scores[key])}`);
  const complaints = [checkins.pain_days && `${checkins.pain_days} ${checkins.pain_days === 1 ? "Tag" : "Tage"} Schmerz`, checkins.illness_days && `${checkins.illness_days} ${checkins.illness_days === 1 ? "Tag" : "Tage"} krank`].filter(Boolean);
  return [
    weekReviewTile("Check-ins", `${checkins.entered} von ${checkins.days}`, null, scores.length ? `Ø ${scores.join(" · ")}` : ""),
    checkins.complaints?.length ? weekReviewComplaintTile(checkins) : weekReviewTile("Beschwerden", complaints.join(", ") || "keine"),
  ];
}

function weekReviewNotableTile(notable) {
  if (!notable?.length) return null;
  return h("div", "week-review-tile week-review-wide", h("small", null, "Besondere Einheiten"), h("ul", null, notable.map((item) => {
    const load = item.reason === "highest_load" && Number.isFinite(item.training_load) ? ` (${AppFormat.number(item.training_load, { digits: 0 })})` : "";
    const race = item.race && item.reason !== "race" ? " · Wettkampf" : "";
    return h("li", null, `${weekReviewDay(item.date)} ${item.name || "Einheit"} · ${WEEK_REVIEW_REASONS[item.reason]}${load}${race}`);
  })));
}

// The tiles in the approved order; `extra` counts the week's completed sessions outside the plan.
// This week runs until its Sunday ends, also on that Sunday, when the review already reaches it.
function weekReviewTiles(review, extra, running) {
  const [sessions, time, load] = weekReviewLoadTiles(review.load, running, extra);
  const [checkins, complaints] = weekReviewCheckinTiles(review.checkins);
  return [weekReviewPlanTile(review.plan), sessions, time, load, ...weekReviewRecoveryTiles(review.recovery), checkins, weekReviewNotableTile(review.notable), complaints].filter(Boolean);
}

function plannedWeekReview(weekKey, todayKey, extra, planLine) {
  const thisWeek = weekKey === planWeekStart(todayKey);
  const card = h("section", { className: "week-review", dataset: { weekReview: weekKey, extra: String(extra), thisWeek: String(thisWeek) }, "aria-labelledby": "weekReviewTitle" },
    h("header", "week-review-head", h("h5", { id: "weekReviewTitle" }, "Wochenrückblick"), h("span", null, [`KW ${isoWeekNumber(weekKey)}`, thisWeek ? "bis heute" : null].filter(Boolean).join(" · "))),
    createLoadingState("Wochenrückblick wird geladen…"));
  weekReviewFallbacks.set(card, planLine);
  void fillWeekReview(card);
  return card;
}

// A calendar render without changes keeps the card, so a new check-in or sync refills it in place.
function refreshWeekReview(root) {
  const card = root.querySelector(".week-review");
  if (card?.dataset.failed) retryWeekReview(card);
  else if (card?.dataset.version && card.dataset.version !== analysisReportVersionKey()) void fillWeekReview(card, true);
}

// A failed or timed-out read never removes the card (#3391): it names the gap and offers a retry;
// the next calendar refresh retries as well.
function weekReviewFailed(card) {
  card.dataset.failed = "true";
  const retry = h("button", { type: "button", className: "secondary-button" }, "Erneut laden");
  retry.addEventListener("click", () => retryWeekReview(card, true));
  card.replaceChildren(card.firstElementChild, h("p", "empty", "Wochenrückblick gerade nicht verfügbar"), retry);
  // A retry started from the button keeps the keyboard there when it fails again (Gitar #3413).
  if (card.dataset.retryFocus && document.activeElement === card) retry.focus();
  delete card.dataset.retryFocus;
}

// The pressed button leaves the card, so a tapped retry moves focus to the card while it loads.
function retryWeekReview(card, focus = false) {
  delete card.dataset.failed;
  card.replaceChildren(card.firstElementChild, createLoadingState("Wochenrückblick wird geladen…"));
  if (focus) {
    card.dataset.retryFocus = "true";
    card.tabIndex = -1;
    card.focus();
  }
  void fillWeekReview(card);
}

// A step to another week or a new calendar render replaces the card; a late answer for it is dropped.
// A week without data shows the plan line instead, or nothing; a failed read shows why with a retry,
// and a failed refill keeps what the card shows.
async function fillWeekReview(card, refill = false) {
  const generation = ++weekReviewGeneration;
  const session = state.sessionGeneration;
  const version = analysisReportVersionKey();
  const { weekReview: weekKey, extra, thisWeek } = card.dataset;
  const current = () => generation === weekReviewGeneration && session === state.sessionGeneration && card.isConnected;
  let tiles = [];
  try {
    // This week asks for "current", so the server's day decides at the turn of the week; an answer
    // for another week shows nothing.
    const review = await requestAnalysisReport(`/api/analysis/week-review?week=${thisWeek === "true" ? "current" : weekKey}`);
    if (!current()) return;
    if (review.week_start === weekKey) tiles = weekReviewTiles(review, Number(extra), thisWeek === "true");
  } catch {
    if (current() && !refill) weekReviewFailed(card);
    return;
  }
  if (!tiles.length) {
    const planLine = weekReviewFallbacks.get(card);
    // A focused card hands the keyboard to the plan line that replaces it (Gitar #3413).
    const hadFocus = card.contains(document.activeElement);
    if (planLine) card.replaceWith(planLine); else card.remove();
    if (hadFocus && planLine) {
      planLine.tabIndex = -1;
      planLine.focus();
    }
    return;
  }
  card.dataset.version = version;
  delete card.dataset.retryFocus;
  card.replaceChildren(card.firstElementChild, h("div", "week-review-tiles", ...tiles));
}

// Wide screens show the open card's body as the detail pane beside the week (styles.css). It starts level
// with the top of the week card, whichever unit is open, and stays in view while a long week scrolls
// (#3156); the calendar keeps room for it, and a week opens with today's or the next unit.
const wideCalendar = globalThis.matchMedia?.("(min-width: 1100px)");
const detailObserver = globalThis.ResizeObserver ? new ResizeObserver(() => syncPlannedDetailHeight()) : null;
const PANE_STICKY_TOP = 24;

function syncPlannedDetailHeight() {
  const root = $("#plannedCalendar");
  if (!root?.getClientRects().length) return;
  const card = wideCalendar?.matches ? shownPlannedEntry(root) : null;
  const body = card?.querySelector(":scope > .planned-entry-details");
  if (!body) {
    root.style.minHeight = "";
    return;
  }
  const box = root.getBoundingClientRect();
  const calendar = (card.closest(".planned-week, .planned-month") || card).getBoundingClientRect();
  const start = Math.max(0, calendar.top - box.top);
  const height = body.offsetHeight;
  // The pane is the open card's body inside its <details>, placed absolutely beside the week, so it cannot use
  // position: sticky; a passive scroll listener moves it with the window, down to the calendar card's end.
  const end = Math.max(start, calendar.bottom - box.top - height);
  body.style.top = `${Math.min(Math.max(start, PANE_STICKY_TOP - box.top), end)}px`;
  root.style.minHeight = `${start + height}px`;
}

// A closed card leaves the pane empty; a collapsed group or a switch to a wide screen opens the default.
function watchPlannedDetail(event) {
  const root = $("#plannedCalendar");
  if (!event?.target?.classList?.contains("planned-entry")) openDefaultPlannedEntry(root?.querySelector(".planned-week"));
  detailObserver?.disconnect();
  const body = shownPlannedEntry(root)?.querySelector(":scope > .planned-entry-details");
  if (body) detailObserver?.observe(body);
  syncPlannedDetailHeight();
}

function openDefaultPlannedEntry(week) {
  if (!wideCalendar?.matches || !week || shownPlannedEntry(week)) return;
  const todayKey = timezoneDateKey(state.data?.profile?.timezone, new Date());
  const upcoming = (cards) => cards.find((item) => item.closest(".planned-day").dataset.date >= todayKey) || cards.at(-1);
  const single = upcoming([...week.querySelectorAll(".planned-day-content > .planned-entry")]);
  const card = single || upcoming([...week.querySelectorAll(".planned-group-items > .planned-entry")]);
  if (!card) return;
  if (!single) card.closest(".planned-group").open = true;
  card.open = true;
}

let plannedRenderSnapshot = null;
let selectedWeekKey = "";
let calendarView = "week";
let calendarSport = readCalendarSport();
let selectedDayKey = "";
let renderedCalendar = [];
// The last day the calendar shows; a move stays inside it, so the moved card and its receipt stay visible.
let plannedLastDateKey = "";

// Keeps the shown week (and in the month view the selected day, which sets the week) inside the loaded weeks.
// The past has no limit: older weeks stay selectable and their sessions load on demand (#2258).
function keepPlannedSelection(todayKey, lastKey) {
  const currentWeekKey = planWeekStart(todayKey);
  if (!selectedWeekKey || selectedWeekKey > lastKey) selectedWeekKey = currentWeekKey;
  if (calendarView !== "month") return;
  if (!selectedDayKey || selectedDayKey > lastKey) selectedDayKey = selectedWeekKey === currentWeekKey ? todayKey : selectedWeekKey;
  selectedWeekKey = planWeekStart(selectedDayKey);
}

// Sessions older than the loaded calendar data come page by page from the session list,
// newest first; sessions the calendar already holds are skipped.
const olderSessions = { items: [], cursor: undefined, loading: false, generation: 0 };

// New calendar data (sync, reload, undo, feedback) drops the cached older pages, so a deleted,
// split or edited session is read again; an answer for the dropped pages is ignored.
function resetOlderSessions() {
  Object.assign(olderSessions, { items: [], cursor: undefined, loading: false, generation: olderSessions.generation + 1 });
}

function withOlderSessions(calendar) {
  const known = completedSessionIds(calendar);
  return calendar.concat(olderSessions.items.filter((item) => !known.has(String(item.activity_id || item.id))));
}

// Only a week before the shown range asks for more: the startup data covers that range.
function loadOlderSessions(calendar, shownStartKey, configuredStartKey) {
  if (olderSessions.loading || olderSessions.cursor === null || shownStartKey >= configuredStartKey) return;
  const loadedStart = calendar.filter((item) => calendarActualActivity(item)).map(plannedEventDate).filter(Boolean).sort()[0];
  if (loadedStart && shownStartKey >= loadedStart) return;
  olderSessions.loading = true;
  const generation = olderSessions.generation;
  const cursor = olderSessions.cursor ? `&cursor=${encodeURIComponent(olderSessions.cursor)}` : "";
  api(`/api/activities?limit=100${cursor}`, { timeoutMs: DATA_LOAD_TIMEOUT_MS }).then((page) => {
    if (generation !== olderSessions.generation) return;
    const known = completedSessionIds(calendar);
    for (const row of Array.isArray(page?.activities) ? page.activities : []) {
      // Only sessions before the loaded ones are new; later ones are in the loaded data already.
      const day = String(row?.started_at_local || "").slice(0, 10);
      if (row?.id == null || known.has(String(row.id)) || (loadedStart && day >= loadedStart)) continue;
      olderSessions.items.push({ ...row, date: String(row.started_at_local || "").slice(0, 10), is_completed_activity: true });
    }
    olderSessions.cursor = page?.next_cursor || null;
    olderSessions.loading = false;
    renderPlanned(renderedCalendar);
  }, () => { if (generation === olderSessions.generation) olderSessions.loading = false; });
}

// A delete receipt stays while its week is shown.
function dropDeleteReceiptsOutsideWeek() {
  for (const [id, receipt] of plannedDeleteReceipts) if (planWeekStart(receipt.day) !== selectedWeekKey) plannedDeleteReceipts.delete(id);
}

// One hint when no upcoming day has a weather forecast.
function appendPlannedWeatherHint(root, { firstWeekKey, todayKey, days, planningContextByDate }) {
  const upcomingDays = Array.from({ length: days }, (_, offset) => addDateKey(firstWeekKey, offset)).filter((dateKey) => dateKey >= todayKey);
  if (!upcomingDays.some((dateKey) => plannedWeatherLabel(plannedDayWeather(planningContextByDate.get(dateKey) || {}, dateKey)))) root.append(plannedWeatherHint());
}

function renderPlanned(trainingCalendar) {
  const root = $("#plannedCalendar");
  if (!root) return;
  if (trainingCalendar !== renderedCalendar) {
    resetOlderSessions();
    plannedSessionReads.clear();
    plannedFuellingReads.clear();
  }
  renderedCalendar = trainingCalendar;
  const calendar = withOlderSessions(Array.isArray(trainingCalendar) ? trainingCalendar : []);
  const todayKey = timezoneDateKey(state.data?.profile?.timezone, new Date());
  const currentWeekKey = planWeekStart(todayKey);
  const pastWeeks = CALENDAR_WEEKS.past;
  const futureWeeks = CALENDAR_WEEKS.future;
  const firstWeekKey = addDateKey(currentWeekKey, -7 * pastWeeks);
  const lastWeekKey = addDateKey(currentWeekKey, 7 * futureWeeks);
  const lastDateKey = addDateKey(lastWeekKey, 6);
  plannedLastDateKey = lastDateKey;
  keepPlannedSelection(todayKey, lastDateKey);
  dropDeleteReceiptsOutsideWeek();
  const snapshot = JSON.stringify([calendar, todayKey, selectedWeekKey, calendarView, calendarSport, selectedDayKey, state.data?.daily_planning_context, state.data?.planning_compliance, state.data?.competitions, state.data?.weather, (state.data?.external_calendar?.events || []).map((event) => [event?.uid, event?.start_local, event?.impact, event?.name]), state.data?.external_calendar?.undecided_upcoming]);
  if (snapshot === plannedRenderSnapshot && root.childElementCount) {
    if (state.plannedTodayFocusPending) requestAnimationFrame(() => focusPlannedToday());
    refreshWeekReview(root);
    return;
  }
  plannedRenderSnapshot = snapshot;
  if (!root.dataset.detailWatch) {
    root.dataset.detailWatch = "true";
    root.addEventListener("toggle", watchPlannedDetail, true);
    wideCalendar?.addEventListener?.("change", watchPlannedDetail);
    globalThis.addEventListener?.("scroll", syncPlannedDetailHeight, { passive: true });
  }
  const focusedEntry = rememberPlannedEntryState(root);
  root.replaceChildren();
  const banner = plannedUndecidedBanner();
  if (banner) root.append(banner);
  const weeklyCompliance = new Map(
    (Array.isArray(state.data?.planning_compliance) ? state.data.planning_compliance : [])
      .filter((item) => item?.week_start)
      .map((item) => [String(item.week_start).slice(0, 10), item]),
  );
  const entries = calendar
    .filter((item) => item && !item.archived && !item.local_deleted && plannedEventDate(item))
    .filter((item) => plannedEventDate(item) <= lastDateKey)
    .sort((left, right) => String(left.starts_at_local || left.started_at_local || left.date || "").localeCompare(String(right.starts_at_local || right.started_at_local || right.date || "")));
  const planningContextByDate = new Map(
    (Array.isArray(state.data?.daily_planning_context) ? state.data.daily_planning_context : [])
      .filter((item) => item?.date)
      .map((item) => [String(item.date).slice(0, 10), item]),
  );
  const allEventsByDate = new Map();
  const eventsByDate = new Map();
  entries.forEach((entry) => {
    const key = plannedEventDate(entry);
    for (const byDate of calendarSportShown(entry) ? [allEventsByDate, eventsByDate] : [allEventsByDate]) {
      if (!byDate.has(key)) byDate.set(key, []);
      byDate.get(key).push(entry);
    }
  });

  appendPlannedWeatherHint(root, { firstWeekKey, todayKey, days: (pastWeeks + futureWeeks + 1) * 7, planningContextByDate });
  const view = {
    calendarEventsByDate: plannedEventsByDate(todayKey),
    leftoversByWeek: plannedLeftoversByWeek(trainingCalendar, currentWeekKey, lastWeekKey),
    allEventsByDate,
    eventsByDate,
    dayKey: selectedDayKey,
    // No earliest week: paging back is unlimited.
    firstWeekKey: "",
    lastDateKey,
    lastWeekKey,
    planningContextByDate,
    todayKey,
    weekKey: selectedWeekKey,
    weeklyCompliance,
  };
  if (calendarView === "month") root.append(renderPlannedMonth(view));
  const week = renderPlannedWeek(view);
  openDefaultPlannedEntry(week);
  root.append(week);
  restorePlannedEntryFocus(root, focusedEntry);
  watchPlannedDetail();
  if (state.plannedTodayFocusPending) requestAnimationFrame(() => focusPlannedToday());
  loadOlderSessions(calendar, calendarView === "month" ? planWeekStart(monthStartKey(selectedDayKey)) : selectedWeekKey, firstWeekKey);
}

// Wahoo state of an exported library workout. A changed one offers the
// explicit send; the job carries the payload hash the server returned, so a
// workout edited meanwhile is refused instead of pushed.
function libraryWahooRow(workout) {
  const wahoo = workout.wahoo_state;
  if (!wahoo?.state || !workout.id) return null;
  const changed = wahoo.state === "changed";
  const row = h("div", "library-wahoo", h("span", `library-wahoo-state is-${changed ? "changed" : "synced"}`, changed ? "Wahoo: geändert" : "Wahoo: synchron"));
  if (!changed || !wahoo.payload_hash) return row;
  state.librarySyncInFlight ||= new Set();
  const button = h("button", { type: "button", className: "secondary-button library-wahoo-send", "aria-label": `${workout.name || "Einheit"} an Wahoo senden`, disabled: state.librarySyncInFlight.has(workout.id) }, "Erneut senden");
  button.addEventListener("click", async () => {
    if (state.librarySyncInFlight.has(workout.id)) return;
    state.librarySyncInFlight.add(workout.id);
    button.disabled = true;
    let failure = "";
    try {
      const done = await runPlanPush(providerIds().wahoo, [{ library_workout_id: workout.id, expected_payload_hash: wahoo.payload_hash }]);
      if (done.status !== "completed") failure = globalThis.providerJobErrorDetail?.(done) || "Synchronisierung fehlgeschlagen";
    } catch (error) {
      failure = error.message;
    }
    state.librarySyncInFlight.delete(workout.id);
    globalThis.toast?.(failure || "An Wahoo gesendet", Boolean(failure));
    button.disabled = false;
    await globalThis.load?.();
  });
  row.append(button);
  return row;
}

// "Einplanen" and "Löschen" without the Coach (#3028): the calendar's day strip plans a local unit from the
// entry; "Löschen" archives the entry and units planned from it stay. Each receipt offers "Rückgängig".
const libraryReceipts = new Map();
// Entries whose day strip is open, so a background reload's redraw keeps it open (#3980).
const libraryOpenStrips = new Set();

function libraryActions(workout) {
  if (!workout.id) return [];
  const id = String(workout.id);
  const name = workout.name || "Bibliotheks-Einheit";
  const toggle = h("button", { type: "button", className: "secondary-button", "aria-expanded": "false" }, "Einplanen");
  const status = h("p", { className: "planned-move-status", role: "status" });
  // The training plans the unit may join (#4054) and the illness block's second tap (#4055).
  const plans = (state.data?.plans || []).filter((plan) => plan.id && !["archived", "completed", "cancelled"].includes(plan.status));
  const planSelect = plans.length ? h("select", null, h("option", { value: "" }, "Ohne Plan"), plans.map((plan) => h("option", { value: plan.id }, plan.name))) : null;
  const planField = planSelect ? h("label", { className: "library-plan-field", hidden: true }, "Trainingsplan", planSelect) : null;
  const again = h("button", { type: "button", className: "secondary-button", hidden: true }, "Trotzdem einplanen");
  let blockedDay = "";
  const schedule = async (day, accept = false) => {
    if (!day) return;
    const buttons = [...days.querySelectorAll("button, input")];
    buttons.forEach((item) => { item.disabled = true; });
    status.replaceChildren();
    again.hidden = true;
    libraryOpenStrips.delete(id);
    try {
      const body = { date: day, ...(planSelect?.value ? { plan_id: planSelect.value } : {}), ...(accept ? { accept_plan_findings: ["illness_hard"] } : {}) };
      const result = await api(`/api/library/${encodeURIComponent(id)}/schedule`, { method: "POST", body: JSON.stringify(body) });
      const text = `Eingeplant: ${MOVE_DAY_FORMAT.format(dateFromKey(day))}`;
      libraryReceipts.clear();
      libraryReceipts.set(id, { text, change: result.change });
      await globalThis.load?.();
      announcePlanChange(text);
    } catch (error) {
      buttons.forEach((item) => { item.disabled = false; });
      status.textContent = error.message;
      libraryOpenStrips.add(id);
      if (error.reason === "illness_hard") { blockedDay = day; again.hidden = false; }
    }
  };
  again.addEventListener("click", () => void schedule(blockedDay, true));
  const days = plannedDayStrip(timezoneDateKey(state.data?.profile?.timezone, new Date()), null, "Tag", (day) => void schedule(day));
  const showDays = (open) => {
    days.hidden = !open;
    if (planField) planField.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) { libraryOpenStrips.add(id); void markBlockedDays(days, `/api/library/${encodeURIComponent(id)}`); } else libraryOpenStrips.delete(id);
  };
  toggle.addEventListener("click", () => showDays(days.hidden));
  if (libraryOpenStrips.has(id)) showDays(true);
  const [remove, confirm] = inCardDelete(async () => {
    const result = await api(`/api/library/${encodeURIComponent(id)}/archive`, { method: "POST", body: JSON.stringify({ confirmed: true }) });
    libraryReceipts.clear();
    libraryReceipts.set(id, { text: `Gelöscht: ${name}`, change: result.change });
    await globalThis.load?.();
    announcePlanChange(`Gelöscht: ${name}`);
  });
  const receipt = libraryReceipts.get(id);
  if (receipt) libraryReceiptLine(status, id, receipt);
  const edit = h("button", { type: "button", className: "secondary-button" }, "Bearbeiten");
  edit.addEventListener("click", () => openLibraryEditor(workout));
  return [h("div", "planned-activity-actions", edit, toggle, remove), confirm, days, planField, again, status];
}

// "+ Einheit" and "Bearbeiten" (#3951): the unit editor over the library use cases; the receipt offers "Rückgängig".
let libraryView = "active";
let libraryArchived = [];
let libraryOpenId = "";

function openLibraryEditor(workout = null) {
  openUnitEditor({
    kind: "library",
    entry: workout,
    onSaved: async (result) => {
      const text = workout ? `Gespeichert: ${result.library_entry?.name || workout.name}` : `Angelegt: ${result.library_entry?.name}`;
      libraryReceipts.clear();
      libraryReceipts.set(String(result.local_id), { text, change: result.change });
      libraryOpenId = String(result.local_id);
      libraryView = "active";
      await globalThis.load?.();
      announcePlanChange(text);
    },
  });
}

async function loadArchivedLibrary() {
  try {
    libraryArchived = (await api("/api/library?archived=1")).workouts || [];
  } catch (error) {
    libraryArchived = [];
    globalThis.toast?.(error.message, true);
  }
  renderLibrary(state.data?.library || [], true);
}

// The training plans at the top of the Bibliothek (#3950): "+ Plan", "Bearbeiten", "Archivieren", "Löschen" (the
// plan only; its units stay) and, among the archived, "Wiederherstellen". Receipts offer "Rückgängig".
const planReceipts = new Map();
const PLAN_STATUS_LABELS = { draft: "Entwurf", planned: "Geplant", active: "Aktiv", paused: "Pausiert", completed: "Beendet", cancelled: "Abgebrochen", archived: "Archiviert" };

async function planChanged(text, result) {
  planReceipts.clear();
  planReceipts.set(String(result.plan_id), { text, change: result.change });
  await globalThis.load?.();
  announcePlanChange(text);
}

function openPlanForm(plan = null) {
  openPlanEditor({ plan, onSaved: (result, body) => planChanged(`${plan ? "Gespeichert" : "Angelegt"}: ${body.name}`, result) });
}

function planPost(plan, path, body, text) {
  return async () => planChanged(text, await api(`/api/training-plans/${encodeURIComponent(plan.id)}/${path}`, { method: "POST", body: JSON.stringify(body) }));
}

// A restored plan runs again when today lies in its period, else it is planned.
function planRestoredStatus(plan) {
  const today = timezoneDateKey(state.data?.profile?.timezone, new Date());
  return plan.start_date <= today && today <= plan.end_date ? "active" : "planned";
}

function planCard(plan) {
  const id = String(plan.id);
  const status = h("p", { className: "planned-move-status", role: "status" });
  const receipt = planReceipts.get(id);
  if (receipt) libraryReceiptLine(status, id, receipt, planReceipts);
  const [remove, removeNote] = inCardDelete(planPost(plan, "delete", { confirmed: true }, `Gelöscht: ${plan.name}`));
  const buttons = [];
  const notes = [removeNote];
  if (plan.status === "archived") {
    const restore = h("button", { type: "button", className: "secondary-button" }, "Wiederherstellen");
    restore.addEventListener("click", () => void planPost(plan, "update", { status: planRestoredStatus(plan) }, `Wiederhergestellt: ${plan.name}`)().catch((error) => { status.textContent = error.message; }));
    buttons.push(restore);
  } else {
    const edit = h("button", { type: "button", className: "secondary-button" }, "Bearbeiten");
    edit.addEventListener("click", () => openPlanForm(plan));
    const [archive, archiveNote] = inCardDelete(planPost(plan, "update", { status: "archived", confirmed: true }, `Archiviert: ${plan.name}`));
    archive.textContent = "Archivieren";
    archive.classList.remove("danger-button");
    buttons.push(edit, archive);
    notes.push(archiveNote);
  }
  const range = [plan.start_date, plan.end_date].filter(Boolean).map((day) => dateLabel(day)).join(" – ");
  return h("article", "library-card",
    h("div", null, h("h4", null, plan.name), h("span", null, PLAN_STATUS_LABELS[plan.status] || plan.status)),
    h("p", null, [range, plan.goal].filter(Boolean).join("\n")),
    h("div", "planned-activity-actions", ...buttons, remove), ...notes, status);
}

function libraryPlans(archivedView) {
  const plans = (Array.isArray(state.data?.plans) ? state.data.plans : []).filter((plan) => (plan.status === "archived") === archivedView);
  const shown = new Set(plans.map((plan) => String(plan.id)));
  const head = [];
  if (!archivedView) {
    const add = h("button", { type: "button", className: "secondary-button" }, "+ Plan");
    add.addEventListener("click", () => openPlanForm());
    head.push(h("div", "library-toolbar library-plans-head", h("strong", null, "Pläne"), add));
  }
  // A deleted plan's card is gone, so its receipt leads the plans.
  const lost = [...planReceipts].filter(([id]) => !shown.has(id)).map(([id, receipt]) => libraryReceiptLine(h("p", { className: "planned-move-status", role: "status" }), id, receipt, planReceipts));
  return [...head, ...lost, ...plans.map(planCard)];
}

function libraryToolbar() {
  const group = h("div", { className: "chip-group", role: "radiogroup", "aria-label": "Bibliothek" });
  for (const [value, label] of [["active", "Aktiv"], ["archived", "Archiviert"]]) {
    const input = h("input", { type: "radio", name: "libraryView", value, checked: libraryView === value });
    input.addEventListener("change", () => {
      libraryView = value;
      // A receipt belongs to the view the change was made in.
      planReceipts.clear();
      renderLibrary(state.data?.library || []);
      $("#library input[name=libraryView]:checked")?.focus();
    });
    group.append(h("label", "chip", input, h("span", null, label)));
  }
  const add = h("button", { type: "button", className: "secondary-button" }, "+ Einheit");
  add.addEventListener("click", () => openLibraryEditor());
  return h("div", "library-toolbar", group, add);
}

// An archived entry: "Wiederherstellen", and "Endgültig löschen" for one no provider holds. Both act at once and
// leave a receipt with "Rückgängig" (#3951); the 30-day clean-up of #3141 is unchanged.
function archivedLibraryCard(workout) {
  const id = String(workout.id);
  const name = workout.name || "Bibliotheks-Einheit";
  const restore = h("button", { type: "button", className: "secondary-button" }, "Wiederherstellen");
  const status = h("p", { className: "planned-move-status", role: "status" });
  restore.addEventListener("click", async () => {
    restore.disabled = true;
    try {
      const result = await api(`/api/library/${encodeURIComponent(id)}/restore`, { method: "POST", body: "{}" });
      libraryReceipts.clear();
      libraryReceipts.set(id, { text: `Wiederhergestellt: ${name}`, change: result.change });
      libraryOpenId = id;
      libraryView = "active";
      await globalThis.load?.();
      announcePlanChange(`Wiederhergestellt: ${name}`);
    } catch (error) {
      restore.disabled = false;
      status.textContent = error.message;
    }
  });
  const buttons = [restore];
  const notes = [status];
  if (!workout.external_id) {
    const [remove, failure] = inCardDelete(async () => {
      const result = await api(`/api/library/${encodeURIComponent(id)}/delete`, { method: "POST", body: JSON.stringify({ confirmed: true }) });
      libraryReceipts.clear();
      libraryReceipts.set(id, { text: `Endgültig gelöscht: ${name}`, change: result.change });
      await loadArchivedLibrary();
      announcePlanChange(`Endgültig gelöscht: ${name}`);
    });
    remove.textContent = "Endgültig löschen";
    buttons.push(remove);
    notes.push(failure);
  }
  return h("article", "library-card",
    h("div", null, h("h4", null, name), h("span", null, workout.planned_duration_s ? AppFormat.duration(workout.planned_duration_s) : "")),
    h("p", null, libraryDescriptionText(workout.description) || "Kein Workout-Text hinterlegt."),
    h("div", "planned-activity-actions", ...buttons), ...notes);
}

function libraryReceiptLine(status, id, receipt, receipts = libraryReceipts) {
  status.append(receipt.text, ...(receipt.change?.id ? plannedMoveUndo(id, receipt, status, receipts) : []));
  return status;
}

// A step line of the stored workout text ("- 20m 55%", "- 20m Z1 HR") in words: "20 min · 55 %", "20 min · Z1 Herzfrequenz".
const LIBRARY_STEP_UNITS = { h: "h", m: "min", s: "s", km: "km", mtr: "m" };

function libraryStepLine(line) {
  const text = line.replace(/^\s*-\s*/, "");
  // Only a line that starts with a size is a step; a prose bullet stays as written.
  if (!/^\d+(?:[.,]\d+)?(?:h|m|s|km|mtr)(?:\s|$)/.test(text)) return line;
  const words = text.split(/\s+/).filter(Boolean).map((word) => {
    const size = /^(\d+(?:[.,]\d+)?)(h|m|s|km|mtr)$/.exec(word);
    if (size) return `${size[1].replace(".", ",")} ${LIBRARY_STEP_UNITS[size[2]]}`;
    if (/^\d+(?:[.,]\d+)?%$/.test(word)) return `${word.slice(0, -1).replace(".", ",")} %`;
    return word === "HR" ? "Herzfrequenz" : word;
  });
  return words.length > 1 ? [words[0], words.slice(1).join(" ")].join(" · ") : words.join("");
}

function libraryDescriptionText(text) {
  return String(text || "").split("\n").map((line) => (/^\s*-/.test(line) ? libraryStepLine(line) : line)).join("\n");
}

// `archivedFresh`: the archived list was just read; every other render of that view reads it again.
function renderLibrary(workouts, archivedFresh = false) {
  const root = $("#library");
  if (!root) return;
  if (libraryView === "archived" && !archivedFresh) void loadArchivedLibrary();
  const openSports = new Set([...root.querySelectorAll("details.library-sport[open]")].map((section) => section.dataset.sport));
  const archivedView = libraryView === "archived";
  root.replaceChildren(libraryToolbar(), ...libraryPlans(archivedView));
  if (!archivedView) appendHistoryPageButton(root, "library");
  const allWorkouts = Array.isArray(workouts) ? workouts : [];
  const visible = archivedView ? libraryArchived : allWorkouts.filter((workout) => !workout.archived && !workout.date);
  // A deleted entry's card is gone, so its receipt leads the library.
  const shown = new Set(visible.map((workout) => String(workout.id)));
  for (const [id, receipt] of libraryReceipts) {
    if (!shown.has(id)) root.append(libraryReceiptLine(h("p", { className: "planned-move-status", role: "status" }), id, receipt));
  }
  if (archivedView) {
    root.append(...visible.map(archivedLibraryCard));
    return;
  }
  if (!visible.length) {
    root.append(h("p", "context-empty", "Noch keine Einheiten in der Bibliothek. Der Coach legt hier Einheiten ab, wenn du sie planst."));
    return;
  }
  const groups = new Map();
  visible.forEach((workout) => {
    const sport = activitySportLabel(workout);
    if (!groups.has(sport)) groups.set(sport, []);
    groups.get(sport).push(workout);
  });
  [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b, "de"))
    .forEach(([sport, sportWorkouts]) => {
      const section = h("details", { className: "library-sport", dataset: { sport } },
        h("summary", null, h("strong", null, sport), h("span", null, `${sportWorkouts.length} Einheit${sportWorkouts.length === 1 ? "" : "en"}`)),
        h("div", "library-sport-cards", sportWorkouts.map((workout) => h("article", "library-card",
          h("div", null,
            h("h4", null, workout.name || "Bibliotheks-Einheit"),
            h("span", null, workout.planned_duration_s ? AppFormat.duration(workout.planned_duration_s) : "")),
          h("p", null, libraryDescriptionText(workout.description) || "Kein Workout-Text hinterlegt."),
          libraryWahooRow(workout),
          ...libraryActions(workout)))));
      section.open = openSports.has(sport) || sportWorkouts.some((workout) => String(workout.id) === libraryOpenId);
      root.append(section);
    });
}



// Race plan of an expanded upcoming competition (#2803).
const RACE_PLAN_WEEKDAY = new Intl.DateTimeFormat("de-DE", { weekday: "short", timeZone: "UTC" });
const RACE_PLAN_KM = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
const RACE_PLAN_UNAVAILABLE = { distance: "Distanz fehlt", duration: "Zielzeit oder erwartete Dauer fehlt" };

function racePlanRange(values, unit) {
  return Array.isArray(values) ? `${values[0]}–${values[1]} ${unit}` : null;
}

function racePlanDay(eventDate, daysBack) {
  const start = Date.parse(`${String(eventDate).slice(0, 10)}T00:00:00Z`);
  return Number.isNaN(start) ? null : RACE_PLAN_WEEKDAY.format(new Date(start - daysBack * 86_400_000));
}

function racePlanDays(eventDate, count) {
  const days = Array.from({ length: count }, (_, index) => racePlanDay(eventDate, count - index));
  if (days.every(Boolean)) return days.join(" + ");
  return count === 1 ? "1 Tag davor" : `${count} Tage davor`;
}

// "+4 %", "−2 %" or the zero form; the minus is typographic.
function racePlanSigned(value, zero = "") {
  if (value > 0) return `+${RACE_PLAN_KM.format(value)} %`;
  if (value < 0) return `−${RACE_PLAN_KM.format(-value)} %`;
  return `${zero}0 %`;
}

// The meal window before the start; a window before midnight names the day before.
function racePlanMeal(eventDate, startsAtLocal, hours) {
  const time = /T(\d{2}):(\d{2})/.exec(String(startsAtLocal || ""));
  const raceDay = racePlanDay(eventDate, 0);
  if (!time || (time[1] === "00" && time[2] === "00")) return [raceDay, `${hours[0]}–${hours[1]} h vor dem Start`].filter(Boolean).join(" ");
  const start = Number(time[1]) * 60 + Number(time[2]);
  const [from, to] = hours.map((offset) => {
    const minutes = start - offset * 60;
    const value = (minutes + 1440) % 1440;
    return { day: racePlanDay(eventDate, minutes < 0 ? 1 : 0), clock: `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}` };
  });
  if (from.day === to.day) return [from.day, `${from.clock}–${to.clock}`].filter(Boolean).join(" ");
  return `${from.day} ${from.clock} – ${to.day} ${to.clock}`;
}

function racePlanDuring(during) {
  if (!during) return null;
  if (during.step === "none") return "keine nötig";
  if (during.step === "mouth_rinse") return "kleine Mengen oder Mund spülen";
  const range = `${during.low_g}–${during.high_g} g/h`;
  return during.established_max_g ? `${range} · Glukose + Fruktose · über ${during.established_max_g} g/h unbelegt` : range;
}

function racePlanHeading(text, badge) {
  return h("h5", null, text, badge ? h("span", "race-plan-source", badge) : null);
}

function racePlanPacing(pacing) {
  if (pacing.status !== "ready") return RACE_PLAN_UNAVAILABLE[pacing.reason] ? [racePlanHeading("Tempo"), h("p", "race-plan-gap", RACE_PLAN_UNAVAILABLE[pacing.reason])] : [];
  const ride = pacing.method === "power_variation";
  const badge = [ride ? "± 5 % Leistung" : "gleiche Anstrengung", pacing.profile_rejected ? "Streckenprofil nicht erkannt" : null].filter(Boolean).join(" · ");
  const columns = ride ? ["Abschnitt", "Steigung", "Leistung"] : ["Abschnitt", "Steigung", "Tempo", "Zeit"];
  const rows = pacing.sections.map((section) => {
    const grade = racePlanSigned(section.grade_pct);
    const span = `km ${RACE_PLAN_KM.format(section.from_km)}–${RACE_PLAN_KM.format(section.to_km)}`;
    const cells = ride
      ? [span, grade, section.power_w == null ? racePlanSigned(section.power_pct, "±") : `${section.power_w} W`]
      : [span, grade, `${AppFormat.clock(section.pace_s_per_km)}/km`, AppFormat.clock(section.time_s)];
    // The column name rides on each cell: a narrow row shows it above the value.
    return cells.map((value, index) => h("td", { dataset: { label: columns[index] } }, value));
  });
  return [racePlanHeading("Tempo", badge), table(columns, rows, { className: "race-plan-table" })];
}

const RACE_PLAN_WEATHER_STATUS = { pending: "Noch keine Vorhersage", not_found: "Ort nicht gefunden", unavailable: "Vorhersage nicht verfügbar" };

// Race-day forecast at the competition location; nothing without a location or before the forecast reaches race day.
function racePlanWeather(weather) {
  const day = weather?.status === "ready" ? weather.day : null;
  if (!day) {
    const text = RACE_PLAN_WEATHER_STATUS[weather?.status === "ready" ? "unavailable" : weather?.status];
    return text ? [racePlanHeading("Wetter"), h("p", "race-plan-gap", text)] : [];
  }
  const place = [weather.name, weather.country].filter(Boolean).join(", ");
  const number = (value) => RACE_PLAN_KM.format(Math.round(value));
  const facts = [
    `${day.icon || ""} ${day.condition || ""}`.trim(),
    day.temperature_min != null && day.temperature_max != null ? `${number(day.temperature_min)}–${number(day.temperature_max)} °C` : null,
    day.wind_speed_max == null ? null : `Wind ${number(day.wind_speed_max)} km/h`,
    day.precipitation_probability_max == null ? null : `Regen ${number(day.precipitation_probability_max)} %`,
  ].filter(Boolean);
  return [racePlanHeading("Wetter", ["Open-Meteo", place].filter(Boolean).join(" · ")), h("p", "race-plan-weather", facts.join(" · "))];
}

function racePlanCarbohydrates(carbohydrates, competition) {
  const weight = carbohydrates.weight?.source ? `Gewicht ${carbohydrates.weight.source}` : null;
  const rows = carbohydrates.before.map((entry) => entry.kind === "meal"
    ? [racePlanMeal(competition.event_date, competition.starts_at_local, entry.hours_before), racePlanRange(entry.grams, "g") || racePlanRange(entry.g_per_kg, "g/kg")]
    : [racePlanDays(competition.event_date, entry.days_before), racePlanRange(entry.grams, "g/Tag") || racePlanRange(entry.g_per_kg, "g/kg/Tag")]);
  const during = racePlanDuring(carbohydrates.during);
  rows.push(["Im Rennen", during || RACE_PLAN_UNAVAILABLE.duration]);
  return [
    racePlanHeading("Kohlenhydrate", [carbohydrates.source, weight].filter(Boolean).join(" · ")),
    h("div", "race-plan-rows", rows.map(([label, value]) => h("div", null, h("span", null, label), h("strong", null, value)))),
  ];
}

// Read on every opening, so the plan follows weight and competition changes; nothing is kept in memory.
function loadRacePlan(section, competition) {
  if (section.dataset.state === "loading") return;
  section.dataset.state = "loading";
  const status = h("output", null, "Rennplan wird berechnet …");
  section.replaceChildren(h("h4", null, "Rennplan"), status);
  api(`/api/competitions/${encodeURIComponent(competition.id)}/race-plan`, { timeoutMs: DATA_LOAD_TIMEOUT_MS }).then((payload) => {
    const plan = payload.race_plan;
    section.dataset.state = "ready";
    section.replaceChildren(h("h4", null, "Rennplan"), ...racePlanPacing(plan.pacing), ...racePlanCarbohydrates(plan.carbohydrates, competition), ...racePlanWeather(plan.weather));
  }).catch(() => {
    section.dataset.state = "failed";
    const retry = h("button", { type: "button", className: "secondary-button" }, "Erneut laden");
    retry.addEventListener("click", () => loadRacePlan(section, competition));
    status.textContent = "Rennplan konnte nicht geladen werden.";
    section.append(retry);
  });
}

function competitionCard(competition, todayKey, openIds) {
  const sport = competition.sport_detail || competitionSportLabel(competition.sport, competition.environment);
  const when = AppFormat.relativeDay(competition.event_date, todayKey);
  const edit = messageActionButton("edit", "Wettkampf bearbeiten");
  edit.addEventListener("click", (event) => {
    // The pencil sits in the summary: it must not open or close the card.
    event.preventDefault();
    event.stopPropagation();
    openCompetitionDialog(competition);
  });
  const summary = h("summary", null,
    h("span", "competition-summary-main",
      h("strong", null, `${competitionDate(competition, todayKey) ? dateLabel(competition.event_date) : "Datum unbekannt"} · ${competition.name || "Wettkampf"}`),
      h("span", "competition-summary-facts", [sport, competitionDistance(competition.distance), `Priorität ${competition.priority || "B"}`, when].filter(Boolean).join(" · "))),
    edit);
  const source = h("small", "competition-sync-state", competition.external_id ? "Intervals.icu" : "Nicht an Intervals.icu gesendet");
  if (competition.sync_state === "local_override") source.textContent = "Lokal geändert";
  // A conflict names itself: the local and the Intervals.icu entry differ and nothing is overwritten (#3932 K-15).
  if (competition.sync_state === "conflict") source.textContent = "Konflikt mit Intervals.icu: lokal und dort unterscheiden sich";
  const optional = [
    ["Ergebnis", competitionResult(competition.result)],
    // Midnight is no start time, as in the dialog; a goal is shown as a time only when it is one.
    ["Startzeit", plannedUnitStartTime(competition.starts_at_local) ? formatLocalCompetitionTime(competition.starts_at_local) : null],
    ["Ziel", competitionGoal(competition.target)],
    ["Erwartete Dauer", competition.planned_duration_s == null ? null : AppFormat.duration(competition.planned_duration_s)],
    ["Ort", competition.location],
    ["Streckenprofil", competition.course_profile],
    ["Beschreibung", competition.description],
    ["Notizen", competition.notes],
  ];
  const facts = optional.filter(([, value]) => value != null && String(value).trim() !== "").map(([label, value]) => competitionFact(label, value));
  const upcoming = competitionDate(competition, todayKey) && competition.event_date >= todayKey;
  const plan = upcoming ? h("section", "race-plan") : null;
  const card = h("details", { className: "competition-card", dataset: { competitionId: String(competition.id) }, open: openIds.has(String(competition.id)) },
    summary, source, facts.length ? h("div", "competition-card-facts", facts) : null, plan);
  if (plan) {
    card.addEventListener("toggle", () => { if (card.open) loadRacePlan(plan, competition); });
    if (card.open) loadRacePlan(plan, competition);
  }
  return card;
}

function renderCompetitions(competitions) {
  const root = $("#competitionList");
  if (!root || !Array.isArray(competitions)) return;
  const todayKey = timezoneDateKey(state.data?.profile?.timezone, new Date());
  const snapshot = JSON.stringify([competitions, todayKey]);
  if (root.dataset.snapshot === snapshot) return;
  const openIds = new Set([...root.querySelectorAll("details[data-competition-id][open]")].map((node) => node.dataset.competitionId));
  const focusedId = document.activeElement?.closest("details[data-competition-id]")?.dataset.competitionId;
  const archiveOpen = root.querySelector(".competition-archive")?.open || false;
  root.dataset.snapshot = snapshot;
  root.replaceChildren();
  if (!competitions.length) {
    root.append(h("p", "context-empty", "Noch keine Wettkämpfe gespeichert."));
    return;
  }
  const ordered = [...competitions].sort((a, b) =>
    String(a.event_date || "").localeCompare(String(b.event_date || ""))
    || String(a.priority || "B").localeCompare(String(b.priority || "B"))
    || String(a.name || "").localeCompare(String(b.name || ""), "de")
    || String(a.id).localeCompare(String(b.id)));
  const past = [], undated = [];
  for (const competition of ordered) {
    if (!competitionDate(competition, todayKey)) undated.push(competition);
    else if (competition.event_date < todayKey) past.push(competition);
    else root.append(competitionCard(competition, todayKey, openIds));
  }
  if (undated.length) {
    root.append(h("h4", null, "Ohne gültiges Datum"), ...undated.map((event) => competitionCard(event, todayKey, openIds)));
  }
  if (past.length) {
    const archive = h("details", { className: "competition-archive", open: archiveOpen }, h("summary", null, `Vergangene Wettkämpfe (${past.length})`));
    past.sort((a, b) => b.event_date.localeCompare(a.event_date) || ordered.indexOf(a) - ordered.indexOf(b));
    archive.append(...past.map((event) => competitionCard(event, todayKey, openIds)));
    root.append(archive);
  }
  if (focusedId) [...root.querySelectorAll("details[data-competition-id]")].find((node) => node.dataset.competitionId === focusedId)?.querySelector("summary")?.focus();
}

function renderCompetitionReadStatus(status) {
  const node = $("#competitionReadStatus");
  const retry = $("#competitionRetry");
  if (!node || !retry) return;
  const hasData = Array.isArray(state.data?.competitions);
  let message = "";
  if (status === "error") {
    message = hasData ? "Gespeicherter Stand. Aktualisierung fehlgeschlagen." : "Wettkämpfe konnten nicht geladen werden.";
  } else if (status === "loading") {
    message = hasData ? "Gespeicherter Stand wird aktualisiert …" : "Wettkämpfe werden geladen …";
  }
  node.textContent = message;
  retry.hidden = status !== "error";
}

// "Unter 45 Minuten" and "3:30" stay as written; only "h:mm:ss" reads as a duration.
function competitionGoal(value) {
  const match = /^\s*(\d{1,2}):(\d{2}):(\d{2})\s*$/.exec(String(value ?? ""));
  if (!match) return value;
  return AppFormat.duration((Number(match[1]) * 60 + Number(match[2])) * 60 + Number(match[3]));
}

function formatLocalCompetitionTime(value) {
  const raw = String(value || "");
  const match = /(?:T|\s)(\d{2}:\d{2})(?::\d{2})?/.exec(raw);
  return match ? match[1] : formatTime(value);
}

// The athlete's own competition entry (#3837): one dialog adds, edits and deletes a competition.
const COMPETITION_SPORT_OPTIONS = { ride: "Ride", run: "Run", strength: "WeightTraining" };
let competitionEdit = null;
let competitionDialogBound = false;

// "h:mm:ss" or "mm:ss" as seconds; empty is null, anything else undefined.
function competitionDurationSeconds(text) {
  const raw = String(text || "").trim();
  if (!raw) return null;
  const parts = raw.split(":");
  if (parts.length < 2 || parts.length > 3 || !parts.every((part) => /^\d+$/.test(part)) || parts.slice(1).some((part) => Number(part) >= 60)) return undefined;
  return parts.map(Number).reduce((total, part) => total * 60 + part, 0);
}

// The stored distance is metres; the dialog reads and writes kilometres.
function competitionDistanceInput(value) {
  if (value == null || String(value).trim() === "") return "";
  const metres = Number(value);
  if (!Number.isFinite(metres)) return String(value);
  return metres < 0 ? "" : String(metres / 1000).replace(".", ",");
}

function competitionDistanceValue(text) {
  const raw = String(text || "").trim();
  return /^[\d.,\s]+$/.test(raw) ? `${raw} km` : raw;
}

function competitionSportChoice(competition) {
  if (competition.sport === "ride" && ["virtual", "indoor"].includes(competition.environment)) return "VirtualRide";
  return COMPETITION_SPORT_OPTIONS[competition.sport] || null;
}

function fillCompetitionDialog(competition) {
  const form = $("#competitionDialogForm");
  const stored = competition || {};
  form.querySelector("option[data-kept]")?.remove();
  let sport = competitionSportChoice(stored);
  if (competition && !sport) {
    // A sport the list does not offer stays as stored unless the athlete picks another.
    form.elements.sport.append(h("option", { value: "keep", dataset: { kept: "" } }, stored.sport_detail || competitionSportLabel(stored.sport, stored.environment)));
    sport = "keep";
  }
  sport ||= "Ride";
  // The distance shown is km text; an untouched field is not sent back, so no rounding rewrites it.
  const distance = competitionDistanceInput(stored.distance);
  competitionEdit = competition ? { competition, sport, distance } : null;
  const time = stored.starts_at_local ? formatLocalCompetitionTime(stored.starts_at_local) : "";
  form.elements.name.value = stored.name || "";
  form.elements.event_date.value = String(stored.event_date || "").slice(0, 10);
  form.elements.start_time.value = time === "00:00" ? "" : time;
  form.elements.sport.value = sport;
  form.elements.priority.value = stored.priority || "B";
  form.elements.distance.value = distance;
  form.elements.target.value = stored.target || "";
  form.elements.duration.value = stored.planned_duration_s == null ? "" : AppFormat.clock(stored.planned_duration_s);
  form.elements.location.value = stored.location || "";
  form.elements.course_profile.value = stored.course_profile || "";
  form.elements.notes.value = stored.notes || "";
  const result = competitionResult(stored.result);
  $("#competitionDialogResult").hidden = !result;
  $("#competitionDialogResult").replaceChildren(...(result ? [competitionFact("Ergebnis", result)] : []));
  $("#competitionDialogRemove").hidden = !competition;
  $("#competitionDialogStatus").textContent = "";
  $("#competitionDialogDurationStatus").textContent = "";
  for (const button of $("#competitionDialog").querySelectorAll("button")) button.disabled = false;
}

// The receipt is one line above the competition list, with "Rückgängig" while the change can be taken back (#3934 G-04).
function showCompetitionReceipt(text, change) {
  const line = $("#competitionReceipt");
  if (!line) return;
  line.hidden = false;
  line.replaceChildren(`${text} `);
  if (!change?.id) return;
  const undo = h("button", { type: "button", className: "message-undo" }, "Rückgängig");
  undo.addEventListener("click", async () => {
    undo.disabled = true;
    try {
      await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: change.id, expected_current_hash: change.after_hash }) });
      await load("/api/bootstrap?local=1", ["profile"]);
      line.replaceChildren("Zurückgenommen.");
    } catch (error) {
      undo.disabled = false;
      line.replaceChildren(`${error.message} `, undo);
    }
  });
  line.append(undo);
}

async function sendCompetition(path, payload, button, status, receipt) {
  button.disabled = true;
  let result;
  try {
    result = await api(path, { method: "PUT", body: JSON.stringify(payload) });
  } catch (error) {
    if (!isSessionEndedError(error)) status.textContent = error.message;
    button.disabled = false;
    return;
  }
  $("#competitionDialog").close();
  await load("/api/bootstrap?local=1", ["profile"]);
  showCompetitionReceipt(receipt(result), result.change);
}

function competitionDialogPayload(form) {
  const duration = competitionDurationSeconds(form.elements.duration.value);
  if (duration === undefined) return null;
  const day = form.elements.event_date.value;
  const time = form.elements.start_time.value;
  const payload = {
    name: form.elements.name.value,
    event_date: day,
    starts_at_local: time ? `${day}T${time}` : "",
    priority: form.elements.priority.value,
    target: form.elements.target.value,
    planned_duration_s: duration,
    location: form.elements.location.value,
    course_profile: form.elements.course_profile.value,
    notes: form.elements.notes.value,
  };
  if (!competitionEdit || form.elements.distance.value !== competitionEdit.distance) payload.distance = competitionDistanceValue(form.elements.distance.value);
  if (!competitionEdit || form.elements.sport.value !== competitionEdit.sport) payload.sport = form.elements.sport.value;
  if (competitionEdit) payload.id = competitionEdit.competition.id;
  return payload;
}

function bindCompetitionDialog() {
  const form = $("#competitionDialogForm");
  if (competitionDialogBound || !form) return;
  competitionDialogBound = true;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const status = $("#competitionDialogStatus");
    const payload = competitionDialogPayload(form);
    if (!payload) {
      // The message sits under the field it is about, with the focus on it.
      $("#competitionDialogDurationStatus").textContent = "Erwartete Dauer als h:mm:ss oder mm:ss angeben.";
      form.elements.duration.focus();
      return;
    }
    void sendCompetition("/api/competitions", payload, $("#competitionDialogSave"), status, () => "Wettkampf gespeichert.");
  });
  $("#competitionDialogCancel").addEventListener("click", () => $("#competitionDialog").close());
  // "Löschen" acts at once and the receipt offers the undo (athlete decision, #3934 G-03).
  $("#competitionDialogRemove").addEventListener("click", () => {
    void sendCompetition("/api/competitions/remove", { id: competitionEdit.competition.id, confirmed: true }, $("#competitionDialogRemove"), $("#competitionDialogStatus"),
      (result) => (result.remote_sync_pending ? "Wettkampf gelöscht. Bei Intervals.icu verschwindet er mit dem nächsten Sync." : "Wettkampf gelöscht."));
  });
}

function openCompetitionDialog(competition = null) {
  bindCompetitionDialog();
  fillCompetitionDialog(competition);
  $("#competitionDialog").showModal();
}


function bindPlanNavigation() {
  document.querySelectorAll("[data-plan-segment]").forEach((link) => link.addEventListener("click", (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    void AppRouter.navigate(`plan/${link.dataset.planSegment}`, { historyMode: "push" });
  }));
}
function bindExternalCalendar(syncExternalCalendar) {
  $("#externalCalendarSyncButton").addEventListener("click", syncExternalCalendar);
}
