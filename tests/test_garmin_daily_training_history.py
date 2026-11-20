"""Per-day history of Garmin's daily training status and load balance (#3729).

Synthetic answers built from the provider fixtures with their days replaced;
temporary SQLite and fake clients only, no Garmin account or token.
"""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from support import temp_database

from backend.canonical.vocabulary import MetricKind
from backend.db import garmin_store
from backend.db.repositories import KeyValueRepository
from backend.db.schema import initialize_schema
from backend.providers.garmin import GarminCollectionOptions, collect_garmin_data
from backend.providers.garmin_health_mapping import map_garmin_health
from backend.sync.garmin import (
    DAILY_TRAINING_BATCH_DAYS,
    GarminPayloadService,
    summarize_history_refetch,
)
from backend.sync.garmin_snapshot_writer import GarminSnapshotWriter
from backend.sync.state import SyncStateRepository

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "providers" / "garmin"
TODAY = date(2026, 11, 20)
NOW = "2026-11-20T12:00:00+00:00"


def _answer(section: str, day: str) -> Any:
    """A fixture answer with every ISO day replaced by ``day``."""
    text = (FIXTURES / f"{section}.json").read_text(encoding="utf-8")
    return json.loads(re.sub(r"\d{4}-\d{2}-\d{2}", day, text))


def _external_call(_service: str, _source: str, call: Any, _details: Any) -> Any:
    return call()


class _Client:
    """Answers the daily training reads for any day; may fail from one day on."""

    def __init__(self, fail_from: str | None = None) -> None:
        self.fail_from = fail_from
        self.days: list[str] = []

    def get_activities_by_date(self, start: str, end: str) -> list[Any]:
        return []

    def get_sleep_daily(self, start: str, end: str) -> list[Any]:
        return []

    def get_hrv_data_range(self, start: str, end: str) -> list[Any]:
        return []

    def get_daily_training_status(self, day: str) -> Any:
        if self.fail_from is not None and day <= self.fail_from:
            raise RuntimeError("rate limited")
        self.days.append(day)
        return _answer("daily_training_status", day)

    def get_training_four_week_load_balance(self, day: str) -> Any:
        return _answer("training_load_balance", day)


class HistoryMappingTests(unittest.TestCase):
    def test_each_history_element_maps_as_its_base_section(self) -> None:
        days = ("2026-09-01", "2026-09-02")
        sections = {
            "daily_training_status_history": [
                _answer("daily_training_status", day) | {"calendarDate": day}
                for day in days
            ],
            "training_load_balance_history": [
                _answer("training_load_balance", day) | {"calendarDate": day}
                for day in days
            ],
        }
        records = map_garmin_health(
            sections, zone=UTC, retrieved_at=datetime(2026, 11, 20, tzinfo=UTC)
        ).records
        found = {(o.kind, o.observed_on.isoformat()) for o in records}
        for day in days:
            self.assertIn((MetricKind.CHRONIC_LOAD, day), found)
            self.assertIn((MetricKind.LOAD_BALANCE_AEROBIC_LOW, day), found)


class DailyTrainingHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = temp_database(self, "test.sqlite3")
        self.key_values = KeyValueRepository(lambda: NOW)
        sync_state = SyncStateRepository(self.database, self.key_values, lambda: NOW)
        with self.database.unit_of_work() as db:
            initialize_schema(db)
        self.service = GarminPayloadService(
            self.database, self.key_values, sync_state, lambda: TODAY
        )

    def sync(self, client: _Client, days: tuple[str, ...] = ()) -> None:
        """Collect, prepare and store one regular sync, as the Garmin service does."""
        payload = collect_garmin_data(
            client,
            [(TODAY, TODAY)],
            start=TODAY,
            today=TODAY,
            synced_at=NOW,
            external_call=_external_call,
            redact=lambda _text: "redacted",
            options=replace(GarminCollectionOptions(), daily_training_days=days),
        )
        payload = self.service.prepare_remote(payload)
        with self.database.unit_of_work() as db:
            GarminSnapshotWriter(self.key_values, lambda: UTC).write(db, payload)

    def history_days(self) -> set[str]:
        with self.database.reader() as db:
            stored = garmin_store.read_sections(db, ["daily_training_status_history"])
        return {
            row["calendarDate"]
            for row in stored.get("daily_training_status_history", [])
        }

    def counts(self) -> dict[str, Any]:
        with self.database.reader() as db:
            return summarize_history_refetch(db)

    def test_a_regular_read_keeps_its_day_in_the_history(self) -> None:
        self.sync(_Client())

        self.assertEqual(self.history_days(), {TODAY.isoformat()})

    def test_a_day_read_twice_in_one_run_is_stored_once(self) -> None:
        # Review of #3729: the current answer dated yesterday meets the batch's
        # first day; the history keeps one element for it.
        yesterday = (TODAY - timedelta(days=1)).isoformat()

        class _Yesterday(_Client):
            def get_daily_training_status(self, day: str) -> Any:
                return _answer("daily_training_status", min(day, yesterday))

        self.sync(_Yesterday(), (yesterday,))

        with self.database.reader() as db:
            rows = db.execute(
                "SELECT COUNT(*) AS n FROM provider_records WHERE source_record_id "
                "LIKE 'daily_training_status_history/%'"
            ).fetchone()["n"]
        self.assertEqual(rows, 1)

    def test_a_batch_reads_14_earlier_days_and_moves_the_cursor(self) -> None:
        batch = self.service.daily_training_batch()
        yesterday = TODAY - timedelta(days=1)
        self.assertEqual(
            batch,
            tuple(
                (yesterday - timedelta(days=i)).isoformat()
                for i in range(DAILY_TRAINING_BATCH_DAYS)
            ),
        )
        self.assertEqual(self.counts()["daily_training_due_days"], 181)

        self.sync(_Client(), batch)

        self.assertLessEqual(set(batch), self.history_days())
        counts = self.counts()
        self.assertEqual(counts["daily_training_state"], "pending")
        self.assertEqual(counts["daily_training_added_days"], 14)
        self.assertEqual(counts["daily_training_due_days"], 181 - 14)
        self.assertEqual(
            self.service.daily_training_batch()[0],
            (yesterday - timedelta(days=14)).isoformat(),
        )

    def test_a_failed_call_ends_the_batch_and_keeps_the_cursor(self) -> None:
        batch = self.service.daily_training_batch()

        self.sync(_Client(fail_from=batch[2]), batch)

        self.assertEqual(self.counts()["daily_training_added_days"], 2)
        self.assertEqual(self.service.daily_training_batch()[0], batch[2])

    def test_the_reread_finishes_at_its_floor(self) -> None:
        for _ in range(14):
            batch = self.service.daily_training_batch()
            if not batch:
                break
            self.sync(_Client(), batch)

        counts = self.counts()
        self.assertEqual(counts["daily_training_state"], "finished")
        self.assertEqual(counts["daily_training_added_days"], 181)
        self.assertEqual(counts["daily_training_due_days"], 0)
        self.assertEqual(self.service.daily_training_batch(), ())


if __name__ == "__main__":
    unittest.main()
