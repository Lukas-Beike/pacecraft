import json
import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

from support import temp_database

from backend.db.repositories import KeyValueRepository
from backend.db.schema import initialize_schema
from backend.errors import AppError
from backend.weather import competition_forecast as forecast

TODAY = date(2026, 11, 14)
NOW = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
PLACE = "Musterstadt"


def race(race_id, days_ahead, location=PLACE):
    return {
        "id": race_id,
        "event_date": (TODAY + timedelta(days=days_ahead)).isoformat(),
        "location": location,
    }


def provider_result(day):
    return {
        "location": {"name": "Musterstadt", "country": "Deutschland"},
        "forecast": {
            "daily": {
                "time": [day],
                "weather_code": [61],
                "temperature_2m_min": [6.0],
                "temperature_2m_max": [11.0],
                "precipitation_probability_max": [40],
                "wind_speed_10m_max": [14.0],
            }
        },
    }


class CompetitionForecastTests(unittest.TestCase):
    def test_only_located_races_within_the_horizon_are_due(self):
        items = forecast.due(
            [
                race("later", 12),
                race("past", -1),
                race("beyond", 13),
                race("none", 3, location="  "),
                race("first", 0, location="  Muster   stadt "),
            ],
            TODAY,
        )
        self.assertEqual(
            [("first", "Muster stadt"), ("later", PLACE)],
            [(item["id"], item["location"]) for item in items],
        )

    def test_one_fetch_per_place_keeps_the_race_day_row_and_prunes_old_entries(self):
        items = forecast.due([race("a", 3), race("b", 3, PLACE.upper())], TODAY)
        day = items[0]["event_date"]
        fetch = Mock(return_value=provider_result(day))
        cache = {"gone": {"location": PLACE, "event_date": day, "status": "ready"}}
        # An obsolete entry makes the cache stale, so the job runs to prune it.
        self.assertTrue(forecast.stale(cache, [], NOW))
        result = forecast.refreshed_cache(cache, items, fetch, now=NOW)
        fetch.assert_called_once_with(PLACE)
        self.assertEqual({"a", "b"}, set(result))
        self.assertEqual("ready", result["a"]["status"])
        self.assertEqual((6.0, 11.0), (
            result["a"]["day"]["temperature_min"], result["a"]["day"]["temperature_max"]
        ))
        self.assertEqual("Deutschland", result["a"]["country"])
        # A fresh entry is not fetched again.
        fetch.reset_mock()
        self.assertEqual(result, forecast.refreshed_cache(result, items, fetch, now=NOW))
        fetch.assert_not_called()

    def test_failures_mark_not_found_or_keep_an_earlier_forecast_without_the_place(self):
        items = forecast.due([race("a", 2), race("b", 4, "Nirgendwo")], TODAY)
        earlier = {**items[0], "status": "ready", "day": {"date": "x"}, "fetched_at": "2026-11-13T00:00:00+00:00"}

        def fetch(place):
            if place == "Nirgendwo":
                raise AppError(400, "Der Wetterort wurde nicht gefunden.")
            raise TimeoutError()

        with self.assertLogs("intervals_coach", "WARNING") as logs:
            result = forecast.refreshed_cache({"a": earlier}, items, fetch, now=NOW)
        # The earlier forecast stays; the failed attempt waits out the cache
        # time, so the job does not retry every scheduler pass.
        self.assertEqual({**earlier, "attempted_at": NOW.isoformat()}, result["a"])
        self.assertFalse(forecast.stale(result, items, NOW))
        self.assertTrue(forecast.stale(result, items, NOW + timedelta(hours=3)))
        self.assertEqual("not_found", result["b"]["status"])
        self.assertNotIn("Nirgendwo", " ".join(logs.output))
        self.assertNotIn(PLACE, " ".join(logs.output))

    def test_fetches_stop_at_the_place_limit_and_the_time_budget(self):
        items = forecast.due([race(str(n), 1, f"Ort {n}") for n in range(7)], TODAY)
        fetch = Mock(side_effect=lambda place: provider_result(items[0]["event_date"]))
        forecast.refreshed_cache({}, items, fetch, now=NOW)
        self.assertEqual(forecast.MAX_LOCATIONS, fetch.call_count)
        ticks = iter([0.0, 0.0, forecast.BUDGET_SECONDS])
        fetch.reset_mock()
        forecast.refreshed_cache({}, items, fetch, now=NOW, clock=lambda: next(ticks))
        self.assertEqual(1, fetch.call_count)

    def test_race_day_weather_names_each_missing_part(self):
        located = race("a", 3)
        cache = {"a": {**located, "status": "ready", "day": {"icon": "x"}, "name": PLACE}}
        for competition, expected in (
            (race("a", 3, ""), "no_location"),
            (race("a", 20), "not_in_horizon"),
            (race("a", 4), "pending"),
            (located, "ready"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(
                    expected,
                    forecast.race_day_weather(competition, cache, TODAY)["status"],
                )

    def test_store_writes_one_key_and_a_failed_refresh_never_raises(self):
        manager = temp_database(self, "forecast.db")
        with manager.unit_of_work() as db:
            initialize_schema(db)
        key_values = KeyValueRepository(lambda: "now")
        client = Mock()
        client.fetch.return_value = provider_result(race("a", 3)["event_date"])
        weather = forecast.CompetitionWeather(
            manager, key_values, lambda: [race("a", 3)], lambda: client, lambda: NOW, lambda: TODAY
        )
        self.assertTrue(weather.configured())
        self.assertTrue(weather.refresh_due())
        self.assertEqual({"competitions": 1}, weather.refresh())
        self.assertFalse(weather.refresh_due())
        with manager.reader() as db:
            stored = json.loads(key_values.get(db, forecast.CACHE_KEY))
        self.assertEqual(["a"], list(stored))
        self.assertEqual("ready", weather.for_competition(race("a", 3))["status"])
        broken = forecast.CompetitionWeather(
            manager, key_values, Mock(side_effect=RuntimeError()), lambda: client, lambda: NOW, lambda: TODAY
        )
        with self.assertLogs("intervals_coach", "WARNING"):
            self.assertEqual({"competitions": 0, "failed": True}, broken.refresh())


if __name__ == "__main__":
    unittest.main()
