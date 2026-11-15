"""The local week review (#2805): its projection and its route without a model."""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from datetime import date
from typing import Any
from unittest.mock import patch

from server_test_support import ServerTestCase, create_test_session, server

from backend.http_api import server as http_server_module
from backend.performance.week_review import week_review
from backend.providers import http as provider_http

MONDAY = date(2026, 11, 9)
THURSDAY = date(2026, 11, 12)


def _unit(
    day: str, name: str, status: str | None, planned: float, actual: float = 0
) -> dict[str, Any]:
    row: dict[str, Any] = {"id": f"u-{day}", "date": day, "name": name}
    if status is not None:
        row["compliance"] = {
            "status": status,
            "planned_duration": planned,
            "actual_duration": actual,
        }
    return row


def _progress(
    sessions: int, seconds: float, load: float, zones: dict[str, float] | None = None
) -> dict[str, Any]:
    bucket: dict[str, Any] = {
        "sessions": sessions,
        "duration_s": {"value": seconds},
        "training_load": {"value": load},
    }
    if zones is not None:
        bucket["intensity_3zone"] = {"seconds": zones}
    return {"week_list": [{"sports": {"all": bucket}}]}


def _session(day: str, name: str, load: float, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"act-{day}",
        "name": name,
        "sport": "run",
        "started_at_local": f"{day}T07:00:00",
        "training_load": load,
        **extra,
    }


def _baseline(
    metric: str, unit: str, history: dict[str, float], **bounds: float
) -> dict[str, Any]:
    return {
        "metric": metric,
        "unit": unit,
        "source": "Garmin Connect",
        "history": [{"date": day, "value": value} for day, value in history.items()],
        **bounds,
    }


def _review() -> dict[str, Any]:
    return week_review(
        monday=MONDAY,
        today=THURSDAY,
        this_week=_progress(3, 5400, 150, {"low": 3600, "moderate": 900, "high": 500}),
        week_before=_progress(2, 4000, 120),
        calendar=[
            _unit("2026-11-09", "Locker", "completed", 2700, 2800),
            _unit("2026-11-10", "Intervalle", "missed", 3600),
            _unit("2026-11-11", "Kraft", "completed", 1800, 1500),
            _unit("2026-11-14", "Langer Lauf", None, 0),
            {
                **_unit("2026-11-09", "Lauf", "completed", 0),
                "is_completed_activity": True,
            },
            _unit("2026-11-03", "Vorwoche", "completed", 1800, 1800),
        ],
        summaries=[
            _session("2026-11-09", "Locker", 60),
            _session("2026-11-11", "Parkrun", 90, purpose="race"),
            _session("2026-11-12", "Auslaufen", 30),
            _session("2026-11-05", "Vorwoche lang", 200),
        ],
        recovery={
            "baselines": [
                _baseline(
                    "sleep",
                    "h",
                    {
                        "2026-11-03": 7.0,
                        "2026-11-05": 7.0,
                        "2026-11-09": 7.0,
                        "2026-11-10": 7.5,
                        "2026-11-11": 8.0,
                    },
                    lower=7.2,
                    upper=7.8,
                ),
                _baseline("hrv", "ms", {"2026-11-10": 50.0}, lower=55.0, upper=70.0),
                _baseline("resting_hr", "bpm", {"2026-11-02": 45.0}),
            ]
        },
        checkins=[
            {
                "checkin_date": "2026-11-09",
                "motivation": 6,
                "stress": 4,
                "soreness": 2,
                "pain": "keine",
            },
            {
                "checkin_date": "2026-11-10",
                "motivation": 8,
                "stress": 2,
                "soreness": 4,
                "pain": "Knie",
            },
            {"checkin_date": "2026-11-12", "illness": "Schnupfen"},
            {"checkin_date": "2026-11-05", "motivation": 1},
        ],
        timezone="",
    )


class WeekReviewProjectionTests(unittest.TestCase):
    def test_plan_counts_the_weeks_units_and_the_share_of_planned_time(self) -> None:
        plan = _review()["plan"]
        # Units of 9-15 Nov only; the completed activity row is no unit.
        self.assertEqual(
            (plan["planned"], plan["completed"], plan["missed"], plan["open"]),
            (4, 2, 1, 1),
        )
        # (2800 + 1500) s done of (2700 + 3600 + 1800) s planned for the passed units.
        self.assertEqual(plan["duration_share"], 53)
        self.assertEqual(
            plan["missed_units"], [{"date": "2026-11-10", "name": "Intervalle"}]
        )

    def test_load_sets_the_week_against_the_week_before(self) -> None:
        review = _review()
        self.assertEqual(
            (review["week_start"], review["week_end"], review["effective_end"]),
            ("2026-11-09", "2026-11-15", "2026-11-12"),
        )
        load = review["load"]
        self.assertEqual(
            load["this_week"], {"duration_s": 5400, "sessions": 3, "training_load": 150}
        )
        self.assertEqual(
            load["week_before"],
            {"duration_s": 4000, "sessions": 2, "training_load": 120},
        )
        # 3600 : 900 : 500 seconds of 5000.
        self.assertEqual(load["shares"], {"low": 72, "moderate": 18, "high": 10})

    def test_recovery_means_sit_against_the_personal_range(self) -> None:
        recovery = _review()["recovery"]
        sleep = recovery["sleep"]
        # 7.0, 7.5 and 8.0 h this week; 7.0 h on the one night of the week before.
        self.assertEqual(
            (sleep["mean"], sleep["nights"], sleep["week_before"], sleep["position"]),
            (7.5, 3, 7.0, "within"),
        )
        self.assertEqual(recovery["hrv"]["position"], "below")
        # No resting heart rate this week: left out.
        self.assertNotIn("resting_hr", recovery)

    def test_notable_sessions_and_check_ins(self) -> None:
        review = _review()
        self.assertEqual(
            [(item["date"], item["reason"]) for item in review["notable"]],
            [("2026-11-10", "missed"), ("2026-11-11", "highest_load")],
        )
        self.assertTrue(review["notable"][1]["race"])
        # Monday to Thursday passed: 3 of 4 days entered, one pain and one illness day.
        self.assertEqual(
            review["checkins"],
            {
                "entered": 3,
                "days": 4,
                "pain_days": 1,
                "illness_days": 1,
                "scores": {"soreness": 3.0, "stress": 3.0, "motivation": 7.0},
            },
        )


class WeekReviewHttpTests(ServerTestCase):
    """The real route on temporary storage; no model provider is reached."""

    def setUp(self) -> None:
        super().setUp()
        for owner, name in (
            (server.PROVIDER_TRANSPORT.json_http_client(), "request"),
            (provider_http, "urlopen"),
        ):
            guard = patch.object(
                owner,
                name,
                side_effect=AssertionError("Unexpected model or network request"),
            )
            guard.start()
            self.addCleanup(guard.stop)
        patcher = patch.object(
            server.app_config, "security_configuration_error", return_value=None
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        handler_class = server.HTTP_API.request_handler_class()
        handler_class.protocol_version = "HTTP/1.1"
        self.httpd = http_server_module.CoachHTTPServer(("127.0.0.1", 0), handler_class)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.session = create_test_session(server)

    def get(self, path: str) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_port, timeout=30
        )
        connection.request(
            "GET", path, headers={"Cookie": f"ic_session={self.session}"}
        )
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data)

    def test_both_weeks_are_reviewed_without_a_model_request(self) -> None:
        for path in (
            "/api/analysis/week-review",
            "/api/analysis/week-review?week=previous",
        ):
            with self.subTest(path):
                status, review = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(review["method"], "local-week-review-v1")
                self.assertEqual(review["plan"]["planned"], 0)
        _status, current = self.get("/api/analysis/week-review")
        _status, previous = self.get("/api/analysis/week-review?week=previous")
        self.assertEqual(
            date.fromisoformat(current["week_start"]).toordinal()
            - date.fromisoformat(previous["week_start"]).toordinal(),
            7,
        )

    def test_an_unknown_week_is_refused(self) -> None:
        status, body = self.get("/api/analysis/week-review?week=next")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "Ungültige Woche.")


if __name__ == "__main__":
    unittest.main()
