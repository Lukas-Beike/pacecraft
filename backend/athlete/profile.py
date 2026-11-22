"""Athlete profile normalization and persistence use cases."""

import json
import math
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend import change_history
from backend.canonical.decimal_text import comma_decimal_float
from backend.canonical.first_threshold import (
    FIRST_THRESHOLD_BOUNDS,
    FIRST_THRESHOLD_FIELD,
    FIRST_THRESHOLD_ORIGIN_FIELD,
    FIRST_THRESHOLD_ORIGINS,
    LEGACY_PERCENT_FIELD,
    ORIGIN_MEASURED,
    first_threshold_bpm,
    first_threshold_percent,
    percent_of_lthr,
)
from backend.canonical.vocabulary import ProviderId
from backend.errors import AppError

DEFAULT_TIMEZONE = "Europe/Berlin"
# Intensity distribution the Coach plans with; pyramidal unless the athlete chooses.
TRAINING_MODELS = ("pyramidal", "polarized", "threshold", "sweet_spot")
# The athlete's preferred source per metric group (#1668). Nothing reads them
# and they have no control (#2084, #3640); they stay so a stored value is kept.
PREFERRED_SOURCE_FIELDS = (
    "preferred_source_thresholds",
    "preferred_source_body",
    "preferred_source_hrv",
    "preferred_source_resting_hr",
    "preferred_source_sleep",
)
PREFERRED_SOURCES = ("", ProviderId.GARMIN.value)
# The athlete's sex for sex-specific methods (M6 TRIMP constants, #1774); empty
# means not set.
SEXES = ("", "male", "female")
# The energy goal (#3547): lose takes a deficit, build a surplus, in kcal per
# day; hold has none; empty means not set.
ENERGY_GOALS = ("", "lose", "hold", "build")
ENERGY_GOAL_KCAL = (0, 1500)
# The athlete's first threshold heart rate (entered in bpm, for example from a lab
# test, or adopted from the estimate) and where it comes from; empty means not
# set. Only the athlete sets them, through the profile form or the API: the Coach
# can read them but never write them.
# The date the athlete took the learned everyday term of the energy table's
# formula rows over (#3577); empty until checked. Only the athlete sets it.
LEARNED_EVERYDAY_FIELD = "learned_everyday_checked"
COACH_READ_ONLY_FIELDS = frozenset(
    {FIRST_THRESHOLD_FIELD, FIRST_THRESHOLD_ORIGIN_FIELD, LEARNED_EVERYDAY_FIELD}
)

# Weight and body fat are measurements Körper enters (athlete decision, #3640):
# no profile save changes them, and no reader takes them as the current value.
# Their stored values and history stay data, read as dated weigh-ins and
# skinfold readings (#3557).
BODY_FIELDS = ("weight_kg", "body_fat_pct")


def coach_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """The profile the Coach reads: the stored body fields are no current value."""
    return {key: value for key, value in profile.items() if key not in BODY_FIELDS}


DEFAULT_PROFILE = {
    "name": "",
    "goals": "",
    "sports": "Cycling",
    "training_background": "",
    "typical_weekly_volume": "",
    "availability": "",
    "constraints": "",
    "equipment": "",
    "training_preferences": "",
    "training_preferences_running": "",
    "training_preferences_cycling": "",
    "training_preferences_swimming": "",
    "training_preferences_strength": "",
    "training_preferences_mobility": "",
    "training_model": TRAINING_MODELS[0],
    "performance_notes": "",
    # Stored data only (#3640): Körper owns weight and body fat; see BODY_FIELDS.
    "weight_kg": "",
    "body_fat_pct": "",
    "height_cm": "",
    "sex": "",
    # Jackson-Pollock needs the age at each skinfold measurement (#3099).
    "birth_year": "",
    "coaching_style": "Supportive, direct, and evidence-aware",
    "timezone": os.environ.get("TZ", DEFAULT_TIMEZONE),
    "weather_location": "",
    "sleep_target_hours": "",
    "fueling_tolerance": "",
    "energy_goal": "",
    "energy_goal_kcal": "",
    LEARNED_EVERYDAY_FIELD: "",
    FIRST_THRESHOLD_FIELD: "",
    FIRST_THRESHOLD_ORIGIN_FIELD: "",
    **dict.fromkeys(PREFERRED_SOURCE_FIELDS, ""),
}


def profile_body_fat_history(db: Any, since: date) -> dict[date, float]:
    """The body fat values once saved in the profile, by the athlete-local day
    of the save, 1 to 70 %: dated 3-site skinfold readings (athlete decisions,
    #3477, #3557; local day #3617)."""
    return _profile_history(db, "body_fat_pct", since, (1, 70))


def profile_weight_history(db: Any, since: date) -> dict[date, float]:
    """The weights once saved in the profile, by the athlete-local day of the
    save, 30 to 300 kg: dated weigh-ins entered in the app (athlete decision,
    #3557; local day #3617)."""
    return _profile_history(db, "weight_kg", since, (30, 300))


def _profile_history(
    db: Any, field: str, since: date, bounds: tuple[float, float]
) -> dict[date, float]:
    # The athlete's day, not the save's UTC day; from the local midnight of
    # ``since`` on, as every other reader counts days (#3617 item 5).
    zone = _profile_zone(db)
    start = datetime.combine(since, time(), zone).astimezone(UTC).isoformat()
    days: dict[date, float] = {}
    for saved_at, value in change_history.field_values(db, "profile", field, start):
        try:
            number = comma_decimal_float(value)
        except ValueError:
            continue
        if bounds[0] <= number <= bounds[1]:
            days[_local_day(saved_at, zone)] = number
    return days


def _local_day(saved_at: str, zone: ZoneInfo) -> date:
    """The athlete-local day of a stored time; a time without an offset is UTC."""
    stamp = datetime.fromisoformat(saved_at)
    stamp = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
    return stamp.astimezone(zone).date()


def _profile_zone(db: Any) -> ZoneInfo:
    """The stored profile's timezone, else the default (#3617 item 5)."""
    has_store = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kv'"
    ).fetchone()
    row = (
        db.execute("SELECT value FROM kv WHERE key = 'profile'").fetchone()
        if has_store
        else None
    )
    try:
        stored = json.loads(row["value"] or "{}").get("timezone") if row else None
    except TypeError, ValueError, AttributeError:
        stored = None
    return ZoneInfo(timezone_name(stored))


def timezone_name(value: Any, *, strict: bool = False) -> str:
    """Return a valid IANA timezone name, falling back when allowed."""
    candidate = str(value or DEFAULT_TIMEZONE).strip()[:120] or DEFAULT_TIMEZONE
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError, ValueError:
        if strict:
            raise AppError(400, "Die Zeitzone muss eine gültige IANA-Zeitzone sein.")
        return DEFAULT_TIMEZONE
    return candidate


def _normalize_preferred_sources(result: dict[str, str], *, strict: bool) -> None:
    """Reject (strict) or clear an unknown preferred source."""
    for field_name in PREFERRED_SOURCE_FIELDS:
        if result[field_name] in PREFERRED_SOURCES:
            continue
        if strict:
            raise AppError(
                400,
                "Unbekannte bevorzugte Quelle; erlaubt: "
                + ", ".join(PREFERRED_SOURCES[1:])
                + " oder leer.",
            )
        result[field_name] = ""


def _normalize_sex(result: dict[str, str], *, strict: bool) -> None:
    """Reject (strict) or clear an unknown sex."""
    if result["sex"] in SEXES:
        return
    if strict:
        raise AppError(400, "Unbekanntes Geschlecht; erlaubt: male, female oder leer.")
    result["sex"] = ""


def _normalize_learned_check(result: dict[str, str], *, strict: bool) -> None:
    """Keep an ISO date or empty; reject (strict) anything else, else clear it."""
    value = result[LEARNED_EVERYDAY_FIELD].strip()
    try:
        result[LEARNED_EVERYDAY_FIELD] = (
            date.fromisoformat(value).isoformat() if value else ""
        )
    except ValueError:
        if strict:
            raise AppError(
                400, "Das Prüfdatum muss ein Datum (JJJJ-MM-TT) sein."
            ) from None
        result[LEARNED_EVERYDAY_FIELD] = ""


def _normalize_energy_goal(result: dict[str, str], *, strict: bool) -> None:
    """Keep a known goal with whole kcal (none for hold); reject (strict) an
    unknown goal or a lose or build without kcal in range, else clear it."""
    goal = result["energy_goal"]
    if goal not in ENERGY_GOALS:
        if strict:
            raise AppError(
                400, "Unbekanntes Energieziel; erlaubt: lose, hold, build oder leer."
            )
        goal = result["energy_goal"] = ""
    if goal in ("", "hold"):
        result["energy_goal_kcal"] = ""
        return
    low, high = ENERGY_GOAL_KCAL
    try:
        kcal = comma_decimal_float(result["energy_goal_kcal"])
    except ValueError:
        kcal = math.nan
    if math.isfinite(kcal) and low <= kcal <= high:
        result["energy_goal_kcal"] = str(round(kcal))
    elif strict:
        raise AppError(400, f"Das Energieziel braucht {low} bis {high} kcal pro Tag.")
    else:
        result["energy_goal"] = result["energy_goal_kcal"] = ""


BIRTH_YEARS = (1920, 2010)


def _normalize_birth_year(result: dict[str, str], *, strict: bool) -> None:
    """Keep a whole birth year in range; reject (strict) or clear another."""
    text = result["birth_year"]
    low, high = BIRTH_YEARS
    if not text or (text.isdigit() and low <= int(text) <= high):
        return
    if strict:
        raise AppError(400, f"Das Geburtsjahr muss zwischen {low} und {high} liegen.")
    result["birth_year"] = ""


def _normalize_first_threshold(result: dict[str, str], *, strict: bool) -> None:
    """Keep a plausible stored first threshold (bpm, #3915); reject or clear another."""
    text = result[FIRST_THRESHOLD_FIELD]
    origin = result[FIRST_THRESHOLD_ORIGIN_FIELD]
    if origin not in FIRST_THRESHOLD_ORIGINS:
        if strict:
            raise AppError(
                400,
                "Die Herkunft der ersten Schwelle muss gemessen oder geschätzt sein.",
            )
        origin = ""
    if not text:
        result[FIRST_THRESHOLD_ORIGIN_FIELD] = ""
        return
    number = first_threshold_bpm(text)
    if number is not None:
        result[FIRST_THRESHOLD_FIELD] = f"{round(number)}"
        # A value stored before the origin existed was entered by the athlete.
        result[FIRST_THRESHOLD_ORIGIN_FIELD] = origin or ORIGIN_MEASURED
        return
    result[FIRST_THRESHOLD_ORIGIN_FIELD] = ""
    if strict:
        low, high = FIRST_THRESHOLD_BOUNDS
        raise AppError(
            400,
            f"Die gemessene erste Schwelle muss zwischen {low} und {high} bpm liegen.",
        )
    result[FIRST_THRESHOLD_FIELD] = ""


# Number fields with their inclusive bounds and the error shown for a bad entry.
_NUMBER_FIELDS = {
    "height_cm": (50, 250, "Die Größe muss zwischen 50 und 250 cm liegen."),
    "sleep_target_hours": (
        4,
        12,
        "Das Schlafziel muss zwischen 4 und 12 Stunden liegen.",
    ),
}


def _normalize_numbers(
    result: dict[str, str], previous: Mapping[str, str] | None, *, strict: bool
) -> None:
    """Parse the number fields (comma decimals allowed) and store normalized text.

    The sleep target is always checked. The body fields are checked only in strict
    mode, and then a value equal to the stored one is kept as is, so a legacy
    spelling never blocks an unrelated edit.
    """
    for field_name, (low, high, message) in _NUMBER_FIELDS.items():
        text = result[field_name]
        if not text or (not strict and field_name != "sleep_target_hours"):
            continue
        if previous is not None and text == previous.get(field_name):
            continue
        try:
            number = comma_decimal_float(text)
        except ValueError as exc:
            raise AppError(400, message) from exc
        if not math.isfinite(number) or not low <= number <= high:
            raise AppError(400, message)
        result[field_name] = f"{number:g}"


def normalize_profile(
    value: dict[str, Any],
    *,
    validate_timezone: bool = False,
    previous: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy and normalize known profile fields without mutating the input.

    ``validate_timezone`` selects strict mode, which rejects bad input; otherwise
    stored legacy values are tolerated. ``previous`` is the stored profile: a
    number field that equals it is not re-validated.
    """
    result = dict(DEFAULT_PROFILE)
    for key in result:
        if key in value:
            result[key] = "" if value[key] is None else str(value[key]).strip()[:4000]
    result["timezone"] = timezone_name(result.get("timezone"), strict=validate_timezone)
    if result["training_model"] not in TRAINING_MODELS:
        if validate_timezone and result["training_model"]:
            raise AppError(
                400,
                "Unbekanntes Trainingsmodell; erlaubt: " + ", ".join(TRAINING_MODELS),
            )
        result["training_model"] = TRAINING_MODELS[0]
    _normalize_preferred_sources(result, strict=validate_timezone)
    _normalize_sex(result, strict=validate_timezone)
    _normalize_birth_year(result, strict=validate_timezone)
    _normalize_energy_goal(result, strict=validate_timezone)
    _normalize_learned_check(result, strict=validate_timezone)
    _normalize_first_threshold(result, strict=validate_timezone)
    _normalize_numbers(result, previous, strict=validate_timezone)
    if validate_timezone:  # a save keeps what is stored (#3640)
        for field_name in BODY_FIELDS:
            result[field_name] = (previous or {}).get(field_name, "")
    return result


def _origin_after_edit(
    profile: dict[str, Any], previous: Mapping[str, str]
) -> dict[str, Any]:
    """Tell where an edited first threshold comes from.

    A value the athlete changes or clears without naming its origin is their own
    entry (measured, or none when cleared), even if the previous value was an
    adopted estimate. A write that keeps the value, or names another origin,
    keeps what it says.
    """
    new = first_threshold_bpm(profile.get(FIRST_THRESHOLD_FIELD))
    if new == first_threshold_bpm(previous.get(FIRST_THRESHOLD_FIELD)):
        return profile
    named = str(profile.get(FIRST_THRESHOLD_ORIGIN_FIELD) or "")
    if named != previous.get(FIRST_THRESHOLD_ORIGIN_FIELD, ""):
        return profile
    return {
        **profile,
        FIRST_THRESHOLD_ORIGIN_FIELD: "" if new is None else ORIGIN_MEASURED,
    }


def audit_view(stored: Mapping[str, Any]) -> dict[str, str]:
    """The profile as change history projects it, legacy percent included.

    Rows recorded before #3915 were hashed with ``first_threshold_pct`` and its
    origin; a stored legacy percent stays in the view so they still match.
    """
    value = normalize_profile(dict(stored))
    percent = first_threshold_percent(stored.get(LEGACY_PERCENT_FIELD))
    if LEGACY_PERCENT_FIELD in stored:
        value[LEGACY_PERCENT_FIELD] = "" if percent is None else f"{percent:g}"
    if percent is not None and not value[FIRST_THRESHOLD_FIELD]:
        value[FIRST_THRESHOLD_ORIGIN_FIELD] = (
            stored.get(FIRST_THRESHOLD_ORIGIN_FIELD) or ORIGIN_MEASURED
        )
    return value


def _legacy_percent_day(db: Any) -> date | None:
    """The day the legacy percent of the first threshold was last changed, if recorded."""
    rows = db.execute(
        "SELECT created_at, diff FROM change_history WHERE entity_type=? "
        "AND entity_id=? ORDER BY created_at DESC, rowid DESC",
        ("profile", "profile"),
    ).fetchall()
    for row in rows:
        try:
            fields = json.loads(row["diff"] or "{}").get("fields") or {}
            if LEGACY_PERCENT_FIELD in fields:
                return date.fromisoformat(str(row["created_at"])[:10])
        except AttributeError, TypeError, ValueError:
            continue
    return None


class ProfileService:
    """Own profile reads, writes, audit history, and cache invalidation."""

    def __init__(
        self,
        manager: Any,
        profile_repository: Any,
        key_value_repository: Any,
        *,
        on_location_changed: Callable[..., Any],
        mutation_coordinator: Any,
        run_lthr_on: Callable[[date | None], float | None] = lambda _day: None,
    ):
        self.mutation_coordinator = mutation_coordinator
        self._manager = manager
        self._profile_repository = profile_repository
        self._key_value_repository = key_value_repository
        self._on_location_changed = on_location_changed
        self._run_lthr_on = run_lthr_on
        # The run threshold lookup reads the athlete's local date, which reads
        # this profile: a legacy conversion never starts a second one (#3915).
        self._converting = threading.local()

    def _stored(self, db: Any) -> dict[str, Any]:
        """The stored profile JSON as written, or an empty one when unreadable."""
        payload = self._profile_repository.get(db)
        try:
            stored = json.loads(payload or "{}")
        except TypeError, json.JSONDecodeError:
            return {}
        return stored if isinstance(stored, dict) else {}

    def get_from_db(self, db: Any) -> dict[str, str]:
        return normalize_profile(self._with_legacy_bpm(self._stored(db), db))

    def _with_legacy_bpm(self, stored: dict[str, Any], db: Any) -> dict[str, Any]:
        """A profile stored before #3915 read with its first threshold in bpm.

        Its percent of LTHR becomes bpm with the run threshold HR in effect on
        the day the percent was last changed (change history), else today. The
        read writes nothing; the next save stores the bpm and drops the percent.
        A read inside the threshold lookup itself sees no first threshold.
        """
        percent = first_threshold_percent(stored.get(LEGACY_PERCENT_FIELD))
        if percent is None or first_threshold_bpm(stored.get(FIRST_THRESHOLD_FIELD)):
            return stored
        if getattr(self._converting, "active", False):
            return stored
        self._converting.active = True
        try:
            day = _legacy_percent_day(db)
            lthr = (self._run_lthr_on(day) if day else None) or self._run_lthr_on(None)
        finally:
            self._converting.active = False
        bpm = None if not lthr else first_threshold_bpm(round(percent * lthr / 100))
        return {**stored, FIRST_THRESHOLD_FIELD: "" if bpm is None else f"{bpm:g}"}

    def _kept_legacy(
        self,
        stored: Mapping[str, Any],
        normalized: Mapping[str, str],
        shown: str = "",
    ) -> dict[str, Any]:
        """``normalized`` as stored: a legacy percent no bpm replaced yet is kept.

        Without a known run threshold HR the percent cannot become bpm; it stays
        in the stored JSON (not in the profile) until a bpm is written (#3915).
        A bpm the profile ``shown`` before that the save empties is a clear, so
        the percent goes too.
        """
        if (
            normalized[FIRST_THRESHOLD_FIELD]
            or shown
            or first_threshold_percent(stored.get(LEGACY_PERCENT_FIELD)) is None
        ):
            return dict(normalized)
        return {
            **normalized,
            LEGACY_PERCENT_FIELD: stored[LEGACY_PERCENT_FIELD],
            FIRST_THRESHOLD_ORIGIN_FIELD: stored.get(FIRST_THRESHOLD_ORIGIN_FIELD)
            or ORIGIN_MEASURED,
        }

    def get(self) -> dict[str, str]:
        with self._manager.reader() as db:
            return self.get_from_db(db)

    def first_threshold_percent(self) -> float | None:
        """The stored first threshold as percent of today's run threshold HR."""
        bpm = first_threshold_bpm(self.get().get(FIRST_THRESHOLD_FIELD))
        lthr = self._run_lthr_on(None)
        return None if bpm is None or not lthr else percent_of_lthr(bpm, lthr)

    def revision(self) -> str | None:
        """Identify the latest committed profile history entry for export ordering."""
        with self._manager.reader() as db:
            row = db.execute(
                "SELECT id FROM change_history WHERE entity_type=? AND entity_id=? ORDER BY rowid DESC LIMIT 1",
                ("profile", "profile"),
            ).fetchone()
            return str(row["id"]) if row else None

    def save_in_transaction(
        self,
        profile: dict[str, Any],
        db: Any,
        *,
        already_normalized: bool = False,
    ) -> dict[str, str]:
        previous = self.get_from_db(db)
        normalized = (
            dict(profile)
            if already_normalized
            else normalize_profile(
                profile,
                validate_timezone=True,
                previous=previous,
            )
        )
        stored = self._stored(db)
        kept = self._kept_legacy(
            stored, normalized, previous.get(FIRST_THRESHOLD_FIELD, "")
        )
        self._profile_repository.set(db, json.dumps(kept, ensure_ascii=False))
        self._on_location_changed(
            previous,
            normalized,
            repository=self._key_value_repository,
            db=db,
        )
        # Recorded as the history's current view rebuilds it, so a kept legacy
        # percent still matches for undo (Gitar, #3915).
        change_history.record_change(
            db, "profile", "profile", "update", audit_view(stored), audit_view(kept)
        )
        return normalized

    def restore_in_transaction(
        self,
        db: Any,
        current: dict[str, Any] | None,
        target: dict[str, Any] | None,
    ) -> dict[str, str]:
        restored = dict(current or DEFAULT_PROFILE)
        restored.update(target or DEFAULT_PROFILE)
        if LEGACY_PERCENT_FIELD in (target or {}):
            # A state from before #3915: its percent (or none), not the current
            # bpm, is restored.
            restored[FIRST_THRESHOLD_FIELD] = (target or {}).get(
                FIRST_THRESHOLD_FIELD, ""
            )
        normalized = normalize_profile(self._with_legacy_bpm(restored, db))
        previous = self.get_from_db(db)
        kept = self._kept_legacy(restored, normalized)
        self._profile_repository.set(db, json.dumps(kept, ensure_ascii=False))
        self._on_location_changed(
            previous,
            normalized,
            repository=self._key_value_repository,
            db=db,
        )
        return normalized

    def save(self, profile: dict[str, Any]) -> dict[str, str]:
        """Replace the whole profile; callers must send every field they keep."""
        submitted = profile
        # Reject bad input before waiting for the coordinator; the check inside the
        # transaction below is the authoritative one.
        normalize_profile(submitted, validate_timezone=True, previous=self.get())
        with self.mutation_coordinator, self._manager.unit_of_work() as db:
            previous = self.get_from_db(db)
            if FIRST_THRESHOLD_ORIGIN_FIELD not in submitted:
                # A replacement that does not mention the origin keeps the stored one.
                submitted = {
                    **submitted,
                    FIRST_THRESHOLD_ORIGIN_FIELD: previous[
                        FIRST_THRESHOLD_ORIGIN_FIELD
                    ],
                }
            normalized = normalize_profile(
                _origin_after_edit(submitted, previous),
                validate_timezone=True,
                previous=previous,
            )
            return self.save_in_transaction(normalized, db, already_normalized=True)

    def patch(self, changes: dict[str, Any]) -> dict[str, str]:
        """Apply only the submitted fields onto the profile stored in this transaction."""
        if not isinstance(changes, dict):
            raise AppError(400, "Die Profiländerung muss ein Objekt sein.")
        with self.mutation_coordinator, self._manager.unit_of_work() as db:
            current = self.get_from_db(db)
            merged = _origin_after_edit({**current, **changes}, current)
            return self.save_in_transaction(merged, db)

    def set_first_threshold(self, bpm: str, origin: str) -> dict[str, str]:
        """Store a first threshold (bpm) with its origin, as one profile change."""
        with self.mutation_coordinator, self._manager.unit_of_work() as db:
            current = self.get_from_db(db)
            return self.save_in_transaction(
                {
                    **current,
                    FIRST_THRESHOLD_FIELD: bpm,
                    FIRST_THRESHOLD_ORIGIN_FIELD: origin,
                },
                db,
            )
