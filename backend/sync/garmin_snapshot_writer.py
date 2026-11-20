"""The one write path for the Garmin collection payload.

Garmin sync, the Morning Body Battery refresh and the targeted nutrition and
analysis refreshes store what they fetched through ``GarminSnapshotWriter``. Inside
the caller's unit of work it upserts one raw-layer row per changed element, the
canonical activities and observations, the coverage summary and the small
``garmin_state`` value together, so a failure in any of them rolls back all.
A write carries a delta: sections it does not mention stay as they are.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, tzinfo
from typing import Any, Final

from backend.athlete.local_date import iso_date_prefix
from backend.canonical.garmin_payload import (
    ACTIVITIES_SECTION,
    ITEM_SECTIONS,
    REPLACE_SECTIONS,
)
from backend.canonical.section_items import record_id, split_section
from backend.canonical.vocabulary import ProviderId
from backend.db import garmin_store
from backend.db.repositories import KeyValueRepository
from backend.providers.canonical_plans import garmin_payload_retrieved_at
from backend.providers.garmin_fields import flatten_daily_summary
from backend.providers.garmin_weight_walk import WEIGHT_SECTIONS
from backend.sync.canonical_coverage_store import (
    CanonicalCoverageStore,
    garmin_ingest_coverage,
)
from backend.sync.canonical_ingest import (
    CanonicalIngestCounters,
    DeletionWindow,
    fetch_deletion_window,
    ingest_garmin_payload,
)

# The sections a targeted analysis refresh may write.
ANALYSIS_SOURCES: Final = frozenset(
    {
        "activities",
        "training_load_activities",
        "daily_training_status",
        "training_load_balance",
        "cycling_ftp",
        "running_threshold",
        "race_predictions",
        "personal_records",
        "max_metrics",
        "cycling_ftp_history",
        "endurance_score",
        "running_tolerance",
        "training_readiness_history",
        "weekly_training_status",
        "sleep",
        "hrv",
        "readiness",
        "daily_stats",
        "resting_hr",
    }
)
NUTRITION_SOURCES: Final = frozenset({"daily_stats", "weight"})
# The values of the day the change check stores by day (#2303).
DAYTIME_SOURCES: Final = frozenset({"daily_stats", "body_battery"})
# The daily summaries: merged field by field, never emptied or pruned (#3157).
DAILY_SUMMARY_SECTION: Final = "daily_stats"


class GarminSnapshotWriter:
    def __init__(
        self,
        key_values: KeyValueRepository,
        zone_provider: Callable[[], tzinfo | None],
    ) -> None:
        self._coverage = CanonicalCoverageStore(key_values)
        self._zone_provider = zone_provider

    def write(
        self,
        db: Any,
        payload: Mapping[str, Any],
        *,
        deletion_window: DeletionWindow | None = None,
    ) -> CanonicalIngestCounters:
        """Write the delta a sync collected: element rows, state and coverage.

        ``deletion_window`` holds the inner days of a complete activity read; a
        stored activity on those days that the payload lacks is marked deleted.
        """
        zone = self._zone_provider() or UTC
        payload = _keeping_daily_values(db, payload)
        counters = ingest_garmin_payload(
            db, payload, zone=zone, deletion_window=deletion_window
        )
        if garmin_payload_retrieved_at(payload, zone=zone) is not None:
            for section in REPLACE_SECTIONS:
                self._replace(db, section, payload.get(section))
        weight_window = _weight_read_window(payload)
        if weight_window is not None:
            # A merged weigh-in answer replaces its read window (#3754).
            self._prune(db, "weight", payload["weight"], weight_window)
        self._patch_state(db, payload)
        coverage = garmin_ingest_coverage(db, payload, zone=zone)
        if coverage is not None and not _current_activity_read(
            payload, coverage.retrieved_at, zone
        ):
            # Only a complete activity read that reached today moves the activity
            # fetch time; a historical chunk or a partial read keeps the last one.
            previous = self._coverage.load(
                db, ProviderId.GARMIN, zone=zone, allow_outdated=True
            )
            coverage = replace(
                coverage,
                activities_retrieved_at=None
                if previous is None
                else previous.activities_retrieved_at,
            )
        self._coverage.persist(db, coverage, zone=zone)
        return counters

    def write_refresh(
        self,
        db: Any,
        *,
        fetched_at: str,
        sections: Mapping[str, Any],
        view: Mapping[str, Any],
        windows: Mapping[str, tuple[date, date]],
        allowed: frozenset[str],
    ) -> CanonicalIngestCounters:
        """Write the sections one targeted refresh changed, without restamping others.

        ``sections`` holds the merged value of each refreshed section; ``windows``
        names the sections whose stored elements inside the day range are replaced
        by it (a complete read), so an element the provider no longer returns goes;
        for the activities that is a tombstone inside the range's inner days.
        The state keys of ``view`` are patched into the stored state.
        """
        if not set(sections).issubset(allowed):
            raise ValueError("Unsupported Garmin refresh source")
        zone = self._zone_provider() or UTC
        sections = _keeping_daily_values(db, sections)
        activity_window = windows.get(ACTIVITIES_SECTION)
        counters = ingest_garmin_payload(
            db,
            {"synced_at": fetched_at, **sections},
            zone=zone,
            deletion_window=fetch_deletion_window(*activity_window)
            if activity_window is not None
            else None,
        )
        for section, value in sections.items():
            window = windows.get(section)
            if window is not None:
                self._prune(db, section, value, window)
            elif section in REPLACE_SECTIONS:
                self._replace(db, section, value)
        self._patch_state(db, view)
        return counters

    @staticmethod
    def _replace(db: Any, section: str, value: Any) -> None:
        """Drop the stored elements a non-empty current answer no longer holds.

        A dated answer replaces only the stored days inside its own day span (and
        the stored undated elements when it holds one), so earlier days stay and
        every re-projection still emits them (#3729). An answer without a dated
        element, and the gear inventory, replace the whole section. A gear
        inventory is only present after a successful read, so an empty list is a
        real answer there and clears the stored items.
        """
        if not value and not (section == "gear" and value == []):
            return
        items = split_section(value)
        days = sorted(item.observed_on for item in items if item.observed_on)
        keep = {record_id(section, item.key) for item in items}
        if section == "gear" or not days:
            garmin_store.prune_section(db, section, keep)
            return
        garmin_store.prune_section(
            db,
            section,
            keep,
            start=days[0],
            end=days[-1],
            undated=any(item.observed_on is None for item in items),
        )

    @staticmethod
    def _prune(db: Any, section: str, value: Any, window: tuple[date, date]) -> None:
        # A stored daily summary stays when a read leaves its day out (#3157).
        if section in ITEM_SECTIONS and section not in (
            ACTIVITIES_SECTION,
            DAILY_SUMMARY_SECTION,
        ):
            garmin_store.prune_section(
                db,
                section,
                _identifiers(section, value),
                start=window[0].isoformat(),
                end=window[1].isoformat(),
                undated=section in WEIGHT_SECTIONS,
            )
            if section in WEIGHT_SECTIONS:
                garmin_store.retire_window_orphans(
                    db,
                    section,
                    _identifiers(section, value),
                    window[0].isoformat(),
                    window[1].isoformat(),
                )

    @staticmethod
    def _patch_state(db: Any, payload: Mapping[str, Any]) -> None:
        state = garmin_store.read_state(db)
        state.update(garmin_store.split_state(payload))
        garmin_store.write_state(db, state, now=datetime.now(UTC).isoformat())


def complete_activity_read_window(
    payload: Mapping[str, Any],
) -> tuple[date, date] | None:
    """Return this run's activity read window, or None unless the read was complete.

    Only a run whose activity list came back for every date window, without an
    error, rate limit, capability pause or cancel, vouches for its ``read_window``.
    """
    provider_sync = payload.get("provider_sync") or {}
    stats = (provider_sync.get("pagination") or {}).get("activities") or {}
    window = provider_sync.get("read_window") or {}
    if stats.get("complete") is not True:
        return None
    try:
        start = date.fromisoformat(iso_date_prefix(str(window["start"])))
        end = date.fromisoformat(iso_date_prefix(str(window["end"])))
    except KeyError, TypeError, ValueError:
        return None
    return start, end


def _weight_read_window(payload: Mapping[str, Any]) -> tuple[date, date] | None:
    """Return the read window of this run's merged weigh-in answer, if any.

    The sync drops the window with an answer without weigh-ins (#3718), so a
    failed or empty read retires nothing.
    """
    window = (payload.get("provider_sync") or {}).get("weight_window")
    if not payload.get("weight") or not isinstance(window, Mapping):
        return None
    try:
        return date.fromisoformat(str(window["start"])), date.fromisoformat(
            str(window["end"])
        )
    except KeyError, ValueError:
        return None


def _current_activity_read(
    payload: Mapping[str, Any], retrieved_at: datetime | None, zone: tzinfo
) -> bool:
    """Return True for a complete activity list read whose window reaches today."""
    window = complete_activity_read_window(payload)
    if window is None or retrieved_at is None:
        return False
    return window[1] >= retrieved_at.astimezone(zone).date()


def _keeping_daily_values(db: Any, sections: Mapping[str, Any]) -> Mapping[str, Any]:
    """Merge each incoming daily summary over its stored one, field by field.

    A read never replaces a stored value with an empty one: a missing, null or
    empty field, or a zero over a stored positive number, keeps the stored
    value; nested records merge the same way (#3157, as #3128 for streams).
    """
    rows = sections.get(DAILY_SUMMARY_SECTION)
    if not isinstance(rows, list):
        return sections
    merged = []
    for item in split_section(rows):
        stored = garmin_store.read_item(db, DAILY_SUMMARY_SECTION, item.key)
        incoming = item.value
        if isinstance(stored, Mapping) and isinstance(incoming, Mapping):
            incoming = _merge_daily(stored, incoming)
        merged.append(incoming)
    return {**sections, DAILY_SUMMARY_SECTION: merged}


def _merge_daily(
    stored: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge one day; a nested incoming value also updates its stored flat alias.

    Flat keys win when a summary is read (``flatten_daily_summary``), so a stale
    flat value kept from an earlier per-day read would hide a newer nested one.
    """
    merged = _merge_fields(stored, incoming)
    for alias, value in flatten_daily_summary(incoming).items():
        stale = alias not in incoming and alias in merged
        if stale and not _empty_over(value, merged[alias]):
            merged[alias] = value
    return merged


def _merge_fields(
    stored: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(stored)
    for key, value in incoming.items():
        old = stored.get(key)
        if isinstance(value, Mapping) and isinstance(old, Mapping):
            merged[key] = _merge_fields(old, value)
        elif not _empty_over(value, old):
            merged[key] = value
    return merged


def _empty_over(value: Any, old: Any) -> bool:
    """Whether ``value`` would only erase ``old``: empty, or zero over a positive."""
    if value is None or value in ("", [], {}):
        return old is not None
    numbers = (int, float)
    zero_over_positive = (
        isinstance(value, numbers) and isinstance(old, numbers) and value == 0 < old
    )
    return zero_over_positive and not isinstance(value, bool)


def _identifiers(section: str, value: Any) -> set[str]:
    return {record_id(section, item.key) for item in split_section(value)}
