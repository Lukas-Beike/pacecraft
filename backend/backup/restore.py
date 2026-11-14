"""Restore a validated database and resume durable work under maintenance."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.backup.database import DatabaseBackupService
from backend.backup.recovery_copies import RecoveryCopies
from backend.backup.restore_validation import DatabaseRestoreValidationService
from backend.coach.job_store import CoachJobStore
from backend.coach.turn_failures import CoachTurnFailureService
from backend.db.manager import DatabaseManager
from backend.errors import AppError
from backend.runtime.maintenance import MaintenanceGate
from backend.sync.queue import SyncJobQueueService


@dataclass(frozen=True)
class DatabaseRestoreConfig:
    data_dir: Path
    database_path: Path


@dataclass(frozen=True)
class RestoreDependencies:
    validation: DatabaseRestoreValidationService
    backup: DatabaseBackupService
    database_manager: Callable[[], DatabaseManager]
    maintenance_gate: MaintenanceGate
    profile_mutation_coordinator: Any
    sync_jobs: SyncJobQueueService
    coach_jobs: CoachJobStore
    coach_failures: CoachTurnFailureService
    sync_wake: Any
    coach_wake: Any
    config: DatabaseRestoreConfig
    redact: Callable[[str], str]
    diagnostics_suspended: Callable[[], AbstractContextManager[None]]
    canonical_backfill_start: Callable[[], None]
    logger: logging.Logger
    # Re-derives the runtime configuration from the restored stored credentials.
    refresh_credentials: Callable[[], Any] | None = None
    # Stops a running Coach turn so the maintenance drain does not wait for it.
    cancel_coach_turns: Callable[[], None] | None = None


class DatabaseRestoreService:
    """Own the complete staged restore, file swap, and worker recovery."""

    def __init__(self, dependencies: RestoreDependencies) -> None:
        self._dependencies = dependencies

    def _replace(self, temporary_path: Path) -> tuple[str, int, bool] | None:
        """Swap the staged file in; return the recovery copy (see _copy_previous)."""
        database_path = self._dependencies.config.database_path
        previous_backup: tuple[str, int, bool] | None = None
        # The caller holds the maintenance gate, which refuses new operations and
        # waits for running ones. restore_drain then waits for every running
        # writer and reader and makes later ones wait until the swap is done, so
        # no lock is needed besides the checkpoint's own unit of work.
        self._dependencies.backup.checkpoint()
        with self._dependencies.database_manager().restore_drain():
            if database_path.exists():
                previous_backup = self._copy_previous(database_path)
            self._swap(temporary_path, database_path)
        return previous_backup

    def _copy_previous(self, database_path: Path) -> tuple[str, int, bool]:
        """Copy the checkpointed, drained live file; never leave a partial copy.

        Returns the copy's name, the size it must have, and whether the live
        file held every commit: a non-empty ``-wal`` left by a failed
        checkpoint means the copy may miss the last transactions.
        """
        size = database_path.stat().st_size
        wal = Path(f"{database_path}-wal")
        holds_every_commit = not (wal.exists() and wal.stat().st_size > 0)
        if not self._dependencies.backup.has_free_space_for(size):
            raise self._storage_full()
        backup_path = self._dependencies.config.data_dir / (
            f"{database_path.name}.pre-restore-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-"
            f"{uuid.uuid4().hex}"
        )
        try:
            shutil.copy2(database_path, backup_path)
        except BaseException:
            backup_path.unlink(missing_ok=True)
            raise
        return backup_path.name, size, holds_every_commit

    def _prune_older_copies(self, copy: tuple[str, int, bool]) -> None:
        """Keep only the newest recovery copy once it is verified (#2914).

        Runs after a complete restore; a copy that may miss commits or does not
        verify keeps every older one. A failed deletion is logged and never
        fails the finished restore.
        """
        name, size, holds_every_commit = copy
        config = self._dependencies.config
        if not holds_every_commit or not self._dependencies.validation.verify_copy(
            config.data_dir / name, size
        ):
            self._dependencies.logger.warning(
                "Recovery copy not verified; older copies kept",
                extra={"event": "database_recovery_copy_unverified"},
            )
            return
        outcome = RecoveryCopies(
            config.data_dir, config.database_path.name
        ).prune_restore_copies(name)
        self._dependencies.logger.info(
            "Older recovery copies pruned",
            extra={
                "event": "database_recovery_copies_pruned",
                "context": {"removed": outcome.removed, "failed": outcome.failed},
            },
        )

    @staticmethod
    def _swap(temporary_path: Path, database_path: Path) -> None:
        """Replace the live file so that its WAL and SHM never meet the restored one.

        The sidecars are renamed aside first: a failed replace renames them back
        to the unchanged live file, a successful one deletes them. SQLite only
        opens the exact ``-wal``/``-shm`` names, so stale frames cannot attach.
        """
        parked: list[tuple[Path, Path]] = []
        try:
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database_path}{suffix}")
                aside = Path(f"{sidecar}.restore-aside")
                try:
                    os.replace(sidecar, aside)
                except FileNotFoundError:
                    continue
                parked.append((sidecar, aside))
            os.replace(temporary_path, database_path)
        except BaseException:
            for sidecar, aside in reversed(parked):
                os.replace(aside, sidecar)
            raise
        for _sidecar, aside in parked:
            # The restore is complete here; a leftover aside file is inert.
            with suppress(OSError):
                aside.unlink()

    def _resume(self) -> None:
        dependencies = self._dependencies
        try:
            # Restored jobs must run against the restored provider accounts.
            if dependencies.refresh_credentials is not None:
                dependencies.refresh_credentials()
            dependencies.sync_jobs.resume_interrupted()
            dependencies.coach_jobs.resume_interrupted(dependencies.coach_failures)
            # Restored canonical backfill state may be unfinished; the worker exits
            # once done or failed, so restart it (idempotent while alive).
            dependencies.canonical_backfill_start()
        finally:
            dependencies.sync_wake.set()
            dependencies.coach_wake.set()

    def _invalid_backup(self, exc: Exception) -> AppError:
        return AppError(
            400,
            "Das Datenbank-Backup konnte nicht validiert werden: "
            f"{self._dependencies.redact(str(exc))[:300]}",
        )

    @staticmethod
    def _storage_full() -> AppError:
        return AppError(
            507,
            "Für die Wiederherstellung ist nicht ausreichend freier Speicher verfügbar.",
            reason="restore_storage_full",
        )

    def _storage_error(self, exc: OSError) -> AppError:
        """Classify a file-system failure without forwarding OS text or paths."""
        if exc.errno == errno.ENOSPC:
            return self._storage_full()
        return AppError(
            500,
            "Das Datenbank-Backup konnte wegen eines Speicherfehlers nicht "
            "wiederhergestellt werden.",
            reason="restore_storage_failed",
        )

    def _logged_storage_error(self, exc: OSError) -> AppError:
        error = self._storage_error(exc)
        # Class and errno only: OS messages can carry data-directory paths.
        self._dependencies.logger.warning(
            "Database restore storage failure",
            extra={
                "event": "database_restore_storage_failed",
                "context": {
                    "error_class": type(exc).__name__,
                    "errno": exc.errno,
                    "status": error.status,
                },
            },
        )
        return error

    def _stage(self, payload: bytes | Iterable[bytes]) -> Path:
        try:
            return self._dependencies.validation.stage(payload)
        except AppError:
            raise
        except OSError as exc:
            raise self._logged_storage_error(exc) from exc
        except Exception as exc:
            raise self._invalid_backup(exc) from exc

    def _promote(self, temporary_path: Path) -> tuple[str, int, bool] | None:
        """Validate the staged file and swap it in; the caller holds the gate."""
        try:
            self._dependencies.validation.validate(temporary_path)
        except AppError:
            raise
        except Exception as exc:
            raise self._invalid_backup(exc) from exc
        try:
            return self._replace(temporary_path)
        except OSError as exc:
            raise self._logged_storage_error(exc) from exc

    def restore(self, payload: bytes | Iterable[bytes]) -> dict[str, Any]:
        """Restore from a whole backup or its ordered chunks.

        The payload is staged before the maintenance gate is taken, so a slow
        upload does not hold back ordinary operations. Validation and the file
        swap run under the gate. The staged file is removed on every path.
        """
        temporary_path = self._stage(payload)
        try:
            with (
                self._dependencies.maintenance_gate.restore(
                    self._dependencies.cancel_coach_turns
                ),
                self._dependencies.profile_mutation_coordinator,
            ):
                # Flushes stay blocked from before the swap until the cache is dropped,
                # so the pre-restore diagnostics cannot overwrite the restored ones.
                with self._dependencies.diagnostics_suspended():
                    previous_backup = self._promote(temporary_path)
                # The live database is already replaced here; failures must not be
                # reported as validation errors.
                try:
                    self._resume()
                except Exception as exc:
                    raise AppError(
                        500,
                        "Das Datenbank-Backup wurde wiederhergestellt, aber die "
                        "Hintergrundaufgaben konnten nicht fortgesetzt werden. "
                        "Bitte starte die App neu.",
                        reason="restored_resume_failed",
                    ) from exc
        finally:
            temporary_path.unlink(missing_ok=True)
        if previous_backup is not None:
            self._prune_older_copies(previous_backup)
        return {
            "status": "ok",
            "restored": True,
            "previous_database_backup": previous_backup[0] if previous_backup else None,
        }
