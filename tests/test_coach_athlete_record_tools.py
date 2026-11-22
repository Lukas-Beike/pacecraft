"""Focused authorization and dispatch tests for local Coach record tools."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import Mock

from support import temp_database

from backend.activities.feedback import ActivityFeedbackService
from backend.athlete.body_entry import BODY_FAT_ENTRY, WEIGHT_ENTRY, BodyEntryService
from backend.athlete.checkins import CheckinService
from backend.coach.athlete_record_tools import CoachAthleteRecordToolService
from backend.coach.dialogue_action import CoachDialogueActionService
from backend.errors import AppError
from backend.planning.competition_service import CompetitionService


class CoachAthleteRecordToolServiceTests(unittest.TestCase):
    def setUp(self):
        self.checkins = Mock()
        self.feedback = Mock()
        self.competitions = Mock()
        self.nutrition = Mock()
        self.service = CoachAthleteRecordToolService(
            self.checkins,
            self.feedback,
            self.competitions,
            self.nutrition,
            self.nutrition,
        )

    def test_success_routes_all_five_tools_and_preserves_response_shapes(self):
        self.checkins.save_coach.return_value = {"status": "ok"}
        self.feedback.save_coach.return_value = {"status": "ok"}
        self.feedback.save.return_value = {"status": "ok"}
        self.competitions.save.return_value = {"status": "created"}
        self.competitions.delete.return_value = {"status": "deleted"}
        self.nutrition.log_meal.return_value = {"id": "nut-1", "meal_type": "lunch"}
        self.nutrition.delete_meal.return_value = {"status": "deleted", "id": "nut-1"}
        # A meal outside a session slot has no session to report.
        self.nutrition.session_info.return_value = None
        cases = (
            ("save_checkin", {"payload": {"notes": "Synthetic"}}, "local_checkin"),
            (
                "save_activity_feedback",
                {"payload": {"activity_id": "a1", "notes": "Synthetic"}},
                "activity_feedback",
            ),
            ("delete_activity_feedback", {"activity_id": " a1 "}, "activity_feedback"),
            (
                "save_competition",
                {"payload": {"name": "Synthetic"}},
                "local_competitions",
            ),
            ("delete_competition", {"competition_id": " a1 "}, "competition:a1"),
            (
                "save_nutrition_entry",
                {"payload": {"description": "Oatmeal", "kcal": 450}},
                "local_nutrition",
            ),
            ("delete_nutrition_entry", {"id": "nut-1"}, "local_nutrition"),
        )
        expected = {
            "save_checkin": {"ok": True, "status": "ok"},
            "save_activity_feedback": {
                "ok": True,
                "stored_locally": True,
                "status": "ok",
            },
            "delete_activity_feedback": {
                "ok": True,
                "stored_locally": True,
                "status": "ok",
            },
            "save_competition": {"ok": True, "status": "created"},
            "delete_competition": {"ok": True, "status": "deleted"},
            "save_nutrition_entry": {
                "ok": True,
                "entry": {"id": "nut-1", "meal_type": "lunch"},
            },
            "delete_nutrition_entry": {"ok": True, "status": "deleted", "id": "nut-1"},
        }
        for name, arguments, scope in cases:
            with self.subTest(name=name):
                result = self.service.execute(
                    name,
                    arguments,
                    {"operation": name, "authorization_scope": [scope]},
                )
                self.assertEqual(result, expected[name])

        self.assertEqual(self.service.execute("other", {}, {}), None)
        self.checkins.save_coach.assert_called_once_with({"notes": "Synthetic"})
        self.feedback.save_coach.assert_called_once_with("a1", {"notes": "Synthetic"})
        self.feedback.save.assert_called_once_with(
            "a1", {"notes": "", "session_rpe": None, "deviation_reason": ""}
        )
        self.competitions.save.assert_called_once_with({"name": "Synthetic"})
        self.competitions.delete.assert_called_once_with("a1")
        self.nutrition.log_meal.assert_called_once_with(
            {"description": "Oatmeal", "kcal": 450}
        )
        self.nutrition.delete_meal.assert_called_once_with("nut-1")

    def test_the_body_tools_record_through_the_koerper_entries(self):
        # #3640: weight and body fat left the profile; the Coach records them as
        # Körper entries, with the dialogs' Garmin and Wahoo writes.
        manager = temp_database(self, schema=True)
        pushed: list[tuple[str, date]] = []

        def entries(kind):
            return BodyEntryService(
                manager,
                now=lambda: "2026-11-19T10:00:00+00:00",
                today=lambda: date(2026, 11, 19),
                entry=BODY_FAT_ENTRY if kind == "body_fat" else WEIGHT_ENTRY,
                on_change=lambda day: pushed.append((kind, day)) or "scheduled",
            )

        service = CoachAthleteRecordToolService(
            Mock(), Mock(), Mock(), body_entries=entries
        )

        def run(name, arguments, scope="local_body"):
            return service.execute(
                name, arguments, {"operation": name, "authorization_scope": [scope]}
            )

        saved = run(
            "save_body_entry", {"kind": "weight", "date": "2026-11-18", "value": 80.4}
        )
        self.assertEqual(
            {"ok": True, "stored_locally": True, "kind": "weight",
             "date": "2026-11-18", "value": 80.4, "remote": "scheduled"},
            saved,
        )  # fmt: skip
        run(
            "save_body_entry", {"kind": "body_fat", "date": "2026-11-18", "value": 14.5}
        )
        # An existing day's value changes only on an explicit replace.
        with self.assertRaises(AppError) as exists:
            run(
                "save_body_entry", {"kind": "weight", "date": "2026-11-18", "value": 80}
            )
        self.assertEqual((409, "body_entry_exists"),
                         (exists.exception.status, exists.exception.reason))  # fmt: skip
        run(
            "save_body_entry",
            {"kind": "weight", "date": "2026-11-18", "value": 80, "replace": True},
        )
        removed = run("delete_body_entry", {"kind": "body_fat", "date": "2026-11-18"})
        self.assertEqual(("body_fat", "2026-11-18"), (removed["kind"], removed["date"]))
        self.assertEqual(
            [("weight", date(2026, 11, 18)), ("body_fat", date(2026, 11, 18)),
             ("weight", date(2026, 11, 18)), ("body_fat", date(2026, 11, 18))],
            pushed,
        )  # fmt: skip
        # Scope, kind and authorization are checked before any write.
        for name, arguments, scope in (
            ("save_body_entry", {"kind": "weight", "date": "2026-11-17", "value": 80}, "local_profile"),
            ("save_body_entry", {"kind": "height", "date": "2026-11-17", "value": 180}, "local_body"),
        ):  # fmt: skip
            with self.subTest(arguments=arguments), self.assertRaises(AppError):
                run(name, arguments, scope)
        self.assertEqual(4, len(pushed))

    def test_the_zone_tools_refuse_without_scope_or_action(self):
        # #3956: authorization, scope and the action are checked before any
        # zone or threshold service is reached.
        zones, first, second = Mock(), Mock(), Mock()
        service = CoachAthleteRecordToolService(
            Mock(), Mock(), Mock(),
            zones=lambda: zones, first_threshold=lambda: first,
            second_threshold=lambda: second, step_test=Mock(),
        )  # fmt: skip
        for name, arguments, operation, scope, status in (
            ("save_zones", {"action": "save", "sport": "run"}, "save_checkin", "local_zones", 403),
            ("save_zones", {"action": "save", "sport": "run"}, "save_zones", "local_profile", 403),
            ("delete_zones", {"key": "k"}, "save_zones", "local_zones", 403),
            ("save_zones", {"action": "remove", "key": "k"}, "save_zones", "local_zones", 400),
            ("save_zones", {}, "save_zones", "local_zones", 400),
        ):  # fmt: skip
            with self.subTest(name=name, arguments=arguments, operation=operation):
                with self.assertRaises(AppError) as refused:
                    service.execute(
                        name,
                        arguments,
                        {"operation": operation, "authorization_scope": [scope]},
                    )
                self.assertEqual(status, refused.exception.status)
        for mock in (zones, first, second):
            self.assertEqual([], mock.method_calls)
        # The removal is the route's confirmed removal by key.
        zones.remove.return_value = {"configuration": None, "change": None}
        removed = service.execute(
            "delete_zones",
            {"key": "run|heart_rate"},
            {"operation": "delete_zones", "authorization_scope": ["local_zones"]},
        )
        zones.remove.assert_called_once_with(
            {"key": "run|heart_rate", "confirmed": True}
        )
        self.assertEqual("remove", removed["action"])

    def test_a_purpose_marking_reaches_the_feedback_service(self):
        for purpose in ("commute", None):
            with self.subTest(purpose=purpose):
                self.feedback.save_coach.reset_mock()
                self.feedback.save_coach.return_value = {"status": "ok"}
                self.service.execute(
                    "save_activity_feedback",
                    {"payload": {"activity_id": "a1", "purpose": purpose}},
                    {
                        "operation": "save_activity_feedback",
                        "authorization_scope": ["activity_feedback"],
                    },
                )

                self.feedback.save_coach.assert_called_once_with(
                    "a1", {"purpose": purpose}
                )

    def test_session_tools_call_the_correction_service_with_coach_source(self):
        sessions = Mock()
        receipt = {"status": "ok", "change": {"id": "c1"}}
        sessions.confirm.return_value = sessions.split.return_value = receipt
        sessions.merge.return_value = receipt
        service = CoachAthleteRecordToolService(
            self.checkins,
            self.feedback,
            self.competitions,
            session_corrections=sessions,
        )
        cases = (
            (
                "confirm_activity_session",
                {"payload": {"activity_id": "act_1"}},
                sessions.confirm,
                ("act_1",),
            ),
            (
                "split_activity_session",
                {"payload": {"activity_id": "act_1", "member_id": "act_2"}},
                sessions.split,
                ("act_1", "act_2"),
            ),
            (
                "merge_activity_sessions",
                {"payload": {"activity_id": "act_1", "other_id": "act_3"}},
                sessions.merge,
                ("act_1", "act_3"),
            ),
        )
        for name, arguments, method, expected in cases:
            with self.subTest(name=name):
                intent = {
                    "operation": name,
                    "authorization_scope": ["local_activity_session"],
                }
                result = service.execute(name, arguments, intent)
                self.assertEqual(
                    result, {"ok": True, "stored_locally": True, **receipt}
                )
                method.assert_called_once_with(*expected, source="coach")
                with self.assertRaises(AppError) as raised:
                    service.execute(
                        name, arguments, {**intent, "authorization_scope": []}
                    )
                self.assertEqual(raised.exception.reason, "intent_scope_denied")

    def test_operation_and_scope_denials_block_each_tool(self):
        cases = (
            (
                "save_checkin",
                {"payload": {}},
                "local_checkin",
                "Die strukturierte Coach-Autorisierung erlaubt diesen Check-in nicht.",
            ),
            (
                "save_activity_feedback",
                {"payload": {}},
                "activity_feedback",
                "Die strukturierte Coach-Autorisierung erlaubt dieses Aktivitätsfeedback nicht.",
            ),
            (
                "delete_activity_feedback",
                {"activity_id": "a1"},
                "activity_feedback",
                "Die strukturierte Coach-Autorisierung erlaubt diese Feedbackänderung nicht.",
            ),
            (
                "save_competition",
                {"payload": {}},
                "local_competitions",
                "Die strukturierte Coach-Autorisierung erlaubt diese Aktion in diesem Turn nicht.",
            ),
            (
                "delete_competition",
                {"competition_id": "a1"},
                "competition:a1",
                "Die strukturierte Coach-Autorisierung erlaubt diese Aktion in diesem Turn nicht.",
            ),
            (
                "save_nutrition_entry",
                {"payload": {}},
                "local_nutrition",
                "Die strukturierte Coach-Autorisierung erlaubt diesen Ernährungseintrag nicht.",
            ),
            (
                "delete_nutrition_entry",
                {"id": "nut-1"},
                "local_nutrition",
                "Die strukturierte Coach-Autorisierung erlaubt das Löschen dieses Eintrags nicht.",
            ),
        )
        for name, arguments, scope, operation_message in cases:
            with (
                self.subTest(name=name, denied="operation"),
                self.assertRaises(AppError) as raised,
            ):
                self.service.execute(name, arguments, {"authorization_scope": [scope]})
            self.assertEqual(raised.exception.status, 403)
            self.assertEqual(raised.exception.reason, "intent_scope_denied")
            self.assertEqual(raised.exception.message, operation_message)
            with (
                self.subTest(name=name, denied="scope"),
                self.assertRaises(AppError) as raised,
            ):
                self.service.execute(name, arguments, {"operation": name})
            self.assertEqual(raised.exception.status, 403)
            self.assertEqual(raised.exception.reason, "intent_scope_denied")
            self.assertEqual(
                raised.exception.message,
                "Die strukturierte Coach-Autorisierung umfasst dieses Objekt nicht.",
            )
        self.checkins.save_coach.assert_not_called()
        self.feedback.save_coach.assert_not_called()
        self.feedback.save.assert_not_called()
        self.competitions.save.assert_not_called()
        self.competitions.delete.assert_not_called()

    def test_meal_template_tools_require_operation_and_nutrition_scope(self):
        for name in (
            "save_nutrition_template",
            "log_nutrition_template",
            "delete_nutrition_template",
        ):
            for intent in (
                {"operation": name},
                {"authorization_scope": ["local_nutrition"]},
            ):
                with (
                    self.subTest(name=name, intent=intent),
                    self.assertRaises(AppError) as raised,
                ):
                    self.service.execute(name, {}, intent)
                self.assertEqual(raised.exception.status, 403)
        self.nutrition.save_template.assert_not_called()
        self.nutrition.log_template.assert_not_called()
        self.nutrition.delete_template.assert_not_called()

    def test_existing_template_save_requires_its_template_scope(self):
        self.nutrition.save_template.return_value = {"id": "template-1"}
        with self.assertRaises(AppError) as raised:
            self.service.execute(
                "save_nutrition_template",
                {"payload": {"id": "template-1", "name": "Meal"}},
                {
                    "operation": "save_nutrition_template",
                    "authorization_scope": ["local_nutrition"],
                },
            )
        self.assertEqual(raised.exception.reason, "intent_scope_denied")
        self.nutrition.save_template.assert_not_called()

        result = self.service.execute(
            "save_nutrition_template",
            {"payload": {"id": "template-1", "name": "Meal"}},
            {
                "operation": "save_nutrition_template",
                "authorization_scope": [
                    "local_nutrition",
                    "nutrition_template:template-1",
                ],
            },
        )
        self.assertEqual(result, {"ok": True, "template": {"id": "template-1"}})
        self.nutrition.save_template.assert_called_once_with(
            {"id": "template-1", "name": "Meal"}
        )

    def test_template_update_needs_only_its_template_scope(self):
        self.nutrition.save_template.return_value = {"id": "template-1"}
        intent = {
            "operation": "save_nutrition_template",
            "authorization_scope": ["nutrition_template:template-1"],
        }
        result = self.service.execute(
            "save_nutrition_template",
            {"payload": {"id": "template-1", "name": "Meal"}},
            intent,
        )
        self.assertEqual(result, {"ok": True, "template": {"id": "template-1"}})
        CoachDialogueActionService._apply_nutrition_object_scope(
            "save_nutrition_template",
            {"payload": {"id": "template-1"}},
            intent,
        )
        with self.assertRaises(AppError):
            self.service.execute(
                "save_nutrition_template",
                {"payload": {"name": "Meal"}},
                intent,
            )

    def test_new_template_save_stays_within_local_nutrition_scope(self):
        self.nutrition.save_template.return_value = {"id": "template-2"}
        self.service.execute(
            "save_nutrition_template",
            {"payload": {"name": "Meal"}},
            {
                "operation": "save_nutrition_template",
                "authorization_scope": ["local_nutrition"],
            },
        )
        self.nutrition.save_template.assert_called_once_with(
            {"name": "Meal", "source": "coach"}
        )

    def test_dialogue_template_save_with_id_requires_template_token(self):
        with self.assertRaises(AppError) as raised:
            CoachDialogueActionService._apply_nutrition_object_scope(
                "save_nutrition_template",
                {"payload": {"id": "template-1"}},
                {"authorization_scope": ["local_nutrition"]},
            )
        self.assertEqual(raised.exception.reason, "intent_scope_denied")
        CoachDialogueActionService._apply_nutrition_object_scope(
            "save_nutrition_template",
            {"payload": {"id": "template-1"}},
            {
                "authorization_scope": [
                    "local_nutrition",
                    "nutrition_template:template-1",
                ]
            },
        )
        CoachDialogueActionService._apply_nutrition_object_scope(
            "save_nutrition_template",
            {"payload": {"name": "Meal"}},
            {"authorization_scope": ["local_nutrition"]},
        )

    def test_product_scope_only_authorizes_product_save(self):
        self.nutrition.product_catalog.save_product.return_value = {"id": "product-1"}
        result = self.service.execute(
            "save_nutrition_product",
            {"payload": {"name": "Synthetic product"}},
            {
                "operation": "save_nutrition_product",
                "authorization_scope": ["local_nutrition_product"],
            },
        )
        self.assertEqual(result, {"ok": True, "product": {"id": "product-1"}})
        self.nutrition.product_catalog.save_product.assert_called_once_with(
            {"name": "Synthetic product"}
        )

        for name, arguments in (
            ("save_nutrition_entry", {"payload": {"description": "Lunch"}}),
            ("update_nutrition_entry", {"id": "entry-1", "changes": {}}),
            ("delete_nutrition_entry", {"id": "entry-1"}),
            ("save_nutrition_template", {"payload": {"name": "Meal"}}),
            ("log_nutrition_template", {"id": "template-1"}),
            ("delete_nutrition_template", {"id": "template-1"}),
        ):
            with self.subTest(name=name), self.assertRaises(AppError) as raised:
                self.service.execute(
                    name,
                    arguments,
                    {
                        "operation": name,
                        "authorization_scope": ["local_nutrition_product"],
                    },
                )
            self.assertEqual(raised.exception.reason, "intent_scope_denied")

    def test_dialogue_action_keeps_product_scope_operation_specific(self):
        action = {"authorization_scope": ["local_nutrition_product"]}
        CoachDialogueActionService._apply_nutrition_object_scope(
            "save_nutrition_product", {"payload": {}}, action
        )
        for name in (
            "save_nutrition_template",
            "delete_nutrition_template",
            "log_nutrition_template",
            "save_nutrition_entry",
            "update_nutrition_entry",
            "delete_nutrition_entry",
        ):
            with self.subTest(name=name), self.assertRaises(AppError) as raised:
                CoachDialogueActionService._apply_nutrition_object_scope(
                    name, {}, action
                )
            self.assertEqual(raised.exception.reason, "intent_scope_denied")

    def test_invalid_payloads_keep_shared_and_domain_validation(self):
        invalid_saves = (
            ("save_checkin", "local_checkin"),
            ("save_activity_feedback", "activity_feedback"),
            ("save_competition", "local_competitions"),
            ("save_nutrition_entry", "local_nutrition"),
        )
        for name, scope in invalid_saves:
            with self.subTest(name=name), self.assertRaises(AppError) as raised:
                self.service.execute(
                    name,
                    {"payload": []},
                    {"operation": name, "authorization_scope": [scope]},
                )
            self.assertEqual(
                (raised.exception.status, raised.exception.reason),
                (400, "invalid_action"),
            )
            self.assertEqual(raised.exception.message, "Diese Aktion benötigt payload.")

        manager = Mock()
        feedback = ActivityFeedbackService(manager, Mock())
        checkins = CheckinService(manager, Mock(), date.today)
        competitions = CompetitionService(
            manager, Mock(), lambda: "2026-09-23T00:00:00Z", date.today
        )
        service = CoachAthleteRecordToolService(checkins, feedback, competitions)
        invalid_ids = (
            ("delete_activity_feedback", {"activity_id": ""}, "activity_feedback"),
            ("delete_competition", {"competition_id": ""}, "competition:"),
        )
        for name, arguments, scope in invalid_ids:
            with self.subTest(name=name), self.assertRaises(AppError) as raised:
                service.execute(
                    name, arguments, {"operation": name, "authorization_scope": [scope]}
                )
            self.assertEqual(raised.exception.status, 400)
            self.assertEqual(
                raised.exception.message,
                "Die Aktivität konnte nicht eindeutig zugeordnet werden."
                if name == "delete_activity_feedback"
                else "Eine lokale Wettkampf-ID ist erforderlich.",
            )


if __name__ == "__main__":
    unittest.main()
