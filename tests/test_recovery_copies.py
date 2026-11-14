from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.backup.recovery_copies import RecoveryCopies

# Written by the Intervals.icu purge of release 2.0.0, which is removed; the copies
# stay on disk until the athlete erases them.
BACKUP_LABEL = "pre-intervals-purge-v1"
PURGE_MARKER_FILE = "intervals-purge-v1.done"
DATABASE = "athlete.db"
STAMP = "20261101120000-" + "0123456789abcdef" * 2


class RecoveryCopiesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.copies = RecoveryCopies(self.root, DATABASE)

    def touch(self, *names: str) -> None:
        for name in names:
            (self.root / name).write_bytes(b"x")

    def test_it_matches_the_names_the_producers_write(self) -> None:
        produced = (
            f"{DATABASE}.pre-restore-{STAMP}",
            f"{DATABASE}.{BACKUP_LABEL}-{STAMP}",
            PURGE_MARKER_FILE,
        )
        self.touch(*produced)
        self.assertEqual([path.name for path in self.copies.paths()], sorted(produced))

    def test_it_never_touches_the_live_database_or_other_files(self) -> None:
        kept = (
            DATABASE,
            f"{DATABASE}-wal",
            f"{DATABASE}-shm",
            f"{DATABASE}.pre-restore-{STAMP}.tmp",
            f"other.db.pre-restore-{STAMP}",
            ".intervals-coach-export-abc.zip",
            "garmin-tokens.json",
        )
        self.touch(*kept)
        (self.root / f"{DATABASE}.pre-restore-{STAMP[:-1]}x").mkdir()
        self.assertEqual(self.copies.paths(), [])
        self.assertEqual(self.copies.erase().removed, 0)
        for name in kept:
            self.assertTrue((self.root / name).exists())

    def test_erase_counts_removed_and_failed_files(self) -> None:
        self.touch(f"{DATABASE}.pre-restore-{STAMP}", PURGE_MARKER_FILE)
        real_unlink = Path.unlink

        def unlink(path: Path, *args: object, **kwargs: object) -> None:
            if path.name == PURGE_MARKER_FILE:
                raise PermissionError("locked")
            real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(Path, "unlink", unlink):
            outcome = self.copies.erase()
        self.assertEqual((outcome.removed, outcome.failed), (1, 1))

    def test_pruning_keeps_the_newest_restore_copy_and_every_other_file(self) -> None:
        older = f"{DATABASE}.pre-restore-20261101120000-{'a' * 32}"
        newest = f"{DATABASE}.pre-restore-20261102120000-{'b' * 32}"
        kept = (
            newest,
            f"{DATABASE}.{BACKUP_LABEL}-{STAMP}",
            PURGE_MARKER_FILE,
            DATABASE,
            f"other.db.pre-restore-{STAMP}",
        )
        self.touch(older, *kept)

        outcome = self.copies.prune_restore_copies(newest)

        self.assertEqual((outcome.removed, outcome.failed), (1, 0))
        self.assertFalse((self.root / older).exists())
        for name in kept:
            self.assertTrue((self.root / name).exists())

    def test_pruning_deletes_nothing_without_the_copy_to_keep(self) -> None:
        older = f"{DATABASE}.pre-restore-{STAMP}"
        self.touch(older)
        for keep in (
            f"{DATABASE}.pre-restore-20261102120000-{'b' * 32}",
            PURGE_MARKER_FILE,
        ):
            with self.subTest(keep=keep):
                self.assertEqual(self.copies.prune_restore_copies(keep).removed, 0)
        self.assertTrue((self.root / older).exists())

    def test_a_missing_data_directory_has_no_copies(self) -> None:
        missing = RecoveryCopies(self.root / "missing", DATABASE)
        self.assertEqual(missing.count(), 0)
        self.assertEqual(missing.erase().removed, 0)


if __name__ == "__main__":
    unittest.main()
