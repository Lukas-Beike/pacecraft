import unittest
from datetime import date
from unittest.mock import Mock

from backend.errors import AppError
from backend.nutrition.session_fuelling import (
    forecast_temperature,
    read_session_fuelling,
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
    base = {"sport": "ride", "environment": "outdoor", "date": "2026-11-20", "duration_s": "9900"}
    return {key: [value] for key, value in {**base, **values}.items()}


class SessionFuellingTests(unittest.TestCase):
    def read(self, request, weight=None):
        self.summaries = Mock(return_value=SESSIONS)
        self.temperatures = Mock(return_value={"a": 10.0, "b": 12.0, "c": 14.0, "hot": 28.0})
        forecast = [{"date": "2026-11-20", "temperature_min": 8.0, "temperature_max": 16.0}]
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
        self.assertEqual([("indoor", 1.2)], sweat_samples(SESSIONS, "ride", indoor=True))
        self.assertEqual(12.0, forecast_temperature(
            [{"date": "2026-11-20", "temperature_min": 8.0, "temperature_max": 16.0}],
            "2026-11-20",
        ))
        self.assertIsNone(forecast_temperature([], "2026-11-20"))

    def test_outdoor_ride_uses_similar_sessions_weight_and_the_shared_carbohydrates(self):
        plan = self.read(query(), {"value": 70, "source": "Garmin"})
        self.summaries.assert_called_once_with(date(2025, 11, 14), TODAY)
        self.assertEqual(12.0, plan["forecast_c"])
        self.assertEqual(
            {"l_per_h": 0.7, "sessions": 3, "similar": True, "temperature_c": [10, 14]},
            plan["sweat_rate"],
        )
        # 0.7 L/h for 2:45 h = 1.925 L minus 1.4 L (2 % of 70 kg) = 0.525 L.
        self.assertEqual((0.19, 0.5), (plan["drinking"]["l_per_h"], plan["drinking"]["total_l"]))
        self.assertEqual("high", plan["carbohydrates"]["step"])
        self.assertEqual({"kg": 70.0, "source": "Garmin"}, plan["weight"])

    def test_indoor_and_unknown_cases(self):
        indoor = self.read(query(environment="virtual"))
        self.temperatures.assert_not_called()
        self.assertEqual((None, 1.2, 1), (
            indoor["forecast_c"], indoor["sweat_rate"]["l_per_h"], indoor["sweat_rate"]["sessions"]
        ))
        self.assertEqual({"max_l_per_h": 1.2, "sodium_g_per_l": [0.5, 0.7]}, indoor["drinking"])
        swim = self.read(query(sport="swim"))
        self.assertEqual({"status": "not_long"}, swim)
        self.summaries.assert_not_called()
        self.assertEqual({"status": "not_long"}, self.read(query(duration_s="3599")))
        generic = read_session_fuelling(
            lambda start, end: [], Mock(return_value={}), list, lambda: None,
            lambda: TODAY, query(sport="run", duration_s="3600"),
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


if __name__ == "__main__":
    unittest.main()
