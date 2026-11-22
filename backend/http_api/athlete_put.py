"""Authenticated local athlete PUT routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.athlete.profile import ProfileService
from backend.errors import NOT_FOUND_ERROR, AppError
from backend.http_api.route_table import Access, Route, RouteRequest

SKINFOLDS_PATH = "/api/body/skinfolds"
WEIGHT_PATH = "/api/body/weight"
BODY_FAT_PATH = "/api/body/body-fat"
COMPETITIONS_PATH = "/api/competitions"
COMPETITION_FIELDS = (
    "name",
    "event_date",
    "starts_at_local",
    "sport",
    "priority",
    "distance",
    "target",
    "planned_duration_s",
    "location",
    "course_profile",
    "notes",
)


class AthletePutRoutes:
    """Dispatch athlete writes through their existing domain services.

    The entered weights and body fats are read here too, beside their writes
    (#3252, #3402).
    """

    def __init__(
        self,
        profile_service: Callable[[], ProfileService],
        database_manager: Callable[[], Any],
        zone_service: Callable[[], Any] | None = None,
        skinfold_service: Callable[[], Any] | None = None,
        weight_service: Callable[[], Any] | None = None,
        body_fat_service: Callable[[], Any] | None = None,
        competition_service: Callable[[], Any] | None = None,
    ) -> None:
        self._competition_service = competition_service
        self._weight_service = weight_service
        self._body_fat_service = body_fat_service
        self._profile_service = profile_service
        self._database_manager = database_manager
        self._zone_service = zone_service
        self._skinfold_service = skinfold_service

    def routes(self) -> list[Route]:
        routes = [
            Route("PUT", "/api/profile", Access.SESSION_CSRF, self._handle_profile)
        ]
        if self._zone_service is not None:
            routes += [
                Route("PUT", "/api/zones", Access.SESSION_CSRF, self._save_zones),
                Route(
                    "PUT", "/api/zones/remove", Access.SESSION_CSRF, self._remove_zones
                ),
            ]
        if self._skinfold_service is not None:
            routes += [
                Route("PUT", SKINFOLDS_PATH, Access.SESSION_CSRF, self._save_skinfolds),
                Route(
                    "PUT",
                    f"{SKINFOLDS_PATH}/remove",
                    Access.SESSION_CSRF,
                    self._remove_skinfolds,
                ),
            ]
        if self._weight_service is not None:
            routes += [
                Route("GET", WEIGHT_PATH, Access.SESSION, self._weights),
                Route(
                    "PUT",
                    WEIGHT_PATH,
                    Access.SESSION_CSRF,
                    self._save_weight,
                    replayable=True,
                    # The local save is followed by the Garmin push in this
                    # request; one value per day, so a repeat sets the same value.
                    replay_with_write=False,
                ),
                Route(
                    "PUT",
                    f"{WEIGHT_PATH}/remove",
                    Access.SESSION_CSRF,
                    self._remove_weight,
                ),
            ]
        if self._body_fat_service is not None:
            routes += [
                Route("GET", BODY_FAT_PATH, Access.SESSION, self._body_fats),
                Route(
                    "PUT",
                    BODY_FAT_PATH,
                    Access.SESSION_CSRF,
                    self._save_body_fat,
                    replayable=True,
                    # The local save is followed by the Garmin push in this
                    # request; one value per day, so a repeat sets the same value.
                    replay_with_write=False,
                ),
                Route(
                    "PUT",
                    f"{BODY_FAT_PATH}/remove",
                    Access.SESSION_CSRF,
                    self._remove_body_fat,
                ),
            ]
        if self._competition_service is not None:
            routes += [
                Route(
                    "PUT",
                    COMPETITIONS_PATH,
                    Access.SESSION_CSRF,
                    self._save_competition,
                ),
                Route(
                    "PUT",
                    f"{COMPETITIONS_PATH}/remove",
                    Access.SESSION_CSRF,
                    self._remove_competition,
                ),
            ]
        return routes

    def _save_competition(self, handler: Any, _request: RouteRequest) -> None:
        """Create or update one local competition the athlete entered.

        Request: JSON body with `id` (an existing competition's UUID to update,
        absent to create) and the fields `name`, `event_date` (`YYYY-MM-DD`),
        `starts_at_local` (`YYYY-MM-DDTHH:MM` on that day, or empty), `sport`
        (`Ride`, `VirtualRide`, `Run` or `WeightTraining`), `priority` (`A`, `B`
        or `C`), `distance`, `target`, `planned_duration_s` (whole seconds, 0 to
        604800, or null), `location`, `course_profile` and `notes`. On an update a
        field not sent keeps its value and an empty one clears it; `sport` not
        sent keeps sport, environment and detail. A new competition on the day of
        a stored one of its sport is created anyway: the athlete chose it. Nothing
        is written to Intervals.icu; the competition waits for the athlete's own
        sync.

        Response: 200 `{"status": "created" | "updated", "competition": object,
        "competitions": object[], "change": object | null}`; `change` is the undo
        reference (`id`, `entity_type`, `action`, `after_hash`) for
        `POST /api/change-history/undo`, null when nothing changed.

        Errors: 400 "Jeder Wettkampf benötigt einen Namen."; 400 "Der
        Startzeitpunkt des Wettkampfs muss ein gültiges Datum sein."; 400
        "Startzeit und Wettkampftag passen nicht zusammen: ..."; 400 "Die
        Kategorie für ... muss RACE_A, RACE_B oder RACE_C sein."; 400 "Die
        Wettkampfdauer muss zwischen 0 und 604800 Sekunden liegen."; 400 "Es können
        maximal 20 anstehende Wettkämpfe gespeichert werden."; 400 "Ungültige lokale
        Wettkampf-ID."; 404 "Wettkampf nicht gefunden.".

        The route exists only where the competition service is configured.
        """
        service = self._competition_service
        if service is None:
            raise AppError(404, NOT_FOUND_ERROR)
        payload = handler.read_json()
        if not isinstance(payload, dict):
            raise AppError(
                400, "Die Wettkampfdaten müssen als Objekt übergeben werden."
            )
        arguments = {key: payload[key] for key in COMPETITION_FIELDS if key in payload}
        if payload.get("id"):
            arguments["competition_id"] = payload["id"]
        else:
            arguments["new_because"] = "Vom Athleten angelegt"
        handler.send_json(200, service().save(arguments, with_change=True))

    def _remove_competition(self, handler: Any, _request: RouteRequest) -> None:
        """Delete one local competition.

        Request: JSON body `{"id": "<competition UUID>", "confirmed": true}`.

        Response: 200 `{"status": "deleted", "competition_id": string,
        "remote_sync_pending": boolean, "competitions": object[], "change": object
        | null}`; `remote_sync_pending` is true when a linked Intervals.icu event
        waits for the athlete's next sync to be removed (nothing is deleted there
        now); `change` as for `PUT /api/competitions`.

        Errors: 400 "Das Löschen eines Wettkampfs muss bestätigt werden." unless
        `confirmed` is `true`; 400 "Ungültige lokale Wettkampf-ID."; 404 "Wettkampf
        nicht gefunden.".

        The route exists only where the competition service is configured.
        """
        service = self._competition_service
        if service is None:
            raise AppError(404, NOT_FOUND_ERROR)
        payload = handler.read_json()
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise AppError(400, "Das Löschen eines Wettkampfs muss bestätigt werden.")
        handler.send_json(200, service().delete(payload.get("id"), with_change=True))

    def _body_fats(self, handler: Any, _request: RouteRequest) -> None:
        """The body fats the athlete entered as a percentage, newest first.

        Request: no query, no body.

        Response: 200 with `entries` (every active one, each `date` and `pct`).

        Errors: none beyond the common ones.
        """
        self._send_entry(self._body_fat_service, handler, "read")

    def _save_body_fat(self, handler: Any, _request: RouteRequest) -> None:
        """Save one body fat percentage; one of the same day is replaced.

        Request: JSON body `{"date": "YYYY-MM-DD", "pct": number}`, 2 to 70 %
        (comma decimals allowed). The date must not be in the future. A repeated
        `Idempotency-Key` header (UUID) answers the stored result and writes
        nothing; the same key on another path answers 409.

        Response: 200 as `GET /api/body/body-fat`.

        Errors: 400 "Das Datum fehlt oder ist ungültig."; 400 "Das Datum darf
        nicht in der Zukunft liegen."; 400 "Der Körperfettanteil braucht einen
        Wert in %."; 400 "Der Körperfettanteil muss zwischen 2 und 70 %
        liegen.".
        """
        self._send_entry(self._body_fat_service, handler, "save")

    def _remove_body_fat(self, handler: Any, _request: RouteRequest) -> None:
        """Remove the entered body fat of one day.

        Request: JSON body `{"date": "YYYY-MM-DD", "confirmed": true}`.

        Response: 200 as `GET /api/body/body-fat`.

        Errors: 400 "Das Löschen eines Körperfettwerts muss bestätigt werden."
        unless `confirmed` is `true`; 400 "Das Datum fehlt oder ist ungültig.";
        404 "Für diesen Tag gibt es keinen eingetragenen Körperfettwert.".
        """
        self._send_entry(self._body_fat_service, handler, "remove")

    def _weights(self, handler: Any, _request: RouteRequest) -> None:
        """The weights the athlete entered by hand, newest first.

        Request: no query, no body.

        Response: 200 with `entries` (every active one, each `date` and `kg`).

        Errors: none beyond the common ones.
        """
        self._send_entry(self._weight_service, handler, "read")

    def _save_weight(self, handler: Any, _request: RouteRequest) -> None:
        """Save one weight the athlete entered; one of the same day is replaced.

        Request: JSON body `{"date": "YYYY-MM-DD", "kg": number}`, 30 to 300 kg
        (comma decimals allowed). The date must not be in the future. A repeated
        `Idempotency-Key` header (UUID) answers the stored result and writes
        nothing; the same key on another path answers 409.

        Response: 200 as `GET /api/body/weight`.

        Errors: 400 "Das Datum fehlt oder ist ungültig."; 400 "Das Datum darf
        nicht in der Zukunft liegen."; 400 "Das Gewicht braucht einen Wert in
        kg."; 400 "Das Gewicht muss zwischen 30 und 300 kg liegen.".
        """
        self._send_entry(self._weight_service, handler, "save")

    def _remove_weight(self, handler: Any, _request: RouteRequest) -> None:
        """Remove the entered weight of one day.

        Request: JSON body `{"date": "YYYY-MM-DD", "confirmed": true}`.

        Response: 200 as `GET /api/body/weight`.

        Errors: 400 "Das Löschen eines Gewichts muss bestätigt werden." unless
        `confirmed` is `true`; 400 "Das Datum fehlt oder ist ungültig."; 404
        "Für diesen Tag gibt es kein eingetragenes Gewicht.".
        """
        self._send_entry(self._weight_service, handler, "remove")

    @staticmethod
    def _send_entry(
        service: Callable[[], Any] | None, handler: Any, action: str
    ) -> None:
        if service is None:
            raise AppError(404, NOT_FOUND_ERROR)
        if action == "read":
            handler.send_json(200, service().read())
            return
        payload = handler.read_json()
        handler.send_json(200, getattr(service(), action)(payload))

    def _save_skinfolds(self, handler: Any, _request: RouteRequest) -> None:
        """Save one 3-site skinfold measurement; one of the same day is replaced.

        Request: JSON body `{"date": "YYYY-MM-DD", "folds_mm": {site: number or
        [number, ...]}}` with exactly the sites `chest`, `abdomen`, `thigh` (men)
        or `triceps`, `suprailiac`, `thigh` (women), each one to three readings of
        2 to 80 mm (comma decimals allowed); the site's value is their median
        (#3556).
        The date must not be in the future.

        Response: 200 as `GET /api/body/skinfolds`.

        Errors: 400 "Das Messdatum fehlt oder ist ungültig."; 400 "Das Messdatum
        darf nicht in der Zukunft liegen."; 400 "Bitte genau drei Stellen messen:
        Brust, Bauch, Oberschenkel oder Trizeps, Hüfte, Oberschenkel."; 400 "Jede
        Hautfalte braucht einen Wert in mm."; 400 "Eine Hautfalte muss zwischen 2
        und 80 mm liegen.".
        """
        self._send_skinfolds(handler, "save")

    def _remove_skinfolds(self, handler: Any, _request: RouteRequest) -> None:
        """Remove the skinfold measurement of one day.

        Request: JSON body `{"date": "YYYY-MM-DD", "confirmed": true}`.

        Response: 200 as `GET /api/body/skinfolds`.

        Errors: 400 "Das Löschen einer Messung muss bestätigt werden." unless
        `confirmed` is `true`; 400 "Das Messdatum fehlt oder ist ungültig."; 404
        "Für diesen Tag gibt es keine Hautfaltenmessung.".
        """
        self._send_skinfolds(handler, "remove")

    def _send_skinfolds(self, handler: Any, action: str) -> None:
        if self._skinfold_service is None:
            raise AppError(404, NOT_FOUND_ERROR)
        payload = handler.read_json()
        service = self._skinfold_service()
        handler.send_json(200, getattr(service, action)(payload))

    def _handle_profile(self, handler: Any, _request: RouteRequest) -> None:
        """Patch only the submitted profile fields.

        Request: JSON object of profile fields; fields not sent keep their value,
        unknown keys are ignored, `null` clears a field, values are trimmed to 4000
        characters. Fields: `name`, `goals`, `sports`, `training_background`,
        `typical_weekly_volume`, `availability`, `constraints`, `equipment`,
        `training_preferences` and its `_running`, `_cycling`, `_swimming`,
        `_strength`, `_mobility` variants, `training_model` (`pyramidal`,
        `polarized`, `threshold`, `sweet_spot`), `performance_notes`, `height_cm`
        (50 to 250), `sex`
        (`male`, `female` or empty), `birth_year` (a whole year, 1920 to 2010),
        `coaching_style`, `timezone` (IANA name), `weather_location`,
        `sleep_target_hours` (4 to 12), `fueling_tolerance`, `energy_goal`
        (`lose`, `hold`, `build` or empty) with `energy_goal_kcal` (whole kcal per
        day, 0 to 1500, required for `lose` and `build`, empty for `hold`),
        `learned_everyday_checked` (`YYYY-MM-DD` the athlete took the learned
        everyday term over, or empty; the Coach cannot set it, #3577),
        `first_threshold_hr` (bpm, 60 to 220, stored as entered, #3915; a changed
        value without an origin is `measured`), `first_threshold_origin`
        (`measured`, `estimated` or empty) and `preferred_source_thresholds`,
        `_body`, `_hrv`, `_resting_hr`, `_sleep` (`garmin` or empty). `weight_kg` and
        `body_fat_pct` are no profile fields (#3640): Körper enters them
        (`/api/body/weight`, `/api/body/body-fat`, skinfolds); a sent value is
        ignored and the stored one stays as data.

        Response: 200 with the saved profile (every field as a string).

        Errors: 400 "Die Zeitzone muss eine gültige IANA-Zeitzone sein."; 400
        "Unbekanntes Trainingsmodell; erlaubt: pyramidal, polarized, threshold,
        sweet_spot"; 400 "Unbekannte bevorzugte Quelle; erlaubt: garmin oder leer.";
        400 "Unbekanntes Geschlecht; erlaubt: male, female oder leer."; 400 "Das
        Geburtsjahr muss zwischen 1920 und 2010 liegen."; 400 "Die Größe muss
        zwischen 50 und 250 cm liegen.", "Das Schlafziel muss zwischen 4 und 12
        Stunden liegen."; 400
        "Die Herkunft der ersten Schwelle muss gemessen oder geschätzt sein."; 400
        "Die gemessene erste Schwelle muss zwischen 60 und 220 bpm liegen.".
        """
        payload = handler.read_json()
        profile = self._profile_service()
        with (
            profile.mutation_coordinator,
            self._database_manager().unit_of_work(),
        ):
            result = profile.patch(payload)
        handler.send_json(200, result)

    def _save_zones(self, handler: Any, request: RouteRequest) -> None:
        """Save one local zone configuration.

        Request: JSON body `{"sport": string, "metric": string, "environment":
        string | null, "limits": number[], "percent_limits": number[],
        "threshold": number, "maximum": number, "valid_from": string,
        "from_beginning": boolean}`. `sport` is `ride`, `run` or `swim`; `metric`
        is `heart_rate`, `power` or `pace`; `environment` is `null` or `indoor`.
        Exactly one of `limits` (absolute upper zone limits in bpm, W or m/s) and
        `percent_limits` (percent of the threshold) is required, each 3 to 7
        numbers. `threshold` is required for `heart_rate` and `power`; `maximum` is
        read for `heart_rate` only. `valid_from` (`YYYY-MM-DD`, not in the future)
        defaults to today; `from_beginning: true` stores an undated base value
        instead.

        Response: 200 `{"configuration": object, "change": object | null}`;
        `change` is the undo reference (`id`, `entity_type`, `action`,
        `after_hash`).

        Errors: 400 "Die Zonen sind ungültig: {reason}." with the invalid field
        named in German ("Sportart", "Messgröße", "Umgebung", "Obergrenzen oder
        Prozentgrenzen", "3 bis 7 {field} erforderlich", "Schwelle", "Maximum",
        "Schwellenpuls (LSHF) fehlt", "FTP fehlt", "Gültig ab"); 400 "Die maximale Herzfrequenz darf nicht unter dem
        Schwellenpuls liegen."; 400 "Die Zonenobergrenzen dürfen nicht über der
        maximalen Herzfrequenz liegen."; 400 "Das Datum „Gilt ab“ darf nicht in der
        Zukunft liegen.".

        The route exists only where zone editing is configured.
        """
        self._handle_zones(handler, request)

    def _remove_zones(self, handler: Any, request: RouteRequest) -> None:
        """Remove one local zone configuration.

        Request: JSON body `{"key": string, "confirmed": true}`; `key` is the `key`
        of a local configuration from `GET /api/zones`.

        Response: 200 `{"configuration": null, "change": object | null}`; `change`
        is the undo reference (`id`, `entity_type`, `action`, `after_hash`).

        Errors: 400 "Das Entfernen lokaler Zonen muss bestätigt werden." unless
        `confirmed` is `true`; 404 "Lokale Zonen nicht gefunden.".

        The route exists only where zone editing is configured.
        """
        self._handle_zones(handler, request)

    def _handle_zones(self, handler: Any, request: RouteRequest) -> None:
        if self._zone_service is None:
            raise AppError(404, NOT_FOUND_ERROR)
        payload = handler.read_json()
        with self._database_manager().unit_of_work():
            service = self._zone_service()
            result = (
                service.save(payload)
                if request.path == "/api/zones"
                else service.remove(payload)
            )
        handler.send_json(200, result)
