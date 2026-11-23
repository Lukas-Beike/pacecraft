"""Authorize and execute the Coach's explicit synchronization tools."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from backend.canonical.labels import provider_name
from backend.canonical.vocabulary import ProviderId
from backend.coach.authorization import authorized_operations, require_coach_scope
from backend.errors import STRUCTURED_AUTHORIZATION_ERROR, AppError
from backend.sync.authority import PlanningAuthorityService
from backend.sync.commands import ProviderRefreshCommandService
from backend.sync.conflict_commands import SyncConflictCommandService
from backend.sync.plan_commands import PlanPushCommandService
from backend.sync.plan_repair import PlanRepairManifestService
from backend.sync.plan_selection import StructuredPlanSyncService
from backend.sync.queue import SyncJobQueueService

# The providers Anbindungen offers a full resync for.
_FULL_RESYNC_PROVIDERS = frozenset({ProviderId.GARMIN.value, ProviderId.WAHOO.value})


class CoachSyncToolService:
    """Own authorization and effects for the Coach sync commands."""

    def __init__(
        self,
        queue: SyncJobQueueService,
        authority: PlanningAuthorityService,
        conflicts: SyncConflictCommandService,
        plan_sync: StructuredPlanSyncService,
        plan_repair: PlanRepairManifestService,
        plan_push: PlanPushCommandService,
        provider_refresh: ProviderRefreshCommandService,
        nutrition_diary: Any | None = None,
        *,
        connection_checks: Any = None,
        yazio_check: Callable[[], dict[str, Any]] | None = None,
        full_resync: Any = None,
        operation_id: Callable[[], str] = lambda: "",
    ) -> None:
        # Anbindungen's checks and full resync (#3960).
        self._connection_checks = connection_checks
        self._yazio_check = yazio_check
        self._full_resync = full_resync
        self._operation_id = operation_id
        self._queue = queue
        self._authority = authority
        self._conflicts = conflicts
        self._plan_sync = plan_sync
        self._plan_repair = plan_repair
        self._plan_push = plan_push
        self._provider_refresh = provider_refresh
        self._nutrition_diary = nutrition_diary

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        intent: dict[str, Any],
        sync_job_ids: list[str],
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        if name == "start_intervals_plan_sync":
            return self._start_plan_sync(arguments, intent, sync_job_ids)
        if name == "push_strength_to_garmin":
            return self._push_strength_to_garmin(arguments, intent, sync_job_ids)
        if name == "check_provider_connection":
            return self._check_connection(arguments, intent)
        if name == "get_sync_job":
            job_id = str(arguments.get("job_id") or "").strip()
            if job_id not in sync_job_ids:
                require_coach_scope(intent, f"sync_job:{job_id}")
            return {"ok": True, "job": self._queue.state(job_id)}
        if name == "sync_competitions":
            return self._sync_competitions(arguments, intent, sync_job_ids)
        if name == "sync_nutrition":
            return self._sync_nutrition(arguments, intent, sync_job_ids)
        if name == "resolve_training_sync_conflict":
            return self._resolve_conflict(arguments, intent, sync_job_ids)
        if name == "start_provider_refresh":
            return self._start_provider_refresh(
                arguments, intent, sync_job_ids, cancel_event
            )
        return None

    def _sync_nutrition(
        self, arguments: dict[str, Any], intent: dict[str, Any], sync_job_ids: list[str]
    ) -> dict[str, Any]:
        if (
            "sync_nutrition" not in authorized_operations(intent)
            or intent.get("target_system") != ProviderId.INTERVALS
        ):
            raise AppError(
                403, STRUCTURED_AUTHORIZATION_ERROR, reason="intent_scope_denied"
            )
        require_coach_scope(intent, "local_nutrition", "intervals_sync")
        manifest = arguments.get("_approval_manifest")
        if not isinstance(manifest, list) or self._nutrition_diary is None:
            raise AppError(409, "Die freigegebene Ern\u00e4hrungsvorschau fehlt.")
        dates = [entry.get("date") for entry in manifest if isinstance(entry, dict)]
        if len(dates) != len(manifest):
            raise AppError(
                409, "Die freigegebene Ern\u00e4hrungsvorschau ist ung\u00fcltig."
            )
        date_value = str(arguments.get("date") or "").strip()
        limit = arguments.get("pending_limit")
        if bool(date_value) == (limit is not None):
            raise AppError(
                400,
                "W\u00e4hle ein Datum oder ausstehende Tage.",
                reason="invalid_job_request",
            )
        if date_value and dates != [date_value]:
            raise AppError(
                409, "Das freigegebene Ern\u00e4hrungsdatum hat sich ge\u00e4ndert."
            )
        if limit is not None and (type(limit) is not int or len(dates) > limit):
            raise AppError(
                409, "Der freigegebene Ern\u00e4hrungszeitraum ist ung\u00fcltig."
            )
        if self._nutrition_diary.sync_status.approval_manifest(dates=dates) != manifest:
            raise AppError(
                409,
                "Die Ern\u00e4hrungsdaten haben sich seit der Freigabe ge\u00e4ndert.",
            )
        job = self._queue.enqueue(
            ProviderId.INTERVALS.value,
            "nutrition_sync",
            {"approval_manifest": manifest},
            requested_by="coach",
        )
        sync_job_ids.append(job["id"])
        return {"ok": True, "status": "queued", "sync_job_id": job["id"]}

    def _start_provider_refresh(
        self,
        arguments: dict[str, Any],
        intent: dict[str, Any],
        sync_job_ids: list[str],
        cancel_event: threading.Event | None,
    ) -> dict[str, Any]:
        if "start_provider_refresh" not in authorized_operations(intent):
            raise AppError(
                403, STRUCTURED_AUTHORIZATION_ERROR, reason="intent_scope_denied"
            )
        provider = str(intent.get("target_system") or "")
        require_coach_scope(intent, f"{provider}_refresh")
        if arguments.get("full_resync") is True:
            return self._start_full_resync(provider)
        result = self._provider_refresh.start(
            provider, arguments, cancel_event=cancel_event
        )
        if result.get("status") == "queued":
            sync_job_ids.append(result["sync_job_id"])
        return result

    def _start_full_resync(self, provider: str) -> dict[str, Any]:
        """The confirmed full resync of Anbindungen, in the background (#3960).

        It reads only and keeps the existing data; a run in progress is
        returned instead of a second one, as the route does.
        """
        if provider not in _FULL_RESYNC_PROVIDERS or self._full_resync is None:
            raise AppError(
                400,
                "Ein vollständiger Resync ist nur für Garmin und Wahoo möglich.",
                reason="invalid_job_request",
            )
        result = self._full_resync.start(provider, self._operation_id())
        return {"ok": True, **result}

    def _check_connection(
        self, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        """One read-only connection check of Anbindungen (#3960)."""
        if "check_provider_connection" not in authorized_operations(intent):
            raise AppError(
                403, STRUCTURED_AUTHORIZATION_ERROR, reason="intent_scope_denied"
            )
        provider = str(arguments.get("provider") or "")
        if provider == "yazio" and self._yazio_check is not None:
            return {"ok": True, "provider": provider, **self._yazio_check()}
        checks = self._connection_checks
        if checks is None or provider not in checks.providers():
            raise AppError(404, "Unbekannter Anbieter.", reason="unknown_provider")
        return {"ok": True, "provider": provider, **checks.check(provider)}

    def _start_plan_sync(
        self, arguments: dict[str, Any], intent: dict[str, Any], sync_job_ids: list[str]
    ) -> dict[str, Any]:
        if (
            "start_intervals_plan_sync" not in authorized_operations(intent)
            or intent.get("target_system") != ProviderId.INTERVALS
        ):
            raise AppError(
                403, STRUCTURED_AUTHORIZATION_ERROR, reason="intent_scope_denied"
            )
        entries = arguments.get("entries")
        if "repair" in arguments and type(arguments["repair"]) is not bool:
            raise AppError(
                400, "repair muss ein Boolean sein.", reason="invalid_job_request"
            )
        if arguments.get("repair"):
            prepared = self._plan_repair.prepare(arguments, intent)
            for scope_group in prepared.required_scope_groups:
                require_coach_scope(intent, *scope_group)
            manifest = self._plan_repair.execute(prepared)
            return self._plan_sync.report_skips(
                self._plan_push.enqueue(
                    manifest,
                    sync_job_ids,
                    reason=str(arguments.get("reason") or "Coach-Reparatur"),
                    repair=True,
                ),
                manifest,
            )
        prepared = self._plan_sync.prepare(entries, intent)
        for scope_group in prepared.required_scope_groups:
            require_coach_scope(intent, *scope_group)
        return self._plan_sync.execute(
            prepared,
            sync_job_ids,
            reason=str(arguments.get("reason") or "Coach-Anfrage"),
        )

    def _push_strength_to_garmin(
        self, arguments: dict[str, Any], intent: dict[str, Any], sync_job_ids: list[str]
    ) -> dict[str, Any]:
        """Queue the approved Garmin push; entries come from the card."""
        if (
            "push_strength_to_garmin" not in authorized_operations(intent)
            or intent.get("target_system") != ProviderId.GARMIN
        ):
            raise AppError(
                403, STRUCTURED_AUTHORIZATION_ERROR, reason="intent_scope_denied"
            )
        require_coach_scope(intent, "garmin_sync")
        entries = arguments.get("entries")
        valid = isinstance(entries, list) and all(
            isinstance(entry, dict)
            and (
                entry.get("remove") is True
                or len(str(entry.get("expected_payload_hash") or "")) == 64
            )
            for entry in entries
        )
        if not valid or not entries:
            raise AppError(409, "Die freigegebene Garmin-Auswahl fehlt.")
        return self._plan_push.enqueue(
            entries,
            sync_job_ids,
            reason=str(arguments.get("reason") or "Coach-Anfrage"),
            provider=ProviderId.GARMIN.value,
        )

    def _sync_competitions(
        self, arguments: dict[str, Any], intent: dict[str, Any], sync_job_ids: list[str]
    ) -> dict[str, Any]:
        if (
            "sync_competitions" not in authorized_operations(intent)
            or intent.get("target_system") != ProviderId.INTERVALS
        ):
            raise AppError(
                403,
                "Die strukturierte Coach-Autorisierung erlaubt diesen Sync nicht.",
                reason="intent_scope_denied",
            )
        require_coach_scope(intent, "local_competitions")
        approval_manifest = arguments.get("_approval_manifest")
        if not isinstance(approval_manifest, list):
            raise AppError(409, "Für den Wettkampf-Sync fehlt die bestätigte Vorschau.")
        manifest = self._authority.mark_competitions_authoritative(approval_manifest)
        if not isinstance(manifest, list):
            raise AppError(500, "Die bestätigte Wettkampf-Vorschau ist ungültig.")
        job = self._queue.enqueue(
            ProviderId.INTERVALS.value,
            "competition_push",
            {
                "reason": str(arguments.get("reason") or "Bestätigter Coach-Auftrag"),
                "approval_manifest": manifest,
            },
            requested_by="coach",
        )
        sync_job_ids.append(job["id"])
        return {"ok": True, "status": "queued", "sync_job_id": job["id"]}

    def _resolve_conflict(
        self, arguments: dict[str, Any], intent: dict[str, Any], sync_job_ids: list[str]
    ) -> dict[str, Any]:
        if "resolve_training_sync_conflict" not in authorized_operations(intent):
            raise AppError(
                403, STRUCTURED_AUTHORIZATION_ERROR, reason="intent_scope_denied"
            )
        local_id = str(arguments.get("local_id") or "").strip()
        if local_id:
            require_coach_scope(intent, f"competition:{local_id}")
            return self._conflicts.resolve_local(
                local_id,
                str(arguments.get("strategy") or "keep_local").strip().casefold(),
            )
        job_id = str(arguments.get("job_id") or "").strip()
        require_coach_scope(intent, f"sync_job:{job_id}")
        previous_job = self._conflicts.job_state(job_id)
        provider = previous_job["provider"]
        push = self._conflicts.is_push_job(previous_job)
        require_coach_scope(
            intent, f"{provider}_sync" if push else f"{provider}_refresh"
        )
        if (
            intent.get("target_system") != provider
            or bool((intent.get("request") or {}).get("remote_write")) != push
        ):
            raise AppError(
                403,
                f"Die Wiederholung benötigt den {provider_name(provider)}-Auftrag des ursprünglichen Jobs.",
                reason="request_target",
            )
        result = self._conflicts.retry_job(job_id)
        sync_job_ids.append(job_id)
        return result
