"""The remaining notification types, each derived from durable state (#3914).

Every function maps state an owner already keeps to at most one lock-screen safe
``Notification``: a fixed German title and body without names, values, places,
provider error text or Coach content, an in-app route and a once-only key; the
"Heute" body alone names today's units (athlete, #4273). Where
several items of one kind are new at once, one notification stands for all of
them (the first key, the others as ``covers``). The functions read only; the
checks decide when to run them and ``PushService`` sends each key once.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from backend.activities.hr_cap import PERCENT_KEY, resolve_bpm, stored_cap
from backend.activities.workout_text import WorkoutTextError, structured_blocks
from backend.canonical.vocabulary import GARMIN_ID, WAHOO_ID, MetricKind, Sport
from backend.db import canonical_store, planning_store
from backend.planning.calendar import workout_is_rest
from backend.planning.library import library_workout_duration_minutes
from backend.planning.sport import planned_sport_environment
from backend.push.service import Notification
from backend.weather.recommendations import day_warning, is_outdoor_activity

_OVERVIEW: Final = "#plan/overview"
_CHECKIN: Final = "#plan/checkin"
# Local hours from which the evening and next-day events are sent.
MISSING_FEEDBACK_HOUR: Final = 20
WEATHER_TOMORROW_HOUR: Final = 18
# A session that started longer ago arrived by a backfill, not as news.
NEW_ACTIVITY_DAYS: Final = 2
_SESSION_ROWS: Final = 50
# The nightly copy is missed once the newest is older than a day and a half: a
# day alone would fire every night in the minutes before the copy is written.
BACKUP_STALE_HOURS: Final = 36.0
# Free space in the data directory below which the server needs attention.
LOW_DISK_BYTES: Final = 1 << 30
_SLEEP_METRICS: Final = (
    MetricKind.SLEEP_DURATION.value,
    MetricKind.SLEEP_SCORE.value,
)
_NIGHT_METRICS: Final = tuple(
    metric.value
    for metric in (
        MetricKind.SLEEP_DURATION,
        MetricKind.SLEEP_SCORE,
        MetricKind.HRV_RMSSD,
        MetricKind.HRV_SDNN,
        MetricKind.RESTING_HR,
    )
)


def _one(
    kind: str, keys: Iterable[str], title: str, body: str, url: str
) -> Notification | None:
    """One notification for every new key, None without one."""
    ordered = sorted(keys)
    if not ordered:
        return None
    first, *rest = ordered
    return Notification(kind, first, title, body, url, covers=tuple(rest))


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=UTC)


def coach_answer_event(client_turn_id: str) -> Notification:
    return Notification(
        "coach_answer",
        f"coach:{client_turn_id}",
        "Coach",
        "Deine Antwort ist da.",
        "#coach",
    )


def planned_units(db: Any, start: date, end: date) -> list[tuple[str, dict[str, Any]]]:
    """The local ids and payloads of the plan units from ``start`` to ``end``."""
    units = []
    rows = planning_store.planned_unit_rows_in_period(
        db, start.isoformat(), end.isoformat()
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except TypeError, ValueError:
            continue
        if (
            isinstance(payload, dict)
            and not payload.get("archived")
            and not payload.get("local_deleted")
        ):
            units.append((str(row["local_id"]), payload))
    return units


def morning_plan_event(
    units: list[tuple[str, dict[str, Any]]],
    today: date,
    lthr_for: Callable[[dict[str, Any]], float | None] = lambda unit: None,
) -> Notification | None:
    """Today's planned units that are no rest day, one line each, once a day (#4273).

    ``lthr_for`` gives the run threshold HR on a unit's date for its stored cap.
    """
    lines = [
        unit_line(unit, lthr_for(unit))
        for _, unit in units
        if str(unit.get("date")).startswith(today.isoformat())
        and not workout_is_rest(unit)
    ]
    if not lines:
        return None
    return Notification(
        "morning_plan",
        f"morning_plan:{today.isoformat()}",
        "Heute",
        "\n".join(lines),
        _OVERVIEW,
    )


def unit_line(unit: dict[str, Any], lthr: float | None) -> str:
    """``{title} · {duration} · {key target}``; a part the unit lacks is left out."""
    minutes = round(library_workout_duration_minutes(unit) or 0)
    parts = (
        str(unit.get("name") or "").strip() or "Einheit",
        f"{minutes} min" if minutes > 0 else "",
        _key_target(unit, lthr),
    )
    return " · ".join(part for part in parts if part)


def _key_target(unit: dict[str, Any], lthr: float | None) -> str:
    """The stored cap of an easy or long unit, else the first repeat block of the
    stored steps unless the title names it already; nothing for strength."""
    cap = stored_cap(unit)
    if cap is not None:
        bpm = resolve_bpm(cap[PERCENT_KEY], lthr)
        return "" if bpm is None else f"Puls bis {bpm}"
    if planned_sport_environment(unit)[0] is Sport.STRENGTH:
        return ""
    try:
        blocks = structured_blocks(str(unit.get("description") or ""))
    except WorkoutTextError:
        return ""
    block = next((item for item in blocks if item["repeat"] > 1), None)
    if block is None:
        return ""
    structure = f"{block['repeat']}×{_step_amount(block['steps'][0])}"
    return "" if _compact(structure) in _compact(str(unit.get("name"))) else structure


def _step_amount(step: dict[str, Any]) -> str:
    if "distance" in step:
        meters = float(step["distance"])
        # Mile and yard steps are stored in metres: round them for the line.
        if meters >= 1000:
            return f"{round(meters / 1000, 1):g} km".replace(".", ",")
        return f"{round(meters)} m"
    seconds = float(step["duration"])
    return f"{seconds / 60:g} min" if seconds % 60 == 0 else f"{seconds:g} s"


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text.casefold().replace("x", "×"))


def weather_event(
    units: list[tuple[str, dict[str, Any]]], days: Any, local: datetime
) -> Notification | None:
    """A planned outdoor unit today (from 07:00) or tomorrow (from 18:00) with a warning."""
    today = local.date().isoformat()
    tomorrow = (local.date() + timedelta(days=1)).isoformat()
    forecast = {
        str(row.get("date")): row
        for row in (days if isinstance(days, list) else [])
        if isinstance(row, dict)
    }
    keys = []
    for unit_id, unit in units:
        day = str(unit.get("date"))[:10]
        due = day == today or (day == tomorrow and local.hour >= WEATHER_TOMORROW_HOUR)
        if due and is_outdoor_activity(unit) and day_warning(forecast.get(day)):
            keys.append(f"weather:{unit_id}:{day}")
    return _one(
        "weather",
        keys,
        "Wetter",
        "Für eine geplante Einheit draußen gibt es einen Wetterhinweis.",
        _OVERVIEW,
    )


def new_sessions(db: Any, since: datetime, today: date) -> list[str]:
    """Ids of recent Garmin or Wahoo sessions stored after ``since``, newest first."""
    rows = db.execute(
        "SELECT activity_id, created_at, started_at FROM activity_sessions "
        "WHERE state = 'active' AND primary_provider IN (?, ?) "
        "ORDER BY created_at DESC, activity_id LIMIT ?",
        (GARMIN_ID, WAHOO_ID, _SESSION_ROWS),
    ).fetchall()
    earliest = today - timedelta(days=NEW_ACTIVITY_DAYS)
    found = []
    for row in rows:
        created, started = _moment(row["created_at"]), _moment(row["started_at"])
        if created and created > since and started and started.date() >= earliest:
            found.append(str(row["activity_id"]))
    return found


def session_groups(db: Any, session_ids: list[str]) -> dict[str, str]:
    """The sport group of each session (#4275), from its primary record's
    canonical sport: ``run``, ``ride``, ``strength`` or ``other``."""
    if not session_ids:
        return {}
    marks = ", ".join("?" for _ in session_ids)
    rows = db.execute(
        "SELECT s.activity_id, r.sport FROM activity_sessions s "
        "JOIN activity_records r ON r.activity_id = s.activity_id "
        "AND r.provider = s.primary_provider AND r.state = 'active' "
        # The earliest record wins, the last row of each session (#4275).
        f"WHERE s.activity_id IN ({marks}) ORDER BY r.started_at DESC",
        tuple(session_ids),
    ).fetchall()
    groups = {Sport.RUN.value: "run", Sport.RIDE.value: "ride"}
    groups[Sport.STRENGTH.value] = "strength"
    return {
        str(row["activity_id"]): groups.get(str(row["sport"]), "other") for row in rows
    }


def new_activity_event(session_ids: list[str]) -> Notification | None:
    """One notification for the newest of the new sessions; the others are covered."""
    if not session_ids:
        return None
    newest, *older = session_ids
    return Notification(
        "new_activity",
        f"new_activity:{newest}",
        "Neue Einheit",
        "Eine neue Einheit ist synchronisiert.",
        _OVERVIEW,
        covers=tuple(f"new_activity:{item}" for item in older),
    )


def morning_data_event(db: Any, today: date) -> Notification | None:
    """Tonight's sleep or HRV or resting heart rate values are stored."""
    if not canonical_store.observed_on_day(db, today.isoformat(), _NIGHT_METRICS):
        return None
    return Notification(
        "morning_data",
        f"morning_data:{today.isoformat()}",
        "Morgenwerte",
        "Die Werte der Nacht sind da.",
        "#analysis/recovery",
    )


def checkin_event(db: Any, today: date) -> Notification | None:
    """Tonight's sleep is stored and today has no check-in yet: once a day (#3953).

    The tap opens the check-in form of today (``#plan/checkin``).
    """
    day = today.isoformat()
    if not canonical_store.observed_on_day(db, day, _SLEEP_METRICS):
        return None
    if db.execute(
        "SELECT 1 FROM athlete_checkins WHERE checkin_date=?", (day,)
    ).fetchone():
        return None
    return Notification(
        "checkin", f"checkin:{day}", "Check-in", "Wie fühlst du dich heute?", _CHECKIN
    )


def maintenance_event(equipment: Any) -> Notification | None:
    """Active equipment at its maintenance interval; the key holds the last maintenance."""
    items = equipment.get("items") if isinstance(equipment, dict) else None
    keys = []
    for item in items if isinstance(items, list) else []:
        usage = item.get("usage")
        if item.get("status") != "active" or not isinstance(usage, dict):
            continue
        if usage.get("maintenance_due") is True:
            done = item.get("maintenance") or [{}]
            keys.append(f"maintenance:{item.get('id')}:{done[-1].get('date', 'none')}")
    return _one(
        "maintenance",
        keys,
        "Wartung fällig",
        "Ein Ausrüstungsteil hat sein Wartungsintervall erreicht.",
        "#more/equipment",
    )


def _record_keys(window: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    """Each record of Analyse › Rekorde with its own identity."""
    running = window.get("running")
    running = running if isinstance(running, dict) else {}
    groups = (
        ("power", window.get("power"), "duration_seconds"),
        ("speed", running.get("speed"), "duration_seconds"),
        ("distance", running.get("distance"), "distance_meters"),
        ("single", window.get("activity_records"), "field"),
    )
    for kind, entries, parameter in groups:
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                identity = (
                    f"{kind}:{entry.get('sport')}:{entry.get('environment')}"
                    f":{entry.get(parameter)}:{entry.get('activity_id')}"
                )
                yield identity, entry


def personal_best_event(
    records: Any, since_day: date, today: date
) -> Notification | None:
    """Records set since the subscription and recently, one key per record.

    Only a record of the last days counts, so an old record whose key aged out of
    the sent record is never announced again.
    """
    window = records.get("all_time") if isinstance(records, dict) else None
    if not isinstance(window, dict):
        return None
    first = max(since_day, today - timedelta(days=NEW_ACTIVITY_DAYS)).isoformat()
    keys = [
        f"record:{key}"
        for key, entry in _record_keys(window)
        if entry.get("status") == "ok" and str(entry.get("date"))[:10] >= first
    ]
    return _one(
        "personal_best",
        keys,
        "Neue Bestleistung",
        "Du hast eine neue Bestleistung.",
        "#analysis/records",
    )


# Per sport: the title's activity, the plural of the body's, the tapped Zonen & Schwellen sport (#4135).
_OFFER_SPORTS: Final = {
    "run": ("Laufen", "Läufen", "#more/profile/run"),
    "ride": ("Radfahren", "Fahrten", "#more/profile/ride"),
}


def threshold_offer_events(offers: Any, since_day: date) -> list[Notification]:
    """One notification per sport and origin for the open threshold offers (#4135).

    The offers are already without the dismissed ones. Only an offer whose newest
    data is younger than the subscription counts: one that was open before is no
    news. Each offer key is its own sent key, so a new value of a source notifies
    again and an unchanged one never does. The day comparison is strict, like
    ``personal_best_event``: data of the subscription day itself is no news. The
    offers are grouped per sport and origin (Garmin's own value or an estimate
    from the athlete's sessions), so a body names the origin of every key it
    announces. No value is named.
    """
    found: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for offer in offers if isinstance(offers, list) else []:
        if (
            isinstance(offer, dict)
            and offer.get("sport") in _OFFER_SPORTS
            and isinstance(offer.get("key"), str)
            # An undated offer is no news: "None" would sort after every date.
            and isinstance(offer.get("newest"), str)
            and offer["newest"][:10] > since_day.isoformat()
        ):
            garmin = offer.get("source") == GARMIN_ID
            found.setdefault((offer["sport"], garmin), []).append(offer)
    events = []
    for (sport, garmin), group in sorted(found.items()):
        activity, sessions, url = _OFFER_SPORTS[sport]
        origin = "Von Garmin" if garmin else f"Aus deinen letzten {sessions}"
        event = _one(
            "new_threshold",
            (f"threshold_offer:{offer['key']}" for offer in group),
            f"Neue Schwelle für {activity}",
            f"{origin} gibt es einen neuen Wert. Tippen zum Ansehen.",
            url,
        )
        if event is not None:
            events.append(event)
    return events


def missing_feedback_event(session_ids: Any, today: date) -> Notification | None:
    """A session of today still has no RPE: once a day."""
    if not session_ids:
        return None
    return Notification(
        "missing_feedback",
        f"missing_feedback:{today.isoformat()}",
        "Feedback",
        "Für eine Einheit von heute fehlt noch dein Feedback.",
        _OVERVIEW,
    )


def server_event(state: Any, today: date) -> Notification | None:
    """A failed or stale automatic backup or little free disk space; per problem and day."""
    state = state if isinstance(state, dict) else {}
    automatic = state.get("automatic")
    automatic = automatic if isinstance(automatic, dict) else {}
    newest = automatic.get("newest_age_hours")
    failed = automatic.get("last_failure_age_hours")
    free = state.get("free_bytes")
    problems = []
    if automatic.get("enabled"):
        # A failure counts until a copy newer than it exists.
        if failed is not None and (newest is None or failed < newest):
            problems.append("backup_failed")
        if newest is not None and newest > BACKUP_STALE_HOURS:
            problems.append("backup_stale")
    if isinstance(free, (int, float)) and free < LOW_DISK_BYTES:
        problems.append("disk_low")
    return _one(
        "server_backup",
        (f"server:{problem}:{today.isoformat()}" for problem in problems),
        "Server",
        "Backup oder Speicher braucht Aufmerksamkeit.",
        "#more/operations",
    )
