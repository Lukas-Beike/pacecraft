"""Drinking and carbohydrates for a long planned run or ride (#2804).

The rules live in ``backend.nutrition.fuelling``; this module gathers the local
inputs: the stored sessions of the last year with Garmin's sweat-loss estimate,
their stored activity weather, the cached forecast of the planned day and the
resolved body weight. It reads only; nothing is fetched or stored.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any, Final

from backend import change_history
from backend.canonical.activity_summary import summary_duration_s, summary_is_indoor
from backend.canonical.vocabulary import ActivitySessionState
from backend.db import canonical_store
from backend.db.repositories import NutritionRepository
from backend.errors import AppError
from backend.nutrition.fuelling import (
    FLUID_POSITION_STAND,
    POSITION_STAND,
    carbohydrate_per_hour,
    drinking,
    sweat_rate,
    weight_block,
)
from backend.nutrition.models import SESSION_MEAL_TYPES

LONG_SESSION_S: Final = 3600
WINDOW_DAYS: Final = 365
SPORTS: Final = frozenset({"run", "ride"})
_INVALID = "Ungültige Angaben für die Verpflegungsempfehlung."


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def sweat_samples(
    summaries: Iterable[dict[str, Any]], sport: str, indoor: bool
) -> list[tuple[str, float]]:
    """``(session id, L/h)`` of the sessions of this sport and setting."""
    samples = []
    for summary in summaries:
        if summary.get("sport") != sport or summary_is_indoor(summary) != indoor:
            continue
        water = _number(summary.get("water_estimated_ml"))
        # Garmin's estimate covers the whole activity, stops included.
        seconds = _number(summary.get("elapsed_duration_s")) or _number(
            summary_duration_s(summary)
        )
        if water is not None and seconds is not None:
            samples.append((str(summary.get("id")), water / 1000 / (seconds / 3600)))
    return samples


def forecast_temperature(days: Iterable[dict[str, Any]], day: str) -> float | None:
    """The middle of the forecast low and high of ``day``."""
    for row in days:
        if row.get("date") == day:
            low, high = row.get("temperature_min"), row.get("temperature_max")
            if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                return round((low + high) / 2, 1)
    return None


def session_fuelling(
    indoor: bool,
    duration_s: float,
    samples: list[tuple[float, float | None]],
    forecast_c: float | None,
    weight: dict[str, Any] | None,
) -> dict[str, Any]:
    """The recommendation for a long run or ride from its local inputs."""
    resolved = weight_block(weight)
    rate = sweat_rate(samples, None if indoor else forecast_c)
    drink = drinking(
        rate["l_per_h"] if rate else None,
        duration_s,
        None if resolved is None else resolved["kg"],
    )
    return {
        "status": "ready",
        "duration_s": int(duration_s),
        "forecast_c": None if indoor else forecast_c,
        "sweat_rate": rate,
        "drinking": drink,
        "drinking_source": FLUID_POSITION_STAND,
        "carbohydrates": carbohydrate_per_hour(duration_s),
        "carbohydrate_source": POSITION_STAND,
        "weight": resolved,
    }


def _request(query: dict[str, list[str]]) -> tuple[str, bool, str, float]:
    def first(name: str) -> str:
        return str((query.get(name) or [""])[0]).strip()

    sport, environment, day = first("sport"), first("environment"), first("date")
    try:
        date.fromisoformat(day)
        duration_s = float(first("duration_s"))
    except ValueError as exc:
        raise AppError(400, _INVALID, reason="invalid_session_fuelling") from exc
    if not 0 < duration_s <= 24 * 3600 or not sport or len(sport) > 40:
        raise AppError(400, _INVALID, reason="invalid_session_fuelling")
    return sport, summary_is_indoor({"environment": environment}), day, duration_s


def read_session_fuelling(
    summaries_between: Callable[[date, date], list[dict[str, Any]] | None],
    session_temperatures: Callable[[list[str]], dict[str, float]],
    forecast_days: Callable[[], list[dict[str, Any]]],
    body_weight: Callable[[], dict[str, Any] | None],
    today: Callable[[], date],
    query: dict[str, list[str]],
) -> dict[str, Any]:
    """The recommendation for a planned unit described by ``query``."""
    sport, indoor, day, duration_s = _request(query)
    # Below an hour or for another sport there is nothing to read.
    if sport not in SPORTS or duration_s < LONG_SESSION_S:
        return {"status": "not_long"}
    end = today()
    rates = sweat_samples(
        summaries_between(end - timedelta(days=WINDOW_DAYS), end) or [], sport, indoor
    )
    temperatures = {} if indoor else session_temperatures([sid for sid, _ in rates])
    samples = [(rate, temperatures.get(sid)) for sid, rate in rates]
    forecast_c = None if indoor else forecast_temperature(forecast_days(), day)
    return session_fuelling(indoor, duration_s, samples, forecast_c, body_weight())


# --- Intake that belongs to a session (#3234) --------------------------------

# A pre-session entry attaches to a start at most this long after it.
PRE_SESSION_WINDOW: Final = timedelta(hours=4)
# Carbohydrates are notable below 4/5 of the advised lower bound (more than 20 % under).
NOTABLE_SHARE: Final = (4, 5)
SESSION_QUESTION: Final = "Zu welcher Einheit gehört das?"


def wall_clock(text: Any) -> datetime:
    """A local time as a naive wall-clock value, dropping any UTC offset.

    Session windows are naive local times, while a diary time written from the
    athlete's clock may carry its offset; both are the same local wall clock.
    """
    return datetime.fromisoformat(str(text)).replace(tzinfo=None)


def session_window(session: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """``(start, end)`` of a session on the naive local wall clock, else None."""
    try:
        start = wall_clock(session["started_at_local"])
    except KeyError, ValueError:
        return None
    seconds = _number(session.get("elapsed_duration_s")) or summary_duration_s(session)
    return start, start + timedelta(seconds=seconds or 0)


def session_brief(session: dict[str, Any]) -> dict[str, Any]:
    """The few fields a diary card and a Coach question show of a session."""
    return {
        "id": session["id"],
        "sport": session.get("sport"),
        "started_at_local": session.get("started_at_local"),
        "elapsed_duration_s": _number(session.get("elapsed_duration_s")),
    }


def active_sessions(
    db: Any, summaries: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The summaries of active sessions; hidden, duplicate and deleted ones read as absent."""
    rows = list(summaries)
    states = canonical_store.activity_sessions_by_ids(db, [row["id"] for row in rows])
    return [
        row
        for row in rows
        if states.get(row["id"], {}).get("state") == ActivitySessionState.ACTIVE.value
    ]


def day_candidates(
    sessions: Iterable[dict[str, Any]], day: str
) -> list[dict[str, Any]]:
    """Sessions whose window overlaps the local ``day``; a midnight crossing counts for both days."""
    begin = datetime.fromisoformat(day)
    end = begin + timedelta(days=1)
    found = []
    for session in sessions:
        window = session_window(session)
        if window and window[0] < end and (window[1] > begin or window[0] >= begin):
            found.append((window[0], session))
    return [session for _start, session in sorted(found, key=lambda item: item[0])]


def attach(
    meal_type: str,
    logged_at: str,
    time_known: bool,
    candidates: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """``(session id, candidates to ask about)`` for one slot entry; never a guess.

    One candidate attaches. Several attach only with a known time: a during
    entry to the session containing it, a pre entry to the first start within
    four hours after it, each only when unique.
    """
    if len(candidates) == 1:
        return str(candidates[0]["id"]), []
    if len(candidates) > 1 and time_known:
        moment = wall_clock(logged_at)
        windows = [(session, session_window(session)) for session in candidates]
        if meal_type == "during_session":
            hits = [s for s, w in windows if w and w[0] <= moment <= w[1]]
        else:
            ahead = [
                (w[0], s)
                for s, w in windows
                if w and moment <= w[0] <= moment + PRE_SESSION_WINDOW
            ]
            first = min((start for start, _s in ahead), default=None)
            hits = [s for start, s in ahead if start == first]
        if len(hits) == 1:
            return str(hits[0]["id"]), []
    return None, [session_brief(session) for session in candidates]


def session_intake(
    session: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Facts on the intake attached to one session; unknown values stay None, never 0.

    ``notable`` is true only when the carbohydrate rate lies more than 20 %
    below the advised lower bound; the none and mouth-rinse steps have no gram
    rate and are never checked.
    """
    window = session_window(session)
    seconds = None if window is None else (window[1] - window[0]).total_seconds()
    hours = seconds / 3600 if seconds else None
    during = [e for e in entries if e["meal_type"] == "during_session"]
    carbs = _sum_known(e.get("carbs_g") for e in during)
    # An entry without ml counts as 0 ml, e.g. a gel; the report names how many
    # had none (athlete decision 2026-11-16, #3234).
    volume = float(sum(e.get("volume_ml") or 0.0 for e in during)) if during else None
    advised = carbohydrate_per_hour(seconds)
    low = (advised or {}).get("low_g")
    notable = bool(
        carbs is not None
        and hours
        and low
        and carbs * NOTABLE_SHARE[1] < NOTABLE_SHARE[0] * low * hours
    )
    return {
        "session_id": session["id"],
        "duration_s": seconds,
        "during": {
            "entries": len(during),
            "carbs_g": carbs,
            "carbs_g_per_h": None if carbs is None or not hours else carbs / hours,
            "advised": advised,
            "notable": notable,
            "volume_ml": volume,
            "ml_per_h": None if volume is None or not hours else volume / hours,
            "entries_without_volume": sum(e.get("volume_ml") is None for e in during),
        },
        "before": [
            {"carbs_g": e.get("carbs_g"), "minutes_before": _minutes_before(window, e)}
            for e in entries
            if e["meal_type"] == "pre_session"
        ],
    }


def reattach_entries(
    db: Any,
    days: Iterable[date],
    sessions: Callable[[date, date], Iterable[dict[str, Any]]],
    stamp: str,
) -> int:
    """Fill the empty session of slot entries near ``days`` after sessions were stored.

    An unattended write: it only fills an empty ``session_id`` by the attach rules
    and skips every entry whose session the athlete or Coach set or cleared. Each
    fill is a change-history record of its own. Returns the number of entries
    attached.
    """
    days = list(days)
    if not days:
        return 0
    repository = NutritionRepository(lambda: stamp)
    first, last = min(days) - timedelta(days=1), max(days) + timedelta(days=1)
    open_entries = [
        entry
        for entry in repository.list_by_range(db, first.isoformat(), last.isoformat())
        if entry["meal_type"] in SESSION_MEAL_TYPES
        and not entry["session_id"]
        and not entry["session_chosen"]
    ]
    if not open_entries:
        return 0
    active = active_sessions(db, sessions(first - timedelta(days=1), last))
    attached = 0
    for entry in open_entries:
        found, _ask = attach(
            entry["meal_type"],
            entry["logged_at"],
            entry["logged_time_known"],
            day_candidates(active, entry["meal_date"]),
        )
        if found is None:
            continue
        updated = repository.update(db, entry["id"], {**entry, "session_id": found})
        change_history.record_change(
            db,
            "nutrition_entry",
            entry["id"],
            "update",
            entry,
            updated,
            source="session_refresh",
        )
        attached += 1
    return attached


def _sum_known(values: Iterable[Any]) -> float | None:
    """The sum, or None when there is no value or one is unknown."""
    numbers = list(values)
    if not numbers or any(value is None for value in numbers):
        return None
    return float(sum(numbers))


def _minutes_before(
    window: tuple[datetime, datetime] | None, entry: dict[str, Any]
) -> float | None:
    if window is None or not entry.get("logged_time_known", True):
        return None
    return (window[0] - wall_clock(entry["logged_at"])).total_seconds() / 60
