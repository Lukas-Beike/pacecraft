"""Mirror the athlete's entered weight and body fat to Garmin Connect (#3403).

The one write the athlete approved for body data (athlete decision 2026-11-17):
saving, changing or removing an entered weight, an entered body fat or a
skinfold measurement in the app writes that day's Garmin body-composition entry
at once. Nothing is written for a day the athlete did not enter in the app, and
nothing is backfilled.

One entry per day: weight, plus the day's body fat (the skinfolds' first, else
the entered percentage). A body fat without an entered weight goes with the
day's Garmin-measured weight (option A, ``FAT_NEEDS_ENTERED_WEIGHT``); without
any weight that day nothing is sent and the receipt says so, because Garmin's
only body-composition write (the SDK's FIT upload) needs a weight.

Garmin's upload returns no entry id. The entry is uploaded at the fixed local
time ``UPLOAD_TIME`` of its day, then read back from the day's weigh-ins and
linked by its ``samplePk`` only when timestamp, weight and body fat all match
(``remote_links``, entity ``body_composition``, local id = the ISO day), so a
weigh-in the athlete made in Garmin is never linked or deleted. A change deletes
the linked entry and uploads the new one; a removal deletes it. An accepted
upload that cannot be read back yet is only searched again, never uploaded twice.

A failed write leaves the local save standing and the link ``pending``; the
next Garmin refresh retries pending days (``retry_pending``), at most
``RETRY_BATCH`` per refresh, and not while Garmin's rate-limit pause holds.
Garmin's own text never reaches the athlete, the logs or the diagnostics; the
counts in ``COUNTS_KEY`` hold numbers only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

from backend.canonical.vocabulary import (
    MetricKind,
    ProviderId,
    RemoteEntityKind,
    RemoteLinkState,
    SourceKind,
)
from backend.db.remote_links import RemoteLink, RemoteLinkRepository
from backend.db.repositories import KeyValueRepository
from backend.sync.garmin import GARMIN_CAPABILITY_PAUSE_SECONDS

GARMIN: Final = ProviderId.GARMIN.value
KIND: Final = RemoteEntityKind.BODY_COMPOSITION
# Capability source of the body write: three 429s in a row pause it (garmin.py).
CAPABILITY_SOURCE: Final = "body_push"
UPLOAD_TIME: Final = time(12, 0)
RETRY_BATCH: Final = 5
# Pending days older than this are no longer retried (no backfill, #3403).
RETRY_DAYS: Final = 30
# Option B of the athlete question (#3403): send a body fat only with a weight
# the athlete entered that day.
FAT_NEEDS_ENTERED_WEIGHT: Final = False
COUNTS_KEY: Final = "garmin_body_push_counts"
# Receipt outcomes: written, removed, nothing to do, retried later, not sendable,
# Garmin not connected for writes.
SENT, REMOVED, UNCHANGED, PENDING, NEEDS_WEIGHT, OFF = (
    "sent",
    "removed",
    "unchanged",
    "pending",
    "needs_weight",
    "off",
)
_UPLOADED: Final = "uploaded"  # remote_external_id: accepted, not read back yet
_LOGGER = logging.getLogger("intervals_coach")
_WEIGHT_TOLERANCE_KG: Final = 0.05
_FAT_TOLERANCE_PCT: Final = 0.05
_ENTERED_SQL: Final = (
    "SELECT value FROM observations WHERE metric = ? AND provider IS NULL "
    "AND source_kind = ? AND observed_on = ? AND state = 'active' "
    "ORDER BY id DESC LIMIT 1"
)
_GARMIN_SQL: Final = (
    "SELECT value FROM observations WHERE metric = ? AND provider = ? "
    "AND observed_on = ? AND state = 'active' AND sport IS NULL "
    "ORDER BY observed_at DESC, id DESC LIMIT 1"
)


@dataclass(frozen=True, slots=True)
class BodyEntry:
    """What Garmin should hold for one day."""

    weight_kg: float
    fat_pct: float | None

    def fingerprint(self) -> str:
        fat = "" if self.fat_pct is None else f"{self.fat_pct:.1f}"
        return f"{self.weight_kg:.1f}|{fat}"


class _Rejected(Exception):
    """Garmin refused the upload; the day stays pending."""


class GarminBodyPushService:
    """Write, change or remove the Garmin body entry of one entered day."""

    def __init__(
        self,
        database_manager: Any,
        sdk_session: Callable[[], AbstractContextManager[Any]],
        sync_state: Any,
        skinfold_fat: Callable[[Any, date], float | None],
        write_ready: Callable[[], bool],
        now: Callable[[], datetime],
    ) -> None:
        self._database_manager = database_manager
        self._sdk_session = sdk_session
        # ``GarminSyncStateService``: the rate-limit pause and capability counts.
        self._sync_state = sync_state
        self._skinfold_fat = skinfold_fat
        self._write_ready = write_ready
        self._now = now
        self._links = RemoteLinkRepository()
        self._key_values = KeyValueRepository(lambda: self._now().isoformat())

    def push(self, day: date, *, retry: bool = False) -> str:
        """Bring Garmin's entry of ``day`` in line with the local values."""
        if not self._write_ready():
            return OFF
        with self._database_manager.reader() as db:
            wanted, needs_weight = self._wanted(db, day)
            link = self._links.get(db, KIND, day.isoformat(), GARMIN)
        target = wanted.fingerprint() if wanted else None
        settled = link is not None and link.sync_state is RemoteLinkState.SYNCED
        if (settled and link.baseline_hash == target) or (link is None and not wanted):
            return NEEDS_WEIGHT if needs_weight else UNCHANGED
        if self._paused():
            self._settle(day, link, None, wanted, failed=True, retry=retry)
            return PENDING
        remote_id = link.remote_id if link else None
        try:
            with self._sdk_session() as sdk:
                if remote_id is not None and not (
                    link and link.baseline_hash == target
                ):
                    _delete(sdk, day, remote_id)
                    remote_id = None
                if wanted is not None and remote_id is None:
                    uploaded = bool(link and link.remote_external_id == target)
                    remote_id = self._upload(sdk, day, wanted, search_only=uploaded)
        except _Unlinked:
            self._settle(day, link, None, wanted, failed=True, retry=retry, uploaded=True)
            return PENDING
        except Exception as error:  # noqa: BLE001 - provider boundary, text never kept
            self._record_failure(error)
            self._settle(day, link, remote_id, wanted, failed=True, retry=retry)
            return PENDING
        self._settle(day, link, remote_id, wanted, failed=False, retry=retry)
        if needs_weight:
            return NEEDS_WEIGHT
        return SENT if wanted else REMOVED

    def retry_pending(self) -> int:
        """Retry the pending days of the last ``RETRY_DAYS``; returns how many were tried."""
        if not self._write_ready() or self._paused():
            return 0
        oldest = (self._now().date() - timedelta(days=RETRY_DAYS)).isoformat()
        with self._database_manager.reader() as db:
            days = [
                link.local_id
                for link in self._links.list_for_provider(db, GARMIN, KIND)
                if link.sync_state is RemoteLinkState.PENDING
                and link.local_id >= oldest
            ][-RETRY_BATCH:]
        for day in days:
            if self.push(date.fromisoformat(day), retry=True) == PENDING:
                break  # one failure ends the run, so an outage is not hammered
        return len(days)

    def _wanted(self, db: Any, day: date) -> tuple[BodyEntry | None, bool]:
        """The day's entry and whether a body fat waits for a weight."""
        weight = _value(db, _ENTERED_SQL, MetricKind.WEIGHT, day)
        fat = self._skinfold_fat(db, day)
        if fat is None:
            fat = _value(db, _ENTERED_SQL, MetricKind.BODY_FAT, day)
        if weight is None and fat is not None and not FAT_NEEDS_ENTERED_WEIGHT:
            weight = _value(db, _GARMIN_SQL, MetricKind.WEIGHT, day, provider=GARMIN)
        if weight is None:
            return None, fat is not None
        return BodyEntry(round(weight, 1), None if fat is None else round(fat, 1)), False

    def _paused(self) -> bool:
        limited = self._sync_state.last_rate_limit()
        if limited is not None:
            if limited.tzinfo is None:
                limited = limited.replace(tzinfo=UTC)
            if self._now() - limited < timedelta(seconds=GARMIN_CAPABILITY_PAUSE_SECONDS):
                return True
        return not self._sync_state.capability_allowed(CAPABILITY_SOURCE)

    def _record_failure(self, error: BaseException) -> None:
        try:
            self._sync_state.record_capability_failure(CAPABILITY_SOURCE, error)
        except Exception:  # noqa: BLE001 - keep the local outcome
            _LOGGER.warning(
                "Garmin body push state not recorded",
                extra={"event": "garmin_body_push_state_failed"},
            )
        _LOGGER.warning(
            "Garmin body push failed",
            extra={"event": "garmin_body_push_failed", "error_type": type(error).__name__},
        )

    def _upload(self, sdk: Any, day: date, wanted: BodyEntry, *, search_only: bool) -> str:
        if search_only:
            # An earlier upload was accepted: find it, never upload it twice.
            found = _find(sdk, day, wanted)
            if found is None:
                raise _Unlinked
            return found
        response = sdk.add_body_composition(
            timestamp=datetime.combine(day, UPLOAD_TIME).isoformat(),
            weight=wanted.weight_kg,
            percent_fat=wanted.fat_pct,
        )
        if _rejected(response):
            raise _Rejected
        found = _find(sdk, day, wanted)
        if found is None:
            raise _Unlinked
        return found

    def _settle(
        self,
        day: date,
        link: RemoteLink | None,
        remote_id: str | None,
        wanted: BodyEntry | None,
        *,
        failed: bool,
        retry: bool,
        uploaded: bool = False,
    ) -> None:
        """Store the day's link and count the outcome, in one transaction."""
        target = wanted.fingerprint() if wanted else None
        stamp = self._now().isoformat()
        with self._database_manager.unit_of_work() as db:
            if not failed and remote_id is None:
                self._links.delete(db, KIND, day.isoformat(), GARMIN)
            elif not failed:
                self._links.upsert(
                    db,
                    RemoteLink(KIND, day.isoformat(), GARMIN, RemoteLinkState.SYNCED,
                               remote_id=remote_id, baseline_hash=target,
                               last_synced_at=stamp),
                )  # fmt: skip
            else:
                kept = remote_id if remote_id is not None else None
                self._links.upsert(
                    db,
                    RemoteLink(KIND, day.isoformat(), GARMIN, RemoteLinkState.PENDING,
                               remote_id=kept,
                               remote_external_id=target if uploaded else (
                                   link.remote_external_id if link else None),
                               baseline_hash=(link.baseline_hash if link and kept else None),
                               last_synced_at=link.last_synced_at if link else None),
                )  # fmt: skip
            name = "failed" if failed else ("sent" if wanted else "removed")
            self._count(db, [name, *(["retried"] if retry else [])])

    def _count(self, db: Any, names: list[str]) -> None:
        try:
            counts = json.loads(self._key_values.get(db, COUNTS_KEY) or "{}")
        except TypeError, ValueError:
            counts = {}
        if not isinstance(counts, dict):
            counts = {}
        for name in names:
            counts[name] = int(counts.get(name) or 0) + 1
        self._key_values.set(db, COUNTS_KEY, json.dumps(counts, sort_keys=True))


class _Unlinked(Exception):
    """Garmin accepted the upload, but the entry cannot be read back yet."""


def _value(
    db: Any, sql: str, kind: MetricKind, day: date, *, provider: str | None = None
) -> float | None:
    params: tuple[Any, ...] = (
        (kind.value, provider, day.isoformat())
        if provider is not None
        else (kind.value, SourceKind.ATHLETE_ENTERED.value, day.isoformat())
    )
    row = db.execute(sql, params).fetchone()
    if row is None:
        return None
    value = row["value"] if isinstance(row, Mapping) else row[0]
    return float(value)


def _delete(sdk: Any, day: date, remote_id: str) -> None:
    """Delete the linked entry; one Garmin no longer has counts as deleted."""
    try:
        sdk.delete_weigh_in(remote_id, day.isoformat())
    except Exception as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status != 404 and type(error).__name__ != "GarminConnectNotFoundError":
            raise


def _find(sdk: Any, day: date, wanted: BodyEntry) -> str | None:
    """The samplePk of the day's one entry this push uploaded, else None.

    It must carry the upload time and the same weight and body fat; two such
    entries are ambiguous and link neither.
    """
    payload = sdk.get_daily_weigh_ins(day.isoformat())
    items = payload.get("dateWeightList") if isinstance(payload, Mapping) else None
    found = [
        str(item["samplePk"])
        for item in items or []
        if isinstance(item, Mapping)
        and item.get("samplePk") is not None
        and _matches(item, day, wanted)
    ]
    return found[0] if len(found) == 1 else None


def _matches(item: Mapping[str, Any], day: date, wanted: BodyEntry) -> bool:
    local_ms = int(datetime.combine(day, UPLOAD_TIME, tzinfo=UTC).timestamp() * 1000)
    if _number(item.get("date")) != local_ms:
        return False
    grams = _number(item.get("weight"))
    if grams is None or abs(grams / 1000 - wanted.weight_kg) > _WEIGHT_TOLERANCE_KG:
        return False
    fat = _number(item.get("bodyFat"))
    if wanted.fat_pct is None or fat is None:
        return wanted.fat_pct is None and fat is None
    return abs(fat - wanted.fat_pct) <= _FAT_TOLERANCE_PCT


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _rejected(response: Any) -> bool:
    """A FIT import Garmin answered with failures and no success."""
    result = response.get("detailedImportResult") if isinstance(response, Mapping) else None
    if not isinstance(result, Mapping):
        return False
    failures, successes = result.get("failures"), result.get("successes")
    return bool(failures) and not successes


def summarize_body_push(db: Any) -> dict[str, Any]:
    """Counts only (#3403): writes, removals, failures and retries so far, and
    the days now linked, pending, and uploaded but not yet read back."""
    try:
        counts = json.loads(KeyValueRepository(lambda: "").get(db, COUNTS_KEY) or "{}")
    except TypeError, ValueError:
        counts = {}
    if not isinstance(counts, dict):
        counts = {}
    links = RemoteLinkRepository().list_for_provider(db, GARMIN, KIND)
    pending = [link for link in links if link.sync_state is RemoteLinkState.PENDING]
    return {
        **{name: int(counts.get(name) or 0) for name in ("sent", "removed", "failed", "retried")},
        "linked_days": len(links) - len(pending),
        "pending_days": len(pending),
        "unlinked_uploads": sum(1 for link in pending if link.remote_external_id),
    }
