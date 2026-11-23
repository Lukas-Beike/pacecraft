"""Which skin-temperature key names Garmin's sleep answers carry (#4017).

Observation only: the names of keys matching ``skin`` or ``temp`` are kept, never
a value and never any other provider text. A path is the keys from the answer
down to the match (``dailySleepDTO.someKey``), restricted to ``[A-Za-z0-9_.]``,
at most 120 characters, and the stored set holds at most 50 paths per collector.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any, Final

from backend.canonical.section_items import record_day
from backend.db import garmin_store
from backend.db.repositories import KeyValueRepository

STATE_KEY: Final = "garmin_sleep_temperature_keys"
MAX_PATHS: Final = 50
MAX_PATH_LENGTH: Final = 120
MAX_DEPTH: Final = 3
STORED_DAYS: Final = 90
STORED: Final = "stored"
# The collector of each external-call operation that receives a sleep answer.
COLLECTORS: Final = {
    "morning_sleep": "get_sleep_data",
    "sleep_night": "get_sleep_data",
    "sleep": "get_sleep_daily",
}
_NAME: Final = re.compile(r"skin|temp", re.IGNORECASE)
_SAFE_PATH: Final = re.compile(f"[A-Za-z0-9_.]{{1,{MAX_PATH_LENGTH}}}")
_LOGGER = logging.getLogger("intervals_coach")


def temperature_key_paths(payload: object) -> set[str]:
    """The safe key paths of one sleep answer whose last key matches skin or temp."""
    found: set[str] = set()

    def walk(node: Mapping[str, Any], prefix: tuple[str, ...]) -> None:
        for key, value in node.items():
            path = (*prefix, str(key))
            if _NAME.search(str(key)):
                text = ".".join(path)
                if _SAFE_PATH.fullmatch(text):
                    found.add(text)
            if isinstance(value, Mapping) and len(path) < MAX_DEPTH:
                walk(value, path)

    if isinstance(payload, Mapping):
        walk(payload, ())
    return found


def _night_paths(night: object) -> tuple[str | None, set[str]]:
    """The day and the paths of one answer: a night or one row of a range."""
    day = record_day(night)
    if day is None and isinstance(night, Mapping):
        day = record_day(night.get("dailySleepDTO"))
    return day, temperature_key_paths(night)


def _nights(result: object) -> list[object]:
    return list(result) if isinstance(result, list) else [result]


def _extend(entry: dict[str, Any], day: str) -> bool:
    """Count ``day`` when it lies outside the days seen, so a re-read adds nothing."""
    first: str | None = entry.get("first")
    last: str = entry.get("last") or day
    entry["first"], entry["last"] = min(first or day, day), max(last, day)
    return first is None or day < first or day > last


def merge_observation(
    state: dict[str, Any], collector: str, nights: list[tuple[str, set[str]]]
) -> None:
    """Add the scanned ``(day, paths)`` nights of ``collector`` to ``state``.

    ``nights`` of a path is a lower bound: a night inside the days already seen
    is not counted again, so repeated syncs never inflate it.
    """
    entry = state.setdefault(collector, {"scanned": {}, "paths": {}})
    for day, paths in nights:
        _extend(entry["scanned"], day)
        for path in sorted(paths):
            known = entry["paths"].get(path)
            if known is None:
                if len(entry["paths"]) >= MAX_PATHS:
                    continue
                known = entry["paths"][path] = {"nights": 0}
            if _extend(known, day):
                known["nights"] += 1


class SleepKeyRecorder:
    """Keep the key paths of every sleep answer the Garmin sync receives."""

    def __init__(
        self,
        database_manager: Any,
        key_values: KeyValueRepository,
        today: Callable[[], date],
    ) -> None:
        self._database_manager = database_manager
        self._key_values = key_values
        self._today = today

    def observe(self, operation: str, result: object) -> None:
        collector = COLLECTORS.get(operation)
        if collector is None:
            return
        try:
            fallback = self._today().isoformat()
            nights = []
            for night in _nights(result):
                day, paths = _night_paths(night)
                nights.append((day or fallback, paths))
            with self._database_manager.unit_of_work() as db:
                state = _load(self._key_values.get(db, STATE_KEY))
                merge_observation(state, collector, nights)
                self._key_values.set(db, STATE_KEY, json.dumps(state, sort_keys=True))
        except Exception as error:  # noqa: BLE001 - an observation never fails a sync
            _LOGGER.warning(
                "Garmin sleep key names not stored",
                extra={
                    "event": "garmin_sleep_keys_failed",
                    "context": {"error_type": type(error).__name__},
                },
            )


def _load(text: str | None) -> dict[str, Any]:
    try:
        state = json.loads(text or "{}")
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _stored_nights(db: Any, today: date) -> list[tuple[str, set[str]]]:
    """The paths in the ``dailySleepDTO`` of the nights stored in the last 90 days."""
    start = (today - timedelta(days=STORED_DAYS)).isoformat()
    rows = garmin_store.read_sections(db, ["sleep"], start=start).get("sleep")
    nights: list[tuple[str, set[str]]] = []
    for row in rows if isinstance(rows, list) else []:
        dto = row.get("dailySleepDTO") if isinstance(row, Mapping) else None
        if isinstance(dto, Mapping):
            day = record_day(row) or record_day(dto) or today.isoformat()
            found = {f"dailySleepDTO.{p}" for p in temperature_key_paths(dto)}
            nights.append((day, {p for p in found if _safe(p)}))
    return nights


def _collector_view(entry: Any) -> tuple[dict[str, int], list[str]] | None:
    """The path counts and scanned days of one stored collector entry."""
    if not isinstance(entry, dict) or not isinstance(entry.get("scanned"), dict):
        return None
    scanned = entry["scanned"]
    days = [str(scanned.get("first") or ""), str(scanned.get("last") or "")]
    known_paths = entry.get("paths")
    items = known_paths.items() if isinstance(known_paths, dict) else ()
    counts = {
        path: int(known.get("nights") or 0)
        for path, known in sorted(items)
        if _safe(path) and isinstance(known, dict)
    }
    return counts, [day for day in days if day]


def summarize_sleep_temperature_keys(
    key_values: KeyValueRepository, db: Any, today: date
) -> dict[str, Any]:
    """Key paths with the nights that carried them, never a value (#4017).

    ``collectors`` holds the paths the fetches saw and, as ``stored``, the ones in
    the ``dailySleepDTO`` of the nights stored in the last 90 days.
    """
    state = _load(key_values.get(db, STATE_KEY))
    nights = _stored_nights(db, today)
    if nights:
        merge_observation(state, STORED, nights)
    collectors: dict[str, dict[str, int]] = {}
    days: list[str] = []
    for name, entry in sorted(state.items()):
        view = _collector_view(entry)
        if view is not None:
            collectors[name] = view[0]
            days += view[1]
    return {
        "collectors": collectors,
        "date_range": {"from": min(days), "to": max(days)} if days else None,
        "none_found": bool(days) and not any(collectors.values()),
    }


def _safe(path: str) -> bool:
    return _SAFE_PATH.fullmatch(path) is not None
