"""Automatic verified database copies: nightly and before a migration (#3760).

Temporary storage and plain SQLite only; the copy is the backup download's
snapshot, whose SQLCipher round trip ``test_database_backup`` covers.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from backend.backup.automatic import (
    DEFAULT_DIRECTORY,
    NIGHTLY_KEEP,
    AutomaticBackups,
    backup_directory,
)
from backend.backup.database import DatabaseBackupConfig, DatabaseBackupService
from backend.backup.restore_validation import (
    DatabaseRestoreValidationConfig,
    DatabaseRestoreValidationService,
)
from backend.db import row_factory
from backend.db.manager import DatabaseManager
from backend.db.schema import database_schema_is_current, initialize_schema

ZONE = ZoneInfo("Europe/Berlin")
NAME = "athlete.db"


class AutomaticBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / NAME
        self.directory = self.root / DEFAULT_DIRECTORY
        self.manager = DatabaseManager(
            self.path, sqlite3, timeout=0.1, row_factory=row_factory
        )
        self.addCleanup(self.manager.close)
        with self.manager.unit_of_work() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE records (value TEXT NOT NULL)")
            db.execute("INSERT INTO records VALUES ('kept')")
        self.logger = Mock()
        self.failures: list[dict] = []
        self.backups = AutomaticBackups(self.directory, NAME, self.service, self.logger)

    def service(self, directory: Path) -> DatabaseBackupService:
        return DatabaseBackupService(
            self.manager,
            DatabaseBackupConfig(
                database_path=self.path,
                data_dir=directory,
                maximum_bytes=1_000_000,
                minimum_free_bytes=10,
                time_limit_seconds=5,
                disk_usage=lambda _path: SimpleNamespace(free=2_000_000),
            ),
            self.logger,
        )

    def names(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir())

    def test_the_nightly_copy_runs_once_from_0330_local_time(self) -> None:
        # The copy is stamped with the real time, so the test day is today.
        early = datetime.now(ZONE).replace(hour=3, minute=29)
        self.assertFalse(self.backups.run_nightly(early, None, self.failures.append))
        self.assertFalse(self.directory.exists())

        late = early.replace(hour=23, minute=59)
        self.assertTrue(self.backups.run_nightly(late, None, self.failures.append))
        self.assertFalse(self.backups.run_nightly(late, None, self.failures.append))

        [name] = self.names()
        self.assertRegex(name, rf"^{NAME}\.auto-nightly-\d{{14}}$")
        copy = sqlite3.connect(self.directory / name)
        self.addCleanup(copy.close)
        self.assertEqual(
            copy.execute("SELECT value FROM records").fetchall(), [("kept",)]
        )
        self.assertEqual(self.failures, [])

    def test_a_failed_copy_is_recorded_and_leaves_no_file(self) -> None:
        def failing(directory: Path) -> DatabaseBackupService:
            service = Mock()
            service.write_verified_copy.side_effect = RuntimeError("disk")
            return service

        backups = AutomaticBackups(self.directory, NAME, failing, self.logger)
        night = datetime(2026, 11, 20, 4, 0, tzinfo=ZONE)

        self.assertFalse(backups.run_nightly(night, None, self.failures.append))

        self.assertEqual([failure["kind"] for failure in self.failures], ["nightly"])
        self.assertEqual(backups.copies(), [])
        self.assertTrue(backups.nightly_due(night))
        # The next attempt waits an hour, not the next 5-minute pass.
        recent = json.dumps(self.failures[0])
        self.assertFalse(backups.run_nightly(night, recent, self.failures.append))
        self.assertEqual(len(self.failures), 1)
        older = json.dumps({"at": "2026-11-19T01:00:00+00:00", "kind": "nightly"})
        self.assertFalse(backups.run_nightly(night, older, self.failures.append))
        self.assertEqual(len(self.failures), 2)
        extra = self.logger.error.call_args.kwargs["extra"]
        self.assertEqual(extra["context"]["error_class"], "RuntimeError")

    def test_a_failed_pre_migration_copy_stops_the_migration(self) -> None:
        backups = AutomaticBackups(
            self.directory,
            NAME,
            lambda _d: Mock(
                **{"write_verified_copy.side_effect": OSError("share gone")}
            ),
            self.logger,
        )
        with self.assertRaises(RuntimeError):
            backups.before_migration(32)

    def test_retention_keeps_seven_nightly_and_the_newest_pre_migration_copy(
        self,
    ) -> None:
        self.directory.mkdir()
        start = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
        for day in range(NIGHTLY_KEEP + 2):
            made = (start + timedelta(days=day)).strftime("%Y%m%d%H%M%S")
            (self.directory / f"{NAME}.auto-nightly-{made}").write_bytes(b"x")
        (self.directory / f"{NAME}.auto-premigration-v31-20261001020000").write_bytes(
            b"x"
        )
        (self.directory / "unrelated.txt").write_bytes(b"x")

        (self.directory / ".database-backup-left.tmp").write_bytes(b"x")
        self.backups.write("nightly", datetime(2026, 11, 20, 2, 30, tzinfo=UTC))
        self.backups.before_migration(32)

        nightly = [n for n in self.names() if ".auto-nightly-" in n]
        self.assertEqual(len(nightly), NIGHTLY_KEEP)
        self.assertIn(f"{NAME}.auto-nightly-20261120023000", nightly)
        premigration = [n for n in self.names() if ".auto-premigration-" in n]
        self.assertEqual(len(premigration), 1)
        self.assertIn("-v32-", premigration[0])
        self.assertIn("unrelated.txt", self.names())
        # The staging file an interrupted copy left behind is gone.
        self.assertNotIn(".database-backup-left.tmp", self.names())

    def test_summary_reports_counts_and_ages_only(self) -> None:
        now = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
        self.assertEqual(
            self.backups.summary(now, None),
            {
                "enabled": True,
                "nightly_count": 0,
                "newest_age_hours": None,
                "premigration_present": False,
                "last_failure_age_hours": None,
            },
        )
        self.backups.write("nightly", datetime(2026, 11, 20, 3, 0, tzinfo=UTC))
        failure = json.dumps({"at": "2026-11-19T06:00:00+00:00", "kind": "nightly"})

        summary = self.backups.summary(now, failure)

        self.assertEqual(summary["nightly_count"], 1)
        self.assertEqual(summary["last_failure_age_hours"], 24.0)
        self.assertNotIn(NAME, json.dumps(summary))

    def test_erase_removes_every_automatic_copy(self) -> None:
        self.backups.write("nightly", datetime(2026, 11, 20, 3, 0, tzinfo=UTC))
        self.backups.before_migration(32)
        (self.directory / "unrelated.txt").write_bytes(b"x")
        # An interrupted copy's staging file, and a copy in the default
        # directory after BACKUP_DIR moved or was switched off.
        (self.directory / ".database-backup-abc.tmp").write_bytes(b"x")
        moved = self.root / "share"
        moved.mkdir()
        purge = AutomaticBackups(
            moved, NAME, self.service, self.logger, earlier=(self.directory,)
        )
        self.assertEqual(purge.directories(), (moved, self.directory))

        erased = purge.erase()

        self.assertEqual((erased.removed, erased.failed), (3, 0))
        self.assertEqual(self.names(), ["unrelated.txt"])

    def test_a_copy_passes_the_restore_validation(self) -> None:
        path = self.root / "current.db"
        manager = DatabaseManager(path, sqlite3, timeout=0.1, row_factory=row_factory)
        self.addCleanup(manager.close)
        with manager.unit_of_work() as db:
            initialize_schema(db)
        backups = AutomaticBackups(
            self.directory,
            path.name,
            lambda directory: DatabaseBackupService(
                manager,
                DatabaseBackupConfig(
                    database_path=path,
                    data_dir=directory,
                    maximum_bytes=10_000_000,
                    minimum_free_bytes=10,
                    time_limit_seconds=5,
                    disk_usage=lambda _path: SimpleNamespace(free=20_000_000),
                ),
                self.logger,
            ),
            self.logger,
        )
        backups.before_migration(32)
        [copy] = backups.copies()
        validator = DatabaseRestoreValidationService(
            DatabaseRestoreValidationConfig(
                data_dir=self.root,
                maximum_bytes=10_000_000,
                app_password="",
                sqlcipher_available=False,
                sqlite_backend=sqlite3,
                configure_cipher=Mock(),
                row_factory=sqlite3.Row,
                schema_is_current=database_schema_is_current,
                startup_invariants=lambda _connection: None,
            )
        )

        validator.validate(validator.stage(copy.path.read_bytes()))

    def test_the_directory_setting(self) -> None:
        data = Path("/data")
        self.assertEqual(backup_directory("", data), data / DEFAULT_DIRECTORY)
        self.assertEqual(backup_directory(" /mnt/share ", data), Path("/mnt/share"))
        self.assertIsNone(backup_directory("OFF", data))


if __name__ == "__main__":
    unittest.main()
