"""Focused unit tests for the activity feedback POST routes."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from support import dispatch_route

from backend.http_api.feedback_post import FeedbackPostRoutes


class FeedbackPostRoutesTests(unittest.TestCase):
    def test_athlete_feedback_post_is_not_registered(self) -> None:
        # Check-ins are saved by the Coach and read from GET /api/feedback.
        handler = SimpleNamespace(read_json=Mock(), send_json=Mock())
        routes = FeedbackPostRoutes()

        self.assertFalse(
            dispatch_route(routes, handler, "/api/feedback", method="POST")
        )
        self.assertFalse(dispatch_route(routes, handler, "/api/other", method="POST"))
        handler.read_json.assert_not_called()
        handler.send_json.assert_not_called()

    def test_infection_hint_answers_use_the_check_in_service(self) -> None:
        # #3821: both answers pass the note's day and return the service's result.
        service = Mock()
        service.report_illness.return_value = {"status": "ok", "change": {"id": "1"}}
        service.dismiss_infection_hint.return_value = {"status": "ok"}
        routes = FeedbackPostRoutes(checkin_service=lambda: service)
        for path, result in (
            ("/api/infection-hint/ill", service.report_illness.return_value),
            ("/api/infection-hint/well", service.dismiss_infection_hint.return_value),
        ):
            handler = SimpleNamespace(
                read_json=Mock(return_value={"date": "2026-11-21"}), send_json=Mock()
            )
            self.assertTrue(dispatch_route(routes, handler, path, method="POST"))
            handler.send_json.assert_called_once_with(200, result)
        service.report_illness.assert_called_once_with("2026-11-21", None)
        service.dismiss_infection_hint.assert_called_once_with("2026-11-21")

    def test_illness_answer_passes_the_symptoms(self) -> None:
        # #3919: the selected symptoms ride with the date.
        service = Mock()
        routes = FeedbackPostRoutes(checkin_service=lambda: service)
        handler = SimpleNamespace(
            read_json=Mock(return_value={"date": "2026-11-21", "symptoms": ["fieber"]}),
            send_json=Mock(),
        )

        self.assertTrue(
            dispatch_route(routes, handler, "/api/infection-hint/ill", method="POST")
        )
        service.report_illness.assert_called_once_with("2026-11-21", ["fieber"])


class ActivityFeedbackPostRouteTests(unittest.TestCase):
    def test_activity_feedback_route_uses_existing_service(self) -> None:
        service = Mock()
        service.save_coach.return_value = {
            "status": "ok",
            "activity_feedback": {"session_rpe": 6},
        }
        handler = SimpleNamespace(
            read_json=Mock(return_value={"session_rpe": 6}), send_json=Mock()
        )
        route = FeedbackPostRoutes(lambda: service)

        self.assertTrue(
            dispatch_route(
                route, handler, "/api/activities/ride-1/feedback", method="POST"
            )
        )
        service.save_coach.assert_called_once_with("ride-1", {"session_rpe": 6})
        handler.send_json.assert_called_once_with(
            200, {"status": "ok", "activity_feedback": {"session_rpe": 6}}
        )

    def test_activity_feedback_save_returns_the_session_load(self) -> None:
        service = Mock()
        service.save_coach.return_value = {"status": "ok"}
        load = Mock(return_value={"value": 300})
        handler = SimpleNamespace(
            read_json=Mock(return_value={"session_rpe": 6}), send_json=Mock()
        )
        route = FeedbackPostRoutes(lambda: service, load)

        self.assertTrue(
            dispatch_route(
                route, handler, "/api/activities/ride-1/feedback", method="POST"
            )
        )
        load.assert_called_once_with("ride-1")
        handler.send_json.assert_called_once_with(
            200, {"status": "ok", "session_rpe_load": {"value": 300}}
        )

    def test_failed_load_read_keeps_the_committed_save_response(self) -> None:
        service = Mock()
        service.save_coach.return_value = {"status": "ok"}
        handler = SimpleNamespace(
            read_json=Mock(return_value={"session_rpe": 6}), send_json=Mock()
        )
        route = FeedbackPostRoutes(
            lambda: service, Mock(side_effect=OSError("synthetic"))
        )

        self.assertTrue(
            dispatch_route(
                route, handler, "/api/activities/ride-1/feedback", method="POST"
            )
        )
        handler.send_json.assert_called_once_with(200, {"status": "ok"})


class ActivityPurposePostRouteTests(unittest.TestCase):
    def post(self, payload: dict) -> Mock:
        service = Mock()
        handler = SimpleNamespace(
            read_json=Mock(return_value=payload), send_json=Mock()
        )
        route = FeedbackPostRoutes(lambda: service)
        self.assertTrue(
            dispatch_route(
                route, handler, "/api/activities/ride-1/feedback", method="POST"
            )
        )
        return service

    def test_marking_a_purpose_needs_a_live_session(self) -> None:
        service = self.post({"purpose": "commute"})

        service.save_coach.assert_called_once_with("ride-1", {"purpose": "commute"})
        service.save.assert_not_called()

    def test_removing_a_purpose_needs_no_live_session(self) -> None:
        for payload in ({"purpose": None}, {"purpose": ""}):
            with self.subTest(payload=payload):
                service = self.post(payload)

                service.save.assert_called_once_with("ride-1", payload)
                service.save_coach.assert_not_called()
