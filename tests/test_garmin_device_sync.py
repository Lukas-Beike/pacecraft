"""The watch's last upload time from the Garmin SDK (#2807).

Fake clients and temporary SQLite only; no Garmin account or token.
"""

from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from support import temp_database
from test_garmin_body_battery_reconciliation import NOW, TODAY, _collect, _RangeClient

from backend.db.repositories import KeyValueRepository
from backend.db.schema import initialize_schema
from backend.performance.freshness import GarminSyncStatus
from backend.providers.garmin import GarminCollectionOptions
from backend.sync.garmin import GarminPayloadService
from backend.sync.garmin_snapshot_writer import GarminSnapshotWriter
from backend.sync.state import SyncStateRepository


class _Client(_RangeClient):
    """Answers the device sync time, or raises it."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls = 0

    def get_sync_timestamp(self) -> Any:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class DeviceSyncTests(unittest.TestCase):
    def test_the_sdk_time_is_kept_as_utc_without_failing_the_run(self) -> None:
        for answer, expected in (
            # The pinned SDK answers UTC without an offset.
            ("2026-09-18T21:05:42.0", "2026-09-18T21:05:42Z"),
            ("2026-09-18T23:05:42+02:00", "2026-09-18T21:05:42Z"),
            ("not a time", None),
            ({"syncTimestamp": "2026-09-18T21:05:42.0"}, None),
            (RuntimeError("rate limited"), None),
        ):
            with self.subTest(answer=answer):
                warnings: list[str] = []
                payload = _collect(_Client(answer), days=0, warnings=warnings)
                self.assertEqual(
                    payload["provider_sync"].get("device_synced_at"), expected
                )
                self.assertEqual(
                    payload["provider_sync"].get("device_sync_read_at"),
                    NOW if expected else None,
                )
                # An optional fact never makes the run partial.
                self.assertEqual(payload["errors"], [])
                self.assertEqual(
                    warnings,
                    ["device_sync_timestamp"] if isinstance(answer, Exception) else [],
                )

    def test_a_client_without_the_method_reads_nothing(self) -> None:
        payload = _collect(_RangeClient(), days=0)

        self.assertNotIn("device_synced_at", payload["provider_sync"])

    def test_only_a_current_sync_reads_the_time(self) -> None:
        client = _Client("2026-09-18T21:05:42.0")
        payload = _collect(
            client,
            days=0,
            options=replace(GarminCollectionOptions(), include_current_metrics=False),
        )

        self.assertEqual(client.calls, 0)
        self.assertNotIn("device_synced_at", payload["provider_sync"])


class StoredDeviceSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = temp_database(self, "test.sqlite3", row_factory=sqlite3.Row)
        self.key_values = KeyValueRepository(lambda: NOW)
        sync_state = SyncStateRepository(self.database, self.key_values, lambda: NOW)
        with self.database.unit_of_work() as db:
            initialize_schema(db)
        self.service = GarminPayloadService(
            self.database, self.key_values, sync_state, lambda: TODAY
        )

    def sync(self, client: _RangeClient) -> None:
        payload = self.service.prepare_remote(_collect(client, days=0))
        with self.database.unit_of_work() as db:
            GarminSnapshotWriter(self.key_values, lambda: UTC).write(db, payload)

    def test_a_run_that_cannot_read_the_time_keeps_the_stored_one(self) -> None:
        self.sync(_Client("2026-09-18T21:05:42.0"))
        self.sync(_Client(RuntimeError("rate limited")))
        self.sync(_RangeClient())

        status = self.service.sync_status()
        self.assertEqual(
            (status.device_synced_at, status.device_sync_read_at),
            ("2026-09-18T21:05:42Z", NOW),
        )
        self.sync(_Client("2026-09-20T06:10:00.0"))
        self.assertEqual(
            self.service.sync_status().device_synced_at, "2026-09-20T06:10:00Z"
        )

    def test_the_time_is_shown_only_while_its_read_is_recent(self) -> None:
        # A stopped sync or a dropped SDK login must not read as the watch's silence.
        status = GarminSyncStatus.from_state(
            {
                "provider_sync": {
                    "device_synced_at": "2026-09-18T21:05:42Z",
                    "device_sync_read_at": "2026-09-20T12:00:00+00:00",
                }
            }
        )
        for now, expected in (
            ("2026-09-21T11:59:00+00:00", "2026-09-18T21:05:42Z"),
            ("2026-09-21T12:01:00+00:00", None),
        ):
            with self.subTest(now=now):
                self.assertEqual(
                    status.current_device_synced_at(datetime.fromisoformat(now)),
                    expected,
                )
        unread = GarminSyncStatus.from_state(
            {"provider_sync": {"device_synced_at": "2026-09-18T21:05:42Z"}}
        )
        self.assertIsNone(unread.current_device_synced_at(datetime.fromisoformat(NOW)))


if __name__ == "__main__":
    unittest.main()
