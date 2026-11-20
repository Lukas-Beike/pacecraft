"""Dispatch read-only Coach tools to their owning domain services."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any, Final

from backend.athlete.profile import ProfileService, coach_profile
from backend.coach.activity_read_tools import CoachActivityReadToolService
from backend.coach.context import (
    bounded_coach_context_sections_by_priority,
    coach_context_json_size,
)
from backend.coach.context_selection import (
    CONTEXT_SECTIONS,
    CoachContextSelection,
)
from backend.errors import AppError
from backend.history.service import ChangeHistoryService
from backend.nutrition.catalog import INCOMPLETE_PRODUCTS_NOTICE
from backend.nutrition.diary import NutritionDiaryService
from backend.nutrition.food_database import names_the_food
from backend.nutrition.meal_library import NutritionMealLibraryService
from backend.nutrition.models import validate_iso_date
from backend.planning.competition_service import CompetitionService
from backend.planning.library import workout_library_type
from backend.planning.library_service import WorkoutLibraryService
from backend.planning.planned_unit_service import PlannedUnitService
from backend.planning.state_service import StructuredTrainingStateService
from backend.planning.training_plans import TrainingPlanService

# All ingredients of one meal fit one lookup, matching nutrition_components.
MAX_FOOD_LOOKUP_QUERIES = 20


FOOD_DATABASE_UNAVAILABLE = "Lebensmitteldatenbank ist nicht verfügbar."

# lookup_food is local first (#3080): a saved product named like the searched
# food answers, and a provider is asked on a miss or when the athlete's own
# request names it. The tool execution sets NAMED_FOOD_SOURCES from that
# request; a model argument of that name is replaced.
NAMED_FOOD_SOURCES: Final = "_named_food_sources"
LOCAL_HIT: Final = "local_hit"
EXTERNAL_AFTER_MISS: Final = "external_after_miss"
EXTERNAL_REQUESTED: Final = "external_requested"
# The diagnostics count these per step, in this order.
LOOKUP_OUTCOMES: Final = (LOCAL_HIT, EXTERNAL_AFTER_MISS, EXTERNAL_REQUESTED)
SAVED_PRODUCT_NOTICE: Final = (
    "Gespeicherte Produkte mit diesem Namen gefunden; nimm das, das dasselbe "
    "Lebensmittel ist, sonst frag nach. Externe Quellen werden nur gefragt, "
    "wenn der Athlet sie selbst nennt."
)
_FOOD_SOURCE_NAMES: Final = (
    ("yazio", re.compile(r"yazio", re.IGNORECASE)),
    ("open_food_facts", re.compile(r"open\s*food\s*facts|\boff\b", re.IGNORECASE)),
    ("bls", re.compile(r"\bbls\b|bundeslebensmittelschl", re.IGNORECASE)),
)


def _library_filter(sport: Any, query: Any) -> Callable[[Mapping[str, Any]], bool]:
    """Templates of the sport type whose name or description holds every query
    word, also inside a longer word ("tempo" finds "Tempolauf"); a query
    without a word matches nothing."""
    wanted = workout_library_type({"sport": sport}) if sport else None
    words = re.findall(r"\w+", str(query or "").casefold())

    def matches(template: Mapping[str, Any]) -> bool:
        if wanted and workout_library_type(dict(template)) != wanted:
            return False
        if query is not None and not words:
            return False
        text = f"{template.get('name') or ''} {template.get('description') or ''}"
        return all(word in text.casefold() for word in words)

    return matches


def athlete_request_text(context: Mapping[str, Any]) -> str:
    """The athlete's own request of the turn: the current message and, while a
    clarification is open, the messages it answers (#3080)."""
    pending = context.get("pending_request")
    asked = pending.get("source_message_ids") if isinstance(pending, Mapping) else None
    ids = {context.get("current_user_message_id"), *(asked or ())}
    return "\n".join(
        str(item.get("content") or "")
        for item in context.get("messages") or ()
        if isinstance(item, Mapping)
        and item.get("role") == "user"
        and item.get("id") in ids
    )


def named_food_sources(text: str) -> list[str]:
    """The food sources the athlete's request names."""
    return [source for source, words in _FOOD_SOURCE_NAMES if words.search(text)]


class CoachReadToolService:
    """Own read-tool selection, bounds, and response projection."""

    def __init__(
        self,
        profile_service: Callable[[], ProfileService],
        structured_training_state_service: Callable[[], StructuredTrainingStateService],
        activity_read_tool_service: Callable[[], CoachActivityReadToolService],
        workout_library_service: Callable[[], WorkoutLibraryService],
        planned_unit_service: Callable[[], PlannedUnitService],
        change_history_service: Callable[[], ChangeHistoryService],
        competition_service: Callable[[], CompetitionService],
        training_plan_service: Callable[[], TrainingPlanService],
        training_change_limit: int,
        nutrition_diary: Callable[[], NutritionDiaryService] | None = None,
        nutrition_meal_library: Callable[[], NutritionMealLibraryService] | None = None,
        context_service: Callable[[], Any] | None = None,
        energy_view: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self._energy_view = energy_view
        self._profile_service = profile_service
        self._structured_training_state_service = structured_training_state_service
        self._activity_read_tool_service = activity_read_tool_service
        self._workout_library_service = workout_library_service
        self._planned_unit_service = planned_unit_service
        self._change_history_service = change_history_service
        self._competition_service = competition_service
        self._training_plan_service = training_plan_service
        self._training_change_limit = training_change_limit
        self._nutrition_diary = nutrition_diary
        self._nutrition_meal_library = nutrition_meal_library
        self._context_service = context_service

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if name == "read_coach_context":
            return self._read_coach_context(arguments)
        return self._execute_domain_read(name, arguments)

    def _read_coach_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        sections = arguments.get("sections")
        if (
            not isinstance(sections, list)
            or not 1 <= len(sections) <= 15
            or any(
                not isinstance(section, str) or section not in CONTEXT_SECTIONS
                for section in sections
            )
        ):
            raise AppError(
                400,
                "Ungültige Coach-Kontextabschnitte.",
                reason="invalid_context_sections",
            )
        if self._context_service is None:
            raise AppError(503, "Coach-Kontext ist nicht verfügbar.")
        requested = list(dict.fromkeys(sections))
        context = self._context_service().build(
            selection=CoachContextSelection(
                "requested_details",
                frozenset(requested),
                include_library=False,
            )
        )
        bounded = bounded_coach_context_sections_by_priority(context, 40_000)
        return {
            "ok": True,
            "context": bounded,
            "projection": {
                "requested_sections": requested,
                "characters": coach_context_json_size(bounded),
                "complete": coach_context_json_size(context) <= 40_000,
                "omitted_sections": sorted(
                    section for section in requested if section not in bounded
                ),
            },
        }

    def _execute_domain_read(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        if name == "read_profile":
            return {"ok": True, "profile": coach_profile(self._profile_service().get())}
        if name == "read_training_state":
            return {
                "ok": True,
                **self._structured_training_state_service().read(
                    include_inactive=bool(arguments.get("include_inactive")),
                    cursor=arguments.get("cursor"),
                    limit=arguments.get("limit"),
                ),
            }
        if name in {
            "list_recent_activities",
            "get_activity_details",
            "get_training_report",
            "read_training_records",
        }:
            return self._activity_read_tool_service().execute(name, arguments)
        if name == "list_workout_library":
            limit = self._bounded_integer(
                arguments, "limit", 100, 100, "Bibliothekslimit ist ungültig."
            )
            sport, query = arguments.get("sport"), arguments.get("query")
            # A filter reads up to 1000 templates, so one beyond the first page
            # is found before a duplicate is created (#3080).
            templates = self._workout_library_service().list(
                1000 if sport or query is not None else limit,
                include_archived=bool(arguments.get("include_archived")),
            )
            return {
                "ok": True,
                "templates": list(filter(_library_filter(sport, query), templates))[
                    :limit
                ],
            }
        if name == "list_planned_workouts":
            limit = self._bounded_integer(
                arguments,
                "limit",
                100,
                self._training_change_limit,
                "Planungslimit ist ungültig.",
            )
            return {"ok": True, **self._planned_unit_service().list_for_coach(limit)}
        if name == "list_change_history":
            limit = self._bounded_integer(
                arguments, "limit", 100, 500, "Historienlimit ist ungültig."
            )
            return {"ok": True, "changes": self._change_history_service().list(limit)}
        if name == "list_competitions":
            return {"ok": True, "competitions": self._competition_service().list()}
        if name == "list_training_plans":
            return {
                "ok": True,
                "training_plans": self._training_plan_service().list(100),
            }
        if name == "read_nutrition":
            return self._read_nutrition(arguments)
        if name in {"lookup_food", "calculate_food_nutrition"}:
            return self._food_database_read(name, arguments)
        if name == "list_incomplete_nutrition_products":
            return self._list_incomplete_products()
        if name == "search_web_for_product_values":
            return self._search_web_values(arguments)
        return None

    def _search_web_values(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._nutrition_meal_library is None:
            raise AppError(503, FOOD_DATABASE_UNAVAILABLE)
        catalog = self._nutrition_meal_library().product_catalog
        return catalog.search_web_values(arguments.get("products"))

    def _list_incomplete_products(self) -> dict[str, Any]:
        if self._nutrition_meal_library is None:
            raise AppError(503, FOOD_DATABASE_UNAVAILABLE)
        return {
            "ok": True,
            "products": self._nutrition_meal_library().product_catalog.list_incomplete_products(),
            "notice": INCOMPLETE_PRODUCTS_NOTICE,
        }

    def _food_database_read(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if self._nutrition_meal_library is None:
            raise AppError(503, FOOD_DATABASE_UNAVAILABLE)
        nutrition = self._nutrition_meal_library()
        if name == "lookup_food":
            return self._lookup_food(nutrition, arguments)
        return self._calculate_food(nutrition, arguments)

    @classmethod
    def _lookup_food(
        cls, nutrition: NutritionMealLibraryService, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        # Set by the tool execution from the athlete's own message, never the model.
        named = frozenset(arguments.get(NAMED_FOOD_SOURCES) or ())
        arguments = {k: v for k, v in arguments.items() if k != NAMED_FOOD_SOURCES}
        queries = arguments.get("queries")
        if queries is None:
            return cls._lookup_one_food(nutrition, arguments, named)
        if arguments.get("barcode") or not isinstance(queries, list):
            raise AppError(
                400, "queries ist eine Liste von Suchbegriffen ohne barcode."
            )
        unique: dict[str, str] = {}
        for term in [arguments.get("query"), *queries]:
            if isinstance(term, str) and term.strip():
                unique.setdefault(term.strip().casefold(), term.strip())
        if not 1 <= len(unique) <= MAX_FOOD_LOOKUP_QUERIES:
            raise AppError(
                400,
                f"queries muss 1 bis {MAX_FOOD_LOOKUP_QUERIES} Suchbegriffe enthalten.",
            )
        source = arguments.get("source")
        results: list[dict[str, Any]] = []
        errors: list[AppError] = []
        # One call covers a whole meal; a failed term keeps the others usable.
        for term in unique.values():
            try:
                result = cls._lookup_one_food(
                    nutrition,
                    {"query": term, **({"source": source} if source else {})},
                    named,
                )
            except AppError as exc:
                errors.append(exc)
                result = {"ok": False, "error": exc.message, "reason": exc.reason}
            results.append({"query": term, **result})
        if len(errors) == len(results):
            raise errors[0]
        return {"ok": True, "results": results}

    @classmethod
    def _lookup_one_food(
        cls,
        nutrition: NutritionMealLibraryService,
        arguments: dict[str, Any],
        named: frozenset[str],
    ) -> dict[str, Any]:
        query = str(arguments.get("query") or arguments.get("q") or "").strip()
        # A source the athlete named stands when the model omits one.
        source = (
            str(arguments.get("source") or next(iter(sorted(named)), ""))
            .strip()
            .lower()
        )
        if (
            query
            and not arguments.get("barcode")
            and source in {"bls", "open_food_facts", "yazio"}
        ):
            return cls._lookup_food_source(
                nutrition, arguments, query, source, requested=source in named
            )
        if not (arguments.get("barcode") or arguments.get("query")):
            return nutrition.food_database.lookup(arguments)
        result = nutrition.product_catalog.lookup_product(arguments)
        local = result.get("local") is True
        return {**result, "lookup_outcome": LOCAL_HIT if local else EXTERNAL_AFTER_MISS}

    @staticmethod
    def _lookup_food_source(
        nutrition: NutritionMealLibraryService,
        arguments: dict[str, Any],
        query: str,
        source: str,
        *,
        requested: bool,
    ) -> dict[str, Any]:
        """One provider's lookup, unless a saved product fits and the athlete's
        own message does not name that provider (#3080)."""
        local_products = nutrition.product_catalog.list_products(query=query)
        fitting = [
            product
            for product in local_products
            if names_the_food(
                query, str(product.get("name") or ""), str(product.get("brand") or "")
            )
        ]
        if fitting and not requested:
            return {
                "ok": True,
                "foods": [],
                "local_products": fitting,
                "lookup_outcome": LOCAL_HIT,
                "notice": SAVED_PRODUCT_NOTICE,
            }
        try:
            result = nutrition.food_database.lookup(
                {**arguments, "query": query, "source": source}
            )
        except AppError as exc:
            # Saved matches stay usable whenever the external lookup fails.
            if not local_products:
                raise
            result = {
                "ok": True,
                "foods": [],
                "notice": f"Externe Suche nicht möglich: {exc.message} Nur gespeicherte Produkte verfügbar.",
            }
        result = {
            **result,
            "lookup_outcome": EXTERNAL_REQUESTED if requested else EXTERNAL_AFTER_MISS,
        }
        return (
            {**result, "local_products": local_products} if local_products else result
        )

    @staticmethod
    def _calculate_food(
        nutrition: NutritionMealLibraryService, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if "components" in arguments and (
            "ingredients" in arguments or "product_id" in arguments
        ):
            raise AppError(
                400,
                "Komponenten können nicht mit Legacy-Lebensmittelreferenzen kombiniert werden.",
            )
        if arguments.get("product_id"):
            return nutrition.product_catalog.calculate_product(
                str(arguments.get("product_id")),
                arguments.get("amount"),
                str(arguments.get("unit") or ""),
            )
        if "components" in arguments:
            return {
                "ok": True,
                **nutrition.product_catalog.calculate_components(
                    arguments["components"]
                ),
            }
        return {
            "ok": True,
            **nutrition.food_database.calculate(arguments.get("ingredients")),
        }

    def _read_nutrition(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("energy") is True and self._energy_view is not None:
            # The view of the asked day, as the diary reads it (#3755); without
            # a date, yesterday.
            day = arguments.get("date")
            return {"ok": True, "energy": self._energy_view(str(day) if day else None)}
        if not self._nutrition_diary or not self._nutrition_meal_library:
            return {"ok": False, "error": "NutritionService ist nicht verfügbar."}
        service = self._nutrition_diary()
        meal_library = self._nutrition_meal_library()
        templates = meal_library.list_templates()
        products = meal_library.product_catalog.list_products()
        product_projection = (
            {"products": products} if isinstance(products, list) else {}
        )
        if arguments.get("date"):
            day = service.with_sessions(
                service.sync_status.get_day_summary(str(arguments["date"]))
            )
            # The app checks, the model explains: each session's intake report
            # (#3234), facts with the one notable flag.
            day["session_intake"] = [
                service.session_intake(s["id"], str(s["started_at_local"])[:10])
                for s in day["sessions"]
            ]
            return {"ok": True, "templates": templates, **product_projection, **day}
        if arguments.get("start") and arguments.get("end"):
            start = validate_iso_date(arguments["start"])
            end = validate_iso_date(arguments["end"])
            if (date.fromisoformat(end) - date.fromisoformat(start)).days > 30:
                raise AppError(
                    400,
                    "Der Ernährungszeitraum darf höchstens 31 Tage umfassen.",
                    reason="range_too_large",
                )
            return {
                "ok": True,
                "templates": templates,
                **product_projection,
                "summaries": service.get_range_summary(start, end),
            }
        return {
            "ok": True,
            "templates": templates,
            **product_projection,
            **service.get_today_summary(),
        }

    @staticmethod
    def _bounded_integer(
        arguments: dict[str, Any], key: str, default: int, maximum: int, error: str
    ) -> int:
        try:
            return max(1, min(int(arguments.get(key, default)), maximum))
        except (TypeError, ValueError) as exc:
            raise AppError(400, error, reason="invalid_list_request") from exc
