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
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
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
# A failed nightly copy is retried after this pause, not on every 5-minute pass.
RETRY_AFTER: Final = timedelta(hours=1)
# The snapshot a copy is staged in until its checks passed (backup.database).
_STAGING_PATTERN: Final = ".database-backup-*.tmp*"


@dataclass(frozen=True)
class _Copy:
    path: Path
    kind: str
    made: datetime


class AutomaticBackups:
    """The automatic copies of one database: written into ``directory``; the
    purge also reads the ``earlier`` directories (#3760)."""

    def __init__(
        self,
        directory: Path,
        database_name: str,
        backup: Callable[[Path], DatabaseBackupService],
        logger: logging.Logger,
        earlier: tuple[Path, ...] = (),
    ) -> None:
        self._directory = directory
        self._directories = tuple(dict.fromkeys((directory, *earlier)))
        self._backup = backup
        self._logger = logger
        self._pattern = re.compile(
            rf"{re.escape(database_name)}\.auto-(?P<kind>{NIGHTLY}|{PREMIGRATION}-v\d+)"
            r"-(?P<made>\d{14})"
        )
        self._database_name = database_name

    def directories(self) -> tuple[Path, ...]:
        return self._directories

    def copies(self) -> list[_Copy]:
        """The verified copies, oldest first; a directory may not exist yet."""
        found = []
        for path in self._entries():
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
            copy.kind == NIGHTLY
            and copy.made.astimezone(zone).date() == local_now.date()
            for copy in self.copies()
        )

    def run_nightly(
        self,
        local_now: datetime,
        last_failure: str | None,
        record_failure: Callable[[dict[str, Any]], None],
    ) -> bool:
        """Write the nightly copy when due; a failure is logged and recorded,
        and the next attempt waits an hour."""
        now = datetime.now(UTC)
        failed_at = _failure_time(last_failure)
        if not self.nightly_due(local_now) or (
            failed_at is not None and now - failed_at < RETRY_AFTER
        ):
            return False
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
        failed_at = _failure_time(failure)
        failure_age = None if failed_at is None else _hours(now, failed_at)
        return {
            "enabled": True,
            "nightly_count": len(nightly),
            "newest_age_hours": _hours(now, copies[-1].made) if copies else None,
            "premigration_present": any(c.kind == PREMIGRATION for c in copies),
            "last_failure_age_hours": failure_age,
        }

    def erase(self) -> RecoveryCopyErasure:
        """Delete every automatic copy and an interrupted copy's staging file
        (the athlete's confirmed purge, #3760)."""
        staged = [
            path for path in self._entries() if fnmatchcase(path.name, _STAGING_PATTERN)
        ]
        return RecoveryCopies.remove([copy.path for copy in self.copies()] + staged)

    def _entries(self) -> list[Path]:
        found = []
        for directory in self._directories:
            try:
                found.extend(path for path in directory.iterdir() if path.is_file())
            except OSError:
                continue
        return found


def _failure_time(failure: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(failure or "null")["at"])
    except TypeError, ValueError, KeyError:
        return None


def _hours(now: datetime, then: datetime) -> float:
    return round((now - then).total_seconds() / 3600, 1)


def backup_directory(setting: str, data_dir: Path) -> Path | None:
    """The configured directory: ``off`` disables, empty means the default."""
    value = setting.strip()
    if value.lower() == "off":
        return None
    return Path(value) if value else data_dir / DEFAULT_DIRECTORY
