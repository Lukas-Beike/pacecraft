from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from backend.backup.restore_validation import (
    DatabaseRestoreValidationConfig,
    DatabaseRestoreValidationService,
)
from backend.db import canonical_store, migrations, row_factory
from backend.db.bootstrap import apply_startup_invariants
from backend.db.migrations import migrate_schema
from backend.db.repositories import KeyValueRepository, PlanningStateRepository
from backend.db.schema import CURRENT_SCHEMA_VERSION, database_schema_is_current
from backend.errors import AppError
from backend.planning.revision import PlanningRevisionService
from backend.providers.canonical_plans import current_observation_mapper_versions
from backend.sync.state import settle_interrupted_sync_state

# Schema v26, release 2.0.5: the only baseline, kept as a frozen fixture.
BASELINE_FIXTURE = Path(__file__).with_name("fixtures") / "schema_v26.sql.txt"


def service(data_dir: Path, **overrides: object) -> DatabaseRestoreValidationService:
    values: dict[str, object] = {
        "data_dir": data_dir,
        "maximum_bytes": 1024,
        "app_password": "",
        "sqlcipher_available": False,
        "sqlite_backend": sqlite3,
        "configure_cipher": Mock(),
        "row_factory": sqlite3.Row,
        "schema_is_current": lambda _connection: True,
        "startup_invariants": lambda _connection: None,
        "migrate_schema": Mock(),
    }
    values.update(overrides)
    return DatabaseRestoreValidationService(DatabaseRestoreValidationConfig(**values))  # type: ignore[arg-type]


class DatabaseRestoreValidationTests(unittest.TestCase):
    def test_verify_copy_needs_the_whole_file_and_the_current_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy = root / "athlete.db.pre-restore-copy"
            with closing(sqlite3.connect(copy)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE kv (key TEXT)")
                connection.commit()
            size = copy.stat().st_size
            current = service(root, schema_is_current=lambda _connection: True)
            outdated = service(root, schema_is_current=lambda _connection: False)

            self.assertTrue(current.verify_copy(copy, size))
            self.assertFalse(current.verify_copy(copy, size + 1))
            self.assertFalse(outdated.verify_copy(copy, size))
            self.assertFalse(current.verify_copy(root / "missing.db", size))
            garbage = root / "garbage.db"
            garbage.write_bytes(b"x" * size)
            self.assertFalse(
                service(root, schema_is_current=database_schema_is_current).verify_copy(
                    garbage, size
                )
            )
            # Read-only and immutable: no journal or sidecar beside the copy.
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["athlete.db.pre-restore-copy", "garbage.db"],
            )

    def test_verify_copy_rejects_a_damaged_data_page_behind_an_intact_schema(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy = root / "athlete.db.pre-restore-damaged"
            with closing(sqlite3.connect(copy)) as connection:
                connection.execute("CREATE TABLE kv (key TEXT)")
                connection.execute("CREATE INDEX kv_key ON kv(key)")
                connection.executemany(
                    "INSERT INTO kv VALUES (?)", [(f"k{i:04}",) for i in range(500)]
                )
                connection.commit()
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                index_page = connection.execute(
                    "SELECT rootpage FROM sqlite_master WHERE name = 'kv_key'"
                ).fetchone()[0]
            intact = copy.read_bytes()
            data = bytearray(intact)
            start = (index_page - 1) * page_size
            data[start : start + page_size] = b"\xff" * page_size
            copy.write_bytes(bytes(data))
            current = service(root, row_factory=row_factory)

            self.assertFalse(current.verify_copy(copy, len(data)))
            copy.write_bytes(intact)
            # The production row factory returns dicts; a clean copy still verifies.
            self.assertTrue(current.verify_copy(copy, len(data)))

    def test_stage_enforces_payload_boundary_and_uses_uuid_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            validator = service(Path(directory), maximum_bytes=4)
            for payload in (b"", b"12345"):
                with self.assertRaises(AppError) as raised:
                    validator.stage(payload)
                self.assertEqual(raised.exception.status, 413)
            self.assertEqual(list(Path(directory).iterdir()), [])

            staged = validator.stage(b"safe")
            self.assertRegex(
                staged.name, r"^\.intervals-coach-restore-[0-9a-f]{32}\.db$"
            )
            self.assertEqual(staged.read_bytes(), b"safe")

    def test_stage_streams_chunks_and_enforces_the_limit_while_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validator = service(root, maximum_bytes=8)
            staged = validator.stage(iter([b"safe", b"-db"]))
            self.assertEqual(staged.read_bytes(), b"safe-db")

            consumed: list[int] = []

            def oversized_chunks():
                for chunk in (b"1234", b"56789", b"never-read"):
                    consumed.append(len(chunk))
                    yield chunk

            with self.assertRaises(AppError) as oversized:
                validator.stage(oversized_chunks())
            self.assertEqual(oversized.exception.status, 413)
            self.assertEqual(consumed, [4, 5])

            with self.assertRaises(AppError) as empty:
                validator.stage(iter([]))
            self.assertEqual(empty.exception.status, 413)

            def broken_chunks():
                yield b"partial"
                raise TimeoutError("socket deadline")

            with self.assertRaises(TimeoutError):
                validator.stage(broken_chunks())

            # Only the earlier successful stage remains; every failed stage cleaned up.
            self.assertEqual([path.name for path in root.iterdir()], [staged.name])

    def test_validate_commits_session_scrubbing_after_schema_integrity_and_fk_checks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE sessions (token TEXT)")
                connection.execute("INSERT INTO sessions VALUES ('synthetic-session')")
                connection.commit()
            service(Path(directory)).validate(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0
                )

    def test_validate_accepts_a_populated_baseline_backup(self) -> None:
        # The baseline (v26), released schemas (v28, #2803; v31, the one before
        # the diary rebuild of #3234; v32, the one before product sizes; v33,
        # production's before the session feel of #4199) and a current backup
        # restore into the current schema with their rows, the diary entry and
        # the session note included.
        for version in (26, 28, 31, 32, 33, CURRENT_SCHEMA_VERSION):
            with self.subTest(version=version):
                self.assert_backup_upgrades(version)

    def assert_backup_upgrades(self, version: int) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                connection.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO messages(role, content, created_at) "
                    "VALUES ('user', 'Hallo', '2026-10-16T08:00:00Z')"
                )
                connection.execute(
                    "INSERT INTO nutrition_logs(id, meal_date, logged_at, meal_type, "
                    "description, kcal, carbs_g, created_at, updated_at) VALUES "
                    "('log', '2026-10-16', '2026-10-16T10:00:00', 'snack', 'Gel', "
                    "100, 25, 'now', 'now')"
                )
                # A weight the athlete entered by hand: no provider (#3252).
                connection.execute(
                    "INSERT INTO observations(natural_key, metric, value, unit, "
                    "source_kind, recorded_by, provider, observed_on, label, "
                    "mapper_version, created_at, updated_at) VALUES ('entered', "
                    "'weight', 72.5, 'kg', 'athlete_entered', 'manual', NULL, "
                    "'2026-10-15', 'Lokal', 1, 'now', 'now')"
                )
                if version in (31, 32, 33):
                    for step in (
                        migrations._canonical_products,
                        migrations._calendar_decisions,
                        migrations._competition_location,
                        migrations._session_curves,
                        migrations._device_workout,
                        # v32, production's since 2.1.28, before the product sizes.
                        *([migrations._session_fuelling] if version >= 32 else []),
                        # v33, production's since 2.1.51, before the session feel.
                        *([migrations._product_sizes] if version == 33 else []),
                    ):
                        step(connection)
                    connection.execute(f"PRAGMA user_version = {version}")
                if version == 33:
                    connection.execute(
                        "INSERT INTO activity_feedback(activity_id, notes, session_rpe, "
                        "created_at, updated_at) VALUES ('act_1', 'Schwere Beine', 6, "
                        "'now', 'now')"
                    )
                if version == CURRENT_SCHEMA_VERSION:
                    migrate_schema(connection)
                if version == 28:
                    migrations._canonical_products(connection)
                    migrations._calendar_decisions(connection)
                    connection.execute("PRAGMA user_version = 28")
                    connection.execute(
                        "INSERT INTO competitions(id, name, event_date, sport, priority, "
                        "distance, target, course_profile, notes, created_at, updated_at) "
                        "VALUES ('race', 'Stadtlauf', '2026-11-28', 'run', 'A', '21100', "
                        "'1:45:00', '', '', 'now', 'now')"
                    )
                connection.commit()
            service(
                Path(directory),
                maximum_bytes=10_000_000,
                schema_is_current=database_schema_is_current,
                migrate_schema=migrate_schema,
            ).validate(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    CURRENT_SCHEMA_VERSION,
                )
                self.assertTrue(database_schema_is_current(connection))
                self.assertEqual(
                    connection.execute("SELECT content FROM messages").fetchone()[0],
                    "Hallo",
                )
                self.assertEqual(
                    [
                        tuple(row)
                        for row in connection.execute(
                            "SELECT id, target, location FROM competitions"
                        )
                    ],
                    [("race", "1:45:00", "")] if version == 28 else [],
                )
                self.assertEqual(
                    [
                        tuple(row)
                        for row in connection.execute(
                            "SELECT id, meal_type, kcal, carbs_g, volume_ml, session_id "
                            "FROM nutrition_logs"
                        )
                    ],
                    [("log", "snack", 100, 25.0, None, None)],
                )
                self.assertEqual(
                    [
                        tuple(row)
                        for row in connection.execute(
                            "SELECT activity_id, notes, session_rpe, feel "
                            "FROM activity_feedback"
                        )
                    ],
                    [("act_1", "Schwere Beine", 6.0, None)] if version == 33 else [],
                )
                served = canonical_store.current_observation_rows(
                    connection, "weight", current_observation_mapper_versions()
                )
                self.assertEqual(
                    [("2026-10-15", 72.5, None)],
                    [
                        (row["observed_on"], row["value"], row["provider"])
                        for row in served
                    ],
                )

    def test_validate_applies_startup_invariants_to_the_restored_database(self) -> None:
        # A backup taken mid-resync, after a privacy purge, before any planning write.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
                connection.execute("DELETE FROM planning_state")
                connection.execute("DELETE FROM kv WHERE key = 'profile'")
                connection.execute(
                    "INSERT INTO kv(key, value, updated_at) VALUES "
                    "('garmin_full_resync_running', '1', 'then'), "
                    "('garmin_full_resync_status', 'running', 'then'), "
                    "('sync_operation_status', 'running', 'then')"
                )
                # Taken mid library push and mid refresh (#2888): restore used to
                # leave these stuck until the next restart.
                connection.execute(
                    "INSERT INTO workout_library(id, local_id, payload, sync_state, "
                    "updated_at) VALUES ('w-1', 'local-1', '{}', 'syncing', 'then')"
                )
                connection.execute(
                    "INSERT INTO provider_refresh_history(id, provider, area, "
                    "operation_id, trigger, started_at, phase, status) VALUES "
                    "('r-1', 'garmin', 'data', 'op', 'scheduler', 'then', "
                    "'fetching', 'running')"
                )
                connection.commit()
            key_values = KeyValueRepository(lambda: "2026-11-09T08:00:00+00:00")
            service(
                Path(directory),
                maximum_bytes=10_000_000,
                schema_is_current=database_schema_is_current,
                migrate_schema=migrate_schema,
                startup_invariants=lambda db: apply_startup_invariants(
                    db,
                    key_values=key_values,
                    now="2026-11-09T08:00:00+00:00",
                    default_profile_json='{"name": "Synthetic"}',
                    settle_interrupted=settle_interrupted_sync_state,
                ),
                row_factory=row_factory,
            ).validate(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = row_factory
                self.assertEqual(
                    key_values.get(connection, "garmin_full_resync_running"), "0"
                )
                self.assertEqual(
                    key_values.get(connection, "garmin_full_resync_status"), ""
                )
                self.assertEqual(
                    key_values.get(connection, "profile"), '{"name": "Synthetic"}'
                )
                self.assertEqual(
                    "", key_values.get(connection, "sync_operation_status")
                )
                unit = connection.execute(
                    "SELECT sync_state, sync_error FROM workout_library"
                ).fetchone()
                self.assertEqual(
                    ("sync_error", "Die Übertragung wurde unterbrochen."),
                    (unit["sync_state"], unit["sync_error"]),
                )
                refresh = connection.execute(
                    "SELECT status, error_code FROM provider_refresh_history"
                ).fetchone()
                self.assertEqual(
                    ("skipped", "process_interrupted"),
                    (refresh["status"], refresh["error_code"]),
                )
                revision = PlanningRevisionService(
                    PlanningStateRepository(), lambda: "2026-11-09T09:00:00+00:00"
                )
                revision.bump(connection)
                self.assertEqual(revision.read(connection), 1)

    def test_validate_rejects_a_pre_baseline_backup_by_release_without_modifying_it(
        self,
    ) -> None:
        # Releases before 2.0.5 wrote schemas below 26; their backups are refused.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO messages(role, content, created_at) VALUES ('user', 'Hallo', 'now')"
                )
                connection.execute("PRAGMA user_version = 25")
                connection.commit()
                before = list(connection.iterdump())
            with self.assertRaises(AppError) as raised:
                service(
                    Path(directory),
                    schema_is_current=database_schema_is_current,
                    migrate_schema=migrate_schema,
                ).validate(path)
            self.assertEqual(raised.exception.status, 400)
            self.assertIn("vor 2.0.5", str(raised.exception))
            self.assertIn("Release 2.0.5 öffnen", str(raised.exception))
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(list(connection.iterdump()), before)
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 25
                )

    def test_validate_rejects_unexpected_schema_without_scrubbing_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE sessions (token TEXT)")
                connection.execute("INSERT INTO sessions VALUES ('synthetic-session')")
                connection.commit()
            with self.assertRaises(AppError) as raised:
                service(
                    Path(directory), schema_is_current=lambda _connection: False
                ).validate(path)
            self.assertEqual(raised.exception.status, 400)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1
                )

    def test_validate_rejects_foreign_key_violations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
                connection.execute(
                    "CREATE TABLE children (parent_id INTEGER REFERENCES parents(id))"
                )
                connection.execute("INSERT INTO children VALUES (404)")
                connection.commit()
            with self.assertRaises(AppError) as raised:
                service(Path(directory)).validate(path)
            self.assertEqual(raised.exception.status, 400)

    def test_validate_rejects_failed_integrity_check_before_scrubbing(self) -> None:
        connection = Mock()
        connection.execute.side_effect = lambda statement: {
            "PRAGMA integrity_check": Mock(
                fetchone=Mock(return_value={"integrity_check": "corrupt"})
            ),
            "PRAGMA foreign_key_check": Mock(fetchall=Mock(return_value=[])),
        }.get(statement, Mock())
        backend = Mock(connect=Mock(return_value=connection))
        validator = service(
            Path("unused"), sqlite_backend=backend, sqlcipher_available=True
        )
        with self.assertRaises(AppError) as raised:
            validator.validate(Path("backup.db"))
        self.assertEqual(raised.exception.status, 400)
        self.assertFalse(
            any(
                call.args == ("DELETE FROM sessions",)
                for call in connection.execute.call_args_list
            )
        )
        connection.commit.assert_not_called()
        connection.close.assert_called_once()

    def test_validate_requires_sqlcipher_for_encrypted_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = Mock()
            validator = service(
                Path(directory),
                app_password="synthetic-key",
                sqlcipher_available=False,
                sqlite_backend=backend,
            )
            with self.assertRaises(AppError) as raised:
                validator.validate(Path(directory) / "does-not-exist.db")
            self.assertEqual(raised.exception.status, 503)
            backend.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
