"""The one-off 6-month re-read of Garmin's range-capable training history (#3729).

Synthetic answers in the shapes the training mapper reads (the endurance score's
``groupMap`` by day, running tolerance as dated rows); temporary SQLite and fake
clients only, no Garmin account or token.
"""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, date, timedelta
from typing import Any
from unittest.mock import Mock

from support import temp_database

from backend.db import garmin_store
from backend.db.repositories import KeyValueRepository
from backend.db.schema import initialize_schema
from backend.providers.garmin import (
    HISTORY_READ_DAYS,
    GarminCollectionOptions,
    collect_garmin_data,
)
from backend.sync.garmin import (
    TRAINING_REFETCH_DAYS,
    GarminPayloadService,
    summarize_history_refetch,
)
from backend.sync.garmin_service import GarminTargetedRefreshService
from backend.sync.garmin_snapshot_writer import GarminSnapshotWriter
from backend.sync.state import SyncStateRepository

TODAY = date(2026, 11, 20)
NOW = "2026-11-20T12:00:00+00:00"
OLD = (TODAY - timedelta(days=150)).isoformat()
RECENT = (TODAY - timedelta(days=10)).isoformat()
REGULAR_START = (TODAY - timedelta(days=HISTORY_READ_DAYS - 1)).isoformat()


def _external_call(_service: str, _source: str, call: Any, _details: Any) -> Any:
    return call()


class _Client:
    """Garmin's training history by day; each range read answers the days inside it."""

    def __init__(
        self,
        days: list[str],
        fail_wide: bool = False,
        empty_wide: bool = False,
        weekly: bool = False,
    ) -> None:
        self.days = days
        self.fail_wide = fail_wide
        self.empty_wide = empty_wide
        if weekly:
            # A section that answers without days (#3756).
            self.get_weekly_training_status = self._weekly
        self.calls: list[tuple[str, str]] = []

    def get_activities_by_date(self, start: str, end: str) -> list[Any]:
        return []

    def get_sleep_daily(self, start: str, end: str) -> list[Any]:
        return []

    def get_hrv_data_range(self, start: str, end: str) -> list[Any]:
        return []

    def _inside(self, start: str, end: str) -> list[str]:
        self.calls.append((start, end))
        if self.fail_wide and start < REGULAR_START:
            raise RuntimeError("range too long")
        if self.empty_wide and start < REGULAR_START:
            return []
        return [day for day in self.days if start <= day <= end]

    def get_endurance_score(self, start: str, end: str, aggregation: str) -> Any:
        days = self._inside(start, end)
        return {
            "overallScore": 6850,
            "groupMap": {day: {"groupAverage": 6800, "groupMax": 6900} for day in days},
        }

    def get_running_tolerance(self, start: str, end: str, aggregation: str) -> Any:
        return [
            {"calendarDate": day, "toleranceScore": 72}
            for day in self._inside(start, end)
        ]

    def _weekly(self, start: str, end: str) -> Any:
        self.calls.append((start, end))
        return {}


class TrainingHistoryRefetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = temp_database(self, "test.sqlite3")
        self.key_values = KeyValueRepository(lambda: NOW)
        sync_state = SyncStateRepository(self.database, self.key_values, lambda: NOW)
        with self.database.unit_of_work() as db:
            initialize_schema(db)
        self.service = GarminPayloadService(
            self.database, self.key_values, sync_state, lambda: TODAY
        )

    def sync(
        self,
        client: _Client,
        history_days: int = HISTORY_READ_DAYS,
        allowed: Any = None,
    ) -> None:
        """Collect, prepare and store one regular sync, as the Garmin service does."""
        payload = collect_garmin_data(
            client,
            [(TODAY, TODAY)],
            start=TODAY,
            today=TODAY,
            synced_at=NOW,
            external_call=_external_call,
            redact=lambda _text: "redacted",
            capability_allowed=allowed,
            options=replace(
                GarminCollectionOptions(),
                include_historic_metrics=True,
                history_days=history_days,
            ),
        )
        payload = self.service.prepare_remote(payload)
        with self.database.unit_of_work() as db:
            GarminSnapshotWriter(self.key_values, lambda: UTC).write(db, payload)

    def refresh_performance(self, groupmap: dict[str, Any]) -> None:
        """One complete targeted Analyse refresh of the endurance score."""
        reader = Mock()
        reader.fetch_analysis_data.return_value = (
            {"endurance_score": {"overallScore": 7000, "groupMap": groupmap}},
            {"endurance_score": True},
        )
        coordination = Mock()
        coordination.acquire.return_value = True
        coordination.operation.return_value = nullcontext()
        service = GarminTargetedRefreshService(
            reader,
            self.database,
            GarminSnapshotWriter(self.key_values, lambda: UTC),
            self.service,
            coordination,
            lambda: NOW,
            lambda: True,
        )
        result = service.refresh_analysis(
            "performance", TODAY - timedelta(days=HISTORY_READ_DAYS - 1), TODAY
        )
        self.assertEqual(result["status"], "ok")

    def counts(self) -> dict[str, Any]:
        with self.database.reader() as db:
            return summarize_history_refetch(db)

    def stored(self) -> dict[str, Any]:
        with self.database.reader() as db:
            return garmin_store.read_sections(
                db, ["endurance_score", "running_tolerance"]
            )

    def test_the_reread_runs_once_and_its_days_outlive_the_regular_read(self) -> None:
        self.assertTrue(self.service.training_refetch_due())
        self.assertLessEqual(
            {
                "training_state": "pending",
                "training_added_days": None,
                "training_due_days": TRAINING_REFETCH_DAYS,
            }.items(),
            self.counts().items(),
        )
        client = _Client([OLD, RECENT])

        self.sync(client, TRAINING_REFETCH_DAYS)

        wide = (TODAY - timedelta(days=TRAINING_REFETCH_DAYS - 1)).isoformat()
        self.assertIn((wide, TODAY.isoformat()), client.calls)
        self.assertFalse(self.service.training_refetch_due())
        self.assertLessEqual(
            {
                "training_state": "finished",
                "training_added_days": 2,
                "training_due_days": 0,
            }.items(),
            self.counts().items(),
        )
        # A later regular read keeps the marker and the earlier days of both shapes.
        self.sync(_Client([RECENT]))
        self.assertFalse(self.service.training_refetch_due())
        stored = self.stored()
        self.assertEqual(set(stored["endurance_score"]["groupMap"]), {OLD, RECENT})
        self.assertEqual(
            {row["calendarDate"] for row in stored["running_tolerance"]}, {OLD, RECENT}
        )

    def test_a_targeted_refresh_keeps_the_reread_days_outside_its_span(self) -> None:
        self.sync(_Client([OLD, RECENT]), TRAINING_REFETCH_DAYS)

        self.refresh_performance({RECENT: {"groupAverage": 7100, "groupMax": 7200}})

        group_map = self.stored()["endurance_score"]["groupMap"]
        self.assertEqual(set(group_map), {OLD, RECENT})
        # Inside its span the refresh answer wins.
        self.assertEqual(group_map[RECENT]["groupAverage"], 7100)
        self.assertEqual(group_map[OLD]["groupAverage"], 6800)

    def test_a_section_without_days_does_not_keep_the_reread_due(self) -> None:
        client = _Client([OLD, RECENT], weekly=True)
        self.sync(client, TRAINING_REFETCH_DAYS)

        self.assertFalse(self.service.training_refetch_due())
        self.assertEqual(self.counts()["training_state"], "finished")
        # The sync widens its read only while the re-read is due, so later days
        # run no extra calls; a regular read keeps the marker.
        self.sync(_Client([RECENT], weekly=True))
        self.assertFalse(self.service.training_refetch_due())

    def test_a_failed_or_empty_reread_stays_due_and_keeps_the_regular_read(
        self,
    ) -> None:
        for client in (_Client([OLD, RECENT], fail_wide=True), _Client([])):
            with self.subTest(fail_wide=client.fail_wide):
                self.sync(client, TRAINING_REFETCH_DAYS)

                self.assertTrue(self.service.training_refetch_due())
                self.assertEqual(self.counts()["training_state"], "pending")
        # The regular 90-day read of the failed run was stored.
        self.assertIn(RECENT, self.stored()["endurance_score"]["groupMap"])

    def test_an_empty_reread_keeps_the_regular_answer(self) -> None:
        # Gitar #3734: an empty wide answer must not replace the regular one.
        self.sync(_Client([OLD, RECENT], empty_wide=True), TRAINING_REFETCH_DAYS)

        self.assertTrue(self.service.training_refetch_due())
        stored = self.stored()
        self.assertEqual(set(stored["endurance_score"]["groupMap"]), {RECENT})
        self.assertEqual(
            {row["calendarDate"] for row in stored["running_tolerance"]}, {RECENT}
        )

    def test_a_paused_section_leaves_the_reread_due(self) -> None:
        # Gitar #3734: a section the capability state pauses is not re-read, so
        # the re-read is not finished.
        self.sync(
            _Client([OLD, RECENT]),
            TRAINING_REFETCH_DAYS,
            allowed=lambda key: key != "running_tolerance",
        )

        self.assertTrue(self.service.training_refetch_due())
        self.assertNotIn("running_tolerance", self.stored())


if __name__ == "__main__":
    unittest.main()
