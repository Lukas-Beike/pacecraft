"""The regular Garmin sync merges its weigh-in answer into the stored ones (#3718).

Synthetic weigh-ins shaped after the range answer; temporary SQLite and fake
clients only, no Garmin account or token.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, timedelta
from typing import Any

from support import temp_database

from backend.db import garmin_store
from backend.db.repositories import KeyValueRepository
from backend.db.schema import initialize_schema
from backend.providers.garmin import (
    WEIGHT_READ_DAYS,
    GarminCollectionOptions,
    collect_garmin_data,
)
from backend.sync.garmin import WEIGHT_REFETCH_DAYS, GarminPayloadService
from backend.sync.garmin_snapshot_writer import (
    NUTRITION_SOURCES,
    GarminSnapshotWriter,
)
from backend.sync.state import SyncStateRepository

TODAY = date(2026, 11, 20)
NOW = "2026-11-20T12:00:00+00:00"
OLD = (TODAY - timedelta(days=150)).isoformat()
RECENT = (TODAY - timedelta(days=10)).isoformat()
LATEST = (TODAY - timedelta(days=5)).isoformat()


def _summary(day: str, pk: int) -> dict[str, Any]:
    reading = {"samplePk": pk, "calendarDate": day, "weight": 72000.0}
    return {"summaryDate": day, "latestWeight": reading, "allWeightMetrics": [reading]}


def _external_call(_service: str, _source: str, call: Any, _details: Any) -> Any:
    return call()


class _Client:
    """Garmin's weigh-ins by day; each range read answers the days inside it."""

    def __init__(self, days: dict[str, int], fail: bool = False) -> None:
        self.days = days
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def get_activities_by_date(self, start: str, end: str) -> list[Any]:
        return []

    def get_sleep_daily(self, start: str, end: str) -> list[Any]:
        return []

    def get_hrv_data_range(self, start: str, end: str) -> list[Any]:
        return []

    def get_weigh_ins(self, start: str, end: str) -> Any:
        self.calls.append((start, end))
        if self.fail:
            raise RuntimeError("rate limited")
        return {
            "dailyWeightSummaries": [
                _summary(day, pk)
                for day, pk in sorted(self.days.items(), reverse=True)
                if start <= day <= end
            ]
        }


class WeightMergeTests(unittest.TestCase):
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
        weight_days: int = WEIGHT_READ_DAYS,
        *,
        without_window: bool = False,
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
            options=replace(
                GarminCollectionOptions(),
                include_daily_metrics=False,
                weight_days=weight_days,
            ),
        )
        payload = self.service.prepare_remote(payload)
        if without_window:
            # The write of a release before #3754: no windowed weigh-in prune.
            payload["provider_sync"].pop("weight_window", None)
        with self.database.unit_of_work() as db:
            GarminSnapshotWriter(self.key_values, lambda: UTC).write(db, payload)

    def weight_days(self) -> list[str]:
        with self.database.reader() as db:
            return [
                row["observed_on"]
                for row in db.execute(
                    "SELECT DISTINCT observed_on FROM observations WHERE "
                    "provider = 'garmin' AND metric = 'weight' AND state = 'active' "
                    "ORDER BY observed_on"
                )
            ]

    def marker(self) -> dict[str, Any]:
        with self.database.reader() as db:
            return garmin_store.weight_origin_counts(db, 0)

    def test_the_6_month_reread_runs_once_and_counts_its_days(self) -> None:
        self.assertTrue(self.service.weight_refetch_due())
        client = _Client({OLD: 1, RECENT: 2})

        self.sync(client, WEIGHT_REFETCH_DAYS)

        start = (TODAY - timedelta(days=WEIGHT_REFETCH_DAYS - 1)).isoformat()
        self.assertEqual(client.calls, [(start, TODAY.isoformat())])
        self.assertFalse(self.service.weight_refetch_due())
        self.assertEqual(
            (self.marker()["refetch_state"], self.marker()["refetch_added_days"]),
            ("finished", 2),
        )
        # A later regular read keeps the marker and the older weigh-in.
        self.sync(_Client({RECENT: 2}))
        self.assertFalse(self.service.weight_refetch_due())
        self.assertEqual(self.marker()["refetch_added_days"], 2)
        self.assertEqual(self.weight_days(), [OLD, RECENT])

    def test_a_regular_read_keeps_weigh_ins_outside_its_window(self) -> None:
        self.sync(_Client({OLD: 1, RECENT: 2}), WEIGHT_REFETCH_DAYS)
        self.sync(_Client({RECENT: 2}))

        with self.database.reader() as db:
            stored = garmin_store.read_sections(db, ["weight"])["weight"]
        summaries = stored[0]["dailyWeightSummaries"]
        self.assertEqual({row["summaryDate"] for row in summaries}, {OLD, RECENT})
        self.assertEqual(self.weight_days(), [OLD, RECENT])

    def test_a_weigh_in_deleted_in_garmin_inside_the_window_goes(self) -> None:
        # The answer still holds another weigh-in of the window: a full answer.
        self.sync(_Client({OLD: 1, RECENT: 2}), WEIGHT_REFETCH_DAYS)
        self.sync(_Client({OLD: 1, LATEST: 3}))

        with self.database.reader() as db:
            stored = garmin_store.read_sections(db, ["weight"])["weight"]
        summaries = stored[0]["dailyWeightSummaries"]
        self.assertEqual({row["summaryDate"] for row in summaries}, {OLD, LATEST})
        self.assertEqual(self.weight_days(), [OLD, LATEST])

    def test_an_orphan_of_an_earlier_envelope_heals_on_the_next_sync(self) -> None:
        self.sync(_Client({OLD: 1, RECENT: 2}), WEIGHT_REFETCH_DAYS)
        self.sync(_Client({OLD: 1, LATEST: 3}), without_window=True)
        self.assertEqual(self.weight_days(), [OLD, RECENT, LATEST])

        self.sync(_Client({OLD: 1, LATEST: 3}))

        self.assertEqual(self.weight_days(), [OLD, LATEST])

    def test_an_orphan_heals_on_a_targeted_weight_refresh(self) -> None:
        self.sync(_Client({OLD: 1, RECENT: 2}), WEIGHT_REFETCH_DAYS)
        self.sync(_Client({OLD: 1, LATEST: 3}), without_window=True)
        start = TODAY - timedelta(days=WEIGHT_READ_DAYS - 1)
        with self.database.reader() as db:
            stored = garmin_store.read_sections(db, ["weight"])["weight"]

        with self.database.unit_of_work() as db:
            GarminSnapshotWriter(self.key_values, lambda: UTC).write_refresh(
                db,
                fetched_at=NOW,
                sections={"weight": stored},
                view={},
                windows={"weight": (start, TODAY)},
                allowed=NUTRITION_SOURCES,
            )

        self.assertEqual(self.weight_days(), [OLD, LATEST])

    def test_an_answer_without_weigh_ins_changes_nothing(self) -> None:
        # It cannot be told from a partial Garmin failure (#3718).
        self.sync(_Client({OLD: 1, RECENT: 2}), WEIGHT_REFETCH_DAYS)
        self.sync(_Client({OLD: 1}))

        with self.database.reader() as db:
            stored = garmin_store.read_sections(db, ["weight"])["weight"]
        summaries = stored[0]["dailyWeightSummaries"]
        self.assertEqual({row["summaryDate"] for row in summaries}, {OLD, RECENT})
        self.assertEqual(self.weight_days(), [OLD, RECENT])

    def test_an_empty_reread_stays_due(self) -> None:
        self.sync(_Client({}), WEIGHT_REFETCH_DAYS)

        self.assertTrue(self.service.weight_refetch_due())
        self.assertEqual(self.marker()["refetch_state"], "pending")

    def test_a_failed_reread_stays_due(self) -> None:
        self.sync(_Client({OLD: 1}, fail=True), WEIGHT_REFETCH_DAYS)

        self.assertTrue(self.service.weight_refetch_due())
        self.assertEqual(self.marker()["refetch_state"], "pending")


if __name__ == "__main__":
    unittest.main()
