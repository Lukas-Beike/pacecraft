"""Backfill Garmin wellness history once, one bounded window per job.

The ``wellness_backfill`` chain walks back from the automatic refresh window to
the history floor, like the activity backfill, but reads only sleep, HRV, daily
summaries (with their resting heart rate), Body Battery reports and weigh-ins.
It adds what is not stored and never deletes:

* an element whose day is already stored stays as it is, except a stored sleep
  night without night values, which the full night element of the same night
  replaces (athlete decision 2026-11-06), so it gains SpO2, respiration and
  sleeping heart rate;
* a Body Battery report of a past day replaces a stored one that differs: the
  morning read stores the partial report of the check-in day, and the backfill
  window holds only completed days, so an identical rerun writes nothing;
* the weigh-ins merge into the stored weight envelope, the stored readings win;
* nothing is pruned in a window, so no element or observation is retired.

A complete window moves the ``wellness_history`` cursor to its first day; the
startup scheduler resumes below it after a restart.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import date, timedelta
from typing import Any, Final, Protocol

from backend.canonical.section_items import SectionItem, split_section
from backend.canonical.vocabulary import GARMIN_ID
from backend.db import garmin_store
from backend.errors import AppError
from backend.providers.garmin import merge_weight_snapshots
from backend.providers.garmin_fields import (
    GARMIN_ACTIVE_KCAL_ALIASES,
    GARMIN_DATE_ALIASES,
    GARMIN_TOTAL_KCAL_ALIASES,
    first_present,
    flatten_daily_summary,
)
from backend.providers.garmin_wellness_history import (
    WellnessSourcePaused,
    has_night_values,
)
from backend.sync.garmin_snapshot_writer import GarminSnapshotWriter
from backend.sync.jobs import WELLNESS_HISTORY_CURSOR
from backend.sync.provider_table import GARMIN_NOT_CONFIGURED_MESSAGE

ITEM_SOURCES: Final = ("sleep", "hrv", "daily_stats", "resting_hr", "body_battery")
# Ended days whose stored daily calories the diagnostics count (#3157).
CALORIE_DAYS: Final = 90
WELLNESS_SOURCES: Final = frozenset((*ITEM_SOURCES, "weight"))
# A backfill chunk waits this long for a running Garmin sync before it fails.
LOCK_WAIT_SECONDS: Final = 120.0


class WellnessHistoryReader(Protocol):
    def fetch_wellness_history(
        self, start: date, end: date, *, needs_night: Callable[[str], bool]
    ) -> dict[str, Any]: ...


class SyncCoordination(Protocol):
    def operation(self) -> AbstractContextManager[Any]: ...

    def acquire(self, *, timeout: float | None = None) -> bool: ...

    def release(self) -> None: ...


@contextmanager
def coordinated_guard(coordination: SyncCoordination) -> Iterator[None]:
    """Hold the Garmin coordination lock, waiting ``LOCK_WAIT_SECONDS`` for a sync.

    The wellness chunk holds it across its read and write; the stream backfill
    holds it per read and per write.
    """
    with coordination.operation():
        if not coordination.acquire(timeout=LOCK_WAIT_SECONDS):
            raise AppError(
                503, "Garmin wird bereits aktualisiert.", reason="provider_busy"
            )
        try:
            yield
        finally:
            coordination.release()


class CursorWriter(Protocol):
    def update_cursor(
        self,
        provider: str,
        stream: str,
        cursor: str,
        high_water_mark: str | None = None,
    ) -> None: ...


class GarminWellnessHistoryService:
    """Read one wellness history window and add what is not stored yet."""

    def __init__(
        self,
        *,
        reader: WellnessHistoryReader,
        database_manager: Any,
        snapshot_writer: GarminSnapshotWriter,
        sync_state_repository: CursorWriter,
        coordination: SyncCoordination,
        utc_now: Callable[[], str],
        configured: Callable[[], bool],
    ) -> None:
        self._reader = reader
        self._database_manager = database_manager
        self._snapshot_writer = snapshot_writer
        self._sync_state = sync_state_repository
        self._coordination = coordination
        self._utc_now = utc_now
        self._configured = configured

    def available(self) -> bool:
        """Return whether a real Garmin read is configured (never in fixture mode)."""
        return self._configured()

    def backfill(self, days: int, end: date) -> dict[str, Any]:
        """Backfill ``days`` days ending on ``end``; return the job result.

        A paused source or a retryable provider error fails the job, so the
        worker retries it later and the cursor stays where it is.
        """
        if not self._configured():
            raise AppError(503, GARMIN_NOT_CONFIGURED_MESSAGE, reason="not_configured")
        start = end - timedelta(days=max(1, days) - 1)
        try:
            with coordinated_guard(self._coordination):
                payload = self._reader.fetch_wellness_history(
                    start, end, needs_night=self._needs_night
                )
                fetched_at = self._utc_now()
                written = self._write(payload, fetched_at)
        except WellnessSourcePaused as paused:
            raise AppError(
                429,
                "Garmin pausiert nach wiederholten Fehlern.",
                reason="rate_limited",
            ) from paused
        self._sync_state.update_cursor(
            GARMIN_ID, WELLNESS_HISTORY_CURSOR, start.isoformat(), fetched_at
        )
        pagination = payload["provider_sync"]["pagination"]
        return {
            "status": "ok",
            "synced_at": fetched_at,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "skipped_sources": list(payload["skipped_sources"]),
            "window_records": sum(
                int(details.get("records") or 0) for details in pagination.values()
            ),
            "written": written,
        }

    def _needs_night(self, day: str) -> bool:
        """A night needs its full element unless the stored one has night values."""
        with self._database_manager.reader() as db:
            return not has_night_values(garmin_store.read_item(db, "sleep", day))

    def _write(self, payload: Mapping[str, Any], fetched_at: str) -> dict[str, int]:
        """Write the new elements in one transaction; return their count per source."""
        with self._database_manager.unit_of_work() as db:
            sections: dict[str, Any] = {}
            for source in ITEM_SOURCES:
                records = payload.get(source)
                if not isinstance(records, list):
                    continue
                kept = [
                    item.value
                    for item in split_section(records)
                    if _insertable(db, source, item)
                ]
                if kept:
                    sections[source] = kept
            weight = _merged_weight(db, payload.get("weight"))
            if weight is not None:
                sections["weight"] = weight
            if sections:
                self._snapshot_writer.write_refresh(
                    db,
                    fetched_at=fetched_at,
                    sections=sections,
                    view={},
                    windows={},
                    allowed=WELLNESS_SOURCES,
                )
        return {
            source: len(value) if source != "weight" else 1
            for source, value in sections.items()
        }


def _calorie_fields(row: Mapping[str, Any]) -> int:
    """How many of total and active kcal a daily summary holds as a positive number."""
    flat = flatten_daily_summary(row)
    return sum(
        isinstance(value := first_present(flat, aliases), int | float) and value > 0
        for aliases in (GARMIN_TOTAL_KCAL_ALIASES, GARMIN_ACTIVE_KCAL_ALIASES)
    )


def daily_calorie_fields(db: Any, today: date) -> dict[str, int]:
    """Per ended day of the last ``CALORIE_DAYS``: total and active kcal stored (0-2).

    A day without a stored daily summary is absent (#3157).
    """
    first = (today - timedelta(days=CALORIE_DAYS)).isoformat()
    last = (today - timedelta(days=1)).isoformat()
    rows = garmin_store.read_sections(db, ["daily_stats"], start=first, end=last)
    fields: dict[str, int] = {}
    for row in rows.get("daily_stats") or []:
        if not isinstance(row, Mapping):
            continue
        day = str(first_present(row, GARMIN_DATE_ALIASES) or "")[:10]
        if first <= day <= last:
            fields[day] = max(fields.get(day, 0), _calorie_fields(row))
    return fields


def summarize_daily_calories(db: Any, today: date) -> dict[str, int]:
    """Counts only, no dates or values, for the diagnostics (#3157).

    A day is ``complete`` with total and active kcal, ``partial`` with one,
    ``without_calories`` with a stored summary holding neither, and
    ``without_summary`` when none is stored.
    """
    counts = list(daily_calorie_fields(db, today).values())
    return {
        "days": CALORIE_DAYS,
        "complete": counts.count(2),
        "partial": counts.count(1),
        "without_calories": counts.count(0),
        "without_summary": CALORIE_DAYS - len(counts),
    }


def _merged_weight(db: Any, weigh_ins: Any) -> list[Any] | None:
    """Return the stored envelope plus new readings, or None when nothing is new.

    The stored envelope is the incoming side of the merge, so its readings win.
    """
    if not weigh_ins:
        return None
    stored = garmin_store.read_view(db, ["weight"]).get("weight")
    merged = merge_weight_snapshots(stored, weigh_ins)
    # The same merge without new weigh-ins is the stored stock in envelope form.
    if stored and merged == merge_weight_snapshots(stored, None):
        return None
    return merged


def _insertable(db: Any, source: str, item: SectionItem) -> bool:
    """Return whether an element is new, a full night for a stored bare night, or a
    completed Body Battery report for a stored partial one."""
    stored = garmin_store.read_item(db, source, item.key)
    if stored is None:
        return True
    if source == "body_battery":
        return stored != item.value
    return (
        source == "sleep"
        and has_night_values(item.value)
        and not has_night_values(stored)
    )
