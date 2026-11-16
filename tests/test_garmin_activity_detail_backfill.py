"""Automatic Garmin activity detail backfill: candidates, batches, pauses, and reader lifecycle."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import Mock, patch

from canonical_detail_support import NOW, ZONE, open_manager, seed_intervals_activity
from support import garmin_services, make_config
from test_garmin_activity_detail_job import _collected, _grades, _with_fit

from backend.canonical.vocabulary import GARMIN_ID, ActivityRecordState, RecordingOrigin
from backend.db.activity_detail_store import (
    activity_detail_backfill_candidates,
    activity_detail_backfill_universe,
    detail_sets_for,
)
from backend.errors import AppError
from backend.providers import canonical_plans as plans
from backend.providers.canonical_plans import ACTIVITY_RECORD_KIND
from backend.providers.garmin_activity_detail_mapping import MAPPER_VERSION
from backend.sync import garmin, garmin_activity_details, garmin_wiring
from backend.sync.canonical_ingest import ingest_plan
from backend.sync.garmin_activity_details import (
    BACKFILL_BATCH_SIZE,
    BACKFILL_RATE_LIMIT_PAUSE,
    BACKFILL_RETRY_ATTEMPTS,
    BACKFILL_STATE_KEY,
    BACKFILL_WINDOW,
    DETAIL_FALLBACK_KEY,
    GarminActivityDetailService,
    _parse_backfill_state,
    detail_replay_size,
    replay_budget_counts,
)
from backend.sync.garmin_service import GarminRemoteReader

NOW_AT = datetime.fromisoformat(NOW)
SINCE = NOW_AT - BACKFILL_WINDOW


def _seed(manager: Any, rows: list[tuple[str, str]]) -> None:
    """Ingest Garmin rides given as (activity id, local start time) pairs."""
    payload = {
        "synced_at": NOW,
        "activities": [
            {
                "activityId": int(activity_id),
                "activityName": "Synthetic ride",
                "activityType": {"typeKey": "cycling"},
                "startTimeLocal": start,
                "distance": 20000.0,
                "duration": 3600.0,
                "averageHR": 140,
                "activityTrainingLoad": 50.0,
            }
            for activity_id, start in rows
        ],
    }
    plan = plans.garmin_ingest_plan(payload, zone=ZONE)
    with manager.unit_of_work() as db:
        ingest_plan(
            db,
            plan,
            retrieved_at=NOW_AT,
            deletion_window=None,
            zone=ZONE,
        )


def _payload(activity_id: str, **extra: Any) -> dict[str, Any]:
    collected = _collected()
    collected["activity_id"] = activity_id
    collected.update(extra)
    return collected


def _partial(activity_id: str, **extra: Any) -> dict[str, Any]:
    """A payload whose splits part failed while the other parts arrived."""
    payload = _payload(activity_id, **extra)
    payload.pop("splits", None)
    payload["errors"] = [{"source": "activity_splits", "message": "synthetic"}]
    return payload


def _summary_hash(manager: Any, activity_id: str) -> str:
    with manager.reader() as db:
        return str(
            db.execute(
                "SELECT payload_hash FROM provider_records "
                "WHERE provider = ? AND record_kind = 'activity' "
                "AND source_record_id = ? AND state = 'active'",
                (GARMIN_ID, activity_id),
            ).fetchone()["payload_hash"]
        )


CHANGED_HASH = hashlib.sha256(b"synthetic changed summary").hexdigest()


def _set_summary_hash(
    manager: Any, activity_id: str, value: str = CHANGED_HASH
) -> None:
    with manager.unit_of_work() as db:
        db.execute(
            "UPDATE provider_records SET payload_hash = ? "
            "WHERE provider = ? AND record_kind = 'activity' AND source_record_id = ?",
            (value, GARMIN_ID, activity_id),
        )


def _candidate_ids(
    manager: Any,
    *,
    limit: int = 100,
    retry_ids: tuple[str, ...] = (),
    mapper_version: int = MAPPER_VERSION,
) -> list[str]:
    with manager.reader() as db:
        rows = activity_detail_backfill_candidates(
            db,
            GARMIN_ID,
            ACTIVITY_RECORD_KIND,
            since=SINCE,
            limit=limit,
            mapper_version=mapper_version,
            retry_ids=retry_ids,
        )
    return [row["provider_activity_id"] for row in rows]


def _stored_kinds(manager: Any, activity_id: str) -> list[str]:
    with manager.reader() as db:
        return sorted(
            item.detail.kind.value
            for item in detail_sets_for(db, GARMIN_ID, activity_id)
        )


class _KeyValues:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, _db: Any, key: str) -> str | None:
        return self.values.get(key)

    def set(self, _db: Any, key: str, value: str) -> None:
        self.values[key] = value


class _Details:
    """Generator-backed reader double that records calls and whether it was closed."""

    def __init__(
        self,
        responses: dict[str, Any],
        *,
        pre_yield: dict[str, Any] | None = None,
    ) -> None:
        self._responses = responses
        self._pre_yield = pre_yield or {}
        self.calls: list[tuple[tuple[str, ...], threading.Event | None]] = []
        self.fetched: list[str] = []
        self.closed = False

    def fetch_activity_details(
        self, provider_activity_ids, *, strength_ids=(), cancel_event=None
    ):
        self.calls.append((tuple(provider_activity_ids), cancel_event))
        return self._iterate(list(provider_activity_ids), cancel_event)

    def _iterate(self, ids: list[str], cancel_event: threading.Event | None):
        try:
            for activity_id in ids:
                if cancel_event is not None and cancel_event.is_set():
                    return
                hook = self._pre_yield.get(activity_id)
                if hook is not None:
                    hook()
                self.fetched.append(activity_id)
                payload = self._responses[activity_id]
                if isinstance(payload, BaseException):
                    raise payload
                yield activity_id, payload
        finally:
            self.closed = True


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = open_manager(directory.name)
        self.addCleanup(self.manager.close)
        self.key_values = _KeyValues()
        self.now = NOW

    def service(
        self,
        reader: Any,
        *,
        configured: bool = True,
        allowed: Any = None,
    ) -> GarminActivityDetailService:
        return GarminActivityDetailService(
            database_manager=self.manager,
            reader=reader,
            configured=lambda: configured,
            guard=Mock(),
            utc_now=lambda: self.now,
            key_values=self.key_values,
            capability_allowed=allowed or (lambda _capability: True),
        )


def _stored_state(key_values: Any) -> dict[str, Any]:
    raw = key_values.values.get(BACKFILL_STATE_KEY)
    return json.loads(raw) if raw else {}


class BackfillCandidateTests(_Base):
    def test_replay_size_counts_every_window_record_a_bump_fetches_again(self) -> None:
        # Diagnostics size the next mapper bump before it ships (#2220).
        _seed(
            self.manager,
            [
                ("1001", "2026-07-19 12:00:00"),
                ("1003", "2026-10-02 07:00:00"),
                ("1004", "2026-10-03 07:00:00"),
            ],
        )
        reader = _Details({"1003": _payload("1003"), "1004": {"activity_id": "1004"}})
        self.service(reader).backfill()
        _seed(self.manager, [("1002", "2026-10-01 07:00:00")])
        self.assertEqual(_candidate_ids(self.manager), ["1004", "1002"])

        with self.manager.reader() as db:
            size = detail_replay_size(db, self.key_values, NOW_AT)

        # Outside the window: 1001. Current sets: 1003, stored by this mapper.
        # Marked empty for its summary: 1004, which the backfill skips. So only
        # 1002 is pending now, while a bump would fetch 1002 and 1003 again.
        # Today's replay budget comes along: nothing replayed yet, start values.
        self.assertEqual(
            size,
            {
                "window_days": 90,
                "pending": 1,
                "records": 2,
                "strength": 0,
                "daily_budget": 20,
                "per_refresh": 20,
                "replayed_today": 0,
                "clean_today": True,
            },
        )

    def test_window_boundary_uses_offset_aware_start_times(self) -> None:
        _seed(
            self.manager,
            [("1001", "2026-07-19 12:00:00"), ("1002", "2026-07-21 12:00:00")],
        )

        self.assertEqual(_candidate_ids(self.manager), ["1002"])
        with self.manager.reader() as db:
            universe = activity_detail_backfill_universe(db, GARMIN_ID, since=SINCE)
        self.assertEqual(universe, ["1002"])

    def test_candidates_are_newest_first(self) -> None:
        _seed(
            self.manager,
            [
                ("1001", "2026-10-01 07:00:00"),
                ("1002", "2026-10-15 07:00:00"),
                ("1003", "2026-10-08 07:00:00"),
            ],
        )

        self.assertEqual(_candidate_ids(self.manager), ["1002", "1003", "1001"])

    def test_limit_caps_the_candidate_list(self) -> None:
        _seed(
            self.manager,
            [
                ("1001", "2026-10-01 07:00:00"),
                ("1002", "2026-10-02 07:00:00"),
                ("1003", "2026-10-03 07:00:00"),
            ],
        )

        self.assertEqual(_candidate_ids(self.manager, limit=2), ["1003", "1002"])

    def test_manual_deleted_and_other_provider_records_are_excluded(self) -> None:
        _seed(
            self.manager,
            [
                ("1001", "2026-10-01 07:00:00"),
                ("1002", "2026-10-02 07:00:00"),
                ("1003", "2026-10-03 07:00:00"),
            ],
        )
        seed_intervals_activity(self.manager, "5550001")
        with self.manager.unit_of_work() as db:
            db.execute(
                "UPDATE activity_records SET recorded_by = ? "
                "WHERE provider = ? AND provider_activity_id = ?",
                (RecordingOrigin.MANUAL.value, GARMIN_ID, "1001"),
            )
            db.execute(
                "UPDATE activity_records SET state = ? "
                "WHERE provider = ? AND provider_activity_id = ?",
                (ActivityRecordState.DELETED.value, GARMIN_ID, "1002"),
            )

        self.assertEqual(_candidate_ids(self.manager), ["1003"])

    def test_activity_with_matching_detail_sets_is_excluded(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        reader = _Details({"1001": _payload("1001")})
        self.service(reader).backfill()
        self.assertEqual(_stored_kinds(self.manager, "1001")[0], "heart_rate_zones")

        self.assertEqual(_candidate_ids(self.manager), [])

    def test_changed_summary_makes_stored_sets_a_candidate_again(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        self.service(_Details({"1001": _payload("1001")})).backfill()
        _set_summary_hash(self.manager, "1001")

        self.assertEqual(_candidate_ids(self.manager), ["1001"])

    def test_sets_from_an_older_mapper_version_are_candidates_again(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        self.service(_Details({"1001": _payload("1001")})).backfill()
        self.assertEqual(_candidate_ids(self.manager), [])

        self.assertEqual(
            _candidate_ids(self.manager, mapper_version=MAPPER_VERSION + 1), ["1001"]
        )
        with self.manager.unit_of_work() as db:
            db.execute(
                "UPDATE activity_detail_sets SET mapper_version = ? "
                "WHERE provider = ? AND provider_activity_id = ?",
                (MAPPER_VERSION - 1, GARMIN_ID, "1001"),
            )
        self.assertEqual(_candidate_ids(self.manager), ["1001"])

    def test_one_set_for_the_current_summary_ends_the_candidacy(self) -> None:
        # A kind Garmin stopped returning keeps its old hash; refetching would loop.
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        self.service(_Details({"1001": _payload("1001")})).backfill()
        _set_summary_hash(self.manager, "1001")
        with self.manager.unit_of_work() as db:
            db.execute(
                "UPDATE activity_detail_sets SET summary_sha256 = ? "
                "WHERE provider = ? AND provider_activity_id = ? AND detail_kind = ?",
                (CHANGED_HASH, GARMIN_ID, "1001", "laps"),
            )

        self.assertEqual(_candidate_ids(self.manager), [])

    def test_retry_ids_keep_an_activity_with_current_sets_a_candidate(self) -> None:
        _seed(
            self.manager,
            [("1001", "2026-10-02 07:00:00"), ("1002", "2026-10-01 07:00:00")],
        )
        self.service(
            _Details({"1001": _payload("1001"), "1002": _payload("1002")})
        ).backfill()

        self.assertEqual(_candidate_ids(self.manager), [])
        self.assertEqual(_candidate_ids(self.manager, retry_ids=("1002",)), ["1002"])

    def test_empty_marked_activity_stays_a_candidate_for_the_query(self) -> None:
        # Empty marks are a Python-side filter; the SQL query keeps the activity.
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])

        self.assertEqual(_candidate_ids(self.manager), ["1001"])


class BackfillBatchTests(_Base):
    def test_batch_is_limited_to_ten_and_reports_remaining(self) -> None:
        rows = [
            (str(1000 + index), f"2026-10-{index + 1:02d} 07:00:00")
            for index in range(12)
        ]
        _seed(self.manager, rows)
        reader = _Details({aid: _payload(aid) for aid, _ in rows})

        result = self.service(reader).backfill()

        self.assertEqual(BACKFILL_BATCH_SIZE, 10)
        self.assertEqual(len(reader.calls), 1)
        ids, _cancel = reader.calls[0]
        self.assertEqual(len(ids), BACKFILL_BATCH_SIZE)
        self.assertEqual(ids[0], "1011")
        self.assertEqual(
            result,
            {
                "status": "completed",
                "fetched": 10,
                "empty": 0,
                "remaining": 2,
                "replayed": 0,
                "replay_remaining": 0,
            },
        )
        self.assertEqual(_stored_kinds(self.manager, "1011")[0], "heart_rate_zones")
        self.assertEqual(_stored_kinds(self.manager, "1000"), [])

    def test_empty_activity_is_marked_and_not_refetched_for_the_same_summary(
        self,
    ) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        reader = _Details({"1001": {"activity_id": "1001"}})

        first = self.service(reader).backfill()
        second_reader = _Details({})
        second = self.service(second_reader).backfill()

        self.assertEqual(first["empty"], 1)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(
            _stored_state(self.key_values)["empty"],
            {"1001": _summary_hash(self.manager, "1001")},
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second_reader.calls, [])

    def test_empty_mark_is_retried_when_the_summary_changes(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        self.service(_Details({"1001": {"activity_id": "1001"}})).backfill()
        _set_summary_hash(self.manager, "1001")
        reader = _Details({"1001": _payload("1001")})

        result = self.service(reader).backfill()

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(reader.fetched, ["1001"])

    def test_malformed_only_activity_is_retried_then_passed_over(self) -> None:
        _seed(
            self.manager,
            [("1001", "2026-10-02 07:00:00"), ("1002", "2026-10-01 07:00:00")],
        )
        summary = _summary_hash(self.manager, "1001")
        responses = {
            "1001": {"activity_id": "1001", "hr_zones": "synthetic malformed"},
            "1002": _payload("1002"),
        }
        for _attempt in range(BACKFILL_RETRY_ATTEMPTS):
            reader = _Details(responses)
            result = self.service(reader).backfill()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["empty"], 0)
            self.assertEqual(reader.fetched, ["1001"])

        reader = _Details(responses)
        result = self.service(reader).backfill()

        self.assertEqual(
            result,
            {
                "status": "completed",
                "fetched": 1,
                "empty": 0,
                "remaining": 0,
                "replayed": 0,
                "replay_remaining": 0,
            },
        )
        self.assertEqual(reader.fetched, ["1002"])
        state = _stored_state(self.key_values)
        self.assertEqual(state["empty"], {})
        self.assertEqual(state["retry"], {"1001": [summary, BACKFILL_RETRY_ATTEMPTS]})

    def test_part_dropped_by_the_mapper_is_retried(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        summary = _summary_hash(self.manager, "1001")

        first = self.service(
            _Details({"1001": _payload("1001", hr_zones="synthetic malformed")})
        ).backfill()

        self.assertEqual(first["fetched"], 1)
        self.assertNotIn("heart_rate_zones", _stored_kinds(self.manager, "1001"))
        self.assertEqual(
            _stored_state(self.key_values)["retry"], {"1001": [summary, 1]}
        )

        reader = _Details({"1001": _payload("1001")})
        self.service(reader).backfill()

        self.assertEqual(reader.fetched, ["1001"])
        self.assertIn("heart_rate_zones", _stored_kinds(self.manager, "1001"))
        self.assertNotIn("retry", _stored_state(self.key_values))

    def test_retry_state_is_committed_with_the_detail_rows(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        summary = _summary_hash(self.manager, "1001")
        service = self.service(_Details({"1001": _partial("1001")}))

        # The run stops before its final state write.
        with (
            patch.object(
                service, "_save_backfill_state", side_effect=RuntimeError("stop")
            ),
            self.assertRaises(RuntimeError),
        ):
            service.backfill()

        self.assertIn("heart_rate_zones", _stored_kinds(self.manager, "1001"))
        self.assertEqual(
            _stored_state(self.key_values)["retry"], {"1001": [summary, 1]}
        )
        self.assertEqual(_candidate_ids(self.manager, retry_ids=("1001",)), ["1001"])

    def test_failed_state_write_rolls_back_the_detail_rows(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        original_set = self.key_values.set
        failures = [RuntimeError("synthetic state write failure")]

        def failing_set(db: Any, key: str, value: str) -> None:
            if failures:
                raise failures.pop()
            original_set(db, key, value)

        with patch.object(self.key_values, "set", side_effect=failing_set):
            result = self.service(_Details({"1001": _partial("1001")})).backfill()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(_stored_kinds(self.manager, "1001"), [])
        self.assertNotIn("retry", _stored_state(self.key_values))
        self.assertEqual(_candidate_ids(self.manager), ["1001"])

    def test_rate_limit_keeps_arrived_parts_pauses_and_stops_the_batch(self) -> None:
        _seed(
            self.manager,
            [("1001", "2026-10-02 07:00:00"), ("1002", "2026-10-01 07:00:00")],
        )
        reader = _Details(
            {
                "1001": _payload("1001", rate_limited=True),
                "1002": _payload("1002"),
            }
        )

        result = self.service(reader).backfill()

        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(reader.fetched, ["1001"])
        self.assertTrue(reader.closed)
        self.assertEqual(_stored_kinds(self.manager, "1001")[0], "heart_rate_zones")
        self.assertEqual(_stored_kinds(self.manager, "1002"), [])
        paused = datetime.fromisoformat(_stored_state(self.key_values)["paused_until"])
        self.assertEqual(paused, NOW_AT + BACKFILL_RATE_LIMIT_PAUSE)

    def test_a_rate_limit_before_the_file_keeps_the_stored_full_sets(self) -> None:
        # #3122: a 429 mid-activity leaves only chart streams and splits; they
        # must not replace the full streams and device laps of an earlier fetch.
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        full = _with_fit()
        full["activity_id"] = "1001"
        self.service(_Details({"1001": full})).backfill()
        before = _grades(self.manager, "1001")
        self.assertEqual(before["streams"][0], "full")
        _set_summary_hash(self.manager, "1001")
        summary = _summary_hash(self.manager, "1001")

        result = self.service(
            _Details({"1001": _payload("1001", rate_limited=True)})
        ).backfill()

        self.assertEqual(result["status"], "rate_limited")
        after = _grades(self.manager, "1001")
        self.assertEqual(
            (after["streams"], after["laps"]), (before["streams"], before["laps"])
        )
        # The attempt counts as incomplete, so a later sync fetches it again.
        self.assertEqual(
            _stored_state(self.key_values)["retry"], {"1001": [summary, 1]}
        )
        self.assertEqual(
            json.loads(self.key_values.values[DETAIL_FALLBACK_KEY]),
            {"kept": 2, "stored": 0},
        )

    def test_a_paused_file_keeps_the_full_sets_without_spending_retries(self) -> None:
        # The original file is a Personal API part: while it is paused the fetch
        # falls back too, but a pause is no attempt (#3122).
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        full = _with_fit()
        full["activity_id"] = "1001"
        self.service(_Details({"1001": full})).backfill()
        before = _grades(self.manager, "1001")
        _set_summary_hash(self.manager, "1001")
        summary = _summary_hash(self.manager, "1001")
        paused = _payload("1001", skipped=["activity_fit"])

        for _ in range(BACKFILL_RETRY_ATTEMPTS + 1):
            self.service(_Details({"1001": paused})).backfill()

        self.assertEqual(_grades(self.manager, "1001")["streams"], before["streams"])
        self.assertEqual(
            _stored_state(self.key_values)["retry"], {"1001": [summary, 0]}
        )

    def test_partial_fetch_is_retried_until_it_completes(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        summary = _summary_hash(self.manager, "1001")

        first = self.service(_Details({"1001": _partial("1001")})).backfill()

        self.assertEqual(first["fetched"], 1)
        self.assertEqual(
            _stored_state(self.key_values)["retry"], {"1001": [summary, 1]}
        )
        self.assertNotIn("laps", _stored_kinds(self.manager, "1001"))

        reader = _Details({"1001": _payload("1001")})
        second = self.service(reader).backfill()

        self.assertEqual(reader.fetched, ["1001"])
        self.assertEqual(second["fetched"], 1)
        self.assertNotIn("retry", _stored_state(self.key_values))
        self.assertIn("laps", _stored_kinds(self.manager, "1001"))

    def test_part_skipped_while_paused_is_retried(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        summary = _summary_hash(self.manager, "1001")
        paused = _payload("1001")
        paused.pop("splits")
        paused["skipped"] = ["activity_splits"]

        # More runs than the retry limit while the part stays paused: a paused
        # part is no attempt, so the activity keeps its retries.
        for _ in range(4):
            self.service(_Details({"1001": paused})).backfill()

        self.assertEqual(
            _stored_state(self.key_values)["retry"], {"1001": [summary, 0]}
        )
        reader = _Details({"1001": _payload("1001")})
        self.service(reader).backfill()

        self.assertEqual(reader.fetched, ["1001"])
        self.assertNotIn("retry", _stored_state(self.key_values))

    def test_fully_skipped_activity_is_not_marked_empty(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])

        result = self.service(
            _Details({"1001": {"activity_id": "1001", "skipped": ["activity_details"]}})
        ).backfill()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(_stored_state(self.key_values)["empty"], {})
        self.assertEqual(_candidate_ids(self.manager), ["1001"])

    def test_failing_activity_stops_holding_back_older_ones(self) -> None:
        _seed(
            self.manager,
            [("1002", "2026-10-02 07:00:00"), ("1001", "2026-10-01 07:00:00")],
        )
        summary = _summary_hash(self.manager, "1002")
        failed = {
            "activity_id": "1002",
            "errors": [{"source": "activity_details", "message": "synthetic"}],
        }
        for _attempt in range(BACKFILL_RETRY_ATTEMPTS):
            reader = _Details({"1002": failed, "1001": _payload("1001")})
            result = self.service(reader).backfill()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(reader.fetched, ["1002"])

        reader = _Details({"1002": failed, "1001": _payload("1001")})
        result = self.service(reader).backfill()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(reader.fetched, ["1001"])
        self.assertEqual(
            _stored_state(self.key_values)["retry"],
            {"1002": [summary, BACKFILL_RETRY_ATTEMPTS]},
        )

    def test_rate_limit_without_stored_parts_is_not_an_attempt(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        limited = {
            "activity_id": "1001",
            "errors": [{"source": "activity_details", "message": "synthetic"}],
            "rate_limited": True,
        }

        result = self.service(_Details({"1001": limited})).backfill()

        self.assertEqual(result["status"], "rate_limited")
        self.assertNotIn("retry", _stored_state(self.key_values))

    def test_partial_fetch_retries_are_bounded_per_summary(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        summary = _summary_hash(self.manager, "1001")
        for _attempt in range(BACKFILL_RETRY_ATTEMPTS):
            self.service(_Details({"1001": _partial("1001")})).backfill()

        reader = _Details({"1001": _partial("1001")})
        result = self.service(reader).backfill()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(reader.calls, [])
        self.assertEqual(
            _stored_state(self.key_values)["retry"],
            {"1001": [summary, BACKFILL_RETRY_ATTEMPTS]},
        )

        # A changed summary makes the stored sets outdated and restarts the count.
        _set_summary_hash(self.manager, "1001")
        reader = _Details({"1001": _partial("1001")})
        self.service(reader).backfill()

        self.assertEqual(reader.fetched, ["1001"])
        self.assertEqual(
            _stored_state(self.key_values)["retry"], {"1001": [CHANGED_HASH, 1]}
        )

    def test_rate_limited_partial_fetch_is_retried_after_the_pause(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])

        self.service(_Details({"1001": _partial("1001", rate_limited=True)})).backfill()
        self.now = (
            NOW_AT + BACKFILL_RATE_LIMIT_PAUSE + timedelta(minutes=1)
        ).isoformat()
        reader = _Details({"1001": _payload("1001")})
        result = self.service(reader).backfill()

        self.assertEqual(reader.fetched, ["1001"])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("retry", _stored_state(self.key_values))

    def test_rate_limit_exception_pauses_without_failing(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        reader = _Details({"1001": AppError(429, "synthetic", reason="garmin_429")})

        result = self.service(reader).backfill()

        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["fetched"], 0)
        self.assertTrue(reader.closed)
        self.assertEqual(
            datetime.fromisoformat(_stored_state(self.key_values)["paused_until"]),
            NOW_AT + BACKFILL_RATE_LIMIT_PAUSE,
        )

    def test_pause_blocks_remote_calls_until_it_expires(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        paused_until = NOW_AT + timedelta(hours=1)
        self.key_values.values[BACKFILL_STATE_KEY] = json.dumps(
            {"empty": {}, "paused_until": paused_until.isoformat()}, sort_keys=True
        )
        reader = _Details({"1001": _payload("1001")})

        paused = self.service(reader).backfill()

        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["remaining"], 1)
        self.assertEqual(reader.calls, [])
        self.assertEqual(
            datetime.fromisoformat(_stored_state(self.key_values)["paused_until"]),
            paused_until,
        )

        self.now = (NOW_AT + timedelta(hours=2)).isoformat()
        resumed = self.service(reader).backfill()

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["fetched"], 1)
        self.assertNotIn("paused_until", _stored_state(self.key_values))

    def test_unavailable_detail_capability_gives_no_remote_call(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        reader = _Details({"1001": _payload("1001")})

        result = self.service(
            reader, allowed=lambda capability: capability != "activity_details"
        ).backfill()

        self.assertEqual(result["status"], "paused")
        self.assertEqual(reader.calls, [])

    def test_paused_sdk_only_parts_do_not_pause_the_backfill(self) -> None:
        sdk_parts = {"activity_splits", "activity_hr_zones", "activity_power_zones"}
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        reader = _Details({"1001": _payload("1001")})

        result = self.service(
            reader, allowed=lambda capability: capability not in sdk_parts
        ).backfill()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(reader.fetched, ["1001"])

    def test_all_parts_failing_stops_without_marking(self) -> None:
        _seed(
            self.manager,
            [("1001", "2026-10-02 07:00:00"), ("1002", "2026-10-01 07:00:00")],
        )
        reader = _Details(
            {
                "1001": {
                    "activity_id": "1001",
                    "errors": [{"source": "activity_details", "message": "synthetic"}],
                },
                "1002": _payload("1002"),
            }
        )

        result = self.service(reader).backfill()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["empty"], 0)
        self.assertEqual(reader.fetched, ["1001"])
        self.assertEqual(_stored_state(self.key_values).get("empty", {}), {})
        self.assertEqual(_stored_kinds(self.manager, "1001"), [])

    def test_unexpected_error_is_logged_by_type_only(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        reader = _Details({"1001": RuntimeError("PRIVATE-PROVIDER-TEXT")})

        with self.assertLogs("intervals_coach", "WARNING") as logs:
            result = self.service(reader).backfill()

        self.assertEqual(result["status"], "failed")
        self.assertTrue(reader.closed)
        self.assertEqual(
            vars(logs.records[0])["context"], {"error_type": "RuntimeError"}
        )
        joined = str(logs.output) + str(result)
        self.assertNotIn("PRIVATE-PROVIDER-TEXT", joined)

    def test_cancel_between_activities_stops_the_batch(self) -> None:
        _seed(
            self.manager,
            [("1001", "2026-10-02 07:00:00"), ("1002", "2026-10-01 07:00:00")],
        )
        cancel = threading.Event()
        reader = _Details(
            {"1001": _payload("1001"), "1002": _payload("1002")},
            pre_yield={"1001": cancel.set},
        )

        result = self.service(reader).backfill(cancel_event=cancel)

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(reader.fetched, ["1001"])
        self.assertIs(reader.calls[0][1], cancel)
        self.assertTrue(reader.closed)

    def test_unconfigured_garmin_gives_not_configured_without_reader_call(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        reader = _Details({"1001": _payload("1001")})

        result = self.service(reader, configured=False).backfill()

        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(reader.calls, [])

    def test_empty_window_completes_without_reader_call(self) -> None:
        reader = _Details({})

        result = self.service(reader).backfill()

        self.assertEqual(
            result,
            {
                "status": "completed",
                "fetched": 0,
                "empty": 0,
                "remaining": 0,
                "replayed": 0,
                "replay_remaining": 0,
            },
        )
        self.assertEqual(reader.calls, [])


# A day with a 429 or a Garmin error: the budget is at its start and does not double.
DIRTY_DAY = {"day": NOW[:10], "budget": 20, "clean": False}


def _full(activity_id: str) -> dict[str, Any]:
    payload = _with_fit()
    payload["activity_id"] = activity_id
    return payload


def _seed_outdated(case: _Base, rows: list[tuple[str, str]]) -> None:
    """Store full sets for the rows as the mapper before the bump wrote them."""
    _seed(case.manager, rows)
    with patch.object(garmin_activity_details, "MAPPER_VERSION", MAPPER_VERSION - 1):
        for _refresh in range(-(-len(rows) // BACKFILL_BATCH_SIZE)):
            case.service(_Details({aid: _full(aid) for aid, _start in rows})).backfill()
    case.key_values.values.clear()


def _days(count: int) -> list[tuple[str, str]]:
    return [
        (str(2000 + index), f"2026-09-{index + 1:02d} 07:00:00")
        for index in range(count)
    ]


class BackfillReplayTests(_Base):
    """A mapper bump re-fetches the window slowly and never before new activities (#2220)."""

    def test_the_candidate_query_tells_replays_from_new_and_changed_activities(
        self,
    ) -> None:
        _seed_outdated(
            self, [("1001", "2026-10-01 07:00:00"), ("1003", "2026-10-03 07:00:00")]
        )
        _set_summary_hash(self.manager, "1003")
        _seed(self.manager, [("1002", "2026-10-02 07:00:00")])
        with self.manager.reader() as db:
            rows = activity_detail_backfill_candidates(
                db,
                GARMIN_ID,
                ACTIVITY_RECORD_KIND,
                since=SINCE,
                limit=10,
                mapper_version=MAPPER_VERSION,
            )

        # Replay: sets of the current summary, only their version is older. A new
        # activity and one whose summary changed carry no set of the current one.
        self.assertEqual(
            {row["provider_activity_id"]: row["replay"] for row in rows},
            {"1003": False, "1002": False, "1001": True},
        )

    def test_a_retry_with_up_to_date_sets_is_not_a_replay(self) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        self.service(_Details({"1001": _payload("1001")})).backfill()
        with self.manager.reader() as db:
            rows = activity_detail_backfill_candidates(
                db,
                GARMIN_ID,
                ACTIVITY_RECORD_KIND,
                since=SINCE,
                limit=10,
                mapper_version=MAPPER_VERSION,
                retry_ids=("1001",),
            )
        self.assertEqual([row["replay"] for row in rows], [False])

    def test_replays_start_at_twenty_per_refresh_and_twenty_per_day(self) -> None:
        rows = _days(30)
        _seed_outdated(self, rows)
        reader = _Details({aid: _full(aid) for aid, _start in rows})
        result = self.service(reader).backfill()
        # One refresh may take the whole day's budget of 20 (orchestrator, 2026-11-16).
        self.assertEqual(
            (len(reader.fetched), result["replayed"], result["fetched"]), (20, 20, 20)
        )
        self.assertEqual(reader.fetched, [aid for aid, _start in reversed(rows)][:20])
        self.assertEqual(
            _stored_state(self.key_values)["replay"],
            {"day": NOW[:10], "count": 20, "budget": 20, "clean": True},
        )

        # The day's budget is spent: nothing runs, and the rest waits.
        reader = _Details({})
        result = self.service(reader).backfill()
        self.assertEqual(reader.calls, [])
        self.assertEqual(
            result,
            {
                "status": "completed",
                "fetched": 0,
                "empty": 0,
                "remaining": 0,
                "replayed": 0,
                "replay_remaining": 10,
            },
        )

        # A clean day doubles the budget to 40: the next UTC day takes the last ten.
        self.now = "2026-10-19T00:30:00+00:00"
        reader = _Details({aid: _full(aid) for aid, _start in rows})
        result = self.service(reader).backfill()
        self.assertEqual((result["replayed"], result["replay_remaining"]), (10, 0))
        self.assertEqual(
            _stored_state(self.key_values)["replay"],
            {"day": "2026-10-19", "count": 10, "budget": 40, "clean": True},
        )

    def test_the_budget_doubles_after_a_clean_day_up_to_eighty(self) -> None:
        def counts(replay: dict[str, Any] | None, today: str) -> dict[str, Any]:
            raw = None if replay is None else json.dumps({"replay": replay})
            return replay_budget_counts(raw, today)

        def day(count: int, budget: int, clean: bool = True) -> dict[str, Any]:
            return {
                "day": "2026-10-17",
                "count": count,
                "budget": budget,
                "clean": clean,
            }

        today = "2026-10-18"
        start = {
            "daily_budget": 20,
            "per_refresh": 20,
            "replayed_today": 0,
            "clean_today": True,
        }
        self.assertEqual(counts(None, today), start)
        # An old state without the budget keys is the start state.
        self.assertEqual(counts({"day": "2026-10-17", "count": 0}, today), start)
        # A clean day with replay fetches doubles: 20, 40, 80, then the cap; a
        # refresh takes at most 20 of it.
        for before, after, per_refresh in ((20, 40, 20), (40, 80, 20), (80, 80, 20)):
            self.assertEqual(
                counts(day(5, before), today),
                {
                    "daily_budget": after,
                    "per_refresh": per_refresh,
                    "replayed_today": 0,
                    "clean_today": True,
                },
            )
        # A day with an error, or without replay fetches, leaves it unchanged.
        self.assertEqual(counts(day(5, 40, clean=False), today)["daily_budget"], 40)
        self.assertEqual(counts(day(0, 40), today)["daily_budget"], 40)
        # The same day keeps its count and does not double again; a refresh takes
        # what is left of the day's budget, at most 20.
        same = {"day": today, "count": 7, "budget": 40, "clean": False}
        self.assertEqual(
            counts(same, today),
            {
                "daily_budget": 40,
                "per_refresh": 20,
                "replayed_today": 7,
                "clean_today": False,
            },
        )
        self.assertEqual(counts(same | {"count": 33}, today)["per_refresh"], 7)
        self.assertEqual(counts(same | {"count": 40}, today)["per_refresh"], 0)

    def _budget_state(self, **replay: Any) -> None:
        stored = {"day": "2026-10-17", "count": 5, "budget": 40, "clean": True}
        self.key_values.values[BACKFILL_STATE_KEY] = json.dumps(
            {"empty": {}, "replay": {**stored, **replay}}
        )

    def test_a_clean_day_doubles_the_budget_on_the_next_refresh(self) -> None:
        rows = _days(30)
        _seed_outdated(self, rows)
        self._budget_state(budget=20)

        result = self.service(_Details({a: _full(a) for a, _s in rows})).backfill()

        # The doubled budget of 40 allows 20 in one refresh, the per-refresh cap.
        self.assertEqual(result["replayed"], 20)
        self.assertEqual(
            _stored_state(self.key_values)["replay"],
            {"day": NOW[:10], "count": 20, "budget": 40, "clean": True},
        )

    def test_a_day_without_replays_leaves_the_budget_alone(self) -> None:
        self._budget_state(count=0)

        self.service(_Details({})).backfill()

        self.assertEqual(
            _stored_state(self.key_values)["replay"],
            {"day": NOW[:10], "count": 0, "budget": 40, "clean": True},
        )

    def test_a_garmin_error_keeps_the_day_from_doubling(self) -> None:
        _seed_outdated(self, _days(2))
        self._budget_state(day=NOW[:10], count=0, budget=20)
        failed = _payload(
            "2001", errors=[{"source": "activity_splits", "message": "synthetic"}]
        )

        self.service(_Details({"2001": failed, "2000": _full("2000")})).backfill()

        replay = _stored_state(self.key_values)["replay"]
        self.assertEqual(
            (replay["count"], replay["budget"], replay["clean"]), (2, 20, False)
        )
        self.now = "2026-10-19T08:00:00+00:00"
        self.assertEqual(
            replay_budget_counts(
                self.key_values.values[BACKFILL_STATE_KEY], "2026-10-19"
            )["daily_budget"],
            20,
        )

    def test_a_429_in_a_replay_resets_the_budget_and_keeps_the_pause(self) -> None:
        _seed_outdated(self, _days(3))
        self._budget_state(day=NOW[:10], count=0, budget=80)
        reader = _Details({"2002": _payload("2002", rate_limited=True)})

        result = self.service(reader).backfill()

        self.assertEqual(result["status"], "rate_limited")
        state = _stored_state(self.key_values)
        self.assertEqual(
            state["replay"], {"day": NOW[:10], "count": 1, "budget": 20, "clean": False}
        )
        self.assertEqual(
            datetime.fromisoformat(state["paused_until"]),
            NOW_AT + BACKFILL_RATE_LIMIT_PAUSE,
        )
        # The day after the pause the budget starts again at 20, not doubled.
        self.assertEqual(
            replay_budget_counts(
                self.key_values.values[BACKFILL_STATE_KEY], "2026-10-19"
            )["daily_budget"],
            20,
        )

    def test_a_429_exception_with_replays_in_the_batch_resets_the_budget(self) -> None:
        _seed_outdated(self, _days(2))
        self._budget_state(day=NOW[:10], count=0, budget=40)
        reader = _Details({"2001": AppError(429, "synthetic", reason="garmin_429")})

        self.service(reader).backfill()

        replay = _stored_state(self.key_values)["replay"]
        self.assertEqual((replay["budget"], replay["clean"]), (20, False))

    def test_any_429_resets_the_budget_even_without_replays_in_the_batch(self) -> None:
        # Any 429 drops the budget back to the start (orchestrator, 2026-11-16).
        _seed(self.manager, [("3001", "2026-10-10 07:00:00")])
        self._budget_state(day=NOW[:10], count=0, budget=40)

        self.service(_Details({"3001": _payload("3001", rate_limited=True)})).backfill()

        replay = _stored_state(self.key_values)["replay"]
        self.assertEqual((replay["budget"], replay["clean"]), (20, False))

    def test_a_garmin_error_on_a_new_activity_also_ends_the_clean_day(self) -> None:
        _seed(self.manager, [("3001", "2026-10-10 07:00:00")])
        self._budget_state(day=NOW[:10], count=3, budget=40)
        failed = _payload(
            "3001", errors=[{"source": "activity_splits", "message": "synthetic"}]
        )

        self.service(_Details({"3001": failed})).backfill()

        replay = _stored_state(self.key_values)["replay"]
        self.assertEqual((replay["budget"], replay["clean"]), (40, False))

    def test_new_activities_come_first_and_replays_fill_the_rest(self) -> None:
        rows = _days(12)
        _seed_outdated(self, rows)
        fresh = [("3001", "2026-10-10 07:00:00"), ("3002", "2026-10-09 07:00:00")]
        _seed(self.manager, fresh)
        reader = _Details(
            {aid: _full(aid) for aid, _start in [*rows, *fresh]},
        )

        result = self.service(reader).backfill()

        ids = reader.calls[0][0]
        self.assertEqual(ids[:2], ("3001", "3002"))
        self.assertEqual(len(ids), 2 + 12)
        self.assertEqual(
            (result["fetched"], result["replayed"], result["remaining"]), (14, 12, 0)
        )
        self.assertEqual(result["replay_remaining"], 0)

    def test_a_full_batch_of_new_activities_still_leaves_the_replays_room(
        self,
    ) -> None:
        # New activities keep their batch of ten; replays follow up to 20.
        outdated = [
            (str(2000 + index), f"2026-08-{index + 1:02d} 07:00:00")
            for index in range(25)
        ]
        _seed_outdated(self, outdated)
        fresh = [
            (str(3000 + index), f"2026-10-{index + 1:02d} 07:00:00")
            for index in range(12)
        ]
        _seed(self.manager, fresh)
        reader = _Details({aid: _full(aid) for aid, _start in [*outdated, *fresh]})

        result = self.service(reader).backfill()

        ids = reader.calls[0][0]
        self.assertEqual(len(ids), BACKFILL_BATCH_SIZE + 20)
        self.assertTrue(all(aid.startswith("30") for aid in ids[:BACKFILL_BATCH_SIZE]))
        self.assertEqual((result["fetched"], result["replayed"]), (30, 20))
        self.assertEqual((result["remaining"], result["replay_remaining"]), (2, 5))

    def test_the_stop_switch_keeps_outdated_sets_and_runs_only_new_ones(self) -> None:
        _seed_outdated(self, _days(3))
        _seed(self.manager, [("3001", "2026-10-10 07:00:00")])
        reader = _Details({"3001": _full("3001")})

        with patch.object(garmin_activity_details, "REPLAY_ENABLED", False):
            result = self.service(reader).backfill()

        self.assertEqual(reader.fetched, ["3001"])
        self.assertEqual((result["replayed"], result["replay_remaining"]), (0, 0))
        self.assertNotIn("replay", _stored_state(self.key_values))
        with self.manager.reader() as db:
            versions = {
                item.mapper_version for item in detail_sets_for(db, GARMIN_ID, "2000")
            }
        self.assertEqual(versions, {MAPPER_VERSION - 1})

    def test_a_state_stored_before_the_replay_still_parses(self) -> None:
        _seed_outdated(self, _days(2))
        summary = _summary_hash(self.manager, "2000")
        older = {"empty": {"9999": "x"}, "retry": {"9998": [summary, 1]}}
        self.key_values.values[BACKFILL_STATE_KEY] = json.dumps(older)

        self.service(_Details({aid: _full(aid) for aid, _s in _days(2)})).backfill()

        state = _stored_state(self.key_values)
        self.assertEqual(
            state["replay"], {"day": NOW[:10], "count": 2, "budget": 20, "clean": True}
        )

        # Unreadable replay entries count as no attempts.
        for broken in (
            {"day": 20261018, "count": "7"},
            {"day": NOW[:10], "count": -3},
            {"count": True, "budget": 7, "clean": "yes"},
            {"budget": 500},
            "x",
        ):
            parsed = _parse_backfill_state(json.dumps({"replay": broken})).replay
            self.assertEqual((parsed.count, parsed.budget, parsed.clean), (0, 20, True))
        start = _parse_backfill_state(json.dumps(older)).replay
        self.assertEqual((start.count, start.budget, start.clean), (0, 20, True))

    def test_a_replay_with_the_file_replaces_the_sets_and_ends_the_candidacy(
        self,
    ) -> None:
        _seed_outdated(self, [("1001", "2026-10-01 07:00:00")])

        result = self.service(_Details({"1001": _full("1001")})).backfill()

        self.assertEqual(result["replayed"], 1)
        with self.manager.reader() as db:
            versions = {
                item.mapper_version for item in detail_sets_for(db, GARMIN_ID, "1001")
            }
        self.assertEqual(versions, {MAPPER_VERSION})
        self.assertEqual(_candidate_ids(self.manager), [])

    def test_a_rate_limited_replay_keeps_the_full_sets_pauses_and_counts(self) -> None:
        # #3122/#3128: a 429 before the file leaves chart streams and splits, which
        # never replace the stored full streams and device laps.
        _seed_outdated(
            self, [("1002", "2026-10-02 07:00:00"), ("1001", "2026-10-01 07:00:00")]
        )
        before = _grades(self.manager, "1002")
        summary = _summary_hash(self.manager, "1002")
        reader = _Details(
            {"1002": _payload("1002", rate_limited=True), "1001": _full("1001")}
        )

        result = self.service(reader).backfill()

        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(reader.fetched, ["1002"])
        after = _grades(self.manager, "1002")
        self.assertEqual(
            (after["streams"], after["laps"]), (before["streams"], before["laps"])
        )
        state = _stored_state(self.key_values)
        self.assertEqual(state["replay"], DIRTY_DAY | {"count": 1})
        self.assertEqual(state["retry"], {"1002": [summary, 1]})
        self.assertEqual(
            datetime.fromisoformat(state["paused_until"]),
            NOW_AT + BACKFILL_RATE_LIMIT_PAUSE,
        )
        # The 24 hour pause holds: nothing is fetched until it ends.
        later = _Details({})
        self.assertEqual(self.service(later).backfill()["status"], "paused")
        self.assertEqual(later.calls, [])

    def test_a_rate_limit_error_counts_the_replay_in_flight(self) -> None:
        _seed_outdated(self, _days(2))
        reader = _Details({"2001": AppError(429, "synthetic", reason="garmin_429")})

        result = self.service(reader).backfill()

        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(
            _stored_state(self.key_values)["replay"], DIRTY_DAY | {"count": 1}
        )

    def test_a_store_error_counts_only_the_replay_that_was_fetched(self) -> None:
        # The error comes while storing 2001; 2000 was never fetched (#3208 review).
        _seed_outdated(self, _days(2))
        reader = _Details({"2001": _full("2001"), "2000": _full("2000")})
        service = self.service(reader)

        with patch.object(
            service, "_store_records", side_effect=RuntimeError("database is locked")
        ):
            result = service.backfill()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(reader.fetched, ["2001"])
        self.assertEqual(_stored_state(self.key_values)["replay"]["count"], 1)

    def test_a_replay_whose_file_call_failed_keeps_the_sets_and_is_passed_over(
        self,
    ) -> None:
        _seed_outdated(self, [("1001", "2026-10-01 07:00:00")])
        before = _grades(self.manager, "1001")
        summary = _summary_hash(self.manager, "1001")
        failed = _payload(
            "1001", errors=[{"source": "activity_fit", "message": "synthetic"}]
        )

        for attempt in range(1, BACKFILL_RETRY_ATTEMPTS + 1):
            self.service(_Details({"1001": failed})).backfill()
            self.assertEqual(
                _stored_state(self.key_values)["retry"], {"1001": [summary, attempt]}
            )
            self.assertEqual(
                _grades(self.manager, "1001")["streams"], before["streams"]
            )

        reader = _Details({"1001": failed})
        self.service(reader).backfill()
        self.assertEqual(reader.calls, [])
        # Every attempt, successful or not, counted against the day.
        self.assertEqual(
            _stored_state(self.key_values)["replay"],
            DIRTY_DAY | {"count": BACKFILL_RETRY_ATTEMPTS},
        )

    def test_a_replay_the_file_never_reaches_cannot_block_the_others(self) -> None:
        # No file and no error (a transport without it): the full sets stay, and
        # each such attempt is incomplete, so the record is passed over.
        _seed_outdated(self, _days(2))
        for _attempt in range(BACKFILL_RETRY_ATTEMPTS):
            self.service(
                _Details({"2001": _payload("2001"), "2000": _full("2000")})
            ).backfill()

        reader = _Details({"2001": _payload("2001"), "2000": _full("2000")})
        self.service(reader).backfill()

        self.assertEqual(reader.calls, [])
        with self.manager.reader() as db:
            versions = {
                item.detail.kind.value: item.mapper_version
                for item in detail_sets_for(db, GARMIN_ID, "2001")
            }
        # The full streams and laps stay; only the parts the payload brought moved on.
        self.assertEqual(
            (versions["streams"], versions["laps"]),
            (MAPPER_VERSION - 1, MAPPER_VERSION - 1),
        )


class BackfillAssemblyTests(unittest.TestCase):
    def test_sync_service_runs_the_backfill_of_the_composed_detail_service(
        self,
    ) -> None:
        services = garmin_services()
        state = Mock(name="state")
        with patch.object(garmin, "GarminSyncStateService", return_value=state):
            detail_service = services.activity_detail_service()
            with patch.object(
                garmin_wiring,
                "GarminActivityDetailService",
                return_value=detail_service,
            ):
                sync_service = services.sync_service()

        self.assertIs(detail_service._capability_allowed, state.capability_allowed)
        self.assertIsNotNone(detail_service._key_values)
        self.assertEqual(
            sync_service._activity_detail_backfill, detail_service.backfill
        )


def _real_reader(garmin_tokens: Any, factory: Any) -> GarminRemoteReader:
    return GarminRemoteReader(
        make_config(
            openai_base_url="",
            openai_model="",
            intervals_athlete_id="",
            data_retention_days=30,
            garmin_email="athlete@example.invalid",
            garmin_password="secret",
            garmin_tokenstore="missing-tokenstore",
        ),
        factory,
        Mock(),
        Mock(),
        lambda value: value,
        Mock(),
        lambda: NOW,
        lambda: date(2026, 10, 18),
        date(2000, 1, 1),
        90,
        -1,
        garmin_tokens=garmin_tokens,
    )


class BackfillReaderLifecycleTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.client = Mock()
        self.client.login.return_value = (False, None)
        self.factory = Mock()
        self.factory.available.return_value = True
        self.factory.create.return_value = self.client
        self.tokens = Mock()
        self.tokens.login_argument.return_value = '{"di_token": "synthetic"}'

    def _run(self, collect: Any, *, cancel: threading.Event | None = None):
        reader = _real_reader(self.tokens, self.factory)
        service = self.service(reader)
        with (
            patch(
                "backend.sync.garmin_service.provider_http.external_call",
                side_effect=lambda _service, _operation, call, _details, **_kwargs: (
                    call()
                ),
            ),
            patch(
                "backend.sync.garmin_service.collect_garmin_activity_detail",
                side_effect=collect,
            ) as collector,
        ):
            result = service.backfill(cancel_event=cancel)
        return result, collector

    def test_one_authentication_and_token_write_back_serve_the_whole_batch(
        self,
    ) -> None:
        _seed(
            self.manager,
            [("1001", "2026-10-02 07:00:00"), ("1002", "2026-10-01 07:00:00")],
        )

        def collect(_client, activity_id, **_kwargs):
            return _payload(activity_id)

        result, collector = self._run(collect)

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(collector.call_count, 2)
        self.factory.create.assert_called_once()
        self.client.login.assert_called_once_with('{"di_token": "synthetic"}')
        self.tokens.write_back.assert_called_once_with(
            self.client, '{"di_token": "synthetic"}'
        )

    def test_tokens_are_persisted_when_the_batch_stops_early(self) -> None:
        _seed(
            self.manager,
            [("1001", "2026-10-02 07:00:00"), ("1002", "2026-10-01 07:00:00")],
        )
        cancel = threading.Event()

        def collect(_client, activity_id, **_kwargs):
            cancel.set()
            return _payload(activity_id)

        result, collector = self._run(collect, cancel=cancel)

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(collector.call_count, 1)
        self.tokens.write_back.assert_called_once_with(
            self.client, '{"di_token": "synthetic"}'
        )

    def test_reported_429_on_one_activity_pauses_the_batch(self) -> None:
        _seed(
            self.manager,
            [("1001", "2026-10-02 07:00:00"), ("1002", "2026-10-01 07:00:00")],
        )

        def collect(_client, activity_id, **kwargs):
            if activity_id == "1001":
                kwargs["capability_failure"](
                    "activity_details", AppError(429, "synthetic")
                )
            return _payload(activity_id)

        result, collector = self._run(collect)

        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(collector.call_count, 1)
        self.assertEqual(
            datetime.fromisoformat(_stored_state(self.key_values)["paused_until"]),
            NOW_AT + BACKFILL_RATE_LIMIT_PAUSE,
        )
        self.tokens.write_back.assert_called_once()

    def test_429_on_one_part_skips_the_remaining_parts_of_the_activity(
        self,
    ) -> None:
        _seed(self.manager, [("1001", "2026-10-01 07:00:00")])
        self.client.get_activity_details.side_effect = AppError(429, "synthetic")
        reader = _real_reader(self.tokens, self.factory)

        with patch(
            "backend.sync.garmin_service.provider_http.external_call",
            side_effect=lambda _service, _operation, call, _details, **_kwargs: call(),
        ):
            result = self.service(reader).backfill()

        self.assertEqual(result["status"], "rate_limited")
        self.client.get_activity_details.assert_called_once()
        self.client.get_activity_splits.assert_not_called()
        self.client.get_activity_hr_in_timezones.assert_not_called()
        self.client.get_activity_power_in_timezones.assert_not_called()


if __name__ == "__main__":
    unittest.main()
