"""The athlete's own apply of a shown adaptive replan preview (#4020).

The same local apply the Coach runs (the latest preview only, under the plan
checks that block an illness override and a unit the replan leaves without its
data); the athlete's tap on "Übernehmen" is the approval the Coach path takes
from the next message. The Intervals.icu write of an illness pause stays with
the athlete's named sync.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.errors import AppError
from backend.planning import plan_checks
from backend.planning.plan_guard_service import PlanGuardService


class AdaptiveReplanActionService:
    """Apply the shown preview locally under the plan checks, or recalculate it."""

    def __init__(
        self,
        database_manager: Any,
        preview_service: Callable[[], Any],
        illness_sync: Callable[[], Any],
        plan_guard: Callable[[], PlanGuardService],
    ) -> None:
        self._database_manager = database_manager
        self._preview_service = preview_service
        self._illness_sync = illness_sync
        self._plan_guard = plan_guard

    def preview(self) -> dict[str, Any]:
        """Recalculate the preview from today's state; nothing changes."""
        return self._preview_service().preview()

    def apply(
        self, adjustment_id: Any, accept: list[str] | None = None
    ) -> dict[str, Any]:
        """Apply the latest preview; a plan check that blocks rolls it back."""
        latest = self._preview_service().latest_preview()
        if (
            not latest
            or str(latest.get("id")) != str(adjustment_id)
            or latest.get("status") != "preview"
        ):
            raise AppError(
                409,
                "Die Vorschau ist nicht mehr aktuell. Bitte neu prüfen.",
                reason="preview_outdated",
            )
        sync = self._illness_sync()
        guard = self._plan_guard()
        with self._database_manager.unit_of_work():
            before = guard.findings()
            applied = sync.apply_local(adjustment_id)
            # The replan rewrites existing units but cannot author a focus,
            # strength exercises or a heart-rate cap: a legacy unit that lacked
            # them before passes, a unit the replan leaves without them blocks.
            notes = guard.verify(
                before, accept or [], inherited=plan_checks.REQUIRED_CODES
            )
        result = sync.complete(applied, sync_illness_to_intervals=False)
        return {**result, "plan_check_notes": notes}
