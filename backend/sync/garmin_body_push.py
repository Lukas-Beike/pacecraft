"""Mirror the athlete's entered weight and body fat to Garmin Connect (#3403).

The one write the athlete approved for body data (athlete decision 2026-11-17):
saving, changing or removing an entered weight, an entered body fat or a
skinfold measurement in the app writes that day's Garmin body-composition entry
at once. Nothing is written for a day the athlete did not enter in the app, and
nothing is backfilled.

One entry per day: weight, plus the day's body fat (the skinfolds' first, else
the entered percentage). A body fat without an entered weight goes with the
day's Garmin-measured weight (option A, ``FAT_NEEDS_ENTERED_WEIGHT``), never the
echo of the app's own upload; without any weight that day nothing is sent and
the receipt says so, because Garmin's only body-composition write (the SDK's
FIT upload) needs a weight.

Garmin's upload returns no entry id. The entry is uploaded at the fixed local
time ``UPLOAD_TIME`` of its day, then read back from the day's weigh-ins and
linked by its ``samplePk`` only when time, weight and body fat all match
(``remote_links``, entity ``body_composition``, local id = the ISO day), so a
weigh-in the athlete made in Garmin is never linked or deleted. Before an upload
the link records what is being sent (``remote_external_id``); an upload whose
outcome is unknown is therefore searched for, deleted or linked later, never
sent twice. A change deletes the linked entry and uploads the new one; a removal
deletes it. One push runs at a time.

A save does not wait for Garmin (#4267): its push runs on its own thread and
the save answers ``running`` unless the outcome is known locally at once; the
browser follows the day through ``push_state`` in the sync status.

A failed write leaves the local save standing and the link ``pending``; the
next Garmin refresh retries pending days (``retry_pending``), at most
``RETRY_BATCH`` per refresh, and not while Garmin's rate-limit pause holds.
Garmin's own text never reaches the athlete, the logs or the diagnostics; the
counts in ``COUNTS_KEY`` hold numbers only.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections import Counter
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
# An odd second, so a weigh-in the athlete types in Garmin never shares it.
UPLOAD_TIME: Final = time(12, 0, 17)
RETRY_BATCH: Final = 5
# Pending days older than this are no longer retried (no backfill, #3403).
RETRY_DAYS: Final = 30
# Option B of the athlete question (#3403): send a body fat only with a weight
# the athlete entered that day.
FAT_NEEDS_ENTERED_WEIGHT: Final = False
COUNTS_KEY: Final = "garmin_body_push_counts"
# Receipt outcomes: written, removed, nothing to do, retried later, not sendable,
# Garmin not connected for writes, still on its way (#4267).
SENT, REMOVED, UNCHANGED, PENDING, NEEDS_WEIGHT, OFF, RUNNING = (
    "sent",
    "removed",
    "unchanged",
    "pending",
    "needs_weight",
    "off",
    "running",
)
# Marks a sent fingerprint Garmin accepted: it is only searched for, never sent again.
ACCEPTED: Final = "accepted:"
_LOGGER = logging.getLogger("intervals_coach")
# One push at a time, so two saves of one day never upload twice.
_PUSH_LOCK = threading.Lock()
_LOCK_WAIT_S: Final = 60.0
# A save waits this long for an outcome known without Garmin (off, unchanged, a
# missing weight, the rate-limit pause); a push that calls Garmin answers running (#4267).
LOCAL_ANSWER_S: Final = 0.5
# The running save pushes per day; a restart forgets them with their threads,
# so the day then reads as its link's pending state.
_RUNNING_DAYS: Counter[str] = Counter()
_RUNNING_LOCK = threading.Lock()
_WEIGHT_TOLERANCE_KG: Final = 0.05
_FAT_TOLERANCE_PCT: Final = 0.05
_ENTERED_SQL: Final = (
    "SELECT value FROM observations WHERE metric = ? AND provider IS NULL "
    "AND source_kind = ? AND observed_on = ? AND state = 'active' "
    "ORDER BY id DESC LIMIT 1"
)
_GARMIN_WEIGHTS_SQL: Final = (
    "SELECT value FROM observations WHERE metric = ? AND provider = ? "
    "AND observed_on = ? AND state = 'active' AND sport IS NULL "
    "ORDER BY observed_at DESC, id DESC"
)


@dataclass(frozen=True, slots=True)
class BodyEntry:
    """What Garmin should hold for one day."""

    weight_kg: float
    fat_pct: float | None

    def fingerprint(self) -> str:
        fat = "" if self.fat_pct is None else f"{self.fat_pct:.1f}"
        return f"{self.weight_kg:.1f}|{fat}"

    @staticmethod
    def parse(fingerprint: str | None) -> BodyEntry | None:
        try:
            weight, fat = str(fingerprint).removeprefix(ACCEPTED).split("|")
            return BodyEntry(float(weight), float(fat) if fat else None)
        except ValueError:
            return None


@dataclass(slots=True)
class _Day:
    """Garmin's side of one day as far as this app knows it."""

    remote_id: str | None  # the linked entry
    baseline: str | None  # what the linked entry holds
    # An upload whose entry is not linked yet: its fingerprint, with ``ACCEPTED``
    # in front once Garmin accepted it.
    sending: str | None

    @staticmethod
    def of(link: RemoteLink | None) -> _Day:
        if link is None:
            return _Day(None, None, None)
        return _Day(link.remote_id, link.baseline_hash, link.remote_external_id)

    def settled_as(self, link: RemoteLink | None, target: str | None) -> bool:
        """Whether Garmin already holds ``target`` (None: nothing) for the day."""
        if link is None:
            return target is None
        return (
            link.sync_state is RemoteLinkState.SYNCED
            and self.baseline == target
            and self.sending is None
        )

    def own_weights(self) -> set[float]:
        entries = (BodyEntry.parse(self.baseline), BodyEntry.parse(self.sending))
        return {entry.weight_kg for entry in entries if entry is not None}


class _Rejected(Exception):
    """Garmin refused the upload; the day stays pending."""


class _Unlinked(Exception):
    """Garmin accepted the upload, but the entry cannot be read back yet."""


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
        start: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._database_manager = database_manager
        # Runs a save's push; a thread unless a test runs it in place.
        self._start = start or (
            lambda run: threading.Thread(
                target=run, name="garmin-body-push", daemon=True
            ).start()
        )
        self._sdk_session = sdk_session
        # ``GarminSyncStateService``: the rate-limit pause and capability counts.
        self._sync_state = sync_state
        self._skinfold_fat = skinfold_fat
        self._write_ready = write_ready
        self._now = now
        self._links = RemoteLinkRepository()
        self._key_values = KeyValueRepository(lambda: self._now().isoformat())

    def push(self, day: date, *, retry: bool = False) -> str:
        """Bring Garmin's entry of ``day`` in line with the local values.

        Never raises: the local save has already been committed. A save's push
        runs on its own thread; an outcome known within ``LOCAL_ANSWER_S`` is
        returned, else ``running`` while the day is in ``push_state``.
        """
        if retry:
            return self._guarded(day, retry=True)
        answers: queue.SimpleQueue[str] = queue.SimpleQueue()
        key = day.isoformat()

        def run() -> None:
            try:
                answers.put(self._guarded(day, retry=False))
            finally:
                _finish(key)

        with _RUNNING_LOCK:
            _RUNNING_DAYS[key] += 1
        try:
            self._start(run)
        except Exception:
            _finish(key)
            raise
        try:
            return answers.get(timeout=LOCAL_ANSWER_S)
        except queue.Empty:
            return RUNNING

    def _guarded(self, day: date, *, retry: bool) -> str:
        try:
            if not self._write_ready():
                # A linked day keeps waiting, so its change reaches Garmin later.
                return PENDING if self._mark_pending(day, linked_only=True) else OFF
            if not _PUSH_LOCK.acquire(timeout=_LOCK_WAIT_S):
                self._mark_pending(day)
                return PENDING
            try:
                return self._push(day, retry)
            finally:
                _PUSH_LOCK.release()
        except Exception as error:  # noqa: BLE001 - a local fault; text never kept
            _LOGGER.warning(
                "Garmin body push stopped",
                extra={"event": "garmin_body_push_error", "context": {"error_type": type(error).__name__}},
            )  # fmt: skip
            return PENDING

    def retry_pending(self) -> int:
        """Retry the pending days of the last ``RETRY_DAYS``; returns how many were tried."""
        try:
            if not self._write_ready() or self._paused():
                return 0
            oldest = (self._now().date() - timedelta(days=RETRY_DAYS)).isoformat()
            with self._database_manager.reader() as db:
                days = [
                    link.local_id
                    for link in self._links.list_for_provider(db, GARMIN, KIND)
                    if link.sync_state is RemoteLinkState.PENDING
                    and link.local_id >= oldest
                ][::-1][:RETRY_BATCH]  # newest first
            tried = 0
            for day in days:
                tried += 1
                if (
                    self.push(date.fromisoformat(day), retry=True) == PENDING
                    and self._paused()
                ):
                    break  # Garmin answered with a rate limit: the pause starts now
            return tried
        except Exception as error:  # noqa: BLE001 - must not fail the refresh job
            _LOGGER.warning(
                "Garmin body retry stopped",
                extra={"event": "garmin_body_push_error", "context": {"error_type": type(error).__name__}},
            )  # fmt: skip
            return 0

    def _push(self, day: date, retry: bool) -> str:
        with self._database_manager.reader() as db:
            link = self._links.get(db, KIND, day.isoformat(), GARMIN)
            known = _Day.of(link)
            wanted, needs_weight = self._wanted(db, day, known.own_weights())
        if known.settled_as(link, wanted.fingerprint() if wanted else None):
            return NEEDS_WEIGHT if needs_weight else UNCHANGED
        if self._paused():
            self._store(day, known, done=False, outcome="failed", retry=retry)
            return PENDING
        try:
            with self._sdk_session() as sdk:
                self._apply(sdk, day, known, wanted)
        except Exception as error:  # noqa: BLE001 - provider boundary, text never kept
            if not isinstance(error, _Unlinked):
                self._record_failure(error)
            self._store(day, known, done=False, outcome="failed", retry=retry)
            return PENDING
        self._record_success()
        self._store(
            day, known, done=True, outcome="sent" if wanted else "removed", retry=retry
        )
        if needs_weight:
            return NEEDS_WEIGHT
        return SENT if wanted else REMOVED

    def _apply(
        self, sdk: Any, day: date, known: _Day, wanted: BodyEntry | None
    ) -> None:
        """Change Garmin's day step by step; ``known`` follows every step."""
        target = wanted.fingerprint() if wanted else None
        if known.remote_id is not None and known.baseline != target:
            _delete(sdk, day, known.remote_id)
            known.remote_id, known.baseline = None, None
            self._store(day, known)
        sending = (known.sending or "").removeprefix(ACCEPTED) or None
        sent = BodyEntry.parse(sending)
        if sent is not None and sending != target:
            # An earlier upload of other values may have landed: remove it.
            stale = _find_all(sdk, day, sent)
            if stale:
                _delete(sdk, day, stale[-1])
            known.sending = None
            self._store(day, known)
        if wanted is not None and known.remote_id is None:
            self._send(sdk, day, known, wanted)

    def _send(self, sdk: Any, day: date, known: _Day, wanted: BodyEntry) -> None:
        """Link the day's upload, sending it only when Garmin may not have it."""
        target = wanted.fingerprint()
        found = _find_all(sdk, day, wanted) if known.sending else []
        if not found and known.sending == ACCEPTED + target:
            raise _Unlinked  # Garmin has it: search again later, never send it twice
        if not found:
            known.sending = target
            self._store(day, known)  # before the upload, so it is never repeated blind
            response = sdk.add_body_composition(
                timestamp=datetime.combine(day, UPLOAD_TIME).isoformat(),
                weight=wanted.weight_kg,
                percent_fat=wanted.fat_pct,
            )
            if _rejected(response):
                raise _Rejected
            known.sending = ACCEPTED + target
            found = _find_all(sdk, day, wanted)
        if not found:
            raise _Unlinked
        # Two matches are both uploads of this day's values: link the newest.
        known.remote_id, known.baseline, known.sending = found[-1], target, None

    def _wanted(
        self, db: Any, day: date, own_weights: set[float]
    ) -> tuple[BodyEntry | None, bool]:
        """The day's entry and whether a body fat waits for a weight."""
        weight = _value(db, MetricKind.WEIGHT, day)
        fat = self._skinfold_fat(db, day)
        if fat is None:
            fat = _value(db, MetricKind.BODY_FAT, day)
        if weight is None and fat is not None and not FAT_NEEDS_ENTERED_WEIGHT:
            weight = next(
                (
                    kg
                    for kg in _garmin_weights(db, day)
                    if round(kg, 1)
                    not in own_weights  # not the echo of the app's upload
                ),
                None,
            )
        if weight is None:
            return None, fat is not None
        return BodyEntry(
            round(weight, 1), None if fat is None else round(fat, 1)
        ), False

    def _paused(self) -> bool:
        limited = self._sync_state.last_rate_limit()
        if limited is not None:
            if limited.tzinfo is None:
                limited = limited.replace(tzinfo=UTC)
            if self._now() - limited < timedelta(
                seconds=GARMIN_CAPABILITY_PAUSE_SECONDS
            ):
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
            extra={
                "event": "garmin_body_push_failed",
                "context": {"error_type": type(error).__name__},
            },
        )

    def _record_success(self) -> None:
        try:
            self._sync_state.record_capability_success(CAPABILITY_SOURCE)
        except Exception:  # noqa: BLE001 - keep the outcome
            _LOGGER.warning(
                "Garmin body push state not recorded",
                extra={"event": "garmin_body_push_state_failed"},
            )

    def mark_restored(self, day: date) -> None:
        """An undo put the day back (#3640): the next Garmin refresh mirrors it
        (``retry_pending``); the undo itself writes nothing to Garmin, so no
        outcome is counted (Gitar)."""
        with self._database_manager.reader() as db:
            link = self._links.get(db, KIND, day.isoformat(), GARMIN)
        self._store(day, _Day.of(link), done=False)

    def _mark_pending(self, day: date, *, linked_only: bool = False) -> bool:
        """Keep the day pending; with ``linked_only`` only a day Garmin holds."""
        with self._database_manager.reader() as db:
            link = self._links.get(db, KIND, day.isoformat(), GARMIN)
        if linked_only and link is None:
            return False
        self._store(day, _Day.of(link), done=False, outcome="failed")
        return True

    def _store(
        self,
        day: date,
        known: _Day,
        *,
        done: bool = False,
        outcome: str | None = None,
        retry: bool = False,
    ) -> None:
        """Store the day's link (and count a final outcome) in one transaction.

        A finished day without a linked or sending entry has no link.
        """
        settled = done and known.sending is None
        with self._database_manager.unit_of_work() as db:
            if settled and known.remote_id is None:
                self._links.delete(db, KIND, day.isoformat(), GARMIN)
            else:
                self._links.upsert(
                    db,
                    RemoteLink(
                        KIND,
                        day.isoformat(),
                        GARMIN,
                        RemoteLinkState.SYNCED if settled else RemoteLinkState.PENDING,
                        remote_id=known.remote_id,
                        remote_external_id=known.sending,
                        baseline_hash=known.baseline,
                        last_synced_at=self._now().isoformat() if settled else None,
                    ),
                )
            if outcome is not None:
                self._count(db, [outcome, *(["retried"] if retry else [])])

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


def _finish(key: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING_DAYS[key] -= 1
        if _RUNNING_DAYS[key] <= 0:
            del _RUNNING_DAYS[key]


def _value(db: Any, kind: MetricKind, day: date) -> float | None:
    """The athlete's entered value of ``kind`` on ``day``."""
    row = db.execute(
        _ENTERED_SQL, (kind.value, SourceKind.ATHLETE_ENTERED.value, day.isoformat())
    ).fetchone()
    if row is None:
        return None
    return float(row["value"] if isinstance(row, Mapping) else row[0])


def _garmin_weights(db: Any, day: date) -> list[float]:
    """Garmin's weights of ``day``, newest first."""
    rows = db.execute(
        _GARMIN_WEIGHTS_SQL, (MetricKind.WEIGHT.value, GARMIN, day.isoformat())
    ).fetchall()
    return [float(row["value"] if isinstance(row, Mapping) else row[0]) for row in rows]


def _delete(sdk: Any, day: date, remote_id: str) -> None:
    """Delete one entry; one Garmin no longer has counts as deleted."""
    try:
        sdk.delete_weigh_in(remote_id, day.isoformat())
    except Exception as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status != 404 and type(error).__name__ != "GarminConnectNotFoundError":
            raise


def _find_all(sdk: Any, day: date, entry: BodyEntry) -> list[str]:
    """The samplePks of the day's entries with the upload time and these values.

    Ordered by samplePk, so the last one is Garmin's newest.
    """
    payload = sdk.get_daily_weigh_ins(day.isoformat())
    items = payload.get("dateWeightList") if isinstance(payload, Mapping) else None
    found = [
        item["samplePk"]
        for item in items or []
        if isinstance(item, Mapping)
        and isinstance(item.get("samplePk"), int | str)
        and _matches(item, day, entry)
    ]
    return [str(pk) for pk in sorted(found, key=lambda pk: (len(str(pk)), str(pk)))]


def _matches(item: Mapping[str, Any], day: date, entry: BodyEntry) -> bool:
    # Garmin dates a weigh-in in the athlete's local time, read as if it were UTC.
    local_ms = int(datetime.combine(day, UPLOAD_TIME, tzinfo=UTC).timestamp() * 1000)
    if _number(item.get("date")) != local_ms:
        return False
    grams = _number(item.get("weight"))
    if grams is None or abs(grams / 1000 - entry.weight_kg) > _WEIGHT_TOLERANCE_KG:
        return False
    fat = _number(item.get("bodyFat"))
    if entry.fat_pct is None or fat is None:
        return entry.fat_pct is None and fat is None
    return abs(fat - entry.fat_pct) <= _FAT_TOLERANCE_PCT


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _rejected(response: Any) -> bool:
    """A FIT import Garmin answered with failures and no success."""
    result = (
        response.get("detailedImportResult") if isinstance(response, Mapping) else None
    )
    if not isinstance(result, Mapping):
        return False
    failures, successes = result.get("failures"), result.get("successes")
    return bool(failures) and not successes


def push_state(db: Any, today: date) -> dict[str, list[str]]:
    """The days whose save push runs now and the days still pending (#4267)."""
    with _RUNNING_LOCK:
        running = sorted(+_RUNNING_DAYS)
    oldest = (today - timedelta(days=RETRY_DAYS)).isoformat()
    pending = sorted(
        link.local_id
        for link in RemoteLinkRepository().list_for_provider(db, GARMIN, KIND)
        if link.sync_state is RemoteLinkState.PENDING and link.local_id >= oldest
    )
    return {"running": running, "pending": pending}


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
    names = ("sent", "removed", "failed", "retried")
    return {
        **{name: int(counts.get(name) or 0) for name in names},
        "linked_days": len(links) - len(pending),
        "pending_days": len(pending),
        "unlinked_uploads": sum(1 for link in pending if link.remote_external_id),
    }
