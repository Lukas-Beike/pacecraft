"""The athlete's own create, edit, archive and restore of planned units (#3950).

The same batch the Coach's ``apply_training_patch`` applies (validation, calendar
conflicts, change history for undo), plus the plan checks as notes: the athlete
decides, so the data-completeness and library-template rules that bind the Coach
are waived and an illness warning is accepted by the athlete's own tap.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.canonical.planning_fields import SESSION_FOCUS_KEYS
from backend.db import planning_store
from backend.db.repositories import PlanningStateRepository, TrainingPlanRepository
from backend.errors import AppError
from backend.planning import library, plan_checks
from backend.planning.plan_guard_service import PlanCheckError, PlanGuardService
from backend.planning.replacement import validate_period_order
from backend.planning.sport import planned_stored_sport

# Fields a create or an edit takes; the day and time go with them.
UNIT_FIELDS = frozenset(
    {
        "date",
        "starts_at_local",
        "sport",
        "environment",
        "name",
        "description",
        "duration_minutes",
        "target",
        "session_focus",
        "strength_intensity",
        "strength_structure",
        "hr_cap",
        "template_id",
    }
)
_CREATE_RATIONALE = "Vom Athleten angelegt"
_REQUIRED_NEW = ("date", "sport", "name", "description", "duration_minutes")
# The unit-level rules written for the Coach; the athlete's form supplies the
# data itself and a variant of a template is the athlete's choice.
_WAIVED = plan_checks.REQUIRED_CODES


def illness_block(error: PlanCheckError) -> PlanCheckError | AppError:
    """The athlete's answer to a plan check that blocks: only the health rule can, and a second tap accepts it."""
    if {item["code"] for item in error.findings} != set(plan_checks.BLOCKING_CODES):
        return error
    return AppError(
        409,
        "Krankheit gemeldet: In den nächsten Tagen keine harte oder lange Einheit.",
        reason="illness_hard",
    )


class PlannedUnitEditService:
    """Apply one athlete edit as the Coach's change batch, under the plan checks."""

    def __init__(
        self,
        database_manager: Any,
        change_service: Callable[[], Any],
        library_service: Callable[[], Any],
        plan_guard: Callable[[], PlanGuardService],
        library_plan_service: Callable[[], Any] | None = None,
        replacement_service: Callable[[], Any] | None = None,
    ) -> None:
        self._database_manager = database_manager
        self._change_service = change_service
        self._library_service = library_service
        self._plan_guard = plan_guard
        self._library_plan_service = library_plan_service
        self._replacement_service = replacement_service

    def schedule(
        self,
        library_workout_id: str,
        date: Any,
        *,
        plan_id: str | None = None,
        accept: list[str] | None = None,
    ) -> dict[str, Any]:
        """Plan a library entry on a day, optionally into a plan, under the plan checks.

        The same local apply the Coach's ``apply_workout_library_plan`` runs; the
        health rule blocks a hard or long unit within days of a reported illness
        unless the athlete accepts it (``accept``), the rules written for the
        Coach's data stay waived (#4055).
        """
        library_plans = self._library_plan_service
        if library_plans is None:
            raise AppError(500, "Die Bibliotheksplanung ist nicht verfügbar.")
        guard = self._plan_guard()
        with self._database_manager.unit_of_work():
            before = guard.findings()
            result = library_plans().apply(
                [
                    {
                        "library_workout_id": library_workout_id,
                        "date": date,
                        **({"plan_id": plan_id} if plan_id else {}),
                    }
                ]
            )
            notes = guard.verify(before, accept or [], waived=_WAIVED)
        return {**result, "plan_check_notes": notes}

    def create(
        self,
        fields: dict[str, Any],
        *,
        plan_id: str | None = None,
        accept: list[str] | None = None,
    ) -> dict[str, Any]:
        """Plan a new unit; without ``template_id`` it also saves its library entry."""
        fields = self._complete_new_unit(fields)

        def build() -> dict[str, Any]:
            return {
                "action": "create",
                **self._new_unit(fields),
                **({"plan_id": plan_id} if plan_id else {}),
            }

        return self._apply(build, plan_id, accept, create=True)

    def _complete_new_unit(self, fields: dict[str, Any]) -> dict[str, Any]:
        """The form's fields, filled from its library entry and checked for the minimum."""
        if fields.get("template_id"):
            fields = {**self._template_fields(fields["template_id"]), **fields}
        if any(fields.get(key) in (None, "") for key in _REQUIRED_NEW):
            raise AppError(
                400,
                "Eine neue Einheit braucht Tag, Sportart, Name, Workout und Dauer.",
                reason="invalid_change",
            )
        return fields

    def _new_unit(self, fields: dict[str, Any]) -> dict[str, Any]:
        """A unit ready for the change batch; without ``template_id`` its library entry is saved first."""
        template_id = fields.get("template_id")
        if not template_id:
            template_id = self._library_service().create_template(
                {
                    key: fields[key]
                    for key in fields
                    if key not in {"date", "starts_at_local", "template_id"}
                },
                "manual",
            )["id"]
        return {
            **{key: fields[key] for key in UNIT_FIELDS if key in fields},
            "template_id": template_id,
            "target": fields.get("target") or "AUTO",
            "rationale": _CREATE_RATIONALE,
        }

    def replace_period(
        self,
        plan_id: str,
        start: str,
        end: str,
        replaced_ids: list[str],
        workouts: list[dict[str, Any]],
        *,
        accept: list[str] | None = None,
    ) -> dict[str, Any]:
        """Swap a plan's units in a period for new ones as the Coach's plan replacement.

        ``replaced_ids`` are the units the athlete saw listed; a period that holds
        others now is refused. The replaced units stay in the local history.
        """
        replacement = self._replacement_service
        if replacement is None or not workouts:
            raise AppError(400, "Der Ersatz braucht mindestens eine neue Einheit.")
        start, end = validate_period_order({"start": start, "end": end})
        guard = self._plan_guard()
        with self._database_manager.unit_of_work() as db:
            plan = TrainingPlanRepository().get(db, plan_id)
            if not plan or plan.get("status") == "archived":
                raise AppError(
                    409,
                    "Der Trainingsplan ist nicht aktiv.",
                    reason="plan_not_available",
                )
            current = sorted(
                str(row["local_id"])
                for row in planning_store.replaceable_planned_unit_rows(
                    db, plan_id, start, end
                )
            )
            if current != sorted(replaced_ids):
                raise AppError(
                    409,
                    "Die Einheiten im Zeitraum haben sich inzwischen geändert.",
                    reason="period_changed",
                )
            before = guard.findings()
            prepared = [
                self._new_unit(self._complete_new_unit(item)) for item in workouts
            ]
            result = replacement().replace(
                {
                    "payload": {
                        "plan_name": plan["name"],
                        "goal": plan.get("goal") or "",
                        "workouts": prepared,
                    },
                    "expected_revision": PlanningStateRepository().read(db),
                    "period": {"start": start, "end": end},
                    "constraints": [],
                },
                selected_plan_id=plan_id,
                keep_plan=True,
            )
            notes = guard.verify(before, accept or [], waived=_WAIVED)
        return {**result, "plan_check_notes": notes}

    def _template_fields(self, template_id: Any) -> dict[str, Any]:
        """The content a unit takes from its library entry, until the form changes it."""
        entry = next(
            (
                item
                for item in self._library_service().list(1000)
                if str(item.get("id")) == str(template_id) and not item.get("date")
            ),
            None,
        )
        if entry is None:
            raise AppError(404, "Bibliothekseinheit nicht gefunden.")
        duration = library.library_workout_duration_minutes(entry)
        return {
            **planned_stored_sport(entry).fields(),
            "name": entry.get("name"),
            "description": entry.get("description"),
            "duration_minutes": None if duration is None else max(5, round(duration)),
            "target": entry.get("target"),
            **{
                key: entry[key]
                for key in SESSION_FOCUS_KEYS
                if entry.get(key) is not None
            },
        }

    def update(
        self,
        local_id: str,
        fields: dict[str, Any],
        expected_hash: Any,
        *,
        accept: list[str] | None = None,
    ) -> dict[str, Any]:
        """Change a unit's content; the hash is the one of the read the form came from."""
        return self._apply(
            lambda: {
                "action": "update",
                "local_id": local_id,
                "expected_payload_hash": self._hash(expected_hash),
                **{key: fields[key] for key in UNIT_FIELDS if key in fields},
            },
            None,
            accept,
        )

    def set_archived(
        self, local_id: str, archived: bool, expected_hash: Any
    ) -> dict[str, Any]:
        """Archive a unit out of the calendar, or restore it."""
        return self._apply(
            lambda: {
                "action": "archive" if archived else "restore",
                "local_id": local_id,
                "expected_payload_hash": self._hash(expected_hash),
            },
            None,
            None,
        )

    @staticmethod
    def _hash(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AppError(
                409,
                "Die Einheit wurde inzwischen geändert.",
                reason="planning_revision_conflict",
            )
        return value

    def _apply(
        self,
        build: Callable[[], dict[str, Any]],
        plan_id: str | None,
        accept: list[str] | None,
        *,
        create: bool = False,
    ) -> dict[str, Any]:
        guard = self._plan_guard()
        with self._database_manager.unit_of_work():
            before = guard.findings()
            change = build()
            result = self._change_service().apply(
                {"changes": [change]},
                authorized_plan_id=plan_id if create else None,
                source="manual",
            )
            notes = guard.verify(before, accept or [], waived=_WAIVED)
        return {**result["changes"][0], "plan_check_notes": notes}
