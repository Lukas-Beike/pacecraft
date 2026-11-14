"""Race-day forecasts at competition locations (#2803).

Only the weather sync job fetches them, never a request: per run at most
``MAX_LOCATIONS`` distinct places within ``BUDGET_SECONDS``, through the weather
owner's Open-Meteo client (geocoding included). The race-day row is stored per
competition id, location and event date under one key; entries of deleted,
moved or past competitions are dropped on the next run. Locations never reach
the logs.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from backend.errors import AppError
from backend.weather.projection import WEATHER_FORECAST_DAYS, daily_summary

CACHE_KEY: Final = "competition_weather"
MAX_LOCATIONS: Final = 5
BUDGET_SECONDS: Final = 60.0
CACHE_SECONDS: Final = 10800
_LOGGER = logging.getLogger("intervals_coach")


def _location(competition: dict[str, Any]) -> str:
    return " ".join(str(competition.get("location") or "").split())[:200]


def _day(competition: dict[str, Any]) -> str:
    return str(competition.get("event_date") or "")[:10]


def in_horizon(competition: dict[str, Any], today: date) -> bool:
    """Whether race day lies within the forecast days from ``today``."""
    try:
        event = date.fromisoformat(_day(competition))
    except ValueError:
        return False
    return today <= event < today + timedelta(days=WEATHER_FORECAST_DAYS)


def due(competitions: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Competitions with a location whose race day has a forecast, by date."""
    return sorted(
        (
            {"id": str(item.get("id")), "location": _location(item), "event_date": _day(item)}
            for item in competitions
            if _location(item) and in_horizon(item, today)
        ),
        key=lambda item: (item["event_date"], item["id"]),
    )


def _matches(entry: Any, item: dict[str, Any]) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("location") == item["location"]
        and entry.get("event_date") == item["event_date"]
    )


def entry_for(cache: dict[str, Any], competition: dict[str, Any]) -> dict[str, Any] | None:
    """The stored forecast of this competition's current location and day."""
    item = {"location": _location(competition), "event_date": _day(competition)}
    entry = cache.get(str(competition.get("id")))
    return entry if _matches(entry, item) else None


def _fresh(entry: Any, now: datetime) -> bool:
    try:
        fetched = datetime.fromisoformat(str(entry.get("fetched_at")))
    except AttributeError, TypeError, ValueError:
        return False
    return (now - fetched).total_seconds() < CACHE_SECONDS


def stale(cache: dict[str, Any], items: list[dict[str, Any]], now: datetime) -> bool:
    """Whether a due competition has no fresh forecast."""
    return any(
        not (_matches(cache.get(item["id"]), item) and _fresh(cache[item["id"]], now))
        for item in items
    )


def _fetched_entries(
    result: dict[str, Any], items: list[dict[str, Any]], fetched_at: str
) -> dict[str, dict[str, Any]]:
    rows = {row["date"]: row for row in daily_summary(result.get("forecast") or {})}
    place = result.get("location") or {}
    return {
        item["id"]: {
            **item,
            "status": "ready" if item["event_date"] in rows else "unavailable",
            "day": rows.get(item["event_date"]),
            "name": str(place.get("name") or "")[:200],
            "country": str(place.get("country") or "")[:100],
            "fetched_at": fetched_at,
        }
        for item in items
    }


def refreshed_cache(
    cache: dict[str, Any],
    items: list[dict[str, Any]],
    fetch: Callable[[str], dict[str, Any]],
    *,
    now: datetime,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The cache for ``items``: fresh entries kept, the rest fetched in budget.

    A place Open-Meteo cannot geocode is ``not_found``; another failure keeps
    an earlier entry of the same location and day, else it is ``unavailable``.
    """
    result = {
        item["id"]: cache[item["id"]]
        for item in items
        if _matches(cache.get(item["id"]), item)
    }
    places: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not _fresh(result.get(item["id"]), now):
            places.setdefault(item["location"].casefold(), []).append(item)
    deadline = clock() + BUDGET_SECONDS
    stamp = now.astimezone(UTC).isoformat()
    for group in list(places.values())[:MAX_LOCATIONS]:
        if clock() >= deadline:
            break
        try:
            result.update(_fetched_entries(fetch(group[0]["location"]), group, stamp))
        except AppError as exc:
            status = "not_found" if exc.status == 400 else "unavailable"
            _failed(result, group, status, stamp, exc)
        except Exception as exc:  # noqa: BLE001 - provider boundary; never fails the sync
            _failed(result, group, "unavailable", stamp, exc)
    return result


def _failed(
    result: dict[str, Any],
    group: list[dict[str, Any]],
    status: str,
    stamp: str,
    error: Exception,
) -> None:
    _LOGGER.warning(
        "Competition weather unavailable",
        extra={
            "event": "competition_weather_failed",
            "context": {"error_type": type(error).__name__, "competitions": len(group)},
        },
    )
    for item in group:
        previous = result.get(item["id"])
        if status == "unavailable" and previous and previous.get("status") == "ready":
            continue
        result[item["id"]] = {**item, "status": status, "fetched_at": stamp}


def race_day_weather(
    competition: dict[str, Any], cache: dict[str, Any], today: date
) -> dict[str, Any]:
    """The weather part of a race plan; statuses name what is missing."""
    if not _location(competition):
        return {"status": "no_location"}
    if not in_horizon(competition, today):
        return {"status": "not_in_horizon"}
    entry = entry_for(cache, competition)
    if entry is None:
        return {"status": "pending"}
    return {
        key: entry.get(key)
        for key in ("status", "day", "name", "country", "fetched_at")
        if entry.get(key) is not None
    }


class CompetitionWeather:
    """Read and refresh the stored race-day forecasts."""

    def __init__(
        self,
        database_manager: Any,
        key_values: Any,
        competitions: Callable[[], list[dict[str, Any]]],
        client_factory: Callable[[], Any],
        now: Callable[[], datetime],
        today: Callable[[], date],
    ) -> None:
        self._database_manager = database_manager
        self._key_values = key_values
        self._competitions = competitions
        self._client_factory = client_factory
        self._now = now
        self._today = today

    def _cache(self) -> dict[str, Any]:
        with self._database_manager.reader() as db:
            raw = self._key_values.get(db, CACHE_KEY)
        try:
            value = json.loads(raw or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def configured(self) -> bool:
        """Whether an upcoming competition has a location within the horizon."""
        return bool(due(self._competitions(), self._today()))

    def refresh_due(self) -> bool:
        return stale(self._cache(), due(self._competitions(), self._today()), self._now())

    def refresh(self) -> dict[str, Any]:
        """Fetch what is due; never raises, so the profile forecast is unaffected."""
        try:
            items = due(self._competitions(), self._today())
            cache = self._cache()
            fresh = refreshed_cache(cache, items, self._client_factory().fetch, now=self._now())
            if fresh != cache:
                with self._database_manager.unit_of_work() as db:
                    self._key_values.set(
                        db,
                        CACHE_KEY,
                        json.dumps(fresh, ensure_ascii=False, separators=(",", ":")),
                    )
        except Exception as exc:  # noqa: BLE001 - best effort beside the profile forecast
            _LOGGER.warning(
                "Competition weather refresh failed",
                extra={
                    "event": "competition_weather_failed",
                    "context": {"error_type": type(exc).__name__},
                },
            )
            return {"competitions": 0, "failed": True}
        return {"competitions": sum(1 for v in fresh.values() if v.get("status") == "ready")}

    def for_competition(self, competition: dict[str, Any]) -> dict[str, Any]:
        return race_day_weather(competition, self._cache(), self._today())
