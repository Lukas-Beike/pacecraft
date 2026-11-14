"""Recovery copies and purge markers the app keeps beside the database.

A restore and the one-time Intervals.icu purge each save an encrypted copy of
the whole database in the data directory, and the purge leaves a marker file
there. Local privacy deletion removes them with the database content: a copy
holds exactly the data the athlete asked to erase.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# ``<database>.<label>-<timestamp>-<id>`` as written by the restore service and,
# in release 2.0.0, by the Intervals.icu purge (removed; its copies stay on disk).
_COPY_LABELS = r"(?P<label>pre-restore|pre-intervals-purge-v\d+)"
# The label of the copy a restore writes; only these are pruned (#2914).
RESTORE_LABEL = "pre-restore"
_MARKER_PATTERN = re.compile(r"intervals-purge-v\d+\.done")


@dataclass(frozen=True)
class RecoveryCopyErasure:
    removed: int
    failed: int


class RecoveryCopies:
    """The recovery copies and purge markers of one data directory."""

    def __init__(self, data_dir: Path, database_name: str) -> None:
        self._data_dir = data_dir
        self._copy_pattern = re.compile(
            rf"{re.escape(database_name)}\.{_COPY_LABELS}-(?P<made>\d{{14}})-[0-9a-f]{{32}}"
        )

    def paths(self) -> list[Path]:
        try:
            with os.scandir(self._data_dir) as entries:
                return sorted(
                    Path(entry.path)
                    for entry in entries
                    if entry.is_file(follow_symlinks=False)
                    and (
                        self._copy_pattern.fullmatch(entry.name)
                        or _MARKER_PATTERN.fullmatch(entry.name)
                    )
                )
        except OSError:
            return []

    def count(self) -> int:
        return len(self.paths())

    def summary(self) -> dict[str, int | float | None]:
        """Count, newest age and total size of the copies; never opened.

        The age comes from the UTC time in the name: a copy keeps the
        database's modification time.
        """
        sizes: list[int] = []
        made: list[datetime] = []
        for path in self.paths():
            match = self._copy_pattern.fullmatch(path.name)
            try:
                if match:
                    sizes.append(path.stat().st_size)
                    stamp = f"{match['made']}+0000"
                    made.append(datetime.strptime(stamp, "%Y%m%d%H%M%S%z"))
            except OSError, ValueError:
                continue
        age = (datetime.now(UTC) - max(made)).total_seconds() / 3600 if made else None
        return {
            "count": len(sizes),
            "newest_age_hours": None if age is None else round(age, 1),
            "total_bytes": sum(sizes),
        }

    def erase(self) -> RecoveryCopyErasure:
        return self._remove(self.paths())

    def prune_restore_copies(self, keep: str) -> RecoveryCopyErasure:
        """Delete every older restore copy, keeping ``keep`` (athlete, 2026-11-14).

        Only copies with the restore label go; purge copies, markers and every
        other file stay. Nothing is deleted unless ``keep`` is itself a restore
        copy that exists.
        """
        restore_copies = [
            path
            for path in self.paths()
            if (match := self._copy_pattern.fullmatch(path.name))
            and match["label"] == RESTORE_LABEL
        ]
        if keep not in {path.name for path in restore_copies}:
            return RecoveryCopyErasure(removed=0, failed=0)
        return self._remove([path for path in restore_copies if path.name != keep])

    @staticmethod
    def _remove(paths: list[Path]) -> RecoveryCopyErasure:
        removed = failed = 0
        for path in paths:
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                failed += 1
        return RecoveryCopyErasure(removed=removed, failed=failed)
