import sqlite3
import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from nutrition_service_support import build_nutrition_services
from support import dispatch_route, temp_database

from backend.coach.change_receipts import change_summary
from backend.coach.read_tools import CoachReadToolService
from backend.errors import AppError
from backend.http_api.nutrition import NutritionPostRoutes
from backend.http_api.route_table import Access
from backend.nutrition.models import meal_type_from_hour, normalize_nutrition_entry
from backend.nutrition.session_fuelling import (
    forecast_temperature,
    read_session_fuelling,
    reattach_entries,
    session_intake,
    sweat_samples,
)

TODAY = date(2026, 11, 14)


def session(session_id, sport="ride", environment="outdoor", water=900, seconds=3600):
    return {
        "id": session_id,
        "sport": sport,
        "environment": environment,
        "water_estimated_ml": water,
        "moving_duration_s": seconds,
    }


SESSIONS = [
    session("a", water=600),
    session("b", water=700),
    session("c", water=800),
    session("hot", water=1400),
    session("indoor", environment="virtual", water=1200),
    session("run", sport="run", water=1000),
    session("no-water", water=None),
]


def query(**values):
    base = {
        "sport": "ride",
        "environment": "outdoor",
        "date": "2026-11-20",
        "duration_s": "9900",
    }
    return {key: [value] for key, value in {**base, **values}.items()}


class SessionFuellingTests(unittest.TestCase):
    def read(self, request, weight=None):
        self.summaries = Mock(return_value=SESSIONS)
        self.temperatures = Mock(
            return_value={"a": 10.0, "b": 12.0, "c": 14.0, "hot": 28.0}
        )
        forecast = [
            {"date": "2026-11-20", "temperature_min": 8.0, "temperature_max": 16.0}
        ]
        return read_session_fuelling(
            self.summaries,
            self.temperatures,
            lambda: forecast,
            lambda: weight,
            lambda: TODAY,
            request,
        )

    def test_samples_keep_the_sport_and_setting_with_a_sweat_loss(self):
        self.assertEqual(
            [("a", 0.6), ("b", 0.7), ("c", 0.8), ("hot", 1.4)],
            sweat_samples(SESSIONS, "ride", indoor=False),
        )
        self.assertEqual(
            [("indoor", 1.2)], sweat_samples(SESSIONS, "ride", indoor=True)
        )
        # The estimate covers stops too, so the elapsed time divides it.
        stopped = {
            **session("stop", water=2400, seconds=9000),
            "elapsed_duration_s": 10800,
        }
        [(session_id, rate)] = sweat_samples([stopped], "ride", indoor=False)
        self.assertEqual("stop", session_id)
        self.assertAlmostEqual(0.8, rate)
        self.assertEqual(
            12.0,
            forecast_temperature(
                [
                    {
                        "date": "2026-11-20",
                        "temperature_min": 8.0,
                        "temperature_max": 16.0,
                    }
                ],
                "2026-11-20",
            ),
        )
        self.assertIsNone(forecast_temperature([], "2026-11-20"))

    def test_outdoor_ride_uses_similar_sessions_weight_and_the_shared_carbohydrates(
        self,
    ):
        plan = self.read(query(), {"value": 70, "source": "Garmin"})
        self.summaries.assert_called_once_with(date(2025, 11, 14), TODAY)
        self.assertEqual(12.0, plan["forecast_c"])
        self.assertEqual(
            {"l_per_h": 0.7, "sessions": 3, "similar": True, "temperature_c": [10, 14]},
            plan["sweat_rate"],
        )
        # 0.7 L/h for 2:45 h = 1.925 L minus 1.4 L (2 % of 70 kg) = 0.525 L (stored as 0.52499...).
        self.assertEqual(
            (0.19, 0.52), (plan["drinking"]["l_per_h"], plan["drinking"]["total_l"])
        )
        self.assertEqual("high", plan["carbohydrates"]["step"])
        self.assertEqual({"kg": 70.0, "source": "Garmin"}, plan["weight"])

    def test_indoor_and_unknown_cases(self):
        indoor = self.read(query(environment="virtual"))
        self.temperatures.assert_not_called()
        self.assertEqual(
            (None, 1.2, 1),
            (
                indoor["forecast_c"],
                indoor["sweat_rate"]["l_per_h"],
                indoor["sweat_rate"]["sessions"],
            ),
        )
        self.assertEqual(
            {"max_l_per_h": 1.2, "sodium_g_per_l": [0.5, 0.7]}, indoor["drinking"]
        )
        swim = self.read(query(sport="swim"))
        self.assertEqual({"status": "not_long"}, swim)
        self.summaries.assert_not_called()
        self.assertEqual({"status": "not_long"}, self.read(query(duration_s="3599")))
        generic = read_session_fuelling(
            lambda start, end: [],
            Mock(return_value={}),
            list,
            lambda: None,
            lambda: TODAY,
            query(sport="run", duration_s="3600"),
        )
        self.assertEqual({"generic_l_per_h": [0.4, 0.8]}, generic["drinking"])
        self.assertIsNone(generic["sweat_rate"])

    def test_invalid_queries_are_rejected(self):
        for bad in (
            query(date="20-11-2026"),
            query(duration_s="lang"),
            query(duration_s="0"),
            query(duration_s=str(25 * 3600)),
            query(sport=""),
        ):
            with self.subTest(bad=bad), self.assertRaises(AppError) as raised:
                self.read(bad)
            self.assertEqual(
                (400, "invalid_session_fuelling"),
                (raised.exception.status, raised.exception.reason),
            )


DAY = "2026-11-14"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
STAMP = "2026-11-14T12:00:00+00:00"


def brief(session_id, start, seconds, sport="ride"):
    return {
        "id": session_id,
        "sport": sport,
        "started_at_local": start,
        "elapsed_duration_s": seconds,
    }


# A: 09:15 for 3:10 h (11400 s) ends 12:25; B: 17:30 for 1 h ends 18:30.
RIDE = brief("act_a", "2026-11-14T09:15:00", 11400)
RUN = brief("act_b", "2026-11-14T17:30:00", 3600, "run")


class SessionIntakeTests(unittest.TestCase):
    """Intake before or during a session: attach rules, reads and the report check."""

    def setUp(self):
        self.manager = temp_database(
            self, "session_intake.db", row_factory=sqlite3.Row, schema=True
        )
        self.sessions = []
        self.service, self.library = build_nutrition_services(
            self.manager, lambda: STAMP, lambda: NOW, activity_summaries=self.summaries
        )

    def summaries(self, db, start, end):
        return [
            s
            for s in self.sessions
            if start.isoformat() <= s["started_at_local"][:10] <= end.isoformat()
        ]

    def store(self, *sessions, state="active", merged_into=None):
        self.sessions.extend(sessions)
        with self.manager.unit_of_work() as db:
            for item in sessions:
                db.execute(
                    "INSERT OR REPLACE INTO activity_sessions(activity_id, primary_provider, "
                    "primary_provider_activity_id, state, merged_into, started_at, "
                    "created_at, updated_at) VALUES (?, 'garmin', ?, ?, ?, ?, ?, ?)",
                    (item["id"], item["id"], state, merged_into, STAMP, STAMP, STAMP),
                )

    def set_state(self, session_id, state, merged_into=None):
        with self.manager.unit_of_work() as db:
            db.execute(
                "UPDATE activity_sessions SET state = ?, merged_into = ? WHERE activity_id = ?",
                (state, merged_into, session_id),
            )

    def log(self, meal_type, time="10:30", **extra):
        payload = {
            "meal_date": DAY,
            "meal_type": meal_type,
            "description": "Gel",
            "kcal": 100,
            "carbs_g": 25,
            **extra,
        }
        if time:
            payload["meal_time"] = time
        else:
            payload["logged_time_known"] = False
        return self.service.log_meal(payload)

    def test_one_session_of_the_day_attaches_whatever_the_time(self):
        self.store(RIDE)
        # 20:00 lies after the ride, yet it is the day's only session.
        self.assertEqual("act_a", self.log("during_session", "20:00")["session_id"])

    def test_several_sessions_during_takes_the_window_containing_the_time(self):
        self.store(RIDE, RUN)
        self.assertEqual("act_a", self.log("during_session", "10:30")["session_id"])
        self.assertEqual("act_b", self.log("during_session", "17:45")["session_id"])
        # A time with its UTC offset is matched on the same local wall clock.
        for meal_type, time in (("during_session", "17:45"), ("pre_session", "14:00")):
            entry = self.log(meal_type, time, logged_at=f"{DAY}T{time}:00+01:00")
            self.assertEqual("act_b", entry["session_id"], meal_type)

    def test_several_sessions_without_a_unique_match_ask_and_never_guess(self):
        self.store(RIDE, RUN)
        # 14:00 lies in no window, an unknown time has none: both stay open.
        for time in ("14:00", None):
            entry = self.log("during_session", time)
            self.assertIsNone(entry["session_id"])
            info = self.service.session_info(entry)
            self.assertEqual(["act_a", "act_b"], [c["id"] for c in info["candidates"]])
            self.assertEqual("Zu welcher Einheit gehört das?", info["question"])

    def test_before_takes_the_first_start_within_four_hours(self):
        self.store(RIDE, RUN)
        # 09:15 - 08:00 = 1:15 h; 17:30 - 14:00 = 3:30 h.
        self.assertEqual("act_a", self.log("pre_session", "08:00")["session_id"])
        self.assertEqual("act_b", self.log("pre_session", "14:00")["session_id"])
        # Exactly four hours (05:15 to 09:15) still attaches, a minute more does not.
        self.assertEqual("act_a", self.log("pre_session", "05:15")["session_id"])
        self.assertIsNone(self.log("pre_session", "05:14")["session_id"])
        # 13:00 to the 17:30 start is 4:30 h.
        self.assertIsNone(self.log("pre_session", "13:00")["session_id"])

    def test_a_midnight_crossing_session_belongs_to_both_days(self):
        # 23:30 for 2 h (7200 s) ends 01:30 on the 14th.
        crossing = brief("act_c", "2026-11-13T23:30:00", 7200)
        self.store(crossing)
        self.assertEqual("act_c", self.log("during_session", "00:45")["session_id"])
        late = self.service.log_meal(
            {
                "meal_date": "2026-11-13",
                "meal_time": "23:45",
                "meal_type": "during_session",
                "description": "Gel",
                "kcal": 100,
            }
        )
        self.assertEqual("act_c", late["session_id"])

    def test_one_session_attaches_without_a_time(self):
        self.store(RIDE)
        self.assertEqual("act_a", self.log("during_session", None)["session_id"])

    def test_hidden_deleted_and_merged_sessions_read_through_but_keep_the_stored_id(
        self,
    ):
        self.store(RIDE)
        entry = self.log("during_session", "10:30")
        self.set_state("act_a", "hidden")
        self.assertIsNone(self.service.session_info(entry)["session"])
        self.assertEqual("act_a", self.service.get_meal(entry["id"])["session_id"])
        self.set_state("act_a", "deleted")
        self.assertIsNone(self.service.session_info(entry)["session"])
        self.assertEqual("act_a", self.service.get_meal(entry["id"])["session_id"])
        # A merge into a survivor rewrites nothing; the read follows it.
        survivor = brief("act_z", "2026-11-14T09:15:00", 11400)
        self.store(survivor)
        self.set_state("act_a", "duplicate", "act_z")
        self.assertEqual("act_z", self.service.session_info(entry)["session"]["id"])
        self.assertEqual("act_a", self.service.get_meal(entry["id"])["session_id"])

    def test_a_named_session_is_stored_through_the_reference_resolver_and_checked(self):
        self.store(RIDE, RUN)
        resolver = Mock()
        resolver.resolve_reference.return_value = SimpleNamespace(activity_id="act_b")
        service, _ = build_nutrition_services(
            self.manager,
            lambda: STAMP,
            lambda: NOW,
            activity_summaries=self.summaries,
            references=resolver,
        )
        entry = service.log_meal(
            {
                "meal_date": DAY,
                "meal_time": "10:30",
                "meal_type": "during_session",
                "description": "Gel",
                "kcal": 100,
                "session_id": "garmin-record-7",
            }
        )
        resolver.resolve_reference.assert_called_once_with("garmin-record-7")
        self.assertEqual("act_b", entry["session_id"])
        resolver.resolve_reference.return_value = SimpleNamespace(
            activity_id="act_nope"
        )
        with self.assertRaises(AppError) as raised:
            service.correct_meal(entry["id"], {"session_id": "act_nope"})
        self.assertEqual("session_not_found", raised.exception.reason)
        self.assertEqual("act_b", service.get_meal(entry["id"])["session_id"])

    def test_a_session_on_another_slot_is_refused(self):
        self.store(RIDE)
        with self.assertRaises(AppError):
            self.log("lunch", "12:30", session_id="act_a")

    def test_leaving_the_slot_clears_the_session_and_moving_the_day_attaches_again(
        self,
    ):
        self.store(RIDE, brief("act_d", "2026-11-15T08:00:00", 3600))
        entry = self.log("during_session", "10:30")
        self.assertEqual("act_a", entry["session_id"])
        moved = self.service.correct_meal(entry["id"], {"meal_date": "2026-11-15"})
        self.assertEqual("act_d", moved["session_id"])
        lunch = self.service.correct_meal(entry["id"], {"meal_type": "lunch"})
        self.assertEqual(("lunch", None), (lunch["meal_type"], lunch["session_id"]))

    def test_switching_between_the_session_slots_keeps_the_chosen_session(self):
        self.store(RIDE, RUN)
        entry = self.log("during_session", "10:30")
        self.service.correct_meal(entry["id"], {"session_id": "act_b"})
        switched = self.service.correct_meal(entry["id"], {"meal_type": "pre_session"})
        self.assertEqual(
            ("pre_session", "act_b"), (switched["meal_type"], switched["session_id"])
        )
        back = self.service.correct_meal(entry["id"], {"meal_type": "during_session"})
        self.assertEqual("act_b", back["session_id"])

    def test_an_edit_keeps_volume_and_session_and_the_choice_is_kept(self):
        self.store(RIDE, RUN)
        entry = self.log("during_session", "14:00", volume_ml="750")
        self.assertEqual((750.0, None), (entry["volume_ml"], entry["session_id"]))
        chosen = self.service.correct_meal(entry["id"], {"session_id": "act_b"})
        self.assertTrue(chosen["session_chosen"])
        edited = self.service.correct_meal(entry["id"], {"description": "Iso"})
        self.assertEqual((750.0, "act_b"), (edited["volume_ml"], edited["session_id"]))
        updated = self.service.update_meal(entry["id"], {"kcal": 120})
        self.assertEqual(
            (750.0, "act_b"), (updated["volume_ml"], updated["session_id"])
        )

    def test_a_refresh_fills_only_open_entries_and_never_overrides_a_choice(self):
        open_entry = self.log("during_session", "10:30")
        cleared = self.log("during_session", "10:40")
        self.assertEqual(
            (None, None), (open_entry["session_id"], cleared["session_id"])
        )
        self.store(RIDE)
        # The athlete states "no session" for the second entry.
        self.service.correct_meal(cleared["id"], {"session_id": None})
        with self.manager.unit_of_work() as db:
            filled = reattach_entries(
                db, [date(2026, 11, 14)], self.summaries_for_hook, STAMP
            )
        self.assertEqual(1, filled)
        self.assertEqual("act_a", self.service.get_meal(open_entry["id"])["session_id"])
        self.assertIsNone(self.service.get_meal(cleared["id"])["session_id"])
        with self.manager.reader() as db:
            row = db.execute(
                "SELECT source FROM change_history WHERE entity_id = ? ORDER BY created_at DESC",
                (open_entry["id"],),
            ).fetchone()
        self.assertEqual("session_refresh", row["source"])
        # Undoing the fill is the athlete's choice: a later refresh leaves it empty.
        with self.manager.unit_of_work() as db:
            self.service.restore_in_transaction(
                db,
                open_entry["id"],
                self.service.get_meal(open_entry["id"]),
                {**self.service.get_meal(open_entry["id"]), "session_id": None},
            )
            refilled = reattach_entries(
                db, [date(2026, 11, 14)], self.summaries_for_hook, STAMP
            )
        self.assertEqual(0, refilled)
        self.assertIsNone(self.service.get_meal(open_entry["id"])["session_id"])
        # Undoing any other edit of an open entry is no choice: it still
        # attaches on the next refresh (Gitar on #3254).
        late = self.log("during_session", "10:50")
        with self.manager.unit_of_work() as db:
            self.service.restore_in_transaction(
                db,
                late["id"],
                {**self.service.get_meal(late["id"]), "session_id": None},
                {
                    **self.service.get_meal(late["id"]),
                    "session_id": None,
                    "description": "Riegel",
                },
            )
            refilled = reattach_entries(
                db, [date(2026, 11, 14)], self.summaries_for_hook, STAMP
            )
        self.assertEqual("act_a", self.service.get_meal(late["id"])["session_id"])

    def summaries_for_hook(self, start, end):
        return self.summaries(None, start, end)

    def test_undo_of_a_change_recorded_before_v32_restores_empty_volume_and_session(
        self,
    ):
        self.store(RIDE)
        entry = self.log("during_session", "10:30", volume_ml=500)
        before_v32 = {"meal_type": "snack", "description": "Gel", "kcal": 100}
        with self.manager.unit_of_work() as db:
            restored = self.service.restore_in_transaction(
                db, entry["id"], self.service.get_meal(entry["id"]), before_v32
            )
        self.assertEqual(
            ("snack", None, None),
            (restored["meal_type"], restored["volume_ml"], restored["session_id"]),
        )

    def test_a_template_keeps_a_classic_slot_and_a_logged_entry_may_use_a_session_slot(
        self,
    ):
        self.store(RIDE)
        with self.assertRaises(AppError):
            self.library.save_template(
                {
                    "name": "Gel",
                    "description": "Gel",
                    "kcal": 100,
                    "meal_type": "during_session",
                }
            )
        template = self.library.save_template(
            {"name": "Gel", "description": "Gel", "kcal": 100}
        )
        entry = self.service.log_template(
            template["id"],
            2,
            meal_date=DAY,
            meal_time="10:30",
            meal_type="during_session",
            volume_ml=500,
        )
        self.assertEqual(
            ("during_session", 500.0, "act_a", 200),
            (
                entry["meal_type"],
                entry["volume_ml"],
                entry["session_id"],
                entry["kcal"],
            ),
        )

    def test_volume_is_validated_and_a_session_slot_is_never_derived_from_the_hour(
        self,
    ):
        for bad in (-1, 0, 10001, "abc"):
            with self.subTest(volume=bad), self.assertRaises(AppError):
                normalize_nutrition_entry(
                    {
                        "meal_date": DAY,
                        "meal_time": "10:00",
                        "meal_type": "snack",
                        "description": "x",
                        "kcal": 1,
                        "volume_ml": bad,
                    }
                )
        absent = normalize_nutrition_entry(
            {
                "meal_date": DAY,
                "meal_time": "10:00",
                "description": "x",
                "kcal": 1,
            }
        )
        self.assertIsNone(absent["volume_ml"])
        self.assertNotIn(
            "session", " ".join(meal_type_from_hour(hour) for hour in range(24))
        )

    def test_the_coachs_day_read_carries_each_sessions_intake_report(self):
        self.store(RIDE)
        self.log("during_session", "10:30", volume_ml=500)
        self.log("pre_session", "08:45")
        tools = CoachReadToolService(
            *(Mock() for _ in range(8)),
            training_change_limit=10,
            nutrition_diary=lambda: self.service,
            nutrition_meal_library=lambda: self.library,
        )
        read = tools.execute("read_nutrition", {"date": DAY})
        self.assertEqual(["act_a"], [s["id"] for s in read["sessions"]])
        (report,) = read["session_intake"]
        # 25 g in the 3:10 h ride; 500 ml; the gel before 09:15 is 30 minutes early.
        self.assertEqual(
            ("act_a", 25.0, 500.0, [{"carbs_g": 25, "minutes_before": 30.0}]),
            (
                report["session_id"],
                report["during"]["carbs_g"],
                report["during"]["volume_ml"],
                report["before"],
            ),
        )

    def test_day_summary_lists_the_sessions_and_each_slot_entrys_session(self):
        self.store(RIDE, RUN)
        attached = self.log("during_session", "10:30")
        open_entry = self.log("during_session", "14:00")
        day = self.service.with_sessions(self.service.sync_status.get_day_summary(DAY))
        self.assertEqual(["act_a", "act_b"], [s["id"] for s in day["sessions"]])
        by_id = {e["id"]: e for e in day["entries"]}
        self.assertEqual("act_a", by_id[attached["id"]]["session"]["id"])
        self.assertIsNone(by_id[open_entry["id"]]["session"])

    def test_the_receipt_names_the_slot_the_session_and_the_ml_or_the_open_question(
        self,
    ):
        self.store(RIDE, RUN)
        for time, volume, expected in (
            ("10:30", 1500, "Während der Einheit · Rad 09:15 · 1.500 ml"),
            (
                "14:00",
                None,
                "Während der Einheit · Zu welcher Einheit gehört das? Rad 09:15 oder Lauf 17:30",
            ),
        ):
            entry = self.log(
                "during_session", time, **({"volume_ml": volume} if volume else {})
            )
            result = {
                "ok": True,
                "entry": entry,
                "session": self.service.session_info(entry),
            }
            receipt = {
                "tool": "save_nutrition_entry",
                "result": result,
                "changes": [{"id": "c"}],
            }
            [label] = change_summary([receipt])["labels"]
            self.assertEqual(f"Ernährungseintrag gespeichert · {expected}", label)

    def test_the_picker_route_is_csrf_protected_and_stores_the_choice(self):
        self.store(RIDE, RUN)
        entry = self.log("during_session", "14:00")
        routes = NutritionPostRoutes(lambda: self.service, lambda: self.library)
        route = next(
            r for r in routes.routes() if r.path == "/api/nutrition/entry/session"
        )
        self.assertEqual(Access.SESSION_CSRF, route.access)
        handler = Mock()
        handler.read_json.return_value = {"id": entry["id"], "session_id": "act_b"}
        dispatch_route(routes, handler, "/api/nutrition/entry/session", method="POST")
        status, body = handler.send_json.call_args.args
        self.assertEqual((200, "act_b"), (status, body["entry"]["session_id"]))
        handler.read_json.return_value = {"id": entry["id"]}
        with self.assertRaises(AppError):
            dispatch_route(
                routes, handler, "/api/nutrition/entry/session", method="POST"
            )


class SessionIntakeReportTests(unittest.TestCase):
    def entry(self, meal_type, carbs, volume=None, at="2026-11-14T10:00:00"):
        return {
            "meal_type": meal_type,
            "carbs_g": carbs,
            "volume_ml": volume,
            "logged_at": at,
            "logged_time_known": True,
        }

    def report(self, seconds, entries):
        ride = brief("act_a", "2026-11-14T09:00:00", seconds)
        return session_intake(ride, entries)["during"]

    def test_exactly_twenty_percent_below_the_lower_bound_is_not_notable(self):
        # 3 h is the 60-120 g/h step: 80 % of 60 is 48 g/h, so 144 g in 3 h.
        exact = self.report(10800, [self.entry("during_session", 144)])
        self.assertEqual((48.0, False), (exact["carbs_g_per_h"], exact["notable"]))
        below = self.report(10800, [self.entry("during_session", 143)])
        self.assertAlmostEqual(47.667, below["carbs_g_per_h"], places=3)
        self.assertTrue(below["notable"])
        self.assertFalse(
            self.report(10800, [self.entry("during_session", 200)])["notable"]
        )

    def test_the_moderate_step_and_the_steps_without_a_gram_rate(self):
        # 2 h is the 30-60 g/h step: 80 % of 30 is 24 g/h, so 47 g in 2 h is notable.
        self.assertTrue(
            self.report(7200, [self.entry("during_session", 47)])["notable"]
        )
        self.assertFalse(
            self.report(7200, [self.entry("during_session", 48)])["notable"]
        )
        # 60 min is the mouth-rinse step; 20 min the "none" step: never checked.
        for seconds in (3600, 1200):
            fact = self.report(seconds, [self.entry("during_session", 0)])
            self.assertFalse(fact["notable"])

    def test_unknown_values_stay_unknown_and_never_become_zero(self):
        fact = self.report(10800, [self.entry("during_session", None, 500)])
        self.assertEqual(
            (None, None, False),
            (fact["carbs_g"], fact["carbs_g_per_h"], fact["notable"]),
        )
        none = self.report(10800, [])
        self.assertEqual(
            (None, None, 0), (none["carbs_g_per_h"], none["ml_per_h"], none["entries"])
        )
        # 500 ml and 1000 ml in 3 h are 500 ml/h; an entry without ml (a gel)
        # counts as 0 ml and is named.
        both = self.report(
            10800,
            [
                self.entry("during_session", 50, 500),
                self.entry("during_session", 50, 1000),
            ],
        )
        self.assertEqual((1500.0, 500.0), (both["volume_ml"], both["ml_per_h"]))
        gap = self.report(
            10800,
            [self.entry("during_session", 50, 500), self.entry("during_session", 50)],
        )
        self.assertEqual((500.0, 1), (gap["volume_ml"], gap["entries_without_volume"]))
        self.assertAlmostEqual(166.667, gap["ml_per_h"], places=3)

    def test_before_grams_come_with_their_minutes_before_the_start(self):
        ride = brief("act_a", "2026-11-14T09:15:00", 10800)
        report = session_intake(
            ride, [self.entry("pre_session", 60, at="2026-11-14T08:45:00")]
        )
        # 08:45 is 30 minutes before the 09:15 start.
        self.assertEqual([{"carbs_g": 60, "minutes_before": 30.0}], report["before"])
        # A time written with its UTC offset is the same local wall clock.
        report = session_intake(
            ride, [self.entry("pre_session", 60, at="2026-11-14T08:45:00+01:00")]
        )
        self.assertEqual(30.0, report["before"][0]["minutes_before"])


if __name__ == "__main__":
    unittest.main()
