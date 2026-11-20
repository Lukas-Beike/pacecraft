"""The one-off 6-month re-read of Garmin's range-capable training history (#3729).

Synthetic answers shaped after the fixtures; temporary SQLite and fake clients
only, no Garmin account or token.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, timedelta
from typing import Any

from support import temp_database

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
from backend.sync.garmin_snapshot_writer import GarminSnapshotWriter
from backend.sync.state import SyncStateRepository

TODAY = date(2026, 11, 20)
NOW = "2026-11-20T12:00:00+00:00"
OLD = (TODAY - timedelta(days=150)).isoformat()
RECENT = (TODAY - timedelta(days=10)).isoformat()


def _external_call(_service: str, _source: str, call: Any, _details: Any) -> Any:
    return call()


class _Client:
    """Garmin's endurance scores by day; each range read answers the days inside it."""

    def __init__(self, days: list[str], fail: bool = False) -> None:
        self.days = days
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def get_activities_by_date(self, start: str, end: str) -> list[Any]:
        return []

    def get_sleep_daily(self, start: str, end: str) -> list[Any]:
        return []

    def get_hrv_data_range(self, start: str, end: str) -> list[Any]:
        return []

    def get_endurance_score(self, start: str, end: str, aggregation: str) -> Any:
        self.calls.append((start, end))
        if self.fail:
            raise RuntimeError("rate limited")
        return [
            {"calendarDate": day, "overallScore": 6850}
            for day in self.days
            if start <= day <= end
        ]


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

    def sync(self, client: _Client, history_days: int = HISTORY_READ_DAYS) -> None:
        """Collect, prepare and store one regular sync, as the Garmin service does."""
        payload = collect_garmin_data(
            client,
            [(TODAY, TODAY)],
            start=TODAY,
            today=TODAY,
            synced_at=NOW,
            external_call=_external_call,
            redact=lambda _text: "redacted",
            options=replace(
                GarminCollectionOptions(),
                include_historic_metrics=True,
                history_days=history_days,
            ),
        )
        payload = self.service.prepare_remote(payload)
        with self.database.unit_of_work() as db:
            GarminSnapshotWriter(self.key_values, lambda: UTC).write(db, payload)

    def counts(self) -> dict[str, Any]:
        with self.database.reader() as db:
            return summarize_history_refetch(db)

    def test_the_reread_runs_once_and_counts_its_days(self) -> None:
        self.assertTrue(self.service.training_refetch_due())
        self.assertEqual(
            self.counts(),
            {
                "training_state": "pending",
                "training_added_days": None,
                "training_due_days": TRAINING_REFETCH_DAYS,
            },
        )
        client = _Client([OLD, RECENT])

        self.sync(client, TRAINING_REFETCH_DAYS)

        start = (TODAY - timedelta(days=TRAINING_REFETCH_DAYS - 1)).isoformat()
        self.assertEqual(client.calls, [(start, TODAY.isoformat())])
        self.assertFalse(self.service.training_refetch_due())
        self.assertEqual(
            self.counts(),
            {
                "training_state": "finished",
                "training_added_days": 2,
                "training_due_days": 0,
            },
        )
        # A later regular read keeps the marker and the older day's element.
        self.sync(_Client([RECENT]))
        self.assertFalse(self.service.training_refetch_due())
        with self.database.reader() as db:
            days = {
                row["observed_on"]
                for row in db.execute(
                    "SELECT observed_on FROM provider_records "
                    "WHERE source_record_id LIKE 'endurance_score/%'"
                )
            }
        self.assertEqual(days, {OLD, RECENT})

    def test_a_failed_or_empty_reread_stays_due(self) -> None:
        for client in (_Client([OLD], fail=True), _Client([])):
            with self.subTest(fail=client.fail):
                self.sync(client, TRAINING_REFETCH_DAYS)

                self.assertTrue(self.service.training_refetch_due())
                self.assertEqual(self.counts()["training_state"], "pending")


if __name__ == "__main__":
    unittest.main()
