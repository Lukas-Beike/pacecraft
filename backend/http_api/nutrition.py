"""Authenticated HTTP API routes for nutrition and calorie tracking."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from backend import change_history
from backend.errors import AppError
from backend.http_api.route_table import Access, Route, RouteRequest
from backend.nutrition.diary import NutritionDiaryService
from backend.nutrition.food_mirror import FOOD_ENTITY_TYPE
from backend.nutrition.macro_ranges import macro_ranges
from backend.nutrition.meal_library import NutritionMealLibraryService
from backend.nutrition.models import validate_iso_date
from backend.nutrition.photo import PHOTO_REQUEST_MAX_BYTES

NUTRITION_ENTRY_PATH = "/api/nutrition/entry"
NUTRITION_PRODUCTS_PATH = "/api/nutrition/products"
TEMPLATES_PATH = "/api/nutrition/templates"
REFRESH_ALL_PATH = f"{NUTRITION_PRODUCTS_PATH}/refresh-all"
CLEANUP_PATH = f"{NUTRITION_PRODUCTS_PATH}/cleanup"
PRODUCT_ENTITY = "nutrition_product"
INVALID_BODY = "Ungültiger Anfrageinhalt."
# The lengths a refresh takes per area: Verlauf's periods for `week` (#3704), the
# weight's for `body`; the first is the default.
REFRESH_PERIODS = {"week": (28, 91, 182, 365), "body": (84, 14)}
REFRESH_PERIOD_ERRORS = {
    "week": "Der Zeitraum muss 28, 91, 182 oder 365 Tage umfassen.",
    "body": "Der Zeitraum muss 14 oder 84 Tage umfassen.",
}


class NutritionGetRoutes:
    """Handle authenticated nutrition query routes."""

    def __init__(
        self,
        diary_service: Callable[[], NutritionDiaryService],
        meal_library_service: Callable[[], NutritionMealLibraryService],
        local_now: Callable[[], datetime],
        macro_context: Callable[[str], tuple[Any, Any]] | None = None,
        session_fuelling: Callable[[dict[str, list[str]]], dict[str, Any]]
        | None = None,
        energy_goal: Callable[[], str] | None = None,
    ) -> None:
        self._energy_goal = energy_goal
        self._session_fuelling = session_fuelling
        self._diary_service = diary_service
        self._meal_library_service = meal_library_service
        self._local_now = local_now
        self._macro_context = macro_context

    def _day_summary(self, date_param: str) -> dict[str, Any]:
        diary = self._diary_service()
        summary = diary.sync_status.get_day_summary(date_param)
        diary.with_sessions(summary)
        if self._macro_context is None:
            return summary
        try:
            weight, calendar = self._macro_context(date_param)
        except AppError, OSError, RuntimeError, ValueError:
            logging.getLogger("intervals_coach").warning(
                "Nutrition reference data unavailable"
            )
            weight, calendar = None, None
        summary["macro_ranges"] = macro_ranges(
            weight,
            calendar,
            date_param,
            summary.get("total_kcal"),
            self._energy_goal() if self._energy_goal else None,
        )
        return summary

    def _products(self, query: dict[str, list[str]]) -> dict[str, Any]:
        meal_library = self._meal_library_service()
        barcode = query.get("barcode", [None])[0]
        term = query.get("q", [None])[0]
        if query.get("source", [None])[0] == "database":
            return {"ok": True, "foods": meal_library.list_database_foods(term)}
        if query.get("scope", [None])[0] == "all":
            return {
                "ok": True,
                "products": meal_library.product_catalog.list_all_products(term),
            }
        if barcode:
            return meal_library.product_catalog.lookup_product({"barcode": barcode})
        return {
            "ok": True,
            "products": meal_library.product_catalog.list_products(
                query=term, favorites_first=True
            ),
        }

    def _components(self, handler: Any, request: RouteRequest) -> None:
        """The components of a logged meal, as the edit addresses them (#3948).

        Request: query `id`, the entry id.

        Response: 200 `{"ok": true, "id", "revision", "components"}`; `revision` is a
        fingerprint of the components that an edit must send back; each component has `index`
        (its position), `kind`, `name`, `amount`, `unit`, the nutrients (`kcal`,
        `carbs_g`, `protein_g`, `fat_g`, `sugar_g`, `fiber_g`, `salt_g`, unknown as
        null) and `nutrition_basis` (its origin). A meal without components is one
        component that keeps its values; a single product or a database meal is
        split into its product or foods.

        Errors: 400 "Ungültige Eintrags-ID."; 404 "Ernährungseintrag nicht gefunden."
        """
        entry_id = request.query.get("id", [""])[0]
        read = self._diary_service().meal_components(entry_id)
        handler.send_json(
            200,
            {
                "ok": True,
                "id": entry_id,
                "revision": read["revision"],
                "components": [
                    {"index": index, **component}
                    for index, component in enumerate(read["components"])
                ],
            },
        )

    def routes(self) -> list[Route]:
        routes = [
            Route("GET", "/api/nutrition/yazio/status", Access.SESSION, self._yazio),
            Route("GET", "/api/nutrition/day", Access.SESSION, self._day),
            Route("GET", "/api/nutrition/week", Access.SESSION, self._week),
            Route("GET", NUTRITION_PRODUCTS_PATH, Access.SESSION, self._product_list),
            Route(
                "GET",
                f"{NUTRITION_ENTRY_PATH}/components",
                Access.SESSION,
                self._components,
            ),
        ]
        if self._session_fuelling is not None:
            routes.append(
                Route(
                    "GET",
                    "/api/nutrition/session-fuelling",
                    Access.SESSION,
                    self._fuelling,
                )
            )
        return routes

    def _fuelling(self, handler: Any, request: RouteRequest) -> None:
        """Drinking and carbohydrates for a long planned run or ride (#2804).

        Request: query `sport` (`run`, `ride`), `environment` (`indoor` and
        `virtual` are indoors, anything else outdoors), `date` (YYYY-MM-DD) and `duration_s` (planned
        seconds, up to 24 h); no body.

        Response: 200 with `status` `not_long` (another sport or under 60 min)
        or `ready` with `duration_s`, `forecast_c` (cached forecast, outdoors),
        `sweat_rate` (`l_per_h`, `sessions`, `similar`, `temperature_c`; null
        without own data), `drinking` (`l_per_h`, `total_l`, `max_l_per_h`,
        `within_loss_share` with the expected `loss_l` when the loss stays
        within 2 % of body weight, `sodium_g_per_l`, or `generic_l_per_h` without own data),
        `drinking_source`, `carbohydrates`, `carbohydrate_source` and
        `weight`. It reads stored data only and fetches nothing. With `session_id`
        (and `date`, the day the session started) in the query instead: the
        intake logged for that performed session, facts only: `session_id`,
        `duration_s`, `during` (`entries`, `carbs_g`, `carbs_g_per_h`, `advised`
        step, `notable` true only when the rate is more than 20 % below the
        advised lower bound, `volume_ml` and `ml_per_h` with an entry without ml
        counted as 0 ml, `entries_without_volume` naming how many;
        unknown values are `null`, never 0) and `before` (list of `carbs_g`,
        `minutes_before`).

        Errors: 400 `invalid_session_fuelling` "Ungültige Angaben für die
        Verpflegungsempfehlung." for a missing or invalid query value; 404
        `session_not_found` "Einheit nicht gefunden." for an unknown, hidden or
        deleted `session_id`.
        """
        assert self._session_fuelling is not None
        query = request.query
        if query.get("session_id"):
            handler.send_json(
                200,
                self._diary_service().session_intake(
                    query["session_id"][0], (query.get("date") or [""])[0]
                ),
            )
            return
        handler.send_json(200, self._session_fuelling(query))

    def _yazio(self, handler: Any, _request: RouteRequest) -> None:
        """Last local Yazio authentication check, without contacting Yazio.

        Request: no body.

        Response: 200 `{"status", "checked_at"}`. `status` is `not_configured`
        (`checked_at` `null`), `unchecked` before the first check, else the result of
        the last check (`success`, `auth_failed`, `rate_limited`, `unavailable`) with
        its `checked_at`.

        Errors: none beyond the common ones.
        """
        handler.send_json(
            200, self._meal_library_service().food_database.yazio_connection_status()
        )

    def _week(self, handler: Any, request: RouteRequest) -> None:
        """Nutrition week summary of the closed days before today.

        Request: query `days` (integer, default `7`; `7`, `28`, `91`, `182` or `365`).

        Response: 200 `{"ok": true, "start_date", "end_date", "days", "averages",
        "balance_kcal", "balance_days", "expenditure_source"}`. Each item of `days`
        has `date`, `intake_kcal`, `expenditure_kcal` (the day's "Aus deinen Daten"
        total, `expenditure_source` "own", #3600), `balance_kcal`,
        `energy_expenditure` (the wearable's day), the macro totals and the intake
        fields; unknown values are `null`. `averages` gives `{"value", "days"}` per
        field.

        Errors: 400 "Ungültiger Zeitraum." for a non-integer `days`; 400 "Zeitraum muss
        7, 28, 91, 182 oder 365 Tage umfassen." for another number.
        """
        try:
            days = int(request.query.get("days", ["7"])[0])
        except ValueError:
            raise AppError(400, "Ungültiger Zeitraum.") from None
        handler.send_json(
            200,
            {"ok": True, **self._diary_service().sync_status.get_week_summary(days)},
        )

    def _product_list(self, handler: Any, request: RouteRequest) -> None:
        """Product search, barcode lookup or food database entries.

        Request: query `q` (search text, optional), `source`, `scope` and `barcode`,
        checked in this order. `source=database` lists the stored food database entries
        (at most 50, newest first without `q`). `scope=all` lists own products and
        stored database foods matching `q`, each with `usage_count` (the logged meals
        that used it): the best name matches of `q` first, then the most used, then by
        name (#3435). A `barcode` looks the product up, local
        first, then the food databases. Otherwise the active own products matching `q`,
        favorites first, at most 50.

        Response: 200. `source=database`: `{"ok": true, "foods"}`, each food with
        `missing_fields`. `scope=all` and the plain list: `{"ok": true, "products"}`.
        Barcode: `{"ok": true, "foods", "product", "local", "source"}` with `source`
        `local`, `yazio` or `open_food_facts`, plus a `notice` for a database hit.

        Errors: none beyond the common ones.
        """
        handler.send_json(200, self._products(request.query))

    def _day(self, handler: Any, request: RouteRequest) -> None:
        """Nutrition day summary with macro ranges.

        Request: query `date` (`YYYY-MM-DD`, default today).

        Response: 200 `{"ok": true, ...}` with the day of `get_day_summary`: `date`,
        `total_kcal`, `total_carbs_g`, `total_protein_g`, `total_fat_g`,
        `total_sugar_g`, `total_salt_g`, `known_macro_totals`, `known_intake_totals`,
        `entries_without_macros`, `entry_count`, `entries` and `energy_expenditure`; a
        macro total is `null` unless every entry reports it. `sessions` lists the
        day's sessions (`id`, `sport`, `started_at_local`, `elapsed_duration_s`); each
        `pre_session`/`during_session` entry carries `session`, its attached
        session or `null`. `macro_ranges` maps
        `protein_g`, `carbs_g` and `fat_g` to `[low, high]` where weight and training
        calendar allow it.

        Errors: 400 "Ungültiges Datumsformat '{value}'. Erwartet wird YYYY-MM-DD." for
        an invalid `date`.
        """
        date_param = (
            request.query.get("date", [None])[0] or self._local_now().date().isoformat()
        )
        handler.send_json(200, {"ok": True, **self._day_summary(date_param)})


class NutritionPostRoutes:
    """Handle authenticated nutrition mutations and synchronization."""

    def __init__(
        self,
        diary_service: Callable[[], NutritionDiaryService],
        meal_library_service: Callable[[], NutritionMealLibraryService],
        nutrition_refresh_service: Callable[[], Any] | None = None,
        local_now: Callable[[], datetime] | None = None,
        product_refresh: Any = None,
        product_cleanup: Callable[[], Any] | None = None,
    ) -> None:
        self._product_cleanup = product_cleanup
        self._product_refresh = product_refresh
        self._diary_service = diary_service
        self._meal_library_service = meal_library_service
        self._nutrition_refresh_service = nutrition_refresh_service
        self._local_now = local_now or datetime.now

    def routes(self) -> list[Route]:
        return (
            [
                Route(
                    "POST",
                    path,
                    Access.SESSION_CSRF,
                    handle,
                    replayable=path == NUTRITION_ENTRY_PATH,
                )
                for path, handle in (
                    ("/api/nutrition/yazio/check", self._check_yazio),
                    (NUTRITION_ENTRY_PATH, self._create),
                    (f"{NUTRITION_ENTRY_PATH}/delete", self._delete),
                    (f"{NUTRITION_ENTRY_PATH}/add-product", self._add_product),
                    (f"{NUTRITION_ENTRY_PATH}/components", self._edit_components),
                    (f"{NUTRITION_ENTRY_PATH}/session", self._set_session),
                    (f"{NUTRITION_ENTRY_PATH}/estimate", self._set_estimate),
                    ("/api/nutrition/refresh", self._refresh),
                    (TEMPLATES_PATH, self._save_template),
                    (f"{TEMPLATES_PATH}/from-entry", self._template_from_entry),
                    (f"{TEMPLATES_PATH}/delete", self._delete_template),
                    (f"{TEMPLATES_PATH}/log", self._log_template),
                    (NUTRITION_PRODUCTS_PATH, self._save_product),
                    (f"{NUTRITION_PRODUCTS_PATH}/lookup", self._lookup_product),
                    (f"{NUTRITION_PRODUCTS_PATH}/extract", self._extract_product),
                    (f"{NUTRITION_PRODUCTS_PATH}/archive", self._archive_product),
                    (f"{NUTRITION_PRODUCTS_PATH}/favorite", self._favorite_product),
                    (
                        f"{NUTRITION_PRODUCTS_PATH}/database/delete",
                        self._forget_database_food,
                    ),
                    (
                        f"{NUTRITION_PRODUCTS_PATH}/database/refresh",
                        self._refresh_database_food,
                    ),
                )
            ]
            + (
                [
                    Route(
                        "POST", REFRESH_ALL_PATH, Access.SESSION_CSRF, self._refresh_all
                    ),
                    Route(
                        "GET", REFRESH_ALL_PATH, Access.SESSION, self._refresh_all_state
                    ),
                ]
                if self._product_refresh is not None
                else []
            )
            + [Route("GET", TEMPLATES_PATH, Access.SESSION, self._list_templates)]
            + (
                [
                    Route("GET", CLEANUP_PATH, Access.SESSION, self._cleanup_preview),
                    Route("POST", CLEANUP_PATH, Access.SESSION_CSRF, self._cleanup),
                ]
                if self._product_cleanup is not None
                else []
            )
        )

    def _refresh_all(self, handler: Any, _request: RouteRequest) -> None:
        """Re-check every product in the background; gaps fill, known values stay.

        Request: JSON object; its content is ignored. A run already in progress
        is not started again.

        Response: 200 as `GET /api/nutrition/products/refresh-all`.

        Errors: none beyond the common ones.
        """
        handler.read_json()
        handler.send_json(200, self._product_refresh.start())

    def _cleanup_preview(self, handler: Any, request: RouteRequest) -> None:
        """The products a cleanup with `days` would remove (#3444).

        Request: query `days`, one of 30, 90, 180 or 365.

        Response: 200 `{"days", "count", "products"}`, each product `id`, `name`,
        `brand`, `last_used` (newest meal day or null) and `created_at`; newest
        use first. Favorites and products a saved meal or template uses are
        never listed.

        Errors: 400 `invalid_days` "Bitte 30, 90, 180 oder 365 Tage wählen.".
        """
        if self._product_cleanup is not None:
            days = request.query.get("days", [None])[0]
            handler.send_json(200, self._product_cleanup().preview(days))

    def _cleanup(self, handler: Any, _request: RouteRequest) -> None:
        """Remove the products not used for `days`, as one undoable run (#3444).

        Request: JSON body `{"days": 30 | 90 | 180 | 365, "confirmed": true}`. The
        server finds the products itself, as the preview does: own products are
        archived, stored database foods removed.

        Response: 200 `{"ok": true, "count", "change"}`; `change` undoes the whole
        run through `POST /api/change-history/undo` (null when nothing was removed).

        Errors: 400 `confirmation_required` "Das Aufräumen muss ausdrücklich
        bestätigt werden."; 400 `invalid_days` "Bitte 30, 90, 180 oder 365 Tage
        wählen.".
        """
        if self._product_cleanup is not None:
            payload = _read_object(handler)
            handler.send_json(200, self._product_cleanup().run(payload))

    def _refresh_all_state(self, handler: Any, _request: RouteRequest) -> None:
        """The progress of the current or last run re-checking every product.

        Request: no query, no body.

        Response: 200 `{"running": bool}`, after a start also `total`, `done`,
        `filled`, `unchanged`, `skipped` (own products without an EAN),
        `failed`, `started_at` and, once ended, `finished_at`.

        Errors: none beyond the common ones.
        """
        handler.send_json(200, self._product_refresh.state())

    def _check_yazio(self, handler: Any, _request: RouteRequest) -> None:
        """Explicitly check Yazio authentication; the result is kept in memory.

        Request: JSON object; its content is ignored.

        Response: 200 `{"status", "checked_at"}`: `not_configured` (`checked_at` `null`)
        without Yazio credentials, else `success`, `auth_failed`, `rate_limited` or
        `unavailable` with the check time.

        Errors: none beyond the common ones.
        """
        handler.read_json()
        handler.send_json(
            200, self._meal_library_service().food_database.check_yazio_connection()
        )

    def _create(self, handler: Any, _request: RouteRequest) -> None:
        """Log a meal; returns a `change` undo reference.

        Request: JSON body, either `components` alone or one meal. Meal fields:
        `description` (cut to 500 characters; required unless `product_id` supplies it),
        `kcal` (0 to 10000, or `calories`; required unless `product_id`,
        `food_ingredients` or `components` supply it), optional `carbs_g`, `protein_g`,
        `fat_g` (0 to 1000, or `carbs`, `carbohydrates`, `protein`, `fat`), `meal_date`
        (or `date`, default today), `meal_time` (or `logged_at`; a time or an ISO
        timestamp; without one `breakfast`, `lunch` and `dinner` take 09:00, 12:00
        and 18:00, other types today the current time, else no known time),
        `logged_time_known` (boolean; `false` needs `meal_type`),
        `estimate_uncertain` (boolean, default false: the amounts were estimated),
        `meal_type` (`breakfast`, `lunch`, `dinner`, `snack`, `pre_session`,
        `during_session`; default derived from the time, never a session slot),
        `volume_ml` (drink volume above 0 up to 10000, absent when not stated),
        `session_id` (only for a session slot: a session of that day, else the
        server attaches the one session or the unique match by time and leaves
        the entry unattached when it is ambiguous),`source` (`manual`, `voice`, `photo`, `coach`; anything else is
        `manual`), `packaging_label` (`true` marks label values), `food_ingredients`
        (list of `{food_id, amount, unit}`) or `product_id` with `amount` and `unit`
        (nutrients calculated from the saved product). `components` is a list of 1 to 20
        items `{kind, amount, unit}` plus `product_id` (`local_product`), `food_id`
        (`database`) or `name`, `kcal`, `carbs_g`, `protein_g`, `fat_g` (`manual`,
        `estimate`, `packaging_label`); `unit` is `g`, `ml` or `portion`. It excludes
        `product_id`, `food_ingredients`, `nutrition_basis` and every nutrient field.

        Response: 200 `{"ok": true, "entry", "change"}`; `entry` is the stored entry
        (`id`, `meal_date`, `logged_at`, `meal_type`, `description`, `kcal`, the macros,
        `source`, `nutrition_basis`, `sync_state`, `logged_time_known`,
        `estimate_uncertain`), `change` the
        undo reference. A repeated `Idempotency-Key` header (UUID) answers the stored
        result and writes nothing; the same key on another path answers 409.

        Errors: 400 "Beschreibung der Mahlzeit ist erforderlich."; 400 "Kalorienangabe
        (kcal) ist erforderlich."; 400 "Kalorien müssen zwischen 0 und 10000 liegen.";
        400 "Ungültiges Datumsformat '{value}'. Erwartet wird YYYY-MM-DD."; 400
        "Ungültige Uhrzeit '{value}'."; 400 "Bei unbekannter Uhrzeit ist der Mahlzeittyp
        erforderlich."; 400 "Ungültiger Mahlzeittyp '{value}'. Erlaubt sind: breakfast,
        lunch, dinner, snack, pre_session, during_session."; 400 "Volumen muss größer als 0
        sein."; 400 `session_not_found` "Einheit nicht gefunden."; 400 "Komponenten dürfen nicht mit Einzelzutaten,
        Summenwerten oder clientseitiger Herkunft kombiniert werden."; 400 "1 bis 20
        Mahlzeitenkomponenten sind erforderlich."; 400 "Ungültige Mahlzeitenkomponente."
        and 400 "Einheit muss g, ml oder portion sein." for a malformed component; 404
        `nutrition_product_not_found` "Produkt nicht gefunden." and 409
        `nutrition_product_archived` "Archivierte Produkte können nicht erfasst werden."
        for an unknown or archived product; 400 `food_unit_mismatch` when `unit` differs
        from the basis unit of the product; 400 `food_energy_missing` for a component
        without calorie value.
        """
        with change_history.collect_changes() as changes:
            entry = self._diary_service().log_meal(handler.read_json())
        handler.send_json(200, {"ok": True, "entry": entry, **_change(changes)})

    def _list_templates(self, handler: Any, _request: RouteRequest) -> None:
        """The saved meals, each for one portion.

        Request: no query, no body.

        Response: 200 `{"templates"}`; each template has `id`, `name`,
        `description`, `meal_type`, `kcal`, `carbs_g`, `protein_g`, `fat_g`,
        `source`, `nutrition_basis` and `updated_at`.

        Errors: none beyond the common ones.
        """
        handler.send_json(
            200, {"templates": self._meal_library_service().list_templates()}
        )

    def _save_template(self, handler: Any, _request: RouteRequest) -> None:
        """Create or change a saved meal, with the server's nutrient values.

        Request: JSON body of the Coach tool `save_nutrition_template`: `name` (1 to 120
        characters, unique), optional `id` (changes that saved meal), `description`,
        `meal_type` and either `components` (1 to 20 items as in `POST
        /api/nutrition/entry`, calculated by the server) or `food_ingredients`, or
        the values `kcal`, `carbs_g`, `protein_g`, `fat_g`. A saved meal never
        changes diary entries already logged.

        Response: 200 `{"ok": true, "template", "change"}`; `change` is the undo
        reference of the saved meal.

        Errors: 400 "Mahlzeitname muss 1 bis 120 Zeichen enthalten."; 409 "Dieser
        Mahlzeitname ist bereits vergeben. Lies die Vorlage und ändere sie
        gezielt."; 400 "Maximal 200 gespeicherte Mahlzeiten sind möglich."; 404
        "Gespeicherte Mahlzeit nicht gefunden." for an unknown `id`; the component
        errors of `POST /api/nutrition/entry`.
        """
        with change_history.collect_changes() as changes:
            template = self._meal_library_service().save_template(_read_object(handler))
        handler.send_json(
            200,
            {
                "ok": True,
                "template": template,
                **_change(changes, "nutrition_template"),
            },
        )

    def _template_from_entry(self, handler: Any, _request: RouteRequest) -> None:
        """Keep a logged meal as a saved meal.

        Request: JSON body `{"id": string, "name": string}`; `id` is the diary
        entry, `name` the saved meal's name (1 to 120 characters, unique). The
        entry's values and basis are copied as stored for one portion; a meal in a
        session slot becomes a snack.

        Response: 200 `{"ok": true, "template", "change"}`.

        Errors: 404 "Ernährungseintrag nicht gefunden." for an unknown entry; the
        name errors of `POST /api/nutrition/templates`.
        """
        payload = _read_object(handler)
        entry = self._diary_service().get_meal(str(payload.get("id") or ""))
        with change_history.collect_changes() as changes:
            template = self._meal_library_service().save_template_from_meal(
                entry, payload.get("name")
            )
        handler.send_json(
            200,
            {
                "ok": True,
                "template": template,
                **_change(changes, "nutrition_template"),
            },
        )

    def _delete_template(self, handler: Any, _request: RouteRequest) -> None:
        """Delete a saved meal; logged diary entries stay as they are.

        Request: JSON body `{"id": string, "confirmed": true}`.

        Response: 200 `{"ok": true, "deleted_id", "change"}`.

        Errors: 409 "Das Löschen muss bestätigt werden." without `confirmed: true`;
        404 "Gespeicherte Mahlzeit nicht gefunden.".
        """
        payload = _read_object(handler)
        if payload.get("confirmed") is not True:
            raise AppError(409, "Das Löschen muss bestätigt werden.")
        result = self._meal_library_service().delete_template(
            str(payload.get("id") or "")
        )
        handler.send_json(200, {"ok": True, **result})

    def _log_template(self, handler: Any, _request: RouteRequest) -> None:
        """Log a saved meal on a day, for a number of portions.

        Request: JSON body `{"id": string, "portions": number, "meal_date": string,
        "meal_time": string, "meal_type": string, "volume_ml": number}`; `portions`
        is above 0 up to 20 (default 1), the other fields are optional and as in
        `POST /api/nutrition/entry`. The values are the saved meal's, scaled by the
        server.

        Response: 200 `{"ok": true, "entry", "change"}`.

        Errors: 404 "Gespeicherte Mahlzeit nicht gefunden."; 400 "Ungültige
        Portionsanzahl." or "Portionsanzahl muss größer als 0 und höchstens 20
        sein."; the date, time and type errors of `POST /api/nutrition/entry`.
        """
        payload = _read_object(handler)
        with change_history.collect_changes() as changes:
            entry = self._diary_service().log_template(
                str(payload.get("id") or ""),
                payload.get("portions", 1),
                meal_date=payload.get("meal_date"),
                meal_time=payload.get("meal_time"),
                meal_type=payload.get("meal_type"),
                volume_ml=payload.get("volume_ml"),
            )
        handler.send_json(200, {"ok": True, "entry": entry, **_change(changes)})

    def _add_product(self, handler: Any, _request: RouteRequest) -> None:
        """Add a saved product to a logged meal as one more component.

        Request: JSON body `{"id": string, "product_id": string, "amount": number,
        "unit": string}`; `id` is the entry id, `amount` is above 0 up to 10000, `unit`
        must be the basis unit of the product (`g`, `ml` or `portion`). The entry
        becomes a composite of at most 20 components and its description gains " +
        {product name}".

        Response: 200 `{"ok": true, "entry", "change"}` with the updated entry and the
        undo reference.

        Errors: 400 "Ungültige Eintrags-ID." for a missing `id`; 404 "Ernährungseintrag
        nicht gefunden."; 404 `nutrition_product_not_found` "Produkt nicht gefunden.";
        409 `nutrition_product_archived` "Archivierte Produkte können nicht erfasst
        werden."; 400 "Komponentenreferenz muss eine Zeichenkette sein." for a missing
        `product_id`; 400 "Ungültige Komponentenmenge." or 400 "Komponentenmenge muss
        größer als 0 sein."; 400 "Einheit muss g, ml oder portion sein."; 400
        `food_unit_mismatch` "Komponenteneinheit stimmt nicht mit dem Produkt überein.";
        400 `food_energy_missing` "Für dieses Produkt fehlt der Kalorienwert."; 400
        "Eine Mahlzeit kann höchstens 20 Komponenten enthalten."
        """
        payload = _read_object(handler)
        with change_history.collect_changes() as changes:
            entry = self._diary_service().add_product_to_meal(
                str(payload.get("id") or ""),
                str(payload.get("product_id") or ""),
                payload.get("amount"),
                str(payload.get("unit") or ""),
            )
        handler.send_json(200, {"ok": True, "entry": entry, **_change(changes)})

    def _edit_components(self, handler: Any, _request: RouteRequest) -> None:
        """Remove, resize or swap components of a logged meal, and change its day, type
        or time (#3948, #3985).

        Request: JSON body `{"id": string, "revision": string, "edits": [...], "meal": {...}}`;
        `edits` and `meal` are each optional, one is required. `meal` changes the meal
        itself: `meal_date` (YYYY-MM-DD, both days are marked for the next Intervals.icu
        synchronization), `meal_type` (a new type moves a default time to the type's
        default), and `meal_time` (HH:MM) or `logged_time_known: false` (the time is
        unknown, only the type stays); a `meal` without `edits` leaves the components and
        the origin as stored. `id` is the
        entry id, `revision` the one `GET /api/nutrition/entry/components` returned, and
        each edit names a component by its `index` (see
        `GET /api/nutrition/entry/components`) and is one of `{"index", "remove": true}`,
        `{"index", "amount"}` (above 0 up to 10000, the same unit) or a swap
        `{"index", "food_id"}` / `{"index", "product_id"}` with optional `amount` and
        `unit` (default: the component's own, so the amount stays). Every index at
        most once, other components stay as stored. The server scales the nutrients from
        the stored snapshot or calculates them from the food or product and the amount;
        the client sends none. The meal becomes a composite and keeps its description;
        its day is marked for the next Intervals.icu synchronization; templates and
        saved meals are not changed.

        Response: 200 `{"ok": true, "entry", "change"}` with the updated entry and the
        undo reference.

        Errors: 400 "Ungültige Eintrags-ID."; 404 "Ernährungseintrag nicht gefunden.";
        409 `meal_changed` "Die Mahlzeit hat sich geändert. Bitte erneut öffnen." when the
        components differ from the `revision`; 400 "Ungültige Änderung der
        Mahlzeitenkomponenten." for a malformed edit; 400
        `last_component` when nothing would remain; 400 "Ungültige Komponentenmenge." or
        "Komponentenmenge muss größer als 0 sein."; 404 `food_not_found`, 404
        `nutrition_product_not_found`, 409 `nutrition_product_archived`, 400
        `food_unit_mismatch` and 400 `food_energy_missing` for a swap target.
        """
        payload = _read_object(handler)
        with change_history.collect_changes() as changes:
            entry = self._diary_service().edit_components(
                str(payload.get("id") or ""),
                payload.get("edits"),
                payload.get("revision"),
                payload.get("meal"),
            )
        handler.send_json(200, {"ok": True, "entry": entry, **_change(changes)})

    def _set_estimate(self, handler: Any, _request: RouteRequest) -> None:
        """Mark a meal's amounts as estimated (restaurant, friends), or clear the mark.

        Request: JSON body `{"id": string, "estimate_uncertain": boolean}`.

        Response: 200 `{"ok": true, "entry", "change"}` with the updated entry and the
        undo reference. The logged values are never changed (#3100).

        Errors: 400 "Ungültige Eintrags-ID."; 404 "Ernährungseintrag nicht gefunden.";
        400 "estimate_uncertain muss true oder false sein."
        """
        payload = _read_object(handler)
        flag = payload.get("estimate_uncertain")
        if not isinstance(flag, bool):
            raise AppError(400, "estimate_uncertain muss true oder false sein.")
        with change_history.collect_changes() as changes:
            entry = self._diary_service().correct_meal(
                str(payload.get("id") or ""), {"estimate_uncertain": flag}
            )
        handler.send_json(200, {"ok": True, "entry": entry, **_change(changes)})

    def _set_session(self, handler: Any, _request: RouteRequest) -> None:
        """Attach a "Vor/Während der Einheit" entry to the session the athlete chose.

        Request: JSON body `{"id": string, "session_id": string}`; `id` is the entry id,
        `session_id` an active session of the entry's day, the day before or after.

        Response: 200 `{"ok": true, "entry", "change"}` with the updated entry and the
        undo reference. The choice is kept: a later refresh never changes it.

        Errors: 400 "Ungültige Eintrags-ID."; 404 "Ernährungseintrag nicht gefunden.";
        400 `session_not_found` "Einheit nicht gefunden."; 400 "Eine Einheit kann nur bei
        „Vor/Während der Einheit“ gesetzt werden." for another meal type; 400
        "session_id ist erforderlich." for an empty `session_id`.
        """
        payload = _read_object(handler)
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            raise AppError(400, "session_id ist erforderlich.")
        with change_history.collect_changes() as changes:
            entry = self._diary_service().correct_meal(
                str(payload.get("id") or ""), {"session_id": session_id}
            )
        handler.send_json(200, {"ok": True, "entry": entry, **_change(changes)})

    def _delete(self, handler: Any, _request: RouteRequest) -> None:
        """Delete a logged meal.

        Request: JSON body `{"id": string}` (or `entry_id`), the entry id.

        Response: 200 `{"ok": true, "status": "ok", "deleted_id", "change"}`; `change`
        is the undo reference.

        Errors: 400 "id ist erforderlich zum Löschen." for a missing id; 404
        "Ernährungseintrag nicht gefunden."
        """
        payload = _read_object(handler)
        entry_id = payload.get("id") or payload.get("entry_id")
        if not entry_id:
            raise AppError(400, "id ist erforderlich zum Löschen.")
        with change_history.collect_changes() as changes:
            result = self._diary_service().delete_meal(str(entry_id))
        handler.send_json(200, {"ok": True, **result, **_change(changes)})

    def _refresh(self, handler: Any, _request: RouteRequest) -> None:
        """Run a targeted nutrition refresh.

        Request: JSON body `{"area": string, "date": "YYYY-MM-DD", "days": integer}` and
        no other key. `area` is `diary`, `week`, `body` or `products`. `date` is
        optional: today for `diary` and `body`, yesterday for `week`; for `week` and
        `body` it must not lie in the future. `days` is `28`, `91`, `182` or `365` for `week`
        (default `28`) and `14` or `84` for `body` (default `84`), and ends at `date`. `diary` refreshes the Garmin energy
        expenditure of that day, `week` the Garmin daily totals, `body` the Garmin
        weight; `products` only reads the local products and ignores `date` and `days`.

        Response: 200 `{"ok": true, "area", ...}`. `diary`: `status`, `meal_status`
        (`local`), `summary` (the day as in `GET /api/nutrition/day`) and
        `expenditure_refresh`. `products`: `status` `local` and `products` (active own
        products). `week` and `body`: the Garmin result, `status` (`ok` or `partial`),
        `source`, `data_source`, `start`, `end`, `records`, `synced_at`, or `status`
        `partial` with `reason` (`not_configured`, `not_supported`,
        `library_unavailable`, `provider_unavailable`) when Garmin cannot be read.

        Errors: 400 "Ungültiger Ernährungsbereich." for another `area`; 400 "Ungültiger
        Anfrageinhalt." for another key; 400 "Ungültiges Datumsformat '{value}'.
        Erwartet wird YYYY-MM-DD." for an invalid `date`; 400 "Der Zeitraum muss 28, 91, 182
        oder 365 Tage umfassen." (`week`) or "... 14 oder 84 Tage ..." (`body`); 400 "Das Datum darf nicht in der Zukunft liegen."; 503
        `provider_busy` "Garmin wird bereits aktualisiert."
        """
        payload = _read_object(handler)
        area = payload.get("area")
        if area not in {"diary", "week", "body", "products"}:
            raise AppError(400, "Ungültiger Ernährungsbereich.")
        if set(payload) - {"area", "date", "days"}:
            raise AppError(400, INVALID_BODY)

        if area == "diary":
            meal_date = validate_iso_date(
                payload.get("date") or self._local_now().date().isoformat()
            )
            expenditure = self._refresh_provider("daily_stats", meal_date, meal_date)
            summary = self._diary_service().sync_status.get_day_summary(meal_date)
            if expenditure.get("status") != "ok":
                summary["energy_expenditure"]["freshness"] = "partial"
            handler.send_json(
                200,
                {
                    "ok": True,
                    "area": area,
                    "status": expenditure.get("status", "partial"),
                    "meal_status": "local",
                    "summary": summary,
                    "expenditure_refresh": expenditure,
                },
            )
            return
        if area == "products":
            products = self._meal_library_service().product_catalog.list_products()
            handler.send_json(
                200,
                {"ok": True, "area": area, "status": "local", "products": products},
            )
            return

        days = payload.get("days", REFRESH_PERIODS[area][0])
        if type(days) is not int or days not in REFRESH_PERIODS[area]:
            raise AppError(400, REFRESH_PERIOD_ERRORS[area])
        default_end = self._local_now().date()
        if area == "week" and "date" not in payload:
            default_end -= timedelta(days=1)
        end = validate_iso_date(payload.get("date") or default_end.isoformat())
        end_date = date.fromisoformat(end)
        today = self._local_now().date()
        if end_date > today:
            raise AppError(400, "Das Datum darf nicht in der Zukunft liegen.")
        start = end_date - timedelta(days=days - 1)
        source = "daily_stats" if area == "week" else "weight"
        result = self._refresh_provider(source, start.isoformat(), end)
        handler.send_json(200, {"ok": True, "area": area, **result})

    def _refresh_provider(self, source: str, start: str, end: str) -> dict[str, Any]:
        if self._nutrition_refresh_service is None:
            return {"status": "partial", "reason": "not_configured"}
        try:
            return self._nutrition_refresh_service().refresh_nutrition(
                source,
                date.fromisoformat(start),
                date.fromisoformat(end),
            )
        except AppError as exc:
            if exc.reason not in {
                "not_configured",
                "not_supported",
                "library_unavailable",
            }:
                raise
            return {"status": "partial", "reason": exc.reason}

    def _save_product(self, handler: Any, _request: RouteRequest) -> None:
        """Save a confirmed product.

        Request: JSON body `{"confirmed": true, "product": {...}}`, or the product
        fields next to `confirmed`. Product fields: `name` (required, at most 200
        characters), `brand` (cut to 120), `barcode` (8, 12, 13 or 14 digits, else
        dropped), `basis_amount` (above 0 up to 10000) and `basis_unit` (`g`, `ml`,
        `portion`), both required, `kcal`, `carbs_g`, `protein_g`, `fat_g`, `sugar_g`,
        `fiber_g`, `salt_g` (0 to 10000, empty is unknown), `confidence` (clamped to 0
        to 1), `source` (`packaging_label` default, `manual`, `open_food_facts`,
        `yazio`, `bls`, `fddb_export`), `source_url` (cut to 500), `external_id` (cut to
        200), `id` (saves over that product) and `status` (`active` or `archived`).
        Saving a new product with a barcode fills its empty fields from the stored
        database foods of that barcode.

        Response: 200 `{"ok": true, "product", "change"}` with the stored product and
        the undo reference.

        Errors: 400 `confirmation_required` "Das Produkt muss ausdrücklich bestätigt
        werden."; 400 `invalid_food_extraction` "Ein Produktname ist erforderlich.",
        "Ungültiger Wert für {field}.", "Ungültige Bezugsmenge.", "Bezugsmenge muss
        zwischen 0 und 10000 liegen." or "Bezugsmenge und Einheit sind erforderlich.";
        400 "Ungültige Produktquelle."; 409 `nutrition_product_barcode_conflict` "Diese
        EAN ist bereits einem lokalen Produkt zugeordnet."
        """
        payload = _read_object(handler)
        confirmed = payload.pop("confirmed", None)
        product = payload.pop("product", payload)
        if confirmed is not True:
            raise AppError(
                400,
                "Das Produkt muss ausdrücklich bestätigt werden.",
                reason="confirmation_required",
            )
        with change_history.collect_changes() as changes:
            saved = self._meal_library_service().product_catalog.save_product(product)
        handler.send_json(
            200, {"ok": True, "product": saved, **_change(changes, PRODUCT_ENTITY)}
        )

    def _lookup_product(self, handler: Any, _request: RouteRequest) -> None:
        """Look a product up by barcode or name, local products first.

        Request: JSON body `{"barcode": string}` or `{"query": string}` (alias `q`, at
        least 2 characters), with the optional `source` `open_food_facts`, `yazio` or
        `bls` for a name search. Without a local hit the food databases are asked.

        Response: 200 `{"ok": true, "foods", "product", "local", "source", "notice"}`;
        `product` is the first hit or `null`; a local hit has `local: true` and `source:
        "local"`; `notice` comes with a database hit.

        Errors: 400 "Suchbegriff muss mindestens 2 Zeichen enthalten." without a barcode
        and a query of fewer than 2 characters; 400 "Suchbegriff muss 2 bis 120 Zeichen
        enthalten." for a longer query; 400 "Ungültige Lebensmitteldatenbank." for
        another `source`.
        """
        payload = _read_object(handler)
        handler.send_json(
            200, self._meal_library_service().product_catalog.lookup_product(payload)
        )

    def _extract_product(self, handler: Any, _request: RouteRequest) -> None:
        """Extract a product from a label photo; the image is not stored.

        Request: JSON body `{"image_data_url": string}`, a `data:image/jpeg`,
        `image/png` or `image/webp` base64 URL of at most 5 MiB decoded. Without
        `image_data_url` the body is validated as an extraction (`name`, `brand`,
        `barcode`, `basis_amount`, `basis_unit`, `kcal`, `carbs_g`, `protein_g`,
        `fat_g`, `sugar_g`, `fiber_g`, `salt_g`, `confidence`; `name` required). The
        body limit is 7 056 044 bytes.

        Response: 200 `{"ok": true, "candidate", "extraction", "provenance":
        "packaging_label", "requires_confirmation": true}`; `extraction` repeats
        `candidate`. Nothing is saved.

        Errors: 413 `food_image_too_large` "Das Verpackungsfoto ist zu groß."; 415
        `food_image_type` "Nur JPEG-, PNG- oder WebP-Fotos werden unterstützt." or
        "Dateityp und Bildinhalt stimmen nicht überein."; 400 `invalid_food_image` "Das
        Verpackungsfoto ist ungültig."; 503 `provider_unavailable` "Fotoerkennung ist
        nicht verfügbar." or "Der OpenAI-Dienst ist nicht verfügbar."; 502
        `invalid_provider_response` "Die Nährwerttabelle konnte nicht gelesen werden.";
        400 `invalid_food_extraction` for an invalid candidate (as for the product
        save).
        """
        payload = _read_object(handler, PHOTO_REQUEST_MAX_BYTES)
        handler.send_json(
            200, self._meal_library_service().extract_packaging_photo(payload)
        )

    def _refresh_database_food(self, handler: Any, _request: RouteRequest) -> None:
        """Re-check one food database entry or own product online now; gaps fill.

        Request: JSON body `{"id": string}`, an own product id or a stored database food
        id (`off:...` or `yazio:...`). Only empty fields are filled; known values stay.

        Response: 200 for an own product: `{"ok": true, "product", "notice", "change"}`
        (`change` only when fields were filled); for a database food: `{"ok": true,
        "food"}` with `missing_fields` and a `notice` when a source was skipped.

        Errors: 409 `nutrition_product_archived` "Archivierte Produkte werden nicht
        aktualisiert."; 400 `nutrition_product_barcode_missing` "Zum Aktualisieren
        braucht das Produkt eine EAN. Trage sie beim Bearbeiten ein."; 404
        `food_not_found` "Datenbankeintrag nicht gefunden."; 409 `not_configured` "Yazio
        ist nicht eingerichtet." for a Yazio food without credentials; 502
        `food_database_unavailable` "Der Datenbankeintrag konnte nicht aktualisiert
        werden."
        """
        payload = _read_object(handler)
        with change_history.collect_changes() as changes:
            result = self._meal_library_service().refresh_database_food(
                str(payload.get("id") or "")
            )
        handler.send_json(200, {**result, **_change(changes, PRODUCT_ENTITY)})

    def _forget_database_food(self, handler: Any, _request: RouteRequest) -> None:
        """Forget a food database entry.

        Request: JSON body `{"id": string, "confirmed": true}`.

        Response: 200 `{"ok": true, "id", "status": "removed"}`.

        Errors: 400 `confirmation_required` "Das Entfernen muss ausdrücklich bestätigt
        werden."; 404 `food_not_found` "Datenbankeintrag nicht gefunden."
        """
        payload = _read_object(handler)
        if payload.get("confirmed") is not True:
            raise AppError(
                400,
                "Das Entfernen muss ausdrücklich bestätigt werden.",
                reason="confirmation_required",
            )
        handler.send_json(
            200,
            self._meal_library_service().forget_database_food(
                str(payload.get("id") or "")
            ),
        )

    def _favorite_product(self, handler: Any, _request: RouteRequest) -> None:
        """Mark or unmark a product as favorite.

        Request: JSON body `{"id": string, "favorite": boolean}`.

        Response: 200 `{"ok": true, "product"}` with the updated product.

        Errors: 400 "favorite muss true oder false sein."; 404
        `nutrition_product_not_found` "Produkt nicht gefunden."
        """
        payload = _read_object(handler)
        product = self._meal_library_service().product_catalog.set_favorite(
            str(payload.get("id") or ""), payload.get("favorite")
        )
        handler.send_json(200, {"ok": True, "product": product})

    def _archive_product(self, handler: Any, _request: RouteRequest) -> None:
        """Archive a product.

        Request: JSON body `{"id": string, "confirmed": true}`.

        Response: 200 `{"ok": true, "product", "change"}` with the archived product and
        the undo reference.

        Errors: 400 `confirmation_required` "Das Archivieren muss ausdrücklich bestätigt
        werden."; 404 `nutrition_product_not_found` "Produkt nicht gefunden."
        """
        payload = _read_object(handler)
        if payload.get("confirmed") is not True:
            raise AppError(
                400,
                "Das Archivieren muss ausdrücklich bestätigt werden.",
                reason="confirmation_required",
            )
        with change_history.collect_changes() as changes:
            archived = self._meal_library_service().product_catalog.archive_product(
                str(payload.get("id") or "")
            )
        handler.send_json(
            200, {"ok": True, "product": archived, **_change(changes, PRODUCT_ENTITY)}
        )


class NutritionPutRoutes:
    """Handle authenticated product updates.

    Diary entries change only through the Coach correction or the component edit
    (``POST /api/nutrition/entry/components``, #3948), which enforce the field
    whitelist, the nutrient recalculation and the undo reference; the old
    ``PUT /api/nutrition/entry`` skipped all three (#2137, F-018).
    """

    def __init__(
        self, meal_library_service: Callable[[], NutritionMealLibraryService]
    ) -> None:
        self._meal_library_service = meal_library_service

    def routes(self) -> list[Route]:
        return [
            Route(
                "PUT",
                NUTRITION_PRODUCTS_PATH,
                Access.SESSION_CSRF,
                self._update_product,
            )
        ]

    def _update_product(self, handler: Any, _request: RouteRequest) -> None:
        """Update a confirmed saved product.

        Request: JSON body `{"id": string, "confirmed": true, ...}` with the fields to
        change, as for the product save; they are merged into the stored product. For a
        stored database food only the nutrient values (`kcal`, `carbs_g`, `protein_g`,
        `fat_g`, `sugar_g`, `fiber_g`, `salt_g`) can change.

        Response: 200 `{"ok": true, "product", "change"}` with the updated product and
        the undo reference.

        Errors: 400 `confirmation_required` "Das Produkt muss ausdrücklich bestätigt
        werden."; 400 "id ist erforderlich zum Aktualisieren."; 404
        `nutrition_product_not_found` "Produkt nicht gefunden."; 404 `food_not_found`
        "Datenbankeintrag nicht gefunden."; 400 `food_database_field_fixed` "Bei einem
        Datenbankeintrag sind nur die Nährwerte änderbar."; the validation errors of the
        product save.
        """
        payload = _read_object(handler)
        if payload.pop("confirmed", None) is not True:
            raise AppError(
                400,
                "Das Produkt muss ausdrücklich bestätigt werden.",
                reason="confirmation_required",
            )
        product_id = str(payload.get("id") or "").strip()
        if not product_id:
            raise AppError(400, "id ist erforderlich zum Aktualisieren.")
        with change_history.collect_changes() as changes:
            updated = self._meal_library_service().product_catalog.update_product(
                product_id, payload
            )
        handler.send_json(
            200,
            {
                "ok": True,
                "product": updated,
                **(
                    _change(changes, PRODUCT_ENTITY)
                    or _change(changes, FOOD_ENTITY_TYPE)
                ),
            },
        )


def _change(
    changes: list[dict[str, str]], entity_type: str = "nutrition_entry"
) -> dict[str, Any]:
    """The undo reference of the last change of one entity type, as ``{"change": ...}``."""
    matching = [item for item in changes if item["entity_type"] == entity_type]
    return {"change": matching[-1]} if matching else {}


def _read_object(handler: Any, max_bytes: int | None = None) -> dict[str, Any]:
    payload = handler.read_json(max_bytes)
    if not isinstance(payload, dict):
        raise AppError(400, INVALID_BODY)
    return payload
