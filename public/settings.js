// Profile values as the form presents them; sports is the comma-joined multi-select.
let profileSaveInFlight = false;
function profileFormValues(form) {
  const values = Object.fromEntries(new FormData(form));
  values.sports = new FormData(form).getAll("sports").filter((value) => typeof value === "string").map((value) => value.trim()).filter(Boolean).join(", ");
  return values;
}

// Entry-only fields: the server turns them into first_threshold_pct and never echoes them.
const PROFILE_ENTRY_FIELDS = ["first_threshold_hr", "first_threshold_on"];

function profileSaveConfirmed(changed, saved) {
  // An entered bpm value must come back as the stored percent.
  if (profileFieldText(changed.first_threshold_hr) && typeof saved.first_threshold_pct !== "string") return false;
  return Object.keys(changed)
    .filter((key) => !PROFILE_ENTRY_FIELDS.includes(key))
    .every((key) => Object.hasOwn(saved, key) && typeof saved[key] === "string");
}

function clearProfileEntryFields(form) {
  for (const key of PROFILE_ENTRY_FIELDS) {
    const field = form.elements?.[key];
    if (field) field.value = "";
  }
}

// After a confirmed save: clear the entry and show the stored percent.
function resetProfileEntryFields(form, savedPercent) {
  clearProfileEntryFields(form);
  const field = form.elements?.first_threshold_pct;
  if (savedPercent !== undefined && field) field.value = savedPercent;
}

function profileFieldText(value) {
  return typeof value === "string" ? value : "";
}

// The adopted value reaches the open form even while it holds unsaved edits: only the
// stored-percent input and the estimate block change, every other edit stays.
function applyAdoptedFirstThreshold(result) {
  const form = $("#profileForm");
  const stored = profileFieldText(result.profile?.first_threshold_pct);
  if (form?.elements.first_threshold_pct) form.elements.first_threshold_pct.value = stored;
  if (state.profileBaseline) state.profileBaseline = { ...state.profileBaseline, first_threshold_pct: stored };
  if (state.data) state.data = { ...state.data, profile: { ...state.data.profile, ...result.profile }, first_threshold_estimate: result.first_threshold_estimate };
  renderFirstThresholdEstimate(result.first_threshold_estimate, result.profile);
  if (state.profileDirty && form && state.profileBaseline) {
    state.profileDirty = Object.keys(determineChangedProfileFields(profileFormValues(form), state.profileBaseline)).length > 0;
    setDirtyIndicator("profileDirtyIndicator", state.profileDirty);
  }
}

async function adoptFirstThreshold(event) {
  const button = event.currentTarget;
  const percent = Number($("#firstThresholdEstimate").dataset.percent);
  if (!Number.isFinite(percent) || button.disabled) return;
  const profile = state.data?.profile || {};
  const replacesMeasured = Boolean(profile.first_threshold_pct) && profile.first_threshold_origin === "measured";
  if (replacesMeasured && !(await requestConfirmation("Der gemessene Wert wird durch den Wert aus DFA-α1 ersetzt.", { title: "Gemessenen Wert ersetzen?", confirmLabel: "Ersetzen" }))) return;
  button.disabled = true;
  try {
    const result = await api("/api/profile/first-threshold/adopt", { method: "POST", body: JSON.stringify({ percent_lthr: percent, ...(replacesMeasured ? { replace_measured: true } : {}) }) });
    applyAdoptedFirstThreshold(result);
    invalidateContextPreview();
    toast(result.adopted === false ? "Wert aus DFA-α1 ist bereits übernommen" : "Erste Schwelle übernommen");
    await load();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function determineChangedProfileFields(formValues, baseline) {
  if (!baseline) {
    throw new Error("Das Profil wird noch geladen. Bitte kurz warten und erneut speichern.");
  }
  return Object.fromEntries(
    Object.entries(formValues).filter(([key, value]) => profileFieldText(value) !== profileFieldText(baseline[key]))
  );
}

async function saveProfile(event) {
  event.preventDefault();
  if (profileSaveInFlight) return;
  profileSaveInFlight = true;
  // The zone editor's save button sits in the same section list but submits its own form.
  const button = event.submitter || event.currentTarget.querySelector(".save-bar button[type=submit]");
  const buttonLabel = button?.textContent || "Profil speichern";
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Profil wird gespeichert…";
  }
  const form = event.currentTarget;
  const generation = state.sessionGeneration;
  const submittedForm = JSON.stringify([...new FormData(form)]);
  try {
    const changed = determineChangedProfileFields(profileFormValues(form), state.profileBaseline);
    const saved = await api("/api/profile", { method: "PUT", body: JSON.stringify(changed) });
    if (!profileSaveConfirmed(changed, saved)) {
      throw new Error("Die Profilbestätigung ist unvollständig. Der Entwurf bleibt erhalten.");
    }
    const entryReset = Object.fromEntries(PROFILE_ENTRY_FIELDS.map((key) => [key, ""]));
    const savedPercent = typeof saved.first_threshold_pct === "string" ? { first_threshold_pct: saved.first_threshold_pct } : {};
    state.profileBaseline = { ...state.profileBaseline, ...changed, ...entryReset, ...savedPercent };
    if (generation !== state.sessionGeneration) return;
    state.profileDirty = JSON.stringify([...new FormData(form)]) !== submittedForm;
    if (!state.profileDirty) resetProfileEntryFields(form, savedPercent.first_threshold_pct);
    setDirtyIndicator("profileDirtyIndicator", state.profileDirty);
    invalidateContextPreview();
    // The birth year and the sex decide the skinfold body fat and sites (#3099).
    if (Object.hasOwn(changed, "birth_year") || Object.hasOwn(changed, "sex")) resetSkinfolds();
    toast("Profil gespeichert");
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = buttonLabel;
    }
    await load();
  } catch (error) {
    toast(error.message, true);
  } finally {
    profileSaveInFlight = false;
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = buttonLabel;
    }
  }
}

// Rejects when the backup fails so callers can report the error and keep their own state consistent.
async function downloadDatabaseBackup() {
  const button = $("#backupDownloadButton");
  if (button) button.disabled = true;
  try {
    const blob = await downloadRequest(
      "/api/privacy/backup",
      "Datenbank-Backup konnte nicht erstellt werden.",
      130_000,
    );
    const url = URL.createObjectURL(blob);
    h("a", { href: url, download: `pacecraft-database-${todayIso()}.backup` }).click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Verschlüsseltes Backup heruntergeladen");
  } finally { if (button) button.disabled = false; }
}

async function restoreDatabaseBackup() {
  const input = $("#backupFileInput");
  const file = input?.files?.[0];
  if (!file || !await requestConfirmation("Das aktuelle Datenbank-Backup wird vorher gesichert und durch die ausgewählte Datei ersetzt.", { title: "Datenbank-Backup wiederherstellen?", confirmLabel: "Wiederherstellen" })) return;
  const button = $("#backupRestoreButton");
  button.disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 130_000);
  try {
    const generation = state.sessionGeneration;
    await globalThis.AppApi.request("/api/privacy/restore", {
      method: "POST", cache: "no-store", signal: controller.signal, timeoutMs: 130_000,
      headers: { "Content-Type": "application/octet-stream" }, body: file,
    }, () => { if (generation === state.sessionGeneration) showLogin(); });
    toast("Backup wiederhergestellt. Bitte erneut anmelden.");
    invalidateProviderChecks();
    showLogin();
  } catch (error) {
    toast(error?.name === "AbortError"
      ? "Die Bestätigung der Wiederherstellung fehlt. Der Server kann den Vorgang noch abschließen; bitte vor einem erneuten Versuch den Status prüfen."
      : error.message, true);
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
  }
}

// One select that saves on change: PUT /api/settings/<name> with the chosen value, then names the active option.
function saveSelectSetting({ currentTarget: select }, name, field, label) {
  return runAction(select, async () => {
    const task = select.dataset?.task ? { task: select.dataset.task } : {};
    await api(`/api/settings/${name}`, { method: "PUT", body: JSON.stringify({ [field]: select.value, ...task }) });
    return `${label}: ${select.options[select.selectedIndex].text}`;
  });
}

// Export and deletion run up to minutes: a second click starts nothing, and no deletion runs
// during an export. aria-disabled keeps the focused button focusable.
const privacyInFlight = { export: false, delete: false };
function setPrivacyButtonBusy(button, busy) {
  if (!button) return;
  for (const name of ["aria-busy", "aria-disabled"]) {
    if (busy) button.setAttribute(name, "true");
    else button.removeAttribute(name);
  }
}

async function downloadPrivacyExport() {
  if (privacyInFlight.export) return;
  const button = $("#privacyExportButton");
  privacyInFlight.export = true;
  setPrivacyButtonBusy(button, true);
  try {
    const blob = await downloadRequest(
      "/api/privacy/export",
      "Privacy-Export konnte nicht erstellt werden.",
      130_000,
    );
    const url = URL.createObjectURL(blob);
    h("a", { href: url, download: `pacecraft-export-${todayIso()}.zip` }).click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Datenexport erstellt");
  } catch (error) { toast(error.message, true); } finally {
    privacyInFlight.export = false;
    setPrivacyButtonBusy(button, false);
  }
}

let gpsTracksState = { confirmation_text: "" };

function renderGpsTracks(gps) {
  gpsTracksState = gps;
  $("#gpsTracksToggle").checked = Boolean(gps.enabled);
  const button = $("#gpsTracksDeleteButton");
  button.hidden = gps.enabled || !gps.stored_tracks;
  button.textContent = `GPS-Spuren löschen (${gps.stored_tracks || 0})`;
}

// #2418: the switch takes clicks only once the latest load showed the stored
// state and no save is pending; a save renders its own confirmed result.
let gpsTracksLoad = 0;
let gpsTracksSaving = false;

async function loadGpsTracks() {
  if (gpsTracksSaving) return;
  const load = ++gpsTracksLoad;
  const toggle = $("#gpsTracksToggle");
  toggle.disabled = true;
  try {
    const gps = await api("/api/privacy/gps");
    if (load !== gpsTracksLoad) return;
    renderGpsTracks(gps);
    toggle.disabled = false;
  } catch (error) {
    if (load === gpsTracksLoad) toast(error.message, true);
  }
}

async function saveGpsTracks(event) {
  const toggle = event.currentTarget;
  gpsTracksSaving = true;
  toggle.disabled = true;
  try {
    renderGpsTracks(await api("/api/privacy/gps", { method: "POST", body: JSON.stringify({ enabled: toggle.checked }) }));
  } catch (error) {
    toggle.checked = Boolean(gpsTracksState.enabled);
    toast(error.message, true);
  } finally {
    gpsTracksSaving = false;
    toggle.disabled = false;
  }
}

async function deleteGpsTracks() {
  try {
    const expectedText = gpsTracksState.confirmation_text;
    if (!expectedText) throw new Error("Die Löschbestätigung ist derzeit nicht verfügbar.");
    const confirmation = await requestConfirmation(
      `Alle gespeicherten GPS-Spuren (${gpsTracksState.stored_tracks}) werden unwiderruflich lokal gelöscht. Streckenerkennung und Wetter am Ort nutzen sie nicht mehr. Die übrigen Aufzeichnungen bleiben.`,
      { title: "GPS-Spuren löschen?", inputLabel: "Bestätigungstext", expectedText, confirmLabel: "Löschen" },
    );
    if (!confirmation) return;
    const result = await api("/api/privacy/gps/delete", { method: "POST", body: JSON.stringify({ confirm: confirmation }) });
    renderGpsTracks(result);
    toast("GPS-Spuren gelöscht");
  } catch (error) { toast(error.message, true); }
}

// Files beside the database that the erasure could not remove.
function privacyDeleteLeftovers(result) {
  const copies = result.recovery_copies_failed
    ? ` ${result.recovery_copies_failed} Wiederherstellungskopie(n) im Datenordner konnten nicht gelöscht werden.`
    : "";
  const backups = result.automatic_backups_failed
    ? ` ${result.automatic_backups_failed} automatische Sicherung(en) konnten nicht gelöscht werden.`
    : "";
  const logs = result.log_files_failed ? " Die Server-Protokolldateien konnten nicht gelöscht werden." : "";
  return copies + backups + logs;
}

async function deletePrivacyData() {
  if (privacyInFlight.delete) return;
  if (privacyInFlight.export) { toast("Der Datenexport läuft noch.", true); return; }
  const button = $("#privacyDeleteButton");
  privacyInFlight.delete = true;
  setPrivacyButtonBusy(button, true);
  const release = () => { privacyInFlight.delete = false; setPrivacyButtonBusy(button, false); };
  try {
    const preview = await api("/api/privacy/delete/preview");
    const categories = (preview.categories || []).map((category) => `${category.label}: ${category.records || 0}`).join("\n");
    const scope = `Unwiderruflich lokal gelöscht werden:\n${categories}\n\n` +
      `${(preview.remote_untouched || []).join("\n")}\n\n` +
      `${preview.openai_conversation || "Eine vorhandene OpenAI-Konversation wird separat behandelt."}\n\n` +
      "Erstelle bei Bedarf vorher ein verschlüsseltes Backup oder einen Export. Dieser Schritt kann nicht rückgängig gemacht werden.";
    // The backend preview is the single source of the required text; fail closed without it.
    if (!preview.confirmation_text) throw new Error("Die Löschbestätigung ist derzeit nicht verfügbar.");
    const confirmation = await requestConfirmation(scope, {
      title: "Lokale Daten endgültig löschen?",
      inputLabel: "Bestätigungstext",
      expectedText: preview.confirmation_text,
      confirmLabel: "Endgültig löschen",
      secondaryAction: { label: "Erst Backup erstellen", onClick: downloadDatabaseBackup },
    });
    if (!confirmation) return;
    const result = await api("/api/privacy/delete", { method: "POST", body: JSON.stringify({ confirm: confirmation }) });
    release();
    const notice = $("#privacyDeleteNotice");
    if (notice) {
      const problems = [];
      if (result.remote_delete_attempted && !result.remote_conversation_deleted) problems.push("die OpenAI-Konversation konnte remote nicht bestätigt gelöscht werden");
      if (result.wahoo_revoke_attempted && !result.wahoo_revoked) problems.push("der Wahoo-Zugriff konnte bei Wahoo nicht widerrufen werden (in den Wahoo-Kontoeinstellungen entfernen)");
      notice.hidden = !problems.length;
      notice.textContent = problems.length
        ? `Lokale Daten wurden gelöscht, aber ${problems.join(" und ")}. Prüfe den Anbieterstatus separat.`
        : "";
    }
    const resultNotice = $("#privacyDeleteResult");
    if (resultNotice) {
      const deleted = Object.entries(result.deleted_categories || {}).map(([category, count]) => `${category}: ${count}`).join(" · ");
      resultNotice.hidden = false;
      resultNotice.textContent = `Lokale Datenklassen gelöscht: ${deleted || "keine"}. Remote-Providerdaten bleiben unverändert.${privacyDeleteLeftovers(result)}`;
    }
    toast("Lokale Daten gelöscht");
    // Deleted records must neither stay on screen nor reach the session snapshot.
    clearStateSnapshot();
    void clearOutbox();
    rememberUnsentDraft(null);
    invalidateProviderChecks();
    void loadProviderChecks();
    // Loads that started before the deletion are dropped instead of rendered.
    state.loadSequence += 1;
    state.replaceDataOnLoad = true;
    state.loadedAreas.clear();
    await load();
  } catch (error) { toast(error.message, true); } finally { release(); }
}

// Local zones win over provider zones; the editor shows both and saves local values only.
const ZONE_METRIC_LABELS = {
  power: { threshold: "FTP (W)", limits: "Zonenobergrenzen (% der FTP)", key: "percent_limits" },
  heart_rate: { threshold: "Schwellenpuls (LSHF, bpm)", limits: "Zonenobergrenzen (bpm)", key: "limits" },
  pace: { threshold: "", limits: "Zonenobergrenzen (% der Schwellenpace)", key: "percent_limits" },
};
// One page per sport with its zone kinds (#2262); the performance values name its thresholds.
const ZONE_SPORTS = {
  ride: { kinds: ["power", "heart_rate"], lt2: ["cycling_ftp_watts", "bike_threshold_hr_bpm"], maximum: "cycling_max_hr_bpm", power: "cycling_ftp_watts" },
  run: { kinds: ["pace", "heart_rate", "power"], lt2: ["run_threshold_pace_seconds_per_km", "run_threshold_hr_bpm"], maximum: "running_max_hr_bpm", power: "run_threshold_watts" },
  swim: { kinds: ["pace", "heart_rate"], lt2: [null, null], maximum: null, power: null },
};
const ZONE_KIND_TITLES = { power: "Leistungszonen", heart_rate: "Herzfrequenzzonen", pace: "Pacezonen" };
const POWER_ZONE_NAMES = ["Regeneration", "Grundlage", "Tempo", "Schwelle", "VO2max", "Anaerob", "Neuromuskulär"];
let zoneConfigurations = [];
let zoneEffective = [];
let zoneToday = "";
let zoneSport = "ride";

function applyZoneData(result) {
  zoneConfigurations = result.configurations || [];
  zoneEffective = result.effective || [];
  zoneToday = result.today || "";
}

async function loadZones() {
  try {
    const result = await api("/api/zones");
    applyZoneData(result);
    renderZoneOverview();
    renderZoneWarnings(result.threshold_warnings);
    // The strip follows a save or removal at once, not only the next bootstrap.
    if (state.data) state.data.threshold_warnings = result.threshold_warnings ?? null;
    updateStatusSources({ thresholds: thresholdStatusItem(result.threshold_warnings) });
  } catch (error) {
    renderZoneWarnings(null);
    toast(error.message, true);
  }
}

const SDK_FAILURE_TEXT = {
  login_failed: "Garmin-Login fehlgeschlagen",
  mfa_required: "Garmin-Login braucht MFA",
  requests_failed: "Garmin-Abruf schlägt wiederholt fehl",
};

// Pure: one line per stale threshold with value, source and age (#1768).
function zoneWarningLine(item) {
  const unit = item.age_days === 1 ? "Tag" : "Tage";
  const age = item.age_days == null ? "" : ` · ${item.age_days} ${unit} alt`;
  const suffix = { expired: " – nicht mehr verwendet", sdk_failed: " – wird nicht aktualisiert" }[item.reason] || "";
  return `${item.label} ${item.value_text} · ${item.source_label}${age}${suffix}`;
}

function zoneWarningTitle(warnings) {
  const sdk = warnings.sdk_failure ? `${SDK_FAILURE_TEXT[warnings.sdk_failure.reason] || "Garmin-SDK ausgefallen"} · ` : "";
  return `${sdk}Schwellen prüfen und neue Werte hier lokal speichern`;
}

// Pure: the status strip notice for stale thresholds (#1768), or null.
function thresholdStatusItem(warnings) {
  const items = Array.isArray(warnings?.items) ? warnings.items : [];
  if (!items.length) return null;
  return {
    level: "warning",
    title: "Schwellenwerte prüfen",
    detail: items.length === 1 ? zoneWarningLine(items[0]) : `${items.length} Werte sind veraltet.`,
    action: { href: "#more/profile", label: "Prüfen" },
  };
}

// The closed section names the stale values; it no longer opens by itself (#2262).
function renderZoneSummary(warnings) {
  const node = $("#zoneSummary");
  if (!node) return;
  const count = Array.isArray(warnings?.items) ? warnings.items.length : 0;
  const noun = count === 1 ? "Wert" : "Werte";
  node.textContent = count ? `${count} ${noun} veraltet` : "";
  node.classList.toggle("is-warning", count > 0);
}

function renderZoneWarnings(warnings) {
  renderZoneSummary(warnings);
  const node = $("#zoneWarnings");
  if (!node) return;
  const items = Array.isArray(warnings?.items) ? warnings.items : [];
  node.hidden = !items.length;
  if (!items.length) {
    node.replaceChildren();
    return;
  }
  // Phrasing content only: <output> holds one line per threshold.
  node.replaceChildren(h("strong", null, zoneWarningTitle(warnings)), ...items.flatMap((item) => [h("br"), `• ${zoneWarningLine(item)}`]));
}

// Pure: the configuration in effect today for one sport, metric and environment, Garmin's
// zones for all sports included (the server picks it and names its key).
function zoneConfigurationFor(sport, metric, environment = null, configurations = zoneConfigurations, effective = zoneEffective) {
  const chosen = effective.find((item) => item.sport === sport && item.metric === metric && (item.environment || null) === environment);
  return chosen ? configurations.find((item) => item.key === chosen.key) || null : null;
}

function zoneLimitsOf(item) {
  return item[ZONE_METRIC_LABELS[item.metric].key] || item.limits || item.percent_limits || [];
}

function zoneLimitsText(item) {
  return zoneLimitsOf(item).join(", ");
}

function zoneMetricValue(key, metrics = state.data?.performance?.metrics) {
  const value = key ? Number(metrics?.[key]?.value) : Number.NaN;
  return Number.isFinite(value) && value > 0 ? value : null;
}

// Pure: a zone's upper limit in its unit (W, bpm, s/km), or null.
function zoneLimitValue(item, limit, thresholdPace) {
  const value = Number(limit);
  if (!Number.isFinite(value) || value <= 0) return null;
  if (item.metric === "pace") return thresholdPace ? thresholdPace * 100 / value : null;
  if (item.metric === "heart_rate" && item.limits) return value;
  const base = item.percent_basis === "maximum" ? item.maximum : item.threshold;
  return Number(base) > 0 ? Number(base) * value / 100 : null;
}

function zoneValueText(metric, value) {
  if (value == null) return "—";
  if (metric === "pace") return formatPace(value).replace(" /km", "");
  return `${Math.round(value)}${metric === "power" ? " W" : ""}`;
}

// Pure: the rows of one zone kind. The stored limits end with the open top zone, so each limit
// is one zone; a limit without a value to convert (no threshold pace) is shown as its percent.
function zoneRows(item, thresholdPace = null) {
  const limits = zoneLimitsOf(item);
  const percent = !(item.metric === "heart_rate" && item.limits);
  const pace = item.metric === "pace";
  const text = (index, step = 0) => {
    const value = zoneLimitValue(item, limits[index], thresholdPace);
    if (value == null) return percent ? `${AppFormat.number(limits[index])} %` : "—";
    return zoneValueText(item.metric, pace ? value : value + step);
  };
  const last = limits.length - 1;
  return limits.map((limit, index) => {
    const label = item.metric === "power" ? POWER_ZONE_NAMES[index] : null;
    const name = label ? `Z${index + 1} ${label}` : `Z${index + 1}`;
    const share = percent && index < last && zoneLimitValue(item, limit, thresholdPace) != null ? ` · ${AppFormat.number(limit)} %` : "";
    return { zone: `Z${Math.min(index + 1, 5)}`, name, range: zoneRange(item, index, last, text) + share };
  });
}

// The range of one zone: pace zones run from slower to faster, the others from low to high.
function zoneRange(item, index, last, text) {
  if (item.metric === "pace") {
    if (index === 0) return `langsamer als ${text(0)}`;
    if (index === last) return `schneller als ${text(index - 1)}`;
    return `${text(index - 1)}–${text(index)}`;
  }
  if (index === 0) return `bis ${text(0)}`;
  if (index === last) {
    const maximum = item.metric === "heart_rate" && item.maximum ? ` · max ${Math.round(item.maximum)}` : "";
    return `ab ${text(index - 1, 1)}${maximum}`;
  }
  return `${text(index - 1, 1).replace(/ W$/, "")}–${text(index)}`;
}

function zoneSourceText(item) {
  if (item.source === "local") return "Lokal gespeichert";
  // The configuration names its provider; the source key stands in without a label.
  const provider = item.label || item.source;
  return item.sport == null ? `${provider}, gilt für alle Sportarten` : provider;
}

const THRESHOLD_ORIGINS = { measured: "gemessen", estimated: "aus der Herzfrequenzvariabilität", own_sessions: "aus eigenen Einheiten", fixed_share: "Standardwert" };

// Pure: LT1 and LT2 of a sport (#2262). LT2 comes from the performance values, LT1 from the
// performance thresholds (#2427); `origins` names where each row's values come from.
function thresholdRows(sport, metrics = state.data?.performance?.metrics || {}, thresholds = state.data?.performance?.thresholds || {}) {
  const spec = ZONE_SPORTS[sport];
  const metric = sport === "ride" ? "power" : "pace";
  const lt2 = { main: zoneMetricValue(spec.lt2[0], metrics), hr: zoneMetricValue(spec.lt2[1], metrics) };
  const values = thresholds.first_thresholds?.[sport] || {};
  const lt1 = { main: (metric === "power" ? values.watts : values.pace_seconds_per_km) ?? null, hr: values.hr_bpm ?? null };
  const sources = [metrics[spec.lt2[0]], metrics[spec.lt2[1]]].filter((item) => item?.value != null);
  const lt1Origins = [["Herzfrequenz", values.hr_origin], [metric === "power" ? "Leistung" : "Pace", metric === "power" ? values.power_origin : values.pace_origin]]
    .filter(([, origin]) => THRESHOLD_ORIGINS[origin]).map(([name, origin]) => `${name} ${THRESHOLD_ORIGINS[origin]}`);
  return {
    metric,
    lt1,
    lt2,
    origins: { lt1: lt1Origins.join(" · ") || null, lt2: lt2Origin(metric, metrics, spec) },
    validFrom: sources.map((item) => String(item.observed_at || "")).filter(Boolean).sort((a, b) => a.localeCompare(b)).at(-1) || null,
  };
}

// One provider names the row; providers that differ per value are named per value.
function lt2Origin(metric, metrics, spec) {
  const named = [[metric === "power" ? "Leistung" : "Pace", metrics[spec.lt2[0]]], ["Herzfrequenz", metrics[spec.lt2[1]]]].filter(([, item]) => item?.value != null && item.source);
  if (!named.length) return null;
  if (new Set(named.map(([, item]) => item.source)).size === 1) return `Von ${named[0][1].source}`;
  return named.map(([name, item]) => `${name} von ${item.source}`).join(" · ");
}

function thresholdMainText(metric, value) {
  return metric === "pace" && value != null ? `${zoneValueText("pace", value)} /km` : zoneValueText(metric, value);
}

function zoneEditButton(label, attributes) {
  return h("button", { type: "button", className: "zone-edit", dataset: attributes }, label);
}

// A small yellow "i" names the origin of a threshold row in a popover (athlete decision 2026-11-12, #2427).
let thresholdInfoId = 0;
function thresholdOriginInfo(label, text) {
  const button = h("button", { type: "button", className: "threshold-info", "aria-label": `Herkunft ${label}` }, "i");
  const info = h("div", { className: "analysis-info-tooltip", id: `threshold-info-${++thresholdInfoId}` }, text);
  bindAnalysisPopover(button, info);
  return [button, info];
}

function thresholdBox(sport) {
  const rows = thresholdRows(sport);
  const heading = h("h3", null, "Schwellen", rows.validFrom ? h("small", null, `gilt seit ${dateLabel(String(rows.validFrom).slice(0, 10))}`) : null);
  const grid = h("div", "zone-thresholds", h("span"), h("span", "zone-muted", rows.metric === "power" ? "Leistung" : "Pace"), h("span", "zone-muted", "Herzfrequenz"));
  const lt2Label = sport === "ride" ? "Anaerob (LT2) / FTP" : "Anaerob (LT2)";
  const lines = [{ label: "Aerob (LT1)", values: rows.lt1, origin: rows.origins.lt1, main: "lt1-main", hr: "lt1-hr" }, { label: lt2Label, values: rows.lt2, origin: rows.origins.lt2, main: "lt2-main", hr: "lt2-hr" }];
  for (const { label, values, origin, ...names } of lines) {
    grid.append(h("span", "zone-muted", label, origin ? thresholdOriginInfo(label, origin) : null),
      h("b", { dataset: { threshold: names.main } }, thresholdMainText(rows.metric, values.main)),
      h("b", { dataset: { threshold: names.hr } }, values.hr == null ? "—" : `${Math.round(values.hr)} bpm`));
  }
  const foot = h("p", "zone-source", zoneEditButton("Bearbeiten", { zoneEdit: "lt1" }));
  const sources = lt2SourceRows(sport);
  const list = sources.length ? h("div", "lt2-sources", h("h4", null, lt2Label), h("ul", null, ...sources.map((row) => lt2SourceItem(sport, row)))) : null;
  return h("section", { className: "zone-box", dataset: { zoneBox: "thresholds" } }, heading, grid, list, foot);
}

// LT2 from Garmin beside the local estimates (#3273): DFA-α1 at 0.5 and CS/CP. Each local value
// is adopted on its own (F8); nothing changes without the athlete's tap.
const LT2_METRICS = { run: { main: "run_threshold_pace_seconds_per_km", hr: "run_threshold_hr_bpm" }, ride: { main: "cycling_ftp_watts", hr: "bike_threshold_hr_bpm" } };
const LT2_SOURCE_NAMES = { dfa_alpha1: "DFA-α1", critical_speed: "CS", critical_power: "CP" };

// The provider's LT2 value and label: the winner when a provider gives it, else a provider
// alternative beside a local value.
function lt2ProviderValue(item) {
  if (item?.source_provider) return { value: item.value ?? null, label: item.source };
  const other = item?.alternatives?.find((candidate) => candidate.source_provider);
  return { value: other?.value ?? null, label: other?.source ?? null };
}

function lt2Duration(seconds) {
  const minutes = seconds.map((value) => Math.round(value / 60));
  return minutes.length > 1 ? `${minutes.slice(0, -1).join(", ")} und ${minutes.at(-1)} min` : `${minutes[0]} min`;
}

// Pure: the adoptable values of one local estimate, each with its label (F8).
function lt2AdoptValues(source, estimate) {
  if (estimate?.status !== "ok") return [];
  const values = [];
  if (estimate.heart_rate_bpm != null) values.push({ value: "heart_rate", shown: estimate.heart_rate_bpm, label: `Herzfrequenz ${estimate.heart_rate_bpm} bpm` });
  if (estimate.pace_seconds_per_km != null) values.push({ value: "pace", shown: estimate.pace_seconds_per_km, label: `Pace ${thresholdMainText("pace", estimate.pace_seconds_per_km)}` });
  if (estimate.power_w != null) values.push({ value: "power", shown: estimate.power_w, label: `Leistung ${estimate.power_w} W` });
  return values;
}

function lt2EstimateBasis(source, estimate) {
  if (source === "dfa_alpha1") return `aus ${estimate.sessions} Läufen der letzten ${estimate.window_days} Tage`;
  return `Bestwerte ${lt2Duration(estimate.durations_s || [])} aus ${estimate.window_days} Tagen`;
}

// Pure: one row per LT2 source of a sport; `inEffect` marks the source whose value applies.
function lt2SourceRows(sport, metrics = state.data?.performance?.metrics || {}, estimates = state.data?.lt2_estimates?.[sport] || {}, effective = (metric) => zoneConfigurationFor(sport, metric)) {
  const keys = LT2_METRICS[sport];
  if (!keys) return [];
  const mainMetric = sport === "ride" ? "power" : "pace";
  const origins = new Set([effective(mainMetric)?.threshold_origin, effective("heart_rate")?.threshold_origin].filter(Boolean));
  const provider = lt2ProviderRow(metrics[keys.main], metrics[keys.hr], mainMetric, origins);
  // A fixed order whatever the server's key order: the provider, DFA-α1, then CS or CP.
  const sources = Object.keys(LT2_SOURCE_NAMES).filter((source) => source in estimates);
  return [...(provider ? [provider] : []), ...sources.map((source) => lt2EstimateRow(source, estimates[source], origins))];
}

function lt2ProviderRow(main, hr, mainMetric, origins) {
  const [first, second] = [lt2ProviderValue(main), lt2ProviderValue(hr)];
  if (first.value == null && second.value == null) return null;
  const texts = [first.value == null ? null : thresholdMainText(mainMetric, first.value), second.value == null ? null : `${Math.round(second.value)} bpm`];
  const observed = main?.source_provider ? main.observed_at : null;
  return {
    source: "provider",
    name: first.label || second.label,
    values: texts.filter(Boolean).join(" · "),
    basis: observed ? `vom ${dateLabel(String(observed).slice(0, 10))}` : "",
    inEffect: !origins.size && Boolean(main?.source_provider),
    adopt: [],
  };
}

function lt2EstimateRow(source, estimate, origins) {
  const known = estimate?.status === "ok";
  const adopt = lt2AdoptValues(source, estimate);
  return {
    source,
    name: LT2_SOURCE_NAMES[source] || source,
    quality: known ? estimate.quality : null,
    values: known ? adopt.map((item) => item.label.replace(/^\S+ /, "")).join(" · ") : "unbekannt",
    unknown: !known,
    basis: known ? lt2EstimateBasis(source, estimate) : String(estimate?.reason || ""),
    inEffect: origins.has(source),
    adopt,
  };
}

// The quality of a local estimate; the method name says what it is (#3297: no qualifier word).
const LT2_QUALITY_CHIPS = { good: ["Qualität gut", "status-chip success"], low: ["Qualität mittel", "status-chip warning"] };

function lt2SourceItem(sport, row) {
  const chips = [];
  if (LT2_QUALITY_CHIPS[row.quality]) {
    const [text, className] = LT2_QUALITY_CHIPS[row.quality];
    chips.push(h("span", className, text));
  }
  if (row.inEffect) chips.push(h("span", "status-chip success", "gilt"));
  const values = h("span", { className: "lt2-source-values", dataset: row.unknown ? { unknown: "" } : {} }, row.values);
  const item = h("li", { className: "lt2-source", dataset: { lt2Source: row.source } }, h("span", "lt2-source-name", row.name, ...chips), values, h("span", "lt2-source-basis", row.basis));
  if (row.adopt.length) {
    const button = h("button", { type: "button", className: "secondary-button" }, "Übernehmen");
    button.addEventListener("click", () => { void adoptLt2(sport, row, button); });
    item.append(button);
  }
  return item;
}

// The DFA row offers its values one by one (F8); a single value is adopted at once.
function chooseLt2Values(row) {
  const dialog = $("#lt2AdoptDialog");
  if (!dialog) return Promise.resolve([]);
  $("#lt2AdoptTitle").textContent = `${row.name} übernehmen`;
  const ride = (row.sport || zoneSport) === "ride";
  $("#lt2AdoptMessage").textContent = `Als ${ride ? "Anaerob (LT2) / FTP" : "Anaerob (LT2)"} ${ride ? "Radfahren" : "Laufen"}, gilt ab heute.`;
  const boxes = row.adopt.map((item) => h("input", { type: "checkbox", checked: true, value: item.value }));
  $("#lt2AdoptValues").replaceChildren(...row.adopt.map((item, index) => h("label", "chip", boxes[index], h("span", null, item.label))));
  return new Promise((resolve) => {
    const finish = (chosen) => { dialog.onclose = null; resolve(chosen); };
    $("#lt2AdoptCancel").onclick = () => dialog.close();
    dialog.onclose = () => finish(dialog.returnValue === "adopt" ? row.adopt.filter((_, index) => boxes[index].checked) : []);
    $("#lt2AdoptAccept").onclick = () => { dialog.returnValue = "adopt"; };
    dialog.returnValue = "";
    dialog.showModal();
  });
}

async function adoptLt2(sport, row, button) {
  const chosen = row.adopt.length > 1 ? await chooseLt2Values(row) : row.adopt;
  if (!chosen.length) return;
  button.disabled = true;
  try {
    for (const item of chosen) {
      if (!await adoptLt2Value(sport, row, item, {})) break;
    }
  } finally { button.disabled = false; }
}

// One value; the server asks for the second step above 5 % (F9) or before replacing an entered value.
async function adoptLt2Value(sport, row, item, flags) {
  try {
    const result = await api("/api/zones/lt2/adopt", { method: "POST", body: JSON.stringify({ sport, source: row.source, value: item.value, shown: item.shown, ...(row.activity_id ? { activity_id: row.activity_id } : {}), ...flags }) });
    showZoneReceipt(`${row.name}: ${item.label} übernommen`, result.change);
    // From the calendar's step test (#3833) the zone receipt is out of view; undo stays in the zones.
    if (row.activity_id) toast(`${row.name}: ${item.label} übernommen`);
    await reloadZonesAndThresholds();
    return true;
  } catch (error) {
    const flag = await confirmLt2Step(error, flags);
    if (flag) return adoptLt2Value(sport, row, item, { ...flags, [flag]: true });
    if (flag === null) toast(error.message, true);
    return false;
  }
}

// The confirmation a refused adoption asks for: the flag to resend, false when declined, null otherwise.
async function confirmLt2Step(error, flags) {
  if (error.reason === "lt2_deviation" && !flags.confirm_deviation) {
    const confirmed = await requestConfirmation(`${error.message} Zonen in % der Schwelle verschieben sich mit.`, { title: "Große Abweichung", confirmLabel: "Trotzdem übernehmen" });
    return confirmed && "confirm_deviation";
  }
  if (error.reason === "lt2_replace_local" && !flags.replace_local) {
    const confirmed = await requestConfirmation(error.message, { title: "Eigenen Wert ersetzen?", confirmLabel: "Ersetzen" });
    return confirmed && "replace_local";
  }
  return null;
}

// F10: the values of a sport as the change notice compares them; null is "unbekannt".
function lt2NoticeValues(sport, metrics = state.data?.performance?.metrics || {}, estimates = state.data?.lt2_estimates?.[sport] || {}) {
  const keys = LT2_METRICS[sport];
  const ok = (estimate) => (estimate?.status === "ok" ? estimate : {});
  const provider = (item) => lt2ProviderValue(item).value;
  if (sport === "run") {
    return { provider_hr: provider(metrics[keys.hr]), provider_pace: provider(metrics[keys.main]), dfa_hr: ok(estimates.dfa_alpha1).heart_rate_bpm ?? null, dfa_pace: ok(estimates.dfa_alpha1).pace_seconds_per_km ?? null, cs_pace: ok(estimates.critical_speed).pace_seconds_per_km ?? null };
  }
  return { provider_power: provider(metrics[keys.main]), provider_hr: provider(metrics[keys.hr]), cp_power: ok(estimates.critical_power).power_w ?? null };
}

// Minimum change per value before a notice (athlete decision F10): 2 bpm, 3 s/km, 5 W.
const LT2_NOTICE_MIN = { provider_hr: 2, dfa_hr: 2, provider_pace: 3, dfa_pace: 3, cs_pace: 3, provider_power: 5, cp_power: 5 };
const LT2_NOTICE_UNITS = { pace: " /km", hr: " bpm", power: " W" };
const LT2_NOTICE_LABELS = { provider_hr: "Herzfrequenz", provider_pace: "Pace", provider_power: "FTP", dfa_hr: "DFA-α1 Herzfrequenz", dfa_pace: "DFA-α1 Pace", cs_pace: "CS", cp_power: "CP" };

// Pure: the changes against the seen values; a switch between a value and "unbekannt" counts too.
function lt2NoticeChanges(seen, current) {
  return Object.keys(current).filter((key) => {
    const before = seen?.[key] ?? null;
    const now = current[key] ?? null;
    if ((before == null) !== (now == null)) return true;
    return before != null && Math.abs(now - before) >= LT2_NOTICE_MIN[key];
  }).map((key) => ({ key, before: seen?.[key] ?? null, now: current[key] ?? null }));
}

function lt2NoticeValueText(key, value) {
  if (value == null) return "unbekannt";
  if (key.endsWith("_pace")) return thresholdMainText("pace", value).replace(" /km", "");
  return String(Math.round(value));
}

// `provider` names the provider the provider_* values come from, such as "Garmin".
function lt2NoticeDetail(changes, provider = "") {
  return changes.map(({ key, before, now }) => {
    const unit = LT2_NOTICE_UNITS[key.split("_").pop()];
    const label = key.startsWith("provider_") ? `${provider} ${LT2_NOTICE_LABELS[key]}`.trim() : LT2_NOTICE_LABELS[key];
    return `${label} ${lt2NoticeValueText(key, before)} → ${lt2NoticeValueText(key, now)}${now == null ? "" : unit}`;
  }).join(", ");
}

// One save per sport at a time; a failed save is not retried by itself (Gitar on #3303).
const lt2BaselineSaving = new Set();

async function seeLt2Values(sport, values) {
  if (lt2BaselineSaving.has(sport)) return;
  lt2BaselineSaving.add(sport);
  try {
    const result = await api("/api/zones/lt2/notice-baseline", { method: "PUT", body: JSON.stringify({ sport, values }) });
    if (state.data) state.data.lt2_notice_baselines = result.lt2_notice_baselines;
  } catch (error) {
    toast(error.message, true);
    return;
  } finally { lt2BaselineSaving.delete(sport); }
  refreshLt2Notice();
}

// One notice per sport in the status strip; the first values become the baseline silently.
function refreshLt2Notice() {
  const data = state.data;
  if (!data?.lt2_estimates || !data.performance?.metrics) return;
  const notices = [];
  for (const [sport, name] of [["run", "Laufen"], ["ride", "Radfahren"]]) {
    const current = lt2NoticeValues(sport);
    const seen = data.lt2_notice_baselines?.[sport];
    if (!seen) {
      if (Object.values(current).some((value) => value != null)) void seeLt2Values(sport, current);
      continue;
    }
    const changes = lt2NoticeChanges(seen, current);
    const keys = LT2_METRICS[sport];
    const provider = lt2ProviderValue(data.performance.metrics[keys.main]).label || lt2ProviderValue(data.performance.metrics[keys.hr]).label || "";
    if (changes.length) notices.push({ sport, name, current, detail: lt2NoticeDetail(changes, provider) });
  }
  const [first, ...rest] = notices;
  updateStatusSources({ lt2: first ? {
    level: "notice",
    title: rest.length ? "LT2 geändert" : `LT2 ${first.name} geändert`,
    detail: notices.map((notice) => notice.detail).join("; "),
    action: { href: "#more/profile", label: "Ansehen", onOpen: () => { for (const notice of notices) void seeLt2Values(notice.sport, notice.current); } },
    dismiss: () => { for (const notice of notices) void seeLt2Values(notice.sport, notice.current); },
  } : null });
}

function zoneKindBox(sport, metric, environment = null) {
  const item = zoneConfigurationFor(sport, metric, environment);
  const basis = { power: sport === "run" ? "% der Schwellenleistung" : "% der FTP", heart_rate: "bpm", pace: "% der Schwellenpace" }[metric];
  const heading = h("h3", null, environment ? `${ZONE_KIND_TITLES[metric]} · Indoor` : ZONE_KIND_TITLES[metric], h("small", null, basis));
  const box = h("section", { className: "zone-box", dataset: { zoneBox: environment ? `${metric}-${environment}` : metric } }, heading);
  const attributes = { zoneEdit: metric, zoneEnvironment: environment || "" };
  if (!item) {
    box.append(h("p", "zone-source", "Noch keine Werte – ", zoneEditButton("Anlegen", attributes)));
    return box;
  }
  const rows = zoneRows(item, metric === "pace" ? zoneMetricValue(ZONE_SPORTS[sport].lt2[0]) : null);
  box.append(h("ul", "zone-list", ...rows.map((row) => h("li", { dataset: { zone: row.zone } }, h("i"), h("span", null, row.name), h("em", null, row.range)))),
    h("p", "zone-source", zoneSourceText(item), zoneEditButton("Bearbeiten", attributes)));
  return box;
}

function renderZoneOverview() {
  const root = $("#zoneOverview");
  if (!root) return;
  document.querySelectorAll("#zoneSportTabs [data-zone-sport]").forEach((button) => {
    const active = button.dataset.zoneSport === zoneSport;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const boxes = [thresholdBox(zoneSport)];
  for (const metric of ZONE_SPORTS[zoneSport].kinds) {
    boxes.push(zoneKindBox(zoneSport, metric));
    // Indoor zones of their own stay visible beside the general ones.
    if (zoneConfigurationFor(zoneSport, metric, "indoor")) boxes.push(zoneKindBox(zoneSport, metric, "indoor"));
  }
  // The editors move under a box when opened; they go back before the boxes are replaced.
  const editors = [$("#firstThresholdEditor"), $("#zoneEditor")];
  editors.forEach((editor) => { editor.hidden = true; });
  $("#zoneReceipt").after(...editors);
  root.replaceChildren(...boxes);
}

function refreshThresholdBox() {
  const box = document.querySelector('#zoneOverview [data-zone-box="thresholds"]');
  if (box) box.replaceWith(thresholdBox(zoneSport));
}

function showZoneSport(sport) {
  if (!ZONE_SPORTS[sport]) return;
  zoneSport = sport;
  renderZoneOverview();
}

// "Bearbeiten" opens the existing form under its box, prefilled with the values in effect.
function openZoneEditor(button) {
  const box = button.closest("[data-zone-box]");
  if (button.dataset.zoneEdit === "lt1") {
    const editor = $("#firstThresholdEditor");
    $("#zoneEditor").hidden = true;
    editor.hidden = false;
    box.after(editor);
    editor.querySelector("input")?.focus();
    return;
  }
  $("#zoneSport").value = zoneSport;
  $("#zoneMetric").value = button.dataset.zoneEdit;
  $("#zoneEnvironment").value = button.dataset.zoneEnvironment || "";
  renderZoneEditor();
  const editor = $("#zoneEditor");
  $("#firstThresholdEditor").hidden = true;
  editor.hidden = false;
  box.after(editor);
  $("#zoneLimits").focus();
}

function selectedZoneConfigurations() {
  const shown = zoneConfigurationFor($("#zoneSport").value, $("#zoneMetric").value, $("#zoneEnvironment").value || null);
  return shown ? zoneConfigurations.filter((item) => item.key === shown.key) : [];
}

// Pure: the editor values in effect today, or the current threshold from the performance
// values when the kind has no zones yet (#2262).
function zoneEditorValues(sport, metric, shown, metricValue = zoneMetricValue) {
  if (shown) return { threshold: shown.threshold ?? "", maximum: shown.maximum ?? "", limits: zoneLimitsText(shown) };
  const spec = ZONE_SPORTS[sport] || {};
  const threshold = { power: metricValue(spec.power), heart_rate: metricValue(spec.lt2?.[1]) }[metric] ?? "";
  return { threshold, maximum: metric === "heart_rate" ? metricValue(spec.maximum) ?? "" : "", limits: "" };
}

function renderZoneEditor() {
  const sport = $("#zoneSport").value;
  const metric = $("#zoneMetric").value;
  const labels = ZONE_METRIC_LABELS[metric];
  const selected = selectedZoneConfigurations();
  const local = selected.find((item) => item.source === "local");
  const values = zoneEditorValues(sport, metric, local || selected[0]);
  $("#zoneThresholdLabel").textContent = labels.threshold;
  $("#zoneThresholdField").hidden = !labels.threshold;
  $("#zoneThreshold").required = Boolean(labels.threshold);
  $("#zoneMaximumField").hidden = metric !== "heart_rate";
  $("#zoneLimitsLabel").textContent = labels.limits;
  $("#zoneThreshold").value = values.threshold;
  $("#zoneMaximum").value = values.maximum;
  $("#zoneLimits").value = values.limits;
  $("#zoneRemoveButton").hidden = !local;
  $("#zoneFromBeginningField").hidden = selected.length > 0;
  $("#zoneFromBeginning").checked = false;
  $("#zoneValidFrom").disabled = false;
  $("#zoneValidFrom").max = zoneToday;
  $("#zoneValidFrom").value = zoneToday;
  $("#zoneRemoveButton").dataset.key = local?.key || "";
  const sources = $("#zoneSources");
  sources.replaceChildren(...selected.map((item) => {
    const threshold = item.threshold == null ? "" : ` · Schwelle ${item.threshold}`;
    return h("li", null, `${item.source === "local" ? "Lokal (gilt)" : item.label || item.source}${threshold} · ${zoneLimitsText(item)}`);
  }));
}

function toggleZoneFromBeginning() {
  const beginning = $("#zoneFromBeginning").checked;
  $("#zoneValidFrom").disabled = beginning;
  $("#zoneValidFrom").required = !beginning;
}

function zoneNumber(value) {
  return value === "" ? null : Number(value);
}

async function saveZones(event) {
  event.preventDefault();
  const metric = $("#zoneMetric").value;
  const button = $("#zoneSaveButton");
  const limits = $("#zoneLimits").value.split(/[\s,;]+/).filter(Boolean).map(Number);
  button.disabled = true;
  try {
    const result = await api("/api/zones", {
      method: "PUT",
      body: JSON.stringify({
        sport: $("#zoneSport").value,
        metric,
        environment: $("#zoneEnvironment").value || null,
        ...($("#zoneFromBeginning").checked ? { from_beginning: true } : { valid_from: $("#zoneValidFrom").value }),
        threshold: ZONE_METRIC_LABELS[metric].threshold ? zoneNumber($("#zoneThreshold").value) : null,
        maximum: metric === "heart_rate" ? zoneNumber($("#zoneMaximum").value) : null,
        [ZONE_METRIC_LABELS[metric].key]: limits,
      }),
    });
    showZoneReceipt("Lokale Zonen gespeichert", result.change);
    await reloadZonesAndThresholds();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function removeZones() {
  const button = $("#zoneRemoveButton");
  const confirmed = await requestConfirmation("Danach gelten wieder die Zonen des Anbieters.", { title: "Lokale Zonen entfernen?", confirmLabel: "Entfernen" });
  if (!confirmed) return;
  button.disabled = true;
  try {
    const result = await api("/api/zones/remove", { method: "PUT", body: JSON.stringify({ key: button.dataset.key, confirmed: true }) });
    showZoneReceipt("Lokale Zonen entfernt", result.change);
    await reloadZonesAndThresholds();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

// A zone change moves LT1 and LT2, which the performance values carry; the zones draw after them.
async function reloadZonesAndThresholds() {
  await globalThis.load?.(undefined, ["performance"]);
  await loadZones();
}

function showZoneReceipt(message, change) {
  const receipt = $("#zoneReceipt");
  receipt.replaceChildren(h("span", null, `${message}. `));
  if (change?.id) {
    const undo = h("button", { type: "button", className: "message-undo" }, "Rückgängig");
    undo.addEventListener("click", async () => {
      undo.disabled = true;
      try {
        await api("/api/change-history/undo", { method: "POST", body: JSON.stringify({ change_id: change.id, expected_current_hash: change.after_hash }) });
        receipt.replaceChildren("Zurückgenommen.");
        await reloadZonesAndThresholds();
      } catch (error) {
        undo.disabled = false;
        toast(error.message, true);
      }
    });
    receipt.append(undo);
  }
  receipt.hidden = false;
}
