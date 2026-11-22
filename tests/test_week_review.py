"""The local week review (#2805): its projection and its route without a model."""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

from server_test_support import ServerTestCase, create_test_session, server

from backend.errors import AppError
from backend.http_api import server as http_server_module
from backend.performance import report_service as report_service_module
from backend.performance.week_review import review_monday, week_review
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


def _week(
    sessions: int, seconds: float, load: float, zones: dict[str, float] | None = None
) -> dict[str, Any]:
    bucket: dict[str, Any] = {
        "sessions": sessions,
        "duration_s": {"value": seconds},
        "training_load": {"value": load},
    }
    if zones is not None:
        bucket["intensity_3zone"] = {"seconds": zones}
    return {"sports": {"all": bucket}}


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
        # The progress read starts with the week before; later weeks are not read.
        progress={
            "week_list": [
                _week(2, 4000, 120),
                _week(3, 5400, 150, {"low": 3600, "moderate": 900, "high": 500}),
                _week(9, 1, 1),
            ]
        },
        calendar=[
            _unit("2026-11-09", "Locker", "completed", 2700, 2800),
            _unit("2026-11-10", "Intervalle", "missed", 3600),
            _unit("2026-11-11", "Kraft", "completed", 1800, 1500),
            _unit("2026-11-14", "Langer Lauf", "planned", 0),
            # A race or note of the calendar carries no compliance: no unit.
            _unit("2026-11-13", "Notiz", None, 0),
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
                "complaints": [{"area": "knee", "side": "left", "severity": 2}],
            },
            {
                "checkin_date": "2026-11-10",
                "motivation": 8,
                "stress": 2,
                "soreness": 4,
                "pain": "Knie",
                "complaints": [
                    {"area": "achilles", "side": "right", "severity": 3},
                    {"area": "knee", "side": "left", "severity": 4},
                ],
            },
            {"checkin_date": "2026-11-12", "illness": "Schnupfen"},
            # The week before: not grouped into this week.
            {
                "checkin_date": "2026-11-05",
                "motivation": 1,
                "complaints": [{"area": "knee", "side": "left", "severity": 9}],
            },
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

    def test_recovery_reads_the_preferred_source(self) -> None:
        week = {"2026-11-09": 50.0, "2026-11-10": 52.0}
        garmin = {
            **_baseline("hrv", "ms", {**week, "2026-11-11": 54.0}),
            "source_provider": "garmin",
        }
        wahoo = {
            **_baseline("hrv", "ms", {day: value + 10 for day, value in week.items()}),
            "source_provider": "wahoo",
        }
        recovery = {
            "baselines": [garmin, wahoo],
            "preferred_providers": {"hrv": "wahoo"},
        }
        review = week_review(
            monday=MONDAY,
            today=THURSDAY,
            progress={},
            calendar=[],
            summaries=[],
            recovery=recovery,
            checkins=[],
            timezone="",
        )
        # Wahoo is preferred although Garmin has one night more.
        self.assertEqual(review["recovery"]["hrv"]["mean"], 61.0)
        self.assertEqual(review["load"]["this_week"]["sessions"], 0)

    def test_pain_told_only_in_words_is_counted_beside_the_structured_days(
        self,
    ) -> None:
        # Gitar on #3806: a structured day without pain text must not hide a text-only day.
        review = week_review(
            monday=MONDAY,
            today=THURSDAY,
            progress={},
            calendar=[],
            summaries=[],
            recovery={},
            checkins=[
                {
                    "checkin_date": "2026-11-09",
                    "complaints": [{"area": "knee", "side": "left", "severity": 3}],
                },
                {"checkin_date": "2026-11-10", "pain": "Rücken"},
            ],
            timezone="",
        )
        self.assertEqual(
            (review["checkins"]["pain_days"], review["checkins"]["pain_text_days"]),
            (1, 1),
        )

    def test_an_unknown_actual_duration_leaves_the_share_out(self) -> None:
        unit = _unit("2026-11-09", "Kraft", "completed", 3600)
        unit["compliance"]["actual_duration"] = None
        review = week_review(
            monday=MONDAY,
            today=THURSDAY,
            progress={},
            calendar=[unit],
            summaries=[],
            recovery={},
            checkins=[],
            timezone="",
        )
        self.assertEqual(
            (review["plan"]["completed"], review["plan"]["duration_share"]), (1, None)
        )

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
                # The pain day has a structured complaint, so none is told only in words.
                "pain_text_days": 0,
                "illness_days": 1,
                # #3766: by area and side, the most days first, with their trend.
                "complaints": [
                    {
                        "area": "knee",
                        "side": "left",
                        "dates": ["2026-11-09", "2026-11-10"],
                        "first_severity": 2,
                        "last_severity": 4,
                        "days": 2,
                    },
                    {
                        "area": "achilles",
                        "side": "right",
                        "dates": ["2026-11-10"],
                        "first_severity": 3,
                        "last_severity": 3,
                        "days": 1,
                    },
                ],
                "scores": {"soreness": 3.0, "stress": 3.0, "motivation": 7.0},
            },
        )


class ReviewMondayTests(unittest.TestCase):
    """The reviewed week: this, the last or a past Monday the calendar can show (#3298)."""

    def test_the_week_is_this_the_last_or_a_shown_past_monday(self) -> None:
        # THURSDAY is 2026-11-12, so this week starts on 2026-11-09; 51 weeks
        # before it is Monday 2025-11-17, whose Sunday lies 361 days back.
        for week, monday in (
            ("current", date(2026, 11, 9)),
            ("previous", date(2026, 11, 2)),
            ("2026-11-09", date(2026, 11, 9)),
            ("2026-10-19", date(2026, 10, 19)),
            ("2025-11-17", date(2025, 11, 17)),
        ):
            with self.subTest(week):
                self.assertEqual(review_monday(week, THURSDAY), monday)

    def test_any_other_week_is_refused(self) -> None:
        for week in (
            "next",
            "",
            "2026-11-16",  # next week
            "2026-11-11",  # a Wednesday
            "2025-11-10",  # 52 weeks back: its check-ins may lie beyond the 365 read
            "2026-W46-1",  # ISO week form
            "20261109",
            "2026-11-09T00:00",
            "2026-02-30",
        ):
            with self.subTest(week), self.assertRaises(AppError) as raised:
                review_monday(week, THURSDAY)
            self.assertEqual(
                (raised.exception.status, raised.exception.message),
                (400, "Ung\u00fcltige Woche."),
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

    def test_the_strength_analysis_route_reads_the_chosen_weeks_locally(self) -> None:
        # Analyse › Kraft; an empty store gives the weeks with 0 sessions on each side.
        status, result = self.get("/api/analysis/strength")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["weeks"]), 13)
        self.assertEqual({week["sessions"] for week in result["weeks"]}, {0})
        self.assertEqual(
            {
                week["upper"] + week["legs"] + week["unknown"]
                for week in result["weeks"]
            },
            {0},
        )
        status, result = self.get("/api/analysis/strength?weeks=4")
        self.assertEqual((status, len(result["weeks"])), (200, 4))
        status, _ = self.get("/api/analysis/strength?weeks=5")
        self.assertEqual(status, 400)

    def test_a_longer_trend_period_reads_its_own_span_on_demand(self) -> None:
        # #3839: 91, 182 or 365 days of history shaped like the performance state's;
        # another span is refused.
        for days in (182, 365):
            with self.subTest(days=days):
                status, result = self.get(f"/api/analysis/trend-history?days={days}")
                self.assertEqual(status, 200)
                start, end = (
                    date.fromisoformat(result["start"]),
                    date.fromisoformat(result["end"]),
                )
                self.assertEqual((end - start).days + 1, days)
                self.assertNotIn("previous_year_days", result)
                history = result["history"]
                self.assertEqual(
                    (history["start"], history["days"]), (result["start"], days)
                )
                for key in (
                    "cycling_ftp_watts",
                    "run_threshold_pace_seconds_per_km",
                    "cycling_vo2max_ml_kg_min",
                    "running_vo2max_ml_kg_min",
                ):
                    points = history["metrics"][key][0]["points"]
                    self.assertEqual(len(points), days, key)
                    self.assertEqual(points[0]["date"], start.isoformat())
                self.assertEqual(list(history["body"]["windows"]), [f"{days}d"])
                self.assertEqual(len(history["load"]["points"]), days)
                self.assertIn("baselines", result["personal_recovery"])
                self.assertEqual(result["training_focus"]["start"], start.isoformat())
        for given in ("14", "84", "364", "30", "x", ""):
            with self.subTest(given):
                status, error = self.get(f"/api/analysis/trend-history?days={given}")
                self.assertEqual(status, 400)
                self.assertEqual(error["error"], "Ungültiger Analysezeitraum.")

    def test_a_past_week_is_reviewed_with_its_recovery_range_of_then(self) -> None:
        _status, current = self.get("/api/analysis/week-review")
        today = date.fromisoformat(current["effective_end"])
        monday = date.fromisoformat(current["week_start"]) - timedelta(weeks=3)
        sunday = monday + timedelta(days=6)
        with patch.object(
            report_service_module,
            "read_recovery_inputs",
            wraps=report_service_module.read_recovery_inputs,
        ) as read:
            status, review = self.get(
                f"/api/analysis/week-review?week={monday.isoformat()}"
            )
            self.get("/api/analysis/week-review")
        self.assertEqual(status, 200)
        self.assertEqual(
            (review["week_start"], review["week_end"], review["effective_end"]),
            (monday.isoformat(), sunday.isoformat(), sunday.isoformat()),
        )
        self.assertEqual(
            [call.kwargs["as_of"] for call in read.call_args_list], [sunday, today]
        )

    def test_an_unknown_week_is_refused(self) -> None:
        _status, current = self.get("/api/analysis/week-review")
        monday = date.fromisoformat(current["week_start"])
        for week in (
            "next",
            monday + timedelta(days=7),
            monday + timedelta(days=1),
            monday - timedelta(weeks=52),
        ):
            with self.subTest(str(week)):
                status, body = self.get(f"/api/analysis/week-review?week={week}")
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "Ungültige Woche.")


if __name__ == "__main__":
    unittest.main()
