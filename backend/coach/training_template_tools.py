"""Local Coach operations for reusable training templates."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.coach.authorization import authorized_operations, require_coach_scope
from backend.db.manager import DatabaseManager
from backend.errors import STRUCTURED_AUTHORIZATION_ERROR, AppError
from backend.planning.library import find_similar_library_workout


class TrainingTemplateToolService:
    """Execute authorized template changes as one local database transaction."""

    def __init__(
        self,
        database_manager: Callable[[], DatabaseManager],
        workout_library_service: Callable[[], Any],
    ) -> None:
        self._database_manager = database_manager
        self._workout_library_service = workout_library_service

    def execute(
        self, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        if "manage_training_templates" not in authorized_operations(intent):
            raise AppError(
                403, STRUCTURED_AUTHORIZATION_ERROR, reason="intent_scope_denied"
            )

        templates = arguments.get("templates")
        if (
            not isinstance(templates, list)
            or not 1 <= len(templates) <= 28
            or not all(isinstance(item, dict) for item in templates)
        ):
            raise AppError(
                400,
                "Ein Coach-Kommando darf 1 bis 28 Vorlagenänderungen enthalten.",
                reason="template_limit",
            )

        actions: list[tuple[str, str]] = []
        for template in templates:
            action = str(template.get("action") or "create").strip().casefold()
            if action in {"update", "archive", "restore", "delete"}:
                local_id = str(template.get("local_id") or "").strip()
                require_coach_scope(
                    intent, f"library_workout:{local_id}", "local_template"
                )
            elif action == "create":
                local_id = ""
                require_coach_scope(intent, "local_template")
            else:
                raise AppError(
                    400,
                    "Unbekannte Aktion für die Bibliothekseinheit.",
                    reason="invalid_template_action",
                )
            actions.append((action, local_id))

        despite = _existing_templates(templates, actions, self._workout_library_service)
        # Restore may replace the active manager. Resolve the library inside
        # the unit of work, so every nested library write joins it.
        with self._database_manager().unit_of_work():
            library = self._workout_library_service()
            results = []
            for template, (action, local_id) in zip(templates, actions, strict=True):
                if action == "create":
                    results.append(library.create_template(template))
                else:
                    results.append(library.update(local_id, template))

        return {
            "ok": True,
            "stored_locally": True,
            "templates": results,
            "template": results[0] if len(results) == 1 else None,
            # Counted by the diagnostics (#3080): new templates the Coach kept
            # beside an existing one, with its reason.
            "created_despite_candidate": despite,
        }


def _existing_templates(
    templates: list[dict[str, Any]],
    actions: list[tuple[str, str]],
    library: Callable[[], Any],
) -> int:
    """Refuse a new template an existing one already covers (#3080).

    The same conservative match as the plan creation's reuse: sport type,
    duration within max(10 min, 20 %) and a nearly equal description, or a
    nearly equal name with a similar description. A create with
    ``new_because`` (why it differs) is kept and counted. The library is read
    only for a create, before the unit of work.
    """
    creates = [
        template
        for template, (action, _local_id) in zip(templates, actions, strict=True)
        if action == "create"
    ]
    existing = library().list(1000) if creates else []
    despite = 0
    for template in creates:
        match = find_similar_library_workout(
            {"duration_minutes": 30, **template}, existing
        )
        if match is None:
            continue
        if str(template.get("new_because") or "").strip():
            despite += 1
            continue
        error = AppError(
            409,
            "Eine passende Vorlage existiert schon: nutze sie mit ihrer local_id "
            "oder nenne in new_because, worin die neue sich unterscheidet.",
            reason="template_exists",
        )
        # For the model only: the existing template to reuse.
        setattr(  # noqa: B010
            error,
            "findings",
            [
                {
                    "code": "template_exists",
                    "local_id": str(match.get("id")),
                    "name": str(match.get("name") or "")[:200],
                    "sport": match.get("sport") or match.get("type"),
                    "duration_minutes": match.get("duration_minutes"),
                }
            ],
        )
        raise error
    return despite
