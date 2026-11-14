from __future__ import annotations

import sqlite3
import unittest

from support import temp_database

from backend.canonical.vocabulary import RemoteEntityKind, RemoteLinkState
from backend.db import row_factory
from backend.db.remote_links import RemoteLink, RemoteLinkRepository
from backend.db.repositories import KeyValueRepository
from backend.sync.state import (
    SyncStateRepository,
    last_error_key,
    last_sync_key,
    resync_key,
    resync_reset_keys,
    settle_interrupted_sync_state,
)

NOW = "2026-09-20T12:00:00+00:00"
DEFAULTS = {"intervals": 90, "garmin": 30}
ALL_DAYS = -1


class ConnectSpy:
    def __init__(self):
        self.connect_count = 0

    def connect(self, *args, **kwargs):
        self.connect_count += 1
        return sqlite3.connect(*args, **kwargs)


class SyncStateRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.backend = ConnectSpy()
        self.database_manager = temp_database(
            self,
            "sync-state.sqlite",
            backend=self.backend,
            row_factory=row_factory,
            schema=True,
        )
        self.key_values = KeyValueRepository(lambda: NOW)
        self.repository = self.make_repository()

    def make_repository(self, key_values=None):
        return SyncStateRepository(
            self.database_manager,
            key_values or self.key_values,
            lambda: NOW,
        )

    def test_sync_period_defaults_clamps_and_all_days(self):
        self.assertEqual(
            self.repository.sync_period("intervals", DEFAULTS, ALL_DAYS), 90
        )
        self.assertEqual(self.repository.sync_period("garmin", DEFAULTS, ALL_DAYS), 30)

        self.repository.set_sync_period("intervals", ALL_DAYS, ALL_DAYS)
        self.assertEqual(
            self.repository.sync_period("intervals", DEFAULTS, ALL_DAYS), ALL_DAYS
        )
        with self.database_manager.unit_of_work() as db:
            self.key_values.set(db, "garmin_sync_days", "1000")
            self.key_values.set(db, "intervals_sync_days", "broken")
        self.assertEqual(self.repository.sync_period("garmin", DEFAULTS, ALL_DAYS), 90)
        self.assertEqual(
            self.repository.sync_period("intervals", DEFAULTS, ALL_DAYS), 90
        )

    def test_set_sync_period_persists_valid_values_and_rejects_invalid_values(self):
        for source, value in (
            ("intervals", 365),
            ("garmin", ALL_DAYS),
            ("garmin", "42"),
        ):
            with self.subTest(source=source, value=value):
                self.assertEqual(
                    self.repository.set_sync_period(source, value, ALL_DAYS),
                    int(value),
                )
                self.assertEqual(
                    self.repository.sync_period(source, DEFAULTS, ALL_DAYS), int(value)
                )

        for source, value in (
            ("intervals", 366),
            ("garmin", 0),
            ("garmin", 91),
            ("intervals", "bad"),
        ):
            with (
                self.subTest(source=source, value=value),
                self.assertRaises(ValueError),
            ):
                self.repository.set_sync_period(source, value, ALL_DAYS)
        with self.assertRaises(KeyError):
            self.repository.sync_period("other", DEFAULTS, ALL_DAYS)
        self.assertEqual(self.repository.set_sync_period("other", 90, ALL_DAYS), 90)
        with self.database_manager.unit_of_work() as db:
            self.assertEqual(self.key_values.get(db, "other_sync_days"), "90")

    def test_cursor_defaults_and_update_use_existing_cursor_contract(self):
        self.assertEqual(
            self.repository.cursor("garmin", "data"),
            {
                "provider": "garmin",
                "stream": "data",
                "cursor": None,
                "high_water_mark": None,
                "updated_at": None,
            },
        )
        self.repository.update_cursor("g" * 40, "d" * 80, "c" * 130, "h" * 130)
        self.assertEqual(
            self.repository.cursor("g" * 40, "d" * 80),
            {
                "provider": "g" * 40,
                "stream": "d" * 80,
                "cursor": "c" * 120,
                "high_water_mark": "h" * 120,
                "updated_at": NOW,
            },
        )
        self.repository.update_cursor("garmin", "data", "next")
        self.assertEqual(
            self.repository.cursor("garmin", "data")["high_water_mark"], ""
        )

    def link_intervals(self, local_id, synced_at, **values):
        values.setdefault("entity_kind", RemoteEntityKind.PLANNED_UNIT)
        values.setdefault("sync_state", RemoteLinkState.SYNCED)
        with self.database_manager.unit_of_work() as db:
            RemoteLinkRepository().upsert(
                db,
                RemoteLink(
                    local_id=local_id,
                    provider="intervals",
                    remote_id=local_id,
                    last_synced_at=synced_at,
                    **values,
                ),
            )

    def stamp(self, key="intervals_last_output_ok_at"):
        with self.database_manager.unit_of_work() as db:
            return self.key_values.get(db, key)

    def test_a_link_from_before_the_stamps_becomes_the_export_stamp_once(self):
        self.repository.adopt_intervals_export_time()
        self.assertFalse(self.stamp())
        self.link_intervals("old", "2026-10-02T08:00:00+00:00")
        # Offset and naive forms are compared as times, not as text.
        self.link_intervals("offset", "2026-10-03T10:00:00+02:00")
        self.link_intervals(
            "competition",
            "2026-10-03T09:00:00",
            entity_kind=RemoteEntityKind.COMPETITION,
        )
        self.link_intervals("malformed", "zzzz")
        self.repository.adopt_intervals_export_time()
        self.assertEqual(self.stamp(), "2026-10-03T09:00:00")
        # A later link never moves the stamp: it is written only once.
        self.link_intervals("newer", "2026-10-09T08:00:00+00:00")
        self.repository.adopt_intervals_export_time()
        self.assertEqual(self.stamp(), "2026-10-03T09:00:00")
        self.assertFalse(self.stamp("intervals_last_output_failed_at"))

    def test_the_stamp_ignores_other_links_and_keeps_existing_stamps(self):
        at = "2026-10-02T08:00:00+00:00"
        self.link_intervals("obs", at, entity_kind=RemoteEntityKind.OBSERVATION)
        self.link_intervals("pending", at, sync_state=RemoteLinkState.PENDING)
        self.link_intervals("none", None)
        self.repository.adopt_intervals_export_time()
        self.assertFalse(self.stamp())
        self.link_intervals("unit", at)
        with self.database_manager.unit_of_work() as db:
            self.key_values.set(db, "intervals_last_output_ok_at", "2026-01-01")
            self.key_values.set(db, "intervals_last_output_failed_at", "2026-02-01")
        self.repository.adopt_intervals_export_time()
        self.assertEqual(self.stamp(), "2026-01-01")
        self.assertEqual(self.stamp("intervals_last_output_failed_at"), "2026-02-01")

    def test_reads_reuse_outer_unit_of_work_connection(self):
        connect_count = self.backend.connect_count
        with self.database_manager.unit_of_work():
            self.assertEqual(
                self.repository.sync_period("intervals", DEFAULTS, ALL_DAYS), 90
            )
            self.repository.cursor("garmin", "data")
            self.assertEqual(self.backend.connect_count, connect_count)

    def test_a_restart_closes_the_refreshes_left_running(self):
        # A crash left refresh rows running, and freshness showed "syncing"
        # until the next attempt (#2707); the start-up settle closes them (#2888).
        with self.database_manager.unit_of_work() as db:
            for row_id, status in (("r-1", "running"), ("r-2", "success")):
                db.execute(
                    "INSERT INTO provider_refresh_history(id, provider, area, "
                    "operation_id, trigger, started_at, finished_at, phase, status) "
                    "VALUES (?, 'garmin', 'data', 'op', 'scheduler', ?, NULL, "
                    "'fetching', ?)",
                    (row_id, NOW, status),
                )
            settle_interrupted_sync_state(db, NOW)
            rows = {
                row["id"]: (row["status"], row["error_code"], row["finished_at"])
                for row in db.execute("SELECT * FROM provider_refresh_history")
            }
        self.assertEqual(
            {
                "r-1": ("skipped", "process_interrupted", NOW),
                "r-2": ("success", None, None),
            },
            rows,
        )

    def test_a_restart_ends_the_full_resyncs_left_running(self):
        # Moved from the start-up invariants into the one settle (#2888).
        stale = {
            "garmin_full_resync_running": "1",
            "garmin_full_resync_status": "Garmin: vollständiger Resync läuft…",
            "wahoo_full_resync_running": "1",
            "wahoo_full_resync_status": "Wahoo: vollständiger Resync läuft…",
        }
        with self.database_manager.unit_of_work() as db:
            for key, value in stale.items():
                self.key_values.set(db, key, value)
            settle_interrupted_sync_state(db, NOW)
            settled = {key: self.key_values.get(db, key) for key in stale}
        self.assertEqual(
            {
                "garmin_full_resync_running": "0",
                "garmin_full_resync_status": "",
                "wahoo_full_resync_running": "0",
                "wahoo_full_resync_status": "",
            },
            settled,
        )

    def test_a_restart_ends_a_garmin_operation_left_running(self):
        # A stale "running" would show as live while a later Garmin push holds
        # the same lock (#2706); the start-up settle ends it (#2888).
        running = {
            "sync_operation_status": "running",
            "sync_operation_phase": "fetching",
            "sync_operation_progress": "10",
            "sync_operation_message": "Garmin-Daten werden gelesen…",
            "sync_operation_id": "op-1",
        }
        with self.database_manager.unit_of_work() as db:
            for key, value in running.items():
                self.key_values.set(db, key, value)
            settle_interrupted_sync_state(db, NOW)
            operation = {key: self.key_values.get(db, key) for key in running}
        # The whole running projection ends; the id names the last operation.
        self.assertEqual(
            {
                "sync_operation_status": "",
                "sync_operation_phase": "",
                "sync_operation_progress": "0",
                "sync_operation_message": "",
                "sync_operation_id": "op-1",
            },
            operation,
        )


class StoredKeyNameTests(unittest.TestCase):
    """The key names are persisted: a rename would orphan stored state."""

    def test_last_sync_and_error_keys_are_the_stored_names(self):
        stored = {
            "garmin": ("last_garmin_sync_at", "last_garmin_error"),
            "wahoo": ("last_wahoo_sync_at", "last_wahoo_sync_error"),
            "calendar": (
                "last_external_calendar_sync_at",
                "last_external_calendar_sync_error",
            ),
            "intervals": ("last_competition_sync_at", "last_competition_sync_error"),
        }
        for provider, (at_key, error_key) in stored.items():
            with self.subTest(provider=provider):
                self.assertEqual(at_key, last_sync_key(provider))
                self.assertEqual(error_key, last_error_key(provider))
        with self.assertRaises(KeyError):
            last_sync_key("weather")
        with self.assertRaises(KeyError):
            last_error_key("weather")

    def test_resync_keys_are_the_stored_names_for_resync_providers_only(self):
        self.assertEqual("garmin_full_resync_running", resync_key("garmin", "running"))
        self.assertEqual("wahoo_full_resync_status", resync_key("wahoo", "status"))
        self.assertEqual("garmin_full_resync_at", resync_key("garmin", "at"))
        self.assertEqual("wahoo_full_resync_error", resync_key("wahoo", "error"))
        self.assertEqual(
            (
                ("garmin_full_resync_running", "garmin_full_resync_status"),
                ("wahoo_full_resync_running", "wahoo_full_resync_status"),
            ),
            resync_reset_keys(),
        )
        with self.assertRaisesRegex(ValueError, "no full resync"):
            resync_key("weather", "running")
        with self.assertRaisesRegex(ValueError, "unknown full resync field"):
            resync_key("garmin", "last_at")


if __name__ == "__main__":
    unittest.main()
