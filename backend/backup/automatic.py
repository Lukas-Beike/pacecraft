"""Automatic verified database copies: nightly and before a schema migration (#3760).

Each copy is the backup download's snapshot (SQLCipher, the database key,
integrity and foreign key checks) written into the backup directory; only a
checked copy gets its final name, so every listed copy is verified. The
directory listing is the state: it survives a restore of the database.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Final

from backend.backup.database import DatabaseBackupService
from backend.backup.recovery_copies import RecoveryCopies, RecoveryCopyErasure

# The default directory below the data directory; ``BACKUP_DIR`` overrides it.
DEFAULT_DIRECTORY: Final = "automatic-backups"
NIGHTLY: Final = "nightly"
PREMIGRATION: Final = "premigration"
# Athlete decision 2026-11-20: 7 nightly copies, the newest pre-migration copy,
# the nightly copy from 03:30 local time on.
NIGHTLY_KEEP: Final = 7
NIGHTLY_TIME: Final = clock_time(3, 30)
FAILURE_KEY: Final = "database_backup_automatic_failure"


@dataclass(frozen=True)
class _Copy:
    path: Path
    kind: str
    made: datetime


class AutomaticBackups:
    """The automatic copies of one database in one backup directory."""

    def __init__(
        self,
        directory: Path,
        database_name: str,
        backup: Callable[[Path], DatabaseBackupService],
        logger: logging.Logger,
    ) -> None:
        self._directory = directory
        self._backup = backup
        self._logger = logger
        self._pattern = re.compile(
            rf"{re.escape(database_name)}\.auto-(?P<kind>{NIGHTLY}|{PREMIGRATION}-v\d+)"
            r"-(?P<made>\d{14})"
        )
        self._database_name = database_name

    def copies(self) -> list[_Copy]:
        """The verified copies, oldest first; the directory may not exist yet."""
        found = []
        try:
            entries = list(self._directory.iterdir())
        except OSError:
            return []
        for path in entries:
            match = self._pattern.fullmatch(path.name)
            if match and path.is_file():
                made = datetime.strptime(match["made"] + "+0000", "%Y%m%d%H%M%S%z")
                kind = NIGHTLY if match["kind"] == NIGHTLY else PREMIGRATION
                found.append(_Copy(path, kind, made))
        return sorted(found, key=lambda copy: (copy.made, copy.path.name))

    def write(self, label: str, now: datetime) -> Path:
        """Write one checked copy, then prune its kind; raises when it fails."""
        self._directory.mkdir(parents=True, exist_ok=True)
        name = f"{self._database_name}.auto-{label}-{now.strftime('%Y%m%d%H%M%S')}"
        target = self._backup(self._directory).write_verified_copy(name)
        kind = NIGHTLY if label == NIGHTLY else PREMIGRATION
        keep = NIGHTLY_KEEP if kind == NIGHTLY else 1
        older = [copy for copy in self.copies() if copy.kind == kind]
        RecoveryCopies.remove([copy.path for copy in older[:-keep]])
        return target

    def nightly_due(self, local_now: datetime) -> bool:
        """Due from 03:30 local time until today's nightly copy exists."""
        if local_now.time() < NIGHTLY_TIME:
            return False
        zone = local_now.tzinfo or UTC
        return not any(
            copy.kind == NIGHTLY and copy.made.astimezone(zone).date() == local_now.date()
            for copy in self.copies()
        )

    def run_nightly(
        self,
        local_now: datetime,
        record_failure: Callable[[dict[str, Any]], None],
    ) -> bool:
        """Write the nightly copy when due; a failure is logged and recorded."""
        if not self.nightly_due(local_now):
            return False
        now = datetime.now(UTC)
        try:
            self.write(NIGHTLY, now)
        except Exception as exc:  # noqa: BLE001 - a failed copy never stops the app.
            self._logger.error(
                "Automatic database backup failed",
                extra={
                    "event": "database_backup_automatic_failed",
                    "context": {"kind": NIGHTLY, "error_class": type(exc).__name__},
                },
            )
            record_failure({"at": now.isoformat(), "kind": NIGHTLY})
            return False
        return True

    def before_migration(self, version: int) -> None:
        """Write the pre-migration copy; its failure stops the migration."""
        try:
            self.write(f"{PREMIGRATION}-v{version}", datetime.now(UTC))
        except Exception as exc:
            self._logger.error(
                "Pre-migration database backup failed",
                extra={
                    "event": "database_backup_automatic_failed",
                    "context": {
                        "kind": PREMIGRATION,
                        "error_class": type(exc).__name__,
                    },
                },
            )
            raise RuntimeError(
                "Die Sicherung vor der Datenbankmigration ist fehlgeschlagen; "
                "die Datenbank bleibt unverändert."
            ) from exc

    def summary(self, now: datetime, failure: str | None) -> dict[str, Any]:
        """Counts and ages only; never a name, path or content."""
        copies = self.copies()
        nightly = [copy for copy in copies if copy.kind == NIGHTLY]
        try:
            failed_at = datetime.fromisoformat(json.loads(failure or "null")["at"])
            failure_age: float | None = _hours(now, failed_at)
        except TypeError, ValueError, KeyError:
            failure_age = None
        return {
            "enabled": True,
            "nightly_count": len(nightly),
            "newest_age_hours": _hours(now, copies[-1].made) if copies else None,
            "premigration_present": any(c.kind == PREMIGRATION for c in copies),
            "last_failure_age_hours": failure_age,
        }

    def erase(self) -> RecoveryCopyErasure:
        """Delete every automatic copy (the athlete's confirmed purge, #3760)."""
        return RecoveryCopies.remove([copy.path for copy in self.copies()])


def _hours(now: datetime, then: datetime) -> float:
    return round((now - then).total_seconds() / 3600, 1)


def backup_directory(setting: str, data_dir: Path) -> Path | None:
    """The configured directory: ``off`` disables, empty means the default."""
    value = setting.strip()
    if value.lower() == "off":
        return None
    return Path(value) if value else data_dir / DEFAULT_DIRECTORY
