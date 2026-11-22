"""Authorize and execute local Coach athlete-record tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.activities.feedback import ActivityFeedbackService
from backend.athlete.body_entry import BodyEntryService
from backend.athlete.checkins import CheckinService
from backend.canonical.first_threshold import FIRST_THRESHOLD_FIELD
from backend.coach.authorization import (
    require_coach_scope,
    require_operation,
    structured_action_payload,
)
from backend.errors import AppError
from backend.nutrition.catalog import NOTHING_FILLED_NOTICE
from backend.nutrition.diary import NutritionDiaryService
from backend.nutrition.meal_library import NutritionMealLibraryService
from backend.planning.competition_service import CompetitionService

_EQUIPMENT_TOOLS = frozenset(
    {
        "save_equipment",
        "assign_activity_equipment",
        "log_equipment_maintenance",
        "set_equipment_rule",
    }
)
_SESSION_TOOLS = frozenset(
    {
        "confirm_activity_session",
        "split_activity_session",
        "merge_activity_sessions",
        "delete_activity_session",
        "rename_activity_route",
    }
)
_NUTRITION_UNAVAILABLE = "NutritionService ist nicht verfügbar."
# Körper's weight and body fat, recorded as the dialogs record them (#3640).
_BODY_TOOLS = frozenset({"save_body_entry", "delete_body_entry"})
# Körper's 3-site skinfolds, with the dialog's Garmin write (#3959).
_SKINFOLD_TOOLS = frozenset({"save_skinfolds", "delete_skinfolds"})
_BODY_KEYS = {"weight": "kg", "body_fat": "pct"}
# Zonen & Schwellen, through the use cases of its routes (#3956).
_ZONE_TOOLS = frozenset({"save_zones", "delete_zones"})
_ZONE_FIELDS = (
    "sport",
    "metric",
    "environment",
    "limits",
    "percent_limits",
    "threshold",
    "maximum",
    "valid_from",
    "from_beginning",
)
_LT2_FIELDS = ("sport", "source", "value", "shown", "confirm_deviation", "activity_id")
# Produkte's card and list actions, through the routes' use cases (#3958).
_PRODUCT_TOOLS = frozenset({"manage_nutrition_products", "refresh_nutrition_products"})
_NUTRITION_TOOLS = frozenset(
    {
        "save_nutrition_template",
        "save_nutrition_product",
        "fill_nutrition_product_gaps",
        "delete_nutrition_template",
        "log_nutrition_template",
        "save_nutrition_entry",
        "update_nutrition_entry",
        "delete_nutrition_entry",
    }
)


class CoachAthleteRecordToolService:
    """Own local Coach mutations for check-ins, feedback, competitions, and nutrition."""

    def __init__(
        self,
        checkins: CheckinService,
        activity_feedback: ActivityFeedbackService,
        competitions: CompetitionService,
        nutrition_diary: NutritionDiaryService | None = None,
        nutrition_meal_library: NutritionMealLibraryService | None = None,
        *,
        equipment: Any = None,
        session_corrections: Any = None,
        body_entries: Callable[[str], BodyEntryService] | None = None,
        athlete: Any = None,
        second_threshold: Callable[[], Any] | None = None,
        step_test: Callable[[str], Any] | None = None,
        activity_routes: Callable[[], Any] | None = None,
        nutrition_services: Any = None,
    ) -> None:
        # Zonen & Schwellen's services (from the athlete services) and the
        # step-test read of one session.
        self._zones = None if athlete is None else athlete.zones
        self._first_threshold = None if athlete is None else athlete.first_threshold
        self._second_threshold = second_threshold
        self._step_test = step_test
        # The recognised routes, renamed as the session details do (#3957).
        self._activity_routes = activity_routes
        # Produkte's clean-up and its background re-check of every product.
        self._product_cleanup = (
            None if nutrition_services is None else nutrition_services.product_cleanup
        )
        self._product_refresh = (
            None if nutrition_services is None else nutrition_services.product_refresh
        )
        # The Körper entry service of "weight" or "body_fat" (#3640).
        self._body_entries = body_entries
        self._checkins = checkins
        self._activity_feedback = activity_feedback
        self._competitions = competitions
        self._nutrition_diary = nutrition_diary
        self._nutrition_meal_library = nutrition_meal_library
        self._equipment = equipment
        self._session_corrections = session_corrections

    def execute(
        self, name: str, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any] | None:
        # Each group of tools has its own handler.
        handler = next(
            (
                handler
                for tools, handler in (
                    (_EQUIPMENT_TOOLS, self._execute_equipment),
                    (_SESSION_TOOLS, self._execute_session),
                    (_BODY_TOOLS, self._execute_body),
                    (_SKINFOLD_TOOLS, self._execute_skinfolds),
                    (_ZONE_TOOLS, self._execute_zones),
                    (_PRODUCT_TOOLS, self._execute_products),
                    (_NUTRITION_TOOLS, self._execute_nutrition),
                )
                if name in tools
            ),
            None,
        )
        if handler is not None:
            return handler(name, arguments, intent)
        if name == "save_checkin":
            self._authorize(
                intent,
                name,
                "Die strukturierte Coach-Autorisierung erlaubt diesen Check-in nicht.",
            )
            require_coach_scope(intent, "local_checkin")
            return {
                "ok": True,
                **self._checkins.save_coach(structured_action_payload(arguments)),
            }
        if name == "save_activity_feedback":
            self._authorize(
                intent,
                name,
                "Die strukturierte Coach-Autorisierung erlaubt dieses Aktivitätsfeedback nicht.",
            )
            require_coach_scope(intent, "activity_feedback")
            payload = structured_action_payload(arguments)
            return {
                "ok": True,
                "stored_locally": True,
                **self._activity_feedback.save_coach(
                    payload.get("activity_id"),
                    {
                        key: payload.get(key)
                        for key in (
                            "activity_name",
                            "activity_date",
                            "notes",
                            "session_rpe",
                            "deviation_reason",
                            "purpose",
                        )
                        if key in payload
                    },
                ),
            }
        if name == "delete_activity_feedback":
            self._authorize(
                intent,
                name,
                "Die strukturierte Coach-Autorisierung erlaubt diese Feedbackänderung nicht.",
            )
            require_coach_scope(intent, "activity_feedback")
            activity_id = str(arguments.get("activity_id") or "").strip()
            return {
                "ok": True,
                "stored_locally": True,
                **self._activity_feedback.save(
                    activity_id,
                    {"notes": "", "session_rpe": None, "deviation_reason": ""},
                ),
            }
        if name == "save_competition":
            self._authorize(
                intent,
                name,
                "Die strukturierte Coach-Autorisierung erlaubt diese Aktion in diesem Turn nicht.",
            )
            payload = structured_action_payload(arguments)
            competition_id = str(payload.get("competition_id") or "").strip()
            require_coach_scope(
                intent,
                f"competition:{competition_id}"
                if competition_id
                else "local_competitions",
            )
            return {"ok": True, **self._competitions.save(payload)}
        if name == "delete_competition":
            self._authorize(
                intent,
                name,
                "Die strukturierte Coach-Autorisierung erlaubt diese Aktion in diesem Turn nicht.",
            )
            competition_id = str(arguments.get("competition_id") or "").strip()
            require_coach_scope(intent, f"competition:{competition_id}")
            return {"ok": True, **self._competitions.delete(competition_id)}
        return None

    def _execute_equipment(
        self, name: str, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> Any:
        """Save, assign or maintain equipment for the Coach."""
        self._authorize(intent, name, "Diese lokale Erfassung ist nicht autorisiert.")
        require_coach_scope(intent, "local_equipment")
        if self._equipment is None:
            raise AppError(503, "Ausrüstung ist nicht verfügbar.")
        payload = structured_action_payload(arguments)
        if name == "set_equipment_rule":
            return self._equipment_rule(payload)
        method = {
            "save_equipment": self._equipment.save,
            "assign_activity_equipment": self._equipment.assign,
            "log_equipment_maintenance": self._equipment.maintain,
        }[name]
        return method(payload)

    def _equipment_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set a kind's default item, count or apply it as Ausrüstung does (#3957).

        ``preview`` changes nothing; ``apply`` is the confirmed bulk change of
        earlier sessions, which the write gate counts as bulk.
        """
        action, kind = payload.get("action"), payload.get("kind")
        if action == "set":
            if "equipment_id" not in payload:
                # null clears the default; a missing item must not (#3957).
                raise AppError(
                    400,
                    "Die Regel braucht equipment_id (null für keine).",
                    reason="tool_arguments_invalid",
                )
            return self._equipment.save_rule(
                {
                    "kind": kind,
                    "equipment_id": payload.get("equipment_id"),
                    "expected_revision": payload.get("expected_revision"),
                }
            )
        if action == "preview":
            preview = self._equipment.rule_apply_preview(kind)
            return {"ok": True, "status": "unchanged", **preview}
        if action == "apply":
            applied = self._equipment.apply_rule({"kind": kind, "confirmed": True})
            return applied | (
                {"status": "unchanged"} if applied["change"] is None else {}
            )
        raise AppError(400, "Unbekannte Regelaktion.", reason="tool_arguments_invalid")

    def _execute_session(
        self, name: str, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        """Confirm, split, merge or delete a session, or rename its route."""
        self._authorize(intent, name, "Diese lokale Korrektur ist nicht autorisiert.")
        require_coach_scope(intent, "local_activity_session")
        payload = structured_action_payload(arguments)
        if name == "rename_activity_route":
            if self._activity_routes is None:
                raise AppError(503, "Strecken sind nicht verfügbar.")
            renamed = self._activity_routes().rename(
                payload.get("route_id"), payload.get("name")
            )
            return {"ok": True, "stored_locally": True, **renamed}
        if self._session_corrections is None:
            raise AppError(503, "Aktivitätskorrekturen sind nicht verfügbar.")
        service = self._session_corrections
        activity_id = str(payload.get("activity_id") or "")
        if name == "delete_activity_session":
            # Local only, as the session details' delete (#3957).
            result = service.delete(activity_id, source="coach")
        elif name == "confirm_activity_session":
            result = service.confirm(activity_id, source="coach")
        elif name == "split_activity_session":
            result = service.split(
                activity_id, str(payload.get("member_id") or ""), source="coach"
            )
        else:
            result = service.merge(
                activity_id, str(payload.get("other_id") or ""), source="coach"
            )
        return {"ok": True, "stored_locally": True, **result}

    def _execute_nutrition(
        self, name: str, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        self._authorize_nutrition_operation(name, intent)
        require_coach_scope(intent, self._nutrition_scope(name, arguments))
        if not self._nutrition_diary or not self._nutrition_meal_library:
            raise AppError(500, _NUTRITION_UNAVAILABLE)
        if name == "save_nutrition_template":
            return self._save_nutrition_template(arguments, intent)
        if name == "save_nutrition_product":
            return self._save_nutrition_product(arguments, intent)
        if name == "fill_nutrition_product_gaps":
            return self._nutrition_meal_library.product_catalog.fill_product_gaps(
                arguments.get("products")
            )
        if name == "delete_nutrition_template":
            return {
                "ok": True,
                **self._nutrition_meal_library.delete_template(
                    str(arguments.get("id") or "")
                ),
            }
        if name == "log_nutrition_template":
            return self._with_session_info(
                self._nutrition_diary.log_template(
                    str(arguments.get("id") or ""),
                    arguments.get("portions", 1),
                    meal_date=arguments.get("meal_date"),
                    meal_time=arguments.get("meal_time"),
                    meal_type=arguments.get("meal_type"),
                    volume_ml=arguments.get("volume_ml"),
                    session_id=arguments.get("session_id"),
                )
            )
        if name in {"save_nutrition_entry", "update_nutrition_entry"}:
            return self._save_or_update_nutrition_entry(name, arguments)
        entry_id = str(arguments.get("id") or arguments.get("entry_id") or "").strip()
        return {"ok": True, **self._nutrition_diary.delete_meal(entry_id)}

    @staticmethod
    def _nutrition_scope(name: str, arguments: dict[str, Any]) -> str:
        if name == "save_nutrition_template":
            payload = arguments.get("payload")
            template_id = (
                str(payload.get("id") or "").strip()
                if isinstance(payload, dict)
                else ""
            )
            return (
                f"nutrition_template:{template_id}"
                if template_id
                else "local_nutrition"
            )
        if name == "fill_nutrition_product_gaps":
            return "local_nutrition_gaps"
        if name != "save_nutrition_product":
            return "local_nutrition"
        product_id = str(structured_action_payload(arguments).get("id") or "").strip()
        return (
            f"nutrition_product:{product_id}"
            if product_id
            else "local_nutrition_product"
        )

    def _authorize_nutrition_operation(self, name: str, intent: dict[str, Any]) -> None:
        message = (
            "Die strukturierte Coach-Autorisierung erlaubt diesen Ern\u00e4hrungseintrag nicht."
            if name in {"save_nutrition_entry", "update_nutrition_entry"}
            else "Die strukturierte Coach-Autorisierung erlaubt das L\u00f6schen dieses Eintrags nicht."
        )
        self._authorize(intent, name, message)

    def _save_nutrition_product(
        self, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        nutrition = self._nutrition_meal_library_service()
        payload = structured_action_payload(arguments)
        product_id = str(payload.get("id") or "").strip()
        if product_id:
            require_coach_scope(intent, f"nutrition_product:{product_id}")
        product = (
            nutrition.product_catalog.update_product(product_id, payload)
            if product_id
            else nutrition.product_catalog.save_product(payload)
        )
        return {"ok": True, "product": product}

    def _save_or_update_nutrition_entry(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        nutrition = self._nutrition_diary_service()
        if name == "save_nutrition_entry":
            return self._with_session_info(
                nutrition.log_meal(structured_action_payload(arguments))
            )
        return self._with_session_info(
            nutrition.correct_meal(
                str(arguments.get("id") or ""), arguments.get("changes")
            )
        )

    def _with_session_info(self, entry: dict[str, Any]) -> dict[str, Any]:
        """The tool result: the entry, and for a session slot where it belongs now."""
        result: dict[str, Any] = {"ok": True, "entry": entry}
        info = self._nutrition_diary_service().session_info(entry)
        if info is not None:
            result["session"] = info
        return result

    def _nutrition_diary_service(self) -> NutritionDiaryService:
        if self._nutrition_diary is None:
            raise AppError(500, _NUTRITION_UNAVAILABLE)
        return self._nutrition_diary

    def _nutrition_meal_library_service(self) -> NutritionMealLibraryService:
        if self._nutrition_meal_library is None:
            raise AppError(500, _NUTRITION_UNAVAILABLE)
        return self._nutrition_meal_library

    def _execute_skinfolds(
        self, name: str, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        """Save or remove one day's 3-site skinfold measurement in Körper (#3959).

        As the dialog does, the day's Garmin body composition follows once the
        turn commits; an existing day changes only with ``replace``.
        """
        self._authorize(
            intent,
            name,
            "Die strukturierte Coach-Autorisierung erlaubt diese Körperwert-Änderung nicht.",
        )
        require_coach_scope(intent, "local_body")
        if self._body_entries is None:
            raise AppError(500, "Körperwerte sind nicht verfügbar.")
        service: Any = self._body_entries("skinfolds")
        day = str(arguments.get("date") or "")
        if name == "delete_skinfolds":
            read = service.remove({"date": day, "confirmed": True})
        else:
            if arguments.get("replace") is not True and any(
                item["date"] == day for item in service.read()["measurements"]
            ):
                raise AppError(
                    409,
                    "Für diesen Tag gibt es schon eine Messung; sie zu ändern braucht "
                    "die ausdrückliche Bitte des Athleten.",
                    reason="body_entry_exists",
                )
            read = service.save({"date": day, "folds_mm": arguments.get("folds_mm")})
        measurement = next(
            (item for item in read["measurements"] if item["date"] == day), None
        )
        return {"ok": True, "stored_locally": True, "date": day,
                "measurement": measurement, "remote": read.get("remote")}  # fmt: skip

    def _execute_body(
        self, name: str, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        """Save or remove one day's weight or body fat in Körper (#3640).

        An existing day's value changes only with ``replace``, which the write
        gate treats as a removal: it needs the athlete's confirmed request.
        """
        self._authorize(
            intent,
            name,
            "Die strukturierte Coach-Autorisierung erlaubt diese Körperwert-Änderung nicht.",
        )
        require_coach_scope(intent, "local_body")
        kind = arguments.get("kind")
        if kind not in _BODY_KEYS:
            raise AppError(
                400,
                "Unbekannter Körperwert; erlaubt: weight oder body_fat.",
                reason="tool_arguments_invalid",
            )
        if self._body_entries is None:
            raise AppError(500, "Körperwerte sind nicht verfügbar.")
        service = self._body_entries(kind)
        day = str(arguments.get("date") or "")
        if name == "delete_body_entry":
            read = service.remove({"date": day, "confirmed": True})
            return {"ok": True, "stored_locally": True, "kind": kind, "date": day,
                    "remote": read.get("remote")}  # fmt: skip
        if arguments.get("replace") is not True and any(
            entry["date"] == day for entry in service.read()["entries"]
        ):
            raise AppError(
                409,
                "Für diesen Tag gibt es schon einen Wert; ihn zu ändern braucht "
                "die ausdrückliche Bitte des Athleten.",
                reason="body_entry_exists",
            )
        read = service.save({"date": day, _BODY_KEYS[kind]: arguments.get("value")})
        value = next(e[_BODY_KEYS[kind]] for e in read["entries"] if e["date"] == day)
        return {"ok": True, "stored_locally": True, "kind": kind, "date": day,
                "value": value, "remote": read.get("remote")}  # fmt: skip

    def _execute_zones(
        self, name: str, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        """Save, remove or adopt zones and thresholds as Zonen & Schwellen does.

        ``replace`` stands for the UI's confirmation to replace a measured or
        entered value; the write gate treats it, like a removal, as a delete.
        """
        self._authorize(
            intent,
            name,
            "Die strukturierte Coach-Autorisierung erlaubt diese Zonenänderung nicht.",
        )
        require_coach_scope(intent, "local_zones")
        zones, first, second = (
            self._zones,
            self._first_threshold,
            self._second_threshold,
        )
        if zones is None or first is None or second is None:
            raise AppError(503, "Die Zonen sind nicht verfügbar.")
        # Only delete_zones removes: the write gate counts it as a delete.
        action = "remove" if name == "delete_zones" else arguments.get("action")
        replace = arguments.get("replace") is True
        if name == "delete_zones":
            result = zones().remove({"key": arguments.get("key"), "confirmed": True})
        elif action == "save":
            result = zones().save(
                {key: arguments[key] for key in _ZONE_FIELDS if key in arguments}
            )
        elif action == "adopt_lt2":
            result = second().adopt(
                {
                    **{key: arguments.get(key) for key in _LT2_FIELDS},
                    "replace_local": replace,
                },
                self._step_test,
            )
        elif action == "adopt_first_threshold":
            result = self._adopt_first_threshold(first(), arguments, replace)
        else:
            raise AppError(
                400,
                "Unbekannte Zonenaktion.",
                reason="tool_arguments_invalid",
            )
        if "change" in result and result["change"] is None:
            # The same configuration again: nothing recorded, no receipt.
            result = {**result, "status": "unchanged"}
        return {"ok": True, "stored_locally": True, "action": action, **result}

    def _adopt_first_threshold(
        self, service: Any, arguments: dict[str, Any], replace: bool
    ) -> dict[str, Any]:
        """The estimate's percent, or a step test's LT1 bpm (``activity_id``)."""
        activity_id = str(arguments.get("activity_id") or "")
        tested = (
            self._step_test(activity_id) if activity_id and self._step_test else None
        )
        shown = (
            {"step_test": activity_id, "heart_rate_bpm": arguments.get("shown")}
            if activity_id
            else {"percent_lthr": arguments.get("shown")}
        )
        result = service.adopt({**shown, "replace_measured": replace}, tested)
        return {
            # An adoption already stored writes nothing: no receipt (#3956).
            **({} if result["adopted"] else {"status": "unchanged"}),
            "first_threshold_bpm": result["profile"].get(FIRST_THRESHOLD_FIELD),
            "first_threshold_estimate": result["first_threshold_estimate"],
        }

    def _execute_products(
        self, name: str, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        """Favourite, archive, forget, clean up or re-check products (#3958).

        Archive, forget and clean-up are deletes for the write gate, the
        clean-up also bulk; its preview and an empty run change nothing.
        """
        self._authorize(
            intent,
            name,
            "Die strukturierte Coach-Autorisierung erlaubt diese Produktänderung nicht.",
        )
        require_coach_scope(intent, "local_nutrition")
        library = self._nutrition_meal_library_service()
        payload = structured_action_payload(arguments)
        product_id = str(payload.get("id") or "")
        if name == "refresh_nutrition_products":
            return self._refresh_products(library, payload, product_id)
        action, catalog = payload.get("action"), library.product_catalog
        if action == "favorite":
            product = catalog.set_favorite(product_id, payload.get("favorite"))
            return {"ok": True, "product": product}
        if action == "archive":
            return {"ok": True, "product": catalog.archive_product(product_id)}
        if action == "forget":
            return library.forget_database_food(product_id)
        if action in {"cleanup_preview", "cleanup"} and self._product_cleanup:
            cleanup = self._product_cleanup()
            if action == "cleanup_preview":
                preview = cleanup.preview(payload.get("days"))
                return {"ok": True, "status": "unchanged", **preview}
            run = cleanup.run({"days": payload.get("days"), "confirmed": True})
            return run | ({"status": "unchanged"} if run["change"] is None else {})
        raise AppError(
            400, "Unbekannte Produktaktion.", reason="tool_arguments_invalid"
        )

    def _refresh_products(
        self, library: Any, payload: dict[str, Any], product_id: str
    ) -> dict[str, Any]:
        """Re-check one product online, or start "Alle aktualisieren"."""
        if payload.get("all") is True:
            if self._product_refresh is None:
                raise AppError(503, "Die Produktaktualisierung ist nicht verfügbar.")
            # The background run, one at a time; starting it changes nothing
            # yet, so the turn shows no receipt.
            return {"ok": True, "status": "unchanged",
                    "run": self._product_refresh.start()}  # fmt: skip
        refreshed = library.refresh_database_food(product_id)
        if NOTHING_FILLED_NOTICE in str(refreshed.get("notice") or ""):
            # An own product with nothing left to fill shows no receipt.
            return {**refreshed, "status": "unchanged"}
        return refreshed

    @staticmethod
    def _authorize(intent: dict[str, Any], operation: str, message: str) -> None:
        if not require_operation(intent, operation):
            raise AppError(403, message, reason="intent_scope_denied")

    def _save_nutrition_template(
        self, arguments: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any]:
        meal_library = self._nutrition_meal_library_service()
        template_payload = structured_action_payload(arguments)
        template_id = str(template_payload.get("id") or "")
        if template_id:
            require_coach_scope(intent, f"nutrition_template:{template_id}")
        else:
            template_payload.setdefault("source", "coach")
        return {
            "ok": True,
            "template": meal_library.save_template(
                template_payload,
                **(
                    {"expected_calculation": arguments["_food_calculation"]}
                    if "_food_calculation" in arguments
                    else {}
                ),
            ),
        }
