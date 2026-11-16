"""Read-only Garmin activity detail loading for local activities.

Two entry points share one map-and-store path. ``refresh`` fetches the bounded
detail sets of one Garmin activity on demand. ``backfill`` runs after a
successful Garmin sync, under the sync lock, and fetches a small batch of recent
activities, newest first, so the last 90 days fill up over several syncs. It
pauses after a Garmin rate limit and skips manual activities. An activity whose
fetch failed or stored only some parts is fetched again on later syncs, at most
``BACKFILL_RETRY_ATTEMPTS`` times per activity summary; after that it is passed
over, so it cannot hold back older activities. A rate limit that stored nothing
does not count as an attempt. Kinds a payload does not return keep their stored
sets. After a mapper bump every activity of the window is a candidate again
(a replay: its sets exist for the current summary, only their mapper version is
older). New or changed activities always come first; replays fill the rest of the
batch within a daily budget that starts at ``REPLAY_START_DAILY`` fetches, doubles
after a UTC day of replays without a rate limit or Garmin error (up to
``REPLAY_MAX_DAILY``) and falls back to the start after a 429, each fetch attempt
counting, so about five Garmin calls per activity never run into the rate limit
all at once (#2220). ``REPLAY_ENABLED`` stops them. A
fetch without the original file falls back to chart streams and splits; those
never replace the full streams and laps an earlier file gave (#3122): the
stored sets stay and diagnostics count it. A failed file call or a rate limit
already makes the attempt incomplete; a transport without the file does not.
Nothing is written back to Garmin.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from backend.canonical.activity_detail import (
    ActivityLapSet,
    ActivityStreamSet,
    ActivityStrengthSetSet,
)
from backend.canonical.mapping import MappingResult
from backend.canonical.vocabulary import (
    GARMIN_ID,
    ActivityRecordState,
    RecordingOrigin,
    StreamResolution,
    StrengthSetSource,
)
from backend.db.activity_detail_store import (
    activity_detail_backfill_candidates,
    activity_detail_backfill_universe,
    detail_sets_for,
    replace_detail_set,
    restamp_detail_set,
    stored_stream_resolution,
    stored_strength_source,
    strength_record_ids,
)
from backend.db.canonical_store import (
    ACTIVITY_ID_PREFIX,
    active_provider_record_hash,
    activity_record_for_session,
    activity_record_link,
)
from backend.errors import AppError
from backend.providers.canonical_plans import ACTIVITY_RECORD_KIND
from backend.providers.garmin_activity_detail_mapping import (
    MAPPER_VERSION,
    map_garmin_activity_detail,
)
from backend.sync.garmin import GARMIN_CAPABILITY_PAUSE_SECONDS
from backend.sync.provider_table import GARMIN_NOT_CONFIGURED_MESSAGE
from backend.sync.refresh import sync_job_error_class
from backend.sync.stream_backfill import count_fit_file

_LOGGER = logging.getLogger("intervals_coach")
ACTIVITY_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,200}")
COOLDOWN: Final = timedelta(minutes=10)
BACKFILL_BATCH_SIZE: Final = 10
# A mapper change re-fetches every activity of the window (about five Garmin calls
# each), so the replays are paced apart from the batch of new activities: False
# keeps outdated sets as they are, and only new or changed activities run.
REPLAY_ENABLED: Final = True
# The daily budget of replay fetches grows with each clean day and resets on a 429;
# a refresh takes what is left of it, at most REPLAY_MAX_PER_REFRESH (orchestrator,
# 2026-11-16: refreshes come only a few times a day, so a refresh may take 20).
REPLAY_START_DAILY: Final = 20
REPLAY_MAX_DAILY: Final = 80
REPLAY_MAX_PER_REFRESH: Final = 20
BACKFILL_WINDOW: Final = timedelta(days=90)
BACKFILL_RATE_LIMIT_PAUSE: Final = timedelta(seconds=GARMIN_CAPABILITY_PAUSE_SECONDS)
BACKFILL_STATE_KEY: Final = "garmin_activity_detail_backfill"
BACKFILL_RETRY_ATTEMPTS: Final = 3
# Counts of fallback sets (chart streams, splits) kept out or stored (#3122).
DETAIL_FALLBACK_KEY: Final = "garmin_detail_fallback"
# The backfill needs only ``activity_details``, the part both transports serve.
# The SDK-only parts (splits, HR and power zones, exercise sets) and the Personal
# API-only FIT file pause only themselves; the collector skips a paused part
# without a call, so a paused part never gates the others or the backfill.
# Exercise sets are fetched for strength activities only.
DETAIL_CAPABILITIES: Final = ("activity_details",)


@dataclass(frozen=True)
class _ReplayBudget:
    """The replay's UTC day: fetches made, the day's budget and whether it ran clean."""

    day: str = ""
    count: int = 0
    budget: int = REPLAY_START_DAILY
    clean: bool = True

    @property
    def per_refresh(self) -> int:
        """Replay fetches one refresh may still make today."""
        return max(0, min(REPLAY_MAX_PER_REFRESH, self.budget - self.count))


@dataclass(frozen=True)
class _BackfillState:
    paused_until: datetime | None = None
    empty: dict[str, str] = field(default_factory=dict)
    # Activity id -> (summary hash, incomplete fetch attempts for that summary).
    retry: dict[str, tuple[str, int]] = field(default_factory=dict)
    replay: _ReplayBudget = field(default_factory=_ReplayBudget)


@dataclass
class _BatchOutcome:
    empty: dict[str, str]
    retry: dict[str, tuple[str, int]]
    replay: _ReplayBudget
    status: str = "completed"
    fetched: int = 0
    marked: int = 0
    skipped: int = 0
    # Activities settled this run (stored, marked empty or skipped), new or changed
    # ones and replays apart; ``replayed`` counts the replays stored.
    done: int = 0
    replay_done: int = 0
    replayed: int = 0
    paused_until: datetime | None = None


def normalize_activity_id(value: Any) -> str:
    """Reject path syntax and ambiguous IDs before provider access."""
    if not isinstance(value, str) or not ACTIVITY_ID_PATTERN.fullmatch(value):
        raise ValueError("Die Aktivitäts-ID ist ungültig.")
    return value


class GarminActivityDetailService:
    """Fetch Garmin activity detail sets on demand or in small batches; never remove sets."""

    def __init__(
        self,
        *,
        database_manager: Any,
        reader: Any,
        configured: Callable[[], bool],
        guard: Callable[[], AbstractContextManager[Any]],
        utc_now: Callable[[], str],
        key_values: Any | None = None,
        capability_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        self._database_manager = database_manager
        self._reader = reader
        self._configured = configured
        self._guard = guard
        self._utc_now = utc_now
        self._key_values = key_values
        self._capability_allowed = capability_allowed

    def refresh(self, activity_id: str) -> dict[str, Any]:
        requested = normalize_activity_id(activity_id)
        with self._database_manager.reader() as db:
            record = _resolve_record(db, requested)
        if record is None:
            raise _not_found()
        provider_activity_id = str(record["provider_activity_id"])
        recorded_by = _recorded_by(record["recorded_by"])
        if not self._configured():
            raise AppError(503, GARMIN_NOT_CONFIGURED_MESSAGE, reason="not_configured")
        if recorded_by == RecordingOrigin.MANUAL:
            return {
                "status": "skipped",
                "reason": "manual_activity",
                "activity_id": provider_activity_id,
            }
        with self._database_manager.reader() as db:
            stored = detail_sets_for(db, GARMIN_ID, provider_activity_id)
            current_summary = active_provider_record_hash(
                db, GARMIN_ID, ACTIVITY_RECORD_KIND, provider_activity_id
            )
            strength = bool(strength_record_ids(db, GARMIN_ID, (provider_activity_id,)))
        if _within_cooldown(stored, current_summary, _aware(self._utc_now())):
            return {
                "status": "unchanged",
                "activity_id": provider_activity_id,
                "kinds": sorted({item.detail.kind.value for item in stored}),
            }

        with self._guard():
            collected = self._reader.fetch_activity_detail(
                provider_activity_id, strength=strength
            )
            now = self._utc_now()
            mapped = _map_collected(collected, recorded_by=recorded_by, now=now)
        self._count_fit(provider_activity_id, collected)
        keep_file_sets = self._keeps_file_sets(provider_activity_id, mapped)
        # Malformed parts are dropped by the mapper, so they count like failures.
        incomplete = _incomplete(collected) or bool(mapped.issues)
        if not mapped.records:
            if incomplete:
                # Every requested part failed: report the job as failed.
                raise AppError(
                    502,
                    "Garmin hat keine Aktivitätsdetails geliefert.",
                    reason="activity_details_unavailable",
                )
            return {
                "status": "unchanged",
                "activity_id": provider_activity_id,
                "kinds": [],
            }
        if not self._store_records(
            provider_activity_id, mapped, now=now, keep_file_sets=keep_file_sets
        ):
            raise _not_found()
        return {
            "status": "partial" if incomplete else "completed",
            "activity_id": provider_activity_id,
            "kinds": sorted({item.kind.value for item in mapped.records}),
        }

    def backfill(self, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        """Fetch details for a small batch of recent Garmin activities.

        The caller MUST already hold the Garmin sync lock and resync gate, as
        ``GarminSyncService`` does during a sync. This method does not take the
        non-blocking guard, because that guard would fail while the sync holds it.
        Returns counts only; no activity ids or provider messages.
        """
        if not self._configured():
            return _backfill_result("not_configured")
        if self._key_values is None or self._capability_allowed is None:
            raise RuntimeError("Garmin activity detail backfill is not composed.")
        now = _aware(self._utc_now())
        state = self._load_backfill_state()
        pending, empty, retry = self._pending_candidates(
            state, since=now - BACKFILL_WINDOW
        )
        fresh = [item for item in pending if not item["replay"]]
        replays = [item for item in pending if item["replay"]] if REPLAY_ENABLED else []
        today = now.astimezone(UTC).date().isoformat()
        replay = _roll_replay(state.replay, today)
        paused_until = state.paused_until
        if paused_until is not None and paused_until <= now:
            paused_until = None
        if paused_until is not None or not self._detail_capabilities_allowed():
            self._save_backfill_state(
                paused_until=paused_until, empty=empty, retry=retry, replay=replay
            )
            return _backfill_result(
                "paused", remaining=len(fresh), replay_remaining=len(replays)
            )
        # New and changed activities first; replays follow within today's budget.
        batch = fresh[:BACKFILL_BATCH_SIZE] + replays[: replay.per_refresh]
        if not batch:
            self._save_backfill_state(
                paused_until=None, empty=empty, retry=retry, replay=replay
            )
            return _backfill_result("completed", replay_remaining=len(replays))

        outcome = _BatchOutcome(empty=empty, retry=retry, replay=replay)
        self._fetch_batch(batch, outcome, cancel_event)
        if outcome.status == "completed" and _is_set(cancel_event):
            outcome.status = "cancelled"
        self._save_backfill_state(
            paused_until=outcome.paused_until,
            empty=outcome.empty,
            retry=outcome.retry,
            replay=outcome.replay,
        )
        return _backfill_result(
            outcome.status,
            fetched=outcome.fetched,
            empty=outcome.marked,
            remaining=len(fresh) - outcome.done,
            replayed=outcome.replayed,
            replay_remaining=len(replays) - outcome.replay_done,
        )

    def _pending_candidates(
        self, state: _BackfillState, *, since: datetime
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, tuple[str, int]]]:
        """Return pending window candidates plus the window's empty marks and retries."""
        with self._database_manager.reader() as db:
            return _window_pending(
                db, state, since=since, mapper_version=MAPPER_VERSION
            )

    def _fetch_batch(
        self,
        batch: list[dict[str, Any]],
        outcome: _BatchOutcome,
        cancel_event: threading.Event | None,
    ) -> None:
        """Fetch and store one batch, updating counts, marks and retries in ``outcome``."""
        by_id = {item["provider_activity_id"]: item for item in batch}
        with self._database_manager.reader() as db:
            strength_ids = strength_record_ids(db, GARMIN_ID, by_id)
        details = self._reader.fetch_activity_details(
            list(by_id), strength_ids=strength_ids, cancel_event=cancel_event
        )
        seen = 0
        # True while a fetched activity is stored: it is already counted then.
        storing = False
        try:
            for provider_activity_id, collected in details:
                seen += 1
                storing = True
                if not self._store_collected(
                    provider_activity_id,
                    collected,
                    by_id[provider_activity_id],
                    outcome,
                ):
                    break
                storing = False
                if not self._detail_capabilities_allowed():
                    outcome.status = "paused"
                    break
                if _is_set(cancel_event):
                    outcome.status = "cancelled"
                    break
        except Exception as error:  # noqa: BLE001 - backfill errors are logged by type and never stop the sync
            # The fetch in flight already made calls: it counts like any attempt.
            # An error while storing came after the count of the stored activity.
            if not storing and seen < len(batch) and batch[seen]["replay"]:
                outcome.replay = replace(outcome.replay, count=outcome.replay.count + 1)
            if sync_job_error_class(error) == "rate_limited":
                outcome.paused_until = (
                    _aware(self._utc_now()) + BACKFILL_RATE_LIMIT_PAUSE
                )
                outcome.status = "rate_limited"
                _note_replay_error(outcome, rate_limited=True)
            else:
                outcome.status = "failed"
                _note_replay_error(outcome, rate_limited=False)
                _LOGGER.warning(
                    "garmin activity detail backfill stopped after an error",
                    extra={
                        "event": "garmin_activity_detail_backfill_failed",
                        "context": {"error_type": type(error).__name__},
                    },
                )
        finally:
            # Closing the generator persists the Garmin tokens on early stop.
            details.close()

    def _count_fit(self, provider_activity_id: str, collected: dict[str, Any]) -> None:
        """Count the downloaded FIT file for the diagnostics inventory (#2220)."""
        if self._key_values is not None and isinstance(collected.get("fit"), bytes):
            with self._database_manager.unit_of_work() as db:
                count_fit_file(
                    db,
                    self._key_values,
                    GARMIN_ID,
                    provider_activity_id,
                    collected["fit"],
                )

    def _store_collected(
        self,
        provider_activity_id: str,
        collected: dict[str, Any],
        candidate: dict[str, Any],
        outcome: _BatchOutcome,
    ) -> bool:
        """Store one fetched activity; False when the batch must stop."""
        observed = self._utc_now()
        # Every replay fetch counts against the day, whatever its outcome.
        _note_replay(outcome, candidate, collected)
        mapped = _map_collected(
            collected,
            recorded_by=_recorded_by(candidate["recorded_by"]),
            now=observed,
        )
        self._count_fit(provider_activity_id, collected)
        keep_file_sets = self._keeps_file_sets(provider_activity_id, mapped)
        # Malformed parts are dropped by the mapper, so they count like failures.
        failed = _failed(collected) or bool(mapped.issues)
        retry = dict(outcome.retry)
        if collected.get("skipped") and not failed:
            # Paused parts are no attempt: keep the activity a candidate without
            # spending its retries, so it is fetched again once the pause ends.
            _keep_candidate(retry, provider_activity_id, candidate["summary_sha256"])
        else:
            _note_attempt(
                retry,
                provider_activity_id,
                candidate["summary_sha256"],
                incomplete=_incomplete_attempt(failed, candidate, keep_file_sets),
            )
        # The retry entry is committed with the detail rows, so a stop before the
        # final state write cannot leave a partial fetch looking complete.
        stored = bool(mapped.records) and self._store_records(
            provider_activity_id,
            mapped,
            now=observed,
            backfill_state=(outcome.empty, retry, outcome.replay),
            keep_file_sets=keep_file_sets,
        )
        if stored:
            outcome.fetched += 1
            _settle(outcome, candidate, stored=True)
        # A rate limit that stored nothing says nothing about this activity.
        if stored or not collected.get("rate_limited"):
            outcome.retry = retry
        if collected.get("rate_limited"):
            # Keep the parts that arrived, then stop until the pause ends.
            outcome.paused_until = _aware(observed) + BACKFILL_RATE_LIMIT_PAUSE
            outcome.status = "rate_limited"
            return False
        if mapped.records:
            if not stored:
                outcome.skipped += 1
                _settle(outcome, candidate, stored=False)
        elif failed or collected.get("skipped"):
            # Nothing is marked, so a later sync retries this activity.
            outcome.status = "failed"
            return False
        else:
            outcome.empty[provider_activity_id] = candidate["summary_sha256"]
            outcome.marked += 1
            _settle(outcome, candidate, stored=False)
        return True

    def _store_records(
        self,
        provider_activity_id: str,
        mapped: MappingResult[Any],
        *,
        now: str,
        backfill_state: (
            tuple[dict[str, str], dict[str, tuple[str, int]], _ReplayBudget] | None
        ) = None,
        keep_file_sets: bool = False,
    ) -> bool:
        """Replace the mapped sets of one active record; False when it is no longer active.

        ``backfill_state`` (empty marks, retries, replay budget) is written in the
        same transaction.
        ``keep_file_sets`` keeps the stored stream and lap sets of an earlier file
        instead of this fetch's fallback sets (#3122).
        """
        with self._database_manager.unit_of_work() as db:
            # Re-check inside the write transaction: the record may have been
            # deleted or merged while the provider call was running.
            link = activity_record_link(db, GARMIN_ID, provider_activity_id)
            if link is None or link["state"] != ActivityRecordState.ACTIVE.value:
                return False
            # The details belong to the activity summary stored right now.
            summary_sha256 = active_provider_record_hash(
                db, GARMIN_ID, ACTIVITY_RECORD_KIND, provider_activity_id
            )
            fallback = _fallback_sets(mapped)
            for item in mapped.records:
                kept = keep_file_sets and item in fallback
                if kept or _keeps_corrected_sets(db, provider_activity_id, item):
                    restamp_detail_set(
                        db,
                        GARMIN_ID,
                        provider_activity_id,
                        item.kind,
                        summary_sha256=summary_sha256,
                        fetched_at=now,
                    )
                    continue
                replace_detail_set(
                    db,
                    item,
                    mapper_version=MAPPER_VERSION,
                    summary_sha256=summary_sha256,
                    now=now,
                )
            self._count_fallback(db, len(fallback), kept=keep_file_sets)
            if backfill_state is not None:
                empty, retry, replay = backfill_state
                self._backfill_key_values().set(
                    db,
                    BACKFILL_STATE_KEY,
                    _encode_backfill_state(
                        paused_until=None, empty=empty, retry=retry, replay=replay
                    ),
                )
        return True

    def _keeps_file_sets(
        self, provider_activity_id: str, mapped: MappingResult[Any]
    ) -> bool:
        """Whether this fetch fell back below the full streams stored now (#3122)."""
        if not _fallback_sets(mapped):
            return False
        with self._database_manager.reader() as db:
            resolution = stored_stream_resolution(db, GARMIN_ID, provider_activity_id)
        return resolution == StreamResolution.FULL.value

    def _count_fallback(self, db: Any, sets: int, *, kept: bool) -> None:
        """Count fallback sets kept out or stored, counts only (#3122)."""
        if not sets or self._key_values is None:
            return
        counts = detail_fallback_counts(db, self._key_values)
        counts["kept" if kept else "stored"] += sets
        self._key_values.set(
            db, DETAIL_FALLBACK_KEY, json.dumps(counts, sort_keys=True)
        )

    def _detail_capabilities_allowed(self) -> bool:
        allowed = self._capability_allowed
        return allowed is not None and all(
            allowed(capability) for capability in DETAIL_CAPABILITIES
        )

    def _backfill_key_values(self) -> Any:
        if self._key_values is None:
            raise RuntimeError("Garmin activity detail backfill is not composed.")
        return self._key_values

    def _load_backfill_state(self) -> _BackfillState:
        key_values = self._backfill_key_values()
        with self._database_manager.reader() as db:
            raw = key_values.get(db, BACKFILL_STATE_KEY)
        return _parse_backfill_state(raw)

    def _save_backfill_state(
        self,
        *,
        paused_until: datetime | None,
        empty: dict[str, str],
        retry: dict[str, tuple[str, int]],
        replay: _ReplayBudget,
    ) -> None:
        key_values = self._backfill_key_values()
        with self._database_manager.unit_of_work() as db:
            key_values.set(
                db,
                BACKFILL_STATE_KEY,
                _encode_backfill_state(
                    paused_until=paused_until, empty=empty, retry=retry, replay=replay
                ),
            )


def _encode_backfill_state(
    *,
    paused_until: datetime | None,
    empty: dict[str, str],
    retry: dict[str, tuple[str, int]],
    replay: _ReplayBudget,
) -> str:
    stored: dict[str, Any] = {"empty": dict(sorted(empty.items()))}
    if replay.count or replay.budget != REPLAY_START_DAILY or not replay.clean:
        stored["replay"] = {
            "day": replay.day,
            "count": replay.count,
            "budget": replay.budget,
            "clean": replay.clean,
        }
    if retry:
        stored["retry"] = {
            key: [summary, attempts]
            for key, (summary, attempts) in sorted(retry.items())
        }
    if paused_until is not None:
        stored["paused_until"] = paused_until.isoformat()
    return json.dumps(stored, sort_keys=True)


def _map_collected(
    collected: dict[str, Any], *, recorded_by: RecordingOrigin, now: str
) -> MappingResult[Any]:
    mapped = map_garmin_activity_detail(
        collected,
        recorded_by=recorded_by,
        retrieved_at=_aware(now),
    )
    if mapped.issues:
        _LOGGER.warning(
            "garmin activity detail mapping produced issues",
            extra={
                "event": "garmin_activity_detail_mapping_issues",
                "context": {"issues": len(mapped.issues)},
            },
        )
    return mapped


def _fallback_sets(mapped: MappingResult[Any]) -> tuple[Any, ...]:
    """The stream and lap sets of a fetch that brought no full-resolution streams.

    Without the original file the mapper falls back to Garmin's chart streams and
    splits: coarser points, laps without kinds and with summed offsets.
    """
    if any(
        isinstance(item, ActivityStreamSet) and item.resolution is StreamResolution.FULL
        for item in mapped.records
    ):
        return ()
    return tuple(
        item
        for item in mapped.records
        if isinstance(item, (ActivityStreamSet, ActivityLapSet))
    )


def detail_fallback_counts(db: Any, key_values: Any) -> dict[str, int]:
    """Fallback sets kept out (a full set was stored) or stored, for diagnostics."""
    try:
        stored = json.loads(key_values.get(db, DETAIL_FALLBACK_KEY) or "{}")
    except ValueError:
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return {
        name: value if isinstance(value := stored.get(name), int) and value >= 0 else 0
        for name in ("kept", "stored")
    }


def _window_pending(
    db: Any, state: _BackfillState, *, since: datetime, mapper_version: int
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, tuple[str, int]]]:
    """Window candidates the backfill fetches, with the window's empty marks and retries.

    Activities marked empty for their summary and those whose retries are used up
    are left out, as the backfill leaves them out.
    """
    universe = activity_detail_backfill_universe(db, GARMIN_ID, since=since)
    window_ids = set(universe)
    retry = {
        provider_activity_id: value
        for provider_activity_id, value in state.retry.items()
        if provider_activity_id in window_ids
    }
    # The limit covers every window record, so marks for empty activities
    # cannot push pending candidates out of the batch.
    candidates = activity_detail_backfill_candidates(
        db,
        GARMIN_ID,
        ACTIVITY_RECORD_KIND,
        since=since,
        limit=len(universe),
        mapper_version=mapper_version,
        retry_ids=[
            provider_activity_id
            for provider_activity_id, (_summary, attempts) in retry.items()
            if attempts < BACKFILL_RETRY_ATTEMPTS
        ],
    )
    empty = {
        provider_activity_id: value
        for provider_activity_id, value in state.empty.items()
        if provider_activity_id in window_ids
    }
    pending = [
        item
        for item in candidates
        if empty.get(item["provider_activity_id"]) != item["summary_sha256"]
        and not _retries_exhausted(
            retry.get(item["provider_activity_id"]), item["summary_sha256"]
        )
    ]
    return pending, empty, retry


def detail_replay_size(db: Any, key_values: Any, now: datetime) -> dict[str, Any]:
    """Window records the backfill fetches and today's replay budget, counts only (#2220).

    ``pending`` records have no set of the current mapper version yet; ``records``
    have none of the next one, so the next mapper bump would fetch them again,
    ``strength`` of them with one call more (their exercise sets). Both leave out
    what the backfill leaves out: empty marks and used-up retries. The budget
    fields are ``replay_budget_counts`` of today (UTC).
    """
    raw = key_values.get(db, BACKFILL_STATE_KEY)
    state = _parse_backfill_state(raw)
    since = now - BACKFILL_WINDOW

    def below(version: int) -> list[str]:
        pending, _empty, _retry = _window_pending(
            db, state, since=since, mapper_version=version
        )
        return [item["provider_activity_id"] for item in pending]

    ids = below(MAPPER_VERSION + 1)
    return {
        "window_days": BACKFILL_WINDOW.days,
        "pending": len(below(MAPPER_VERSION)),
        "records": len(ids),
        "strength": len(strength_record_ids(db, GARMIN_ID, ids)),
        **replay_budget_counts(raw, now.astimezone(UTC).date().isoformat()),
    }


def _keeps_corrected_sets(db: Any, provider_activity_id: str, item: Any) -> bool:
    """Whether stored exercise sets must stay instead of the file's set messages.

    Garmin's exercise sets carry the athlete's Connect corrections, which the
    original file lacks. A fetch without that part (paused, failed or no SDK)
    falls back to the file, which must not overwrite the corrected sets.
    """
    return (
        isinstance(item, ActivityStrengthSetSet)
        and item.source is StrengthSetSource.FIT_SET_MESSAGES
        and stored_strength_source(db, GARMIN_ID, provider_activity_id)
        == StrengthSetSource.GARMIN_EXERCISE_SETS.value
    )


def _resolve_record(db: Any, activity_id: str) -> dict[str, Any] | None:
    """Map a local ``act_`` session or a bare Garmin id to its active record."""
    if activity_id.startswith(ACTIVITY_ID_PREFIX):
        return activity_record_for_session(db, GARMIN_ID, activity_id)
    link = activity_record_link(db, GARMIN_ID, activity_id)
    if link is None or link["state"] != ActivityRecordState.ACTIVE.value:
        return None
    return {
        "provider_activity_id": activity_id,
        "recorded_by": link["recorded_by"],
    }


def _not_found() -> AppError:
    return AppError(
        404,
        "Diese Aktivität ist lokal nicht vorhanden.",
        reason="activity_details_not_found",
    )


def _recorded_by(value: object) -> RecordingOrigin:
    if not isinstance(value, str):
        return RecordingOrigin.UNKNOWN
    try:
        return RecordingOrigin(value)
    except ValueError:
        return RecordingOrigin.UNKNOWN


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("utc_now must return an ISO timestamp with an offset")
    return parsed


def _within_cooldown(
    stored: list[Any], current_summary: str | None, now: datetime
) -> bool:
    """True when the stored sets match the current summary and are recent.

    A changed activity summary makes the stored sets stale, so it bypasses the
    cooldown.
    """
    if any(item.summary_sha256 != current_summary for item in stored):
        return False
    newest = max(
        (parsed for parsed in map(_stored_fetch_time, stored) if parsed is not None),
        default=None,
    )
    return newest is not None and now - newest < COOLDOWN


def _stored_fetch_time(item: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(item.fetched_at))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _parse_backfill_state(raw: object) -> _BackfillState:
    """Read the stored backfill state; unreadable content resets to an empty state."""
    if not isinstance(raw, str) or not raw:
        return _BackfillState()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _BackfillState()
    if not isinstance(parsed, dict):
        return _BackfillState()
    marks = parsed.get("empty")
    empty = (
        {str(key): str(value) for key, value in marks.items()}
        if isinstance(marks, dict)
        else {}
    )
    return _BackfillState(
        paused_until=_parse_time(parsed.get("paused_until")),
        empty=empty,
        retry=_parse_retry(parsed.get("retry")),
        replay=_parse_replay(parsed.get("replay")),
    )


def _parse_replay(value: object) -> _ReplayBudget:
    """Read the replay day; an absent or unreadable entry is the start state."""
    if not isinstance(value, dict):
        return _ReplayBudget()
    day, count = value.get("day"), value.get("count")
    budget, clean = value.get("budget"), value.get("clean")
    return _ReplayBudget(
        day=day if isinstance(day, str) else "",
        count=count if type(count) is int and count > 0 else 0,
        budget=(
            budget
            if type(budget) is int and REPLAY_START_DAILY <= budget <= REPLAY_MAX_DAILY
            else REPLAY_START_DAILY
        ),
        clean=clean if isinstance(clean, bool) else True,
    )


def _roll_replay(previous: _ReplayBudget, today: str) -> _ReplayBudget:
    """The replay state of ``today``: a new UTC day doubles the budget (to the cap)
    when the previous one made replay fetches and ran clean, else keeps it."""
    if previous.day == today:
        return previous
    budget = previous.budget
    if previous.count and previous.clean:
        budget = min(2 * budget, REPLAY_MAX_DAILY)
    return _ReplayBudget(day=today, budget=budget)


def replay_budget_counts(raw_state: object, today: str) -> dict[str, Any]:
    """The replay budget of ``today`` from the stored backfill state, counts only.

    ``raw_state`` is the stored text of ``BACKFILL_STATE_KEY`` (None before the
    first backfill). The daily budget includes a doubling due at the day change.
    """
    replay = _roll_replay(_parse_backfill_state(raw_state).replay, today)
    return {
        "daily_budget": replay.budget,
        "per_refresh": replay.per_refresh,
        "replayed_today": replay.count,
        "clean_today": replay.clean,
    }


def _parse_retry(value: object) -> dict[str, tuple[str, int]]:
    """Read retry entries; malformed entries are dropped."""
    if not isinstance(value, dict):
        return {}
    retry: dict[str, tuple[str, int]] = {}
    for key, entry in value.items():
        if (
            isinstance(entry, list)
            and len(entry) == 2
            and isinstance(entry[0], str)
            and type(entry[1]) is int
            and entry[1] >= 0
        ):
            retry[str(key)] = (entry[0], entry[1])
    return retry


def _incomplete(collected: Mapping[str, Any]) -> bool:
    """True when a part failed, was rate limited or was skipped while paused."""
    return bool(
        collected.get("errors")
        or collected.get("rate_limited")
        or collected.get("skipped")
    )


def _failed(collected: Mapping[str, Any]) -> bool:
    """True when a part failed or was rate limited; a paused part is not a failure."""
    return bool(collected.get("errors") or collected.get("rate_limited"))


def _keep_candidate(
    retry: dict[str, tuple[str, int]], provider_activity_id: str, summary_sha256: str
) -> None:
    """Keep a retry entry for the summary without counting another attempt."""
    previous = retry.get(provider_activity_id)
    attempts = (
        previous[1] if previous is not None and previous[0] == summary_sha256 else 0
    )
    retry[provider_activity_id] = (summary_sha256, attempts)


def _retries_exhausted(entry: tuple[str, int] | None, summary_sha256: str) -> bool:
    """True when the current summary already used up its incomplete-fetch attempts."""
    return (
        entry is not None
        and entry[0] == summary_sha256
        and entry[1] >= BACKFILL_RETRY_ATTEMPTS
    )


def _note_attempt(
    retry: dict[str, tuple[str, int]],
    provider_activity_id: str,
    summary_sha256: str,
    *,
    incomplete: bool,
) -> None:
    """Count an incomplete fetch per summary hash, or clear the entry on success."""
    if not incomplete:
        retry.pop(provider_activity_id, None)
        return
    previous = retry.get(provider_activity_id)
    attempts = (
        previous[1] + 1 if previous is not None and previous[0] == summary_sha256 else 1
    )
    retry[provider_activity_id] = (summary_sha256, attempts)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _backfill_result(
    status: str,
    *,
    fetched: int = 0,
    empty: int = 0,
    remaining: int = 0,
    replayed: int = 0,
    replay_remaining: int = 0,
) -> dict[str, Any]:
    return {
        "status": status,
        "fetched": fetched,
        "empty": empty,
        "remaining": remaining,
        "replayed": replayed,
        "replay_remaining": replay_remaining,
    }


def _note_replay(
    outcome: _BatchOutcome, candidate: dict[str, Any], collected: Mapping[str, Any]
) -> None:
    """Count a replay fetch against the day; a Garmin error keeps the day from
    doubling the budget and a 429 resets it."""
    if candidate["replay"]:
        outcome.replay = replace(outcome.replay, count=outcome.replay.count + 1)
    if collected.get("rate_limited"):
        _note_replay_error(outcome, rate_limited=True)
    elif collected.get("errors"):
        _note_replay_error(outcome, rate_limited=False)


def _note_replay_error(outcome: _BatchOutcome, *, rate_limited: bool) -> None:
    """Any 429 resets the budget; any other Garmin error ends the clean day."""
    budget = REPLAY_START_DAILY if rate_limited else outcome.replay.budget
    outcome.replay = replace(outcome.replay, budget=budget, clean=False)


def _incomplete_attempt(
    failed: bool, candidate: dict[str, Any], keep_file_sets: bool
) -> bool:
    """Whether an attempt counts toward the retry limit.

    A replay that kept its stored full sets did not get the new fields either, so
    a record the file never reaches is passed over after its attempts instead of
    blocking the replay.
    """
    return failed or (candidate["replay"] and keep_file_sets)


def _settle(outcome: _BatchOutcome, candidate: dict[str, Any], *, stored: bool) -> None:
    """Count one activity as settled for this run, replays apart from new ones."""
    if candidate["replay"]:
        outcome.replay_done += 1
        outcome.replayed += stored
    else:
        outcome.done += 1


def _is_set(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()
