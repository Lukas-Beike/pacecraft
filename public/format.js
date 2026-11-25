(function initAppFormat() {
  const LOCALE = "de-DE";
  const DAY_MS = 24 * 60 * 60 * 1000;
  const DATE_KEY = /^(\d{4})-(\d{2})-(\d{2})$/;

  // Returns the UTC midnight timestamp for a valid YYYY-MM-DD key, otherwise null.
  function dateKeyUtc(value) {
    const match = DATE_KEY.exec(String(value ?? ""));
    if (!match) return null;
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const timestamp = Date.UTC(year, month - 1, day);
    const check = new Date(timestamp);
    if (check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day) return null;
    return timestamp;
  }

  function toDate(value) {
    if (value == null || value === "") return null;
    // A server timestamp may carry microseconds or a space for the T; not every engine reads those.
    const text = typeof value === "string" ? value.replace(/^(\d{4}-\d{2}-\d{2}) /, "$1T").replace(/(\.\d{3})\d+/, "$1") : value;
    const parsed = value instanceof Date ? value : new Date(text);
    return Number.isNaN(parsed.valueOf()) ? null : parsed;
  }

  // Formats in the requested zone; an invalid or missing zone falls back to the browser zone.
  function formatDateTime(options, date, timeZone) {
    if (timeZone) {
      try {
        return new Intl.DateTimeFormat(LOCALE, { ...options, timeZone }).format(date);
      } catch (_) {
        // Invalid zone names are ignored; the browser zone is used below.
      }
    }
    return new Intl.DateTimeFormat(LOCALE, options).format(date);
  }

  // `minDigits` keeps trailing zeros, such as a training effect of 0,0. A negative value
  // takes the minus sign "−"; a value that rounds to zero carries no sign (#3933).
  function number(value, { digits = 1, minDigits = 0 } = {}) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return null;
    const text = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: minDigits, maximumFractionDigits: Math.max(digits, minDigits), signDisplay: "negative" }).format(numeric);
    return text.replace("-", "−");
  }

  // A percentage with a space before the sign, as in "42,5 %".
  function percent(value, { digits = 1 } = {}) {
    const text = number(value, { digits });
    return text == null ? null : `${text} %`;
  }

  // A change with an explicit sign: "+1,2", "−0,4", "0" (never a signed zero).
  function signed(value, { digits = 1, minDigits = 0 } = {}) {
    const text = number(value, { digits, minDigits });
    if (text == null) return null;
    return Number(value) > 0 && text !== "0" && /[1-9]/.test(text) ? `+${text}` : text;
  }

  // "1 Einheit" / "2 Einheiten"; a fraction such as 0,5 takes the plural.
  function plural(count, one, many) {
    const numeric = Number(count);
    const value = Number.isFinite(numeric) ? numeric : 0;
    return `${number(value, { digits: 1 })} ${value === 1 ? one : many}`;
  }

  // Speed in km/h with one decimal, "32,0 km/h".
  function speed(kmh) {
    const text = number(kmh, { digits: 1, minDigits: 1 });
    return text == null ? null : `${text} km/h`;
  }

  function distance(meters) {
    if (meters == null || meters === "") return null;
    const value = Number(meters);
    if (!Number.isFinite(value) || value <= 0) return null;
    const km = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(value / 1000);
    return `${km} km`;
  }

  // A duration of a session or a total in words (#4194): "45 min", "1 h 5 min", "2 h".
  function duration(seconds) {
    if (seconds == null || seconds === "") return null;
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return null;
    const totalMinutes = Math.round(value / 60);
    if (totalMinutes < 60) return `${totalMinutes} min`;
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  }

  // Stopwatch-style clock for pace and lap values: "h:mm:ss" or "m:ss".
  function clock(seconds) {
    if (seconds == null || Number.isNaN(Number(seconds))) return null;
    const total = Math.round(Number(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = total % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
      : `${minutes}:${String(remainder).padStart(2, "0")}`;
  }

  // Running pace per kilometre, "4:34 /km".
  function pace(seconds) {
    const text = clock(seconds);
    return text == null ? null : `${text} /km`;
  }

  function date(value, { timeZone } = {}) {
    const dayStart = dateKeyUtc(value);
    if (dayStart != null) return formatDateTime({ dateStyle: "medium", timeZone: "UTC" }, new Date(dayStart));
    const parsed = toDate(value);
    return parsed ? formatDateTime({ dateStyle: "medium" }, parsed, timeZone) : null;
  }

  function dateTime(value, { timeZone } = {}) {
    const parsed = toDate(value);
    return parsed ? formatDateTime({ dateStyle: "medium", timeStyle: "short" }, parsed, timeZone) : null;
  }

  function relativeDay(dateKey, todayKey) {
    const target = dateKeyUtc(dateKey);
    const today = dateKeyUtc(todayKey);
    if (target == null || today == null) return null;
    const days = Math.round((target - today) / DAY_MS);
    if (days === 0) return "Heute";
    if (days === 1) return "Morgen";
    if (days === -1) return "Gestern";
    return days > 0 ? `in ${days} Tagen` : `vor ${-days} Tagen`;
  }

  // German labels of the canonical exercise categories (ExerciseCategory).
  const STRENGTH_CATEGORIES = Object.freeze({ bench_press: "Bankdrücken", calf_raise: "Wadenheben", cardio: "Cardio", carry: "Tragen", chop: "Holzhacker", core: "Rumpf", crunch: "Crunch", curl: "Bizepscurl", deadlift: "Kreuzheben", flye: "Fliegende", hip_raise: "Hüftheben", hip_stability: "Hüftstabilität", hip_swing: "Kettlebell-Swing", hyperextension: "Rückenstrecker", lateral_raise: "Seitheben", leg_curl: "Beinbeuger", leg_raise: "Beinheben", lunge: "Ausfallschritt", olympic_lift: "Olympisches Heben", plank: "Unterarmstütz", plyo: "Sprünge", pull_up: "Klimmzug", push_up: "Liegestütz", row: "Rudern", shoulder_press: "Schulterdrücken", shoulder_stability: "Schulterstabilität", shrug: "Schulterheben", sit_up: "Sit-up", squat: "Kniebeuge", total_body: "Ganzkörper", triceps_extension: "Trizepsstrecken", warm_up: "Aufwärmen", run: "Laufen", other: "Unbekannte Übung" });

  function strengthCategory(category) {
    return typeof category === "string" ? STRENGTH_CATEGORIES[category] || null : null;
  }

  // The duration a completed session is shown and summed with (#2317): strength leads with the
  // total time, because rest between sets is part of the work; other sports with the moving time.
  // Only a positive time counts, as on the server; otherwise the other one stands in.
  function sessionSeconds(activity, sport = activity?.sport) {
    const times = [activity?.moving_duration_s ?? activity?.duration_s, activity?.elapsed_duration_s];
    if (sport === "strength") times.reverse();
    return times.find((value) => Number(value) > 0);
  }

  const AppFormat = Object.freeze({ number, percent, signed, plural, speed, pace, distance, duration, clock, date, dateTime, relativeDay, strengthCategory, sessionSeconds, STRENGTH_CATEGORIES });
  globalThis.AppFormat = AppFormat;
  if (typeof module !== "undefined") module.exports = AppFormat;
})();
