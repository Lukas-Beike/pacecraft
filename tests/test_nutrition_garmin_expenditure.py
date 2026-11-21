import unittest
from datetime import UTC, date, datetime, timedelta
from typing import Any

from canonical_daily_health_support import MappedDailyReader
from nutrition_service_support import build_nutrition_services
from support import temp_database

from backend.db.repositories import KeyValueRepository
from backend.sync.garmin_snapshot_writer import GarminSnapshotWriter


class NutritionGarminExpenditureTests(unittest.TestCase):
    def setUp(self) -> None:
        manager = temp_database(self, "nutrition.db", schema=True)
        self.manager = manager
        self.fixed_now = "2026-09-24T12:00:00+00:00"
        self.service, _ = build_nutrition_services(
            manager,
            lambda: self.fixed_now,
            lambda: datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
        )

    def store_garmin(self, payload: dict[str, Any]) -> None:
        payload = {"synced_at": "2026-09-24T08:00:00Z", **payload}
        writer = GarminSnapshotWriter(
            KeyValueRepository(lambda: self.fixed_now), lambda: UTC
        )
        with self.manager.unit_of_work() as db:
            writer.write(db, payload)

    def test_day_summary_includes_persisted_garmin_expenditure(self) -> None:
        self.store_garmin(
            {
                "daily_stats": [
                    {
                        "date": "2026-09-24",
                        "activeKilocalories": 600,
                        "bmrKilocalories": 1500,
                        "totalKilocalories": 2100,
                    }
                ],
                "source_freshness": {
                    "daily_stats": {
                        "freshness": "current",
                        "fetched_at": "2026-09-24T08:00:00Z",
                    }
                },
            }
        )

        expenditure = self.service.sync_status.get_day_summary("2026-09-24")[
            "energy_expenditure"
        ]

        self.assertEqual(expenditure["active_kcal"], 600)
        self.assertEqual(expenditure["resting_kcal"], 1500)
        self.assertEqual(expenditure["total_kcal"], 2100)
        self.assertEqual(expenditure["source"], "Garmin Connect")
        self.assertEqual(expenditure["measured_date"], "2026-09-24")
        self.assertEqual(expenditure["freshness"], "current")
        self.assertEqual(expenditure["synced_at"], "2026-09-24T08:00:00Z")
        self.assertTrue(expenditure["provisional"])
        self.assertEqual(expenditure["status"], "measured")

    def test_day_and_week_read_measured_energy_from_observations(self) -> None:
        reader = MappedDailyReader(
            {
                "daily_stats": [
                    {"date": "2026-09-17", "totalKilocalories": 2000},
                    {"date": "2026-09-18", "totalKilocalories": 2100},
                    {
                        "date": "2026-09-24",
                        "totalKilocalories": 2100,
                        "activeKilocalories": 600,
                        "bmrKilocalories": 1500,
                    },
                ]
            }
        )
        service, _ = build_nutrition_services(
            self.manager,
            lambda: self.fixed_now,
            lambda: datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
            observation_reader=lambda: reader,
        )
        # The stored rows keep only the sync status; every value is canonical.
        self.store_garmin(
            {
                "daily_stats": [{"date": "2026-09-24"}],
                "source_freshness": {"daily_stats": {"freshness": "current"}},
            }
        )

        day = service.sync_status.get_day_summary("2026-09-24")["energy_expenditure"]
        week = service.sync_status.get_week_summary()

        self.assertEqual(
            (day["total_kcal"], day["active_kcal"], day["resting_kcal"]),
            (2100, 600, 1500),
        )
        self.assertEqual(day["source"], "Garmin Connect")
        self.assertEqual(day["freshness"], "current")
        self.assertEqual(day["status"], "measured")
        self.assertEqual(
            [entry["energy_expenditure"]["total_kcal"] for entry in week["days"][:3]],
            [2000, 2100, None],
        )

    def test_today_projects_persisted_resting_reference_with_current_active_energy(
        self,
    ) -> None:
        today = {
            "date": "2026-09-24",
            "activeKilocalories": 436,
            "bmrKilocalories": 1659,
            "totalKilocalories": 2095,
        }
        self.store_garmin(
            {
                "daily_stats": [
                    today,
                    {"date": "2026-09-23", "bmrKilocalories": 2200},
                ],
                "source_freshness": {"daily_stats": {"freshness": "current"}},
                "provider_sync": {
                    "read_window": {
                        "start": "2026-09-17",
                        "end": "2026-09-24",
                    }
                },
            }
        )
        reader = MappedDailyReader(
            {
                "daily_stats": [
                    today,
                    {"date": "2026-09-23", "bmrKilocalories": 2200},
                ]
            }
        )
        service, _ = build_nutrition_services(
            self.manager,
            lambda: self.fixed_now,
            lambda: datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
            observation_reader=lambda: reader,
        )

        energy = service.sync_status.get_day_summary("2026-09-24")["energy_expenditure"]

        self.assertEqual(energy["projected_total_kcal"], 2636)
        self.assertEqual(energy["projection_resting_kcal"], 2200)
        self.assertEqual(energy["projection_reference_date"], "2026-09-23")
        self.assertEqual(energy["total_kcal"], 2095)
        self.assertEqual(energy["resting_kcal"], 1659)
        closed = service.sync_status.get_day_summary("2026-09-23")["energy_expenditure"]
        self.assertIsNone(closed["projected_total_kcal"])

    def test_malformed_garmin_snapshot_leaves_local_day_summary_available(self) -> None:
        with self.manager.unit_of_work() as db:
            db.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?)",
                ("garmin_state", "not-json", self.fixed_now),
            )

        summary = self.service.sync_status.get_day_summary("2026-09-24")

        self.assertEqual(summary["date"], "2026-09-24")
        self.assertEqual(summary["energy_expenditure"]["status"], "unavailable")
        self.assertTrue(summary["energy_expenditure"]["provisional"])
        self.assertIsNone(summary["energy_expenditure"]["total_kcal"])

    def test_targeted_nutrition_refresh_freshness_is_scoped_to_requested_day(
        self,
    ) -> None:
        self.store_garmin(
            {
                "daily_stats": [
                    {"calendarDate": "2026-09-23", "totalKilocalories": 2000},
                    {"calendarDate": "2026-09-24", "totalKilocalories": 2100},
                ],
                "source_freshness": {
                    "daily_stats": {"freshness": "current", "fetched_at": "older"}
                },
                "nutrition_refresh": {
                    "daily_stats": {
                        "2026-09-23": {
                            "freshness": "partial",
                            "fetched_at": "targeted-read",
                        }
                    }
                },
            }
        )

        requested = self.service.sync_status.get_day_summary("2026-09-23")[
            "energy_expenditure"
        ]
        untouched = self.service.sync_status.get_day_summary("2026-09-24")[
            "energy_expenditure"
        ]

        self.assertEqual(requested["freshness"], "partial")
        self.assertEqual(requested["fetched_at"], "targeted-read")
        self.assertEqual(untouched["freshness"], "current")
        self.assertEqual(untouched["fetched_at"], "older")

    def test_week_uses_closed_days_and_only_paired_values_for_balance(self) -> None:
        for day, kcal, carbs in [
            ("2026-09-17", 2200, 200),
            ("2026-09-18", 1800, None),
            ("2026-09-19", 1600, 100),
            ("2026-09-24", 9999, 999),
        ]:
            self.service.log_meal(
                {
                    "meal_date": day,
                    "description": "Synthetic meal",
                    "kcal": kcal,
                    "carbs_g": carbs,
                    "protein_g": 100,
                    "fat_g": 50,
                }
            )
        self.store_garmin(
            {
                "daily_stats": [
                    {"date": "2026-09-17", "totalKilocalories": 2000},
                    {"date": "2026-09-18", "totalKilocalories": 2100},
                    {"date": "2026-09-20", "totalKilocalories": 2300},
                    {
                        "date": "2026-09-19",
                        "activeKilocalories": 600,
                        "bmrKilocalories": 1500,
                    },
                    {"date": "2026-09-24", "totalKilocalories": 9999},
                ]
            }
        )
        week = self.service.sync_status.get_week_summary()
        self.assertEqual(
            (week["start_date"], week["end_date"]), ("2026-09-17", "2026-09-23")
        )
        self.assertEqual(len(week["days"]), 7)
        self.assertEqual(week["balance_kcal"], -100)
        self.assertEqual(week["balance_days"], 2)
        self.assertEqual(week["averages"]["intake_kcal"], {"value": 1866.7, "days": 3})
        self.assertEqual(
            week["averages"]["expenditure_kcal"], {"value": 2133.3, "days": 3}
        )
        self.assertEqual(week["averages"]["carbs_g"], {"value": 150, "days": 2})
        self.assertIsNone(week["days"][2]["balance_kcal"])
        self.assertIsNone(week["days"][3]["intake_kcal"])
        self.assertEqual(
            week["days"][0]["energy_expenditure"]["source"], "Garmin Connect"
        )

    def test_the_period_balance_takes_the_own_day_totals(self) -> None:
        # #3600, athlete decision A: the wearable counts 2,000 kcal a day while
        # the weight falls; the own day totals (intake against the scale) are
        # 2,900. 2,400 kcal eaten: the wearable shows a +400 surplus a day, the
        # own data the -500 deficit the weight shows. A day without an own
        # total stays open.
        own_totals: dict[date, float | None] = {
            date(2026, 9, 17) + timedelta(days=n): 2900.0 for n in range(7)
        }
        own_totals[date(2026, 9, 20)] = None
        asked: list[list[date]] = []

        def own(days: list[date]) -> dict[date, float | None]:
            asked.append(days)
            return {day: own_totals.get(day) for day in days}

        service, _ = build_nutrition_services(
            self.manager,
            lambda: self.fixed_now,
            lambda: datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
            own_day_totals=own,
        )
        for n in range(7):
            day = (date(2026, 9, 17) + timedelta(days=n)).isoformat()
            service.log_meal({"meal_date": day, "description": "Synthetic meal",
                              "kcal": 2400, "protein_g": 150, "carbs_g": 250, "fat_g": 80})  # fmt: skip
        self.store_garmin({"daily_stats": [
            {"date": (date(2026, 9, 17) + timedelta(days=n)).isoformat(),
             "totalKilocalories": 2000} for n in range(7)
        ]})  # fmt: skip
        device = self.service.sync_status.get_week_summary()
        self.assertEqual(
            ("device", 7 * 400), (device["expenditure_source"], device["balance_kcal"])
        )
        week = service.sync_status.get_week_summary()
        self.assertEqual(
            [[date(2026, 9, 17) + timedelta(days=n) for n in range(7)]], asked
        )
        self.assertEqual("own", week["expenditure_source"])
        self.assertEqual((6 * -500, 6), (week["balance_kcal"], week["balance_days"]))
        self.assertEqual(
            {"value": 2900.0, "days": 6}, week["averages"]["expenditure_kcal"]
        )
        open_day = week["days"][3]
        self.assertEqual((2400, None, None), (open_day["intake_kcal"],
                         open_day["expenditure_kcal"], open_day["balance_kcal"]))  # fmt: skip
        # The wearable's day stays readable beside it.
        self.assertEqual(2000, open_day["energy_expenditure"]["total_kcal"])

    def test_history_periods_keep_closed_days_and_missing_values(self) -> None:
        for count in (28, 91, 182, 365):
            with self.subTest(days=count):
                summary = self.service.sync_status.get_week_summary(count)
                self.assertEqual(len(summary["days"]), count)
                self.assertEqual(
                    summary["start_date"],
                    (datetime(2026, 9, 24, tzinfo=UTC) - timedelta(days=count))
                    .date()
                    .isoformat(),
                )
                self.assertEqual(summary["end_date"], "2026-09-23")
                self.assertIsNone(summary["balance_kcal"])
                self.assertEqual(summary["balance_days"], 0)
                self.assertTrue(
                    all(day["intake_kcal"] is None for day in summary["days"])
                )

    def test_history_rejects_unsupported_periods(self) -> None:
        from backend.errors import AppError

        invalid_periods: tuple[Any, ...] = (0, 14, 84, 183, True, "28")
        for count in invalid_periods:
            with self.subTest(days=count), self.assertRaises(AppError):
                self.service.sync_status.get_week_summary(count)

    def test_empty_week_does_not_invent_zero_intake_or_macros(self) -> None:
        week = self.service.sync_status.get_week_summary()
        self.assertIsNone(week["balance_kcal"])
        self.assertEqual(week["balance_days"], 0)
        for metric in week["averages"].values():
            self.assertEqual(metric, {"value": None, "days": 0})

    def refresh_daily_stats(self, records: list[dict[str, Any]], day: str) -> None:
        """Run one complete targeted daily-stats read of ``day``, as the sync does."""
        fetched_at = "2026-09-24T10:00:00+00:00"
        writer = GarminSnapshotWriter(
            KeyValueRepository(lambda: self.fixed_now), lambda: UTC
        )
        refreshed = date.fromisoformat(day)
        with self.manager.unit_of_work() as db:
            writer.write_refresh(
                db,
                fetched_at=fetched_at,
                sections={"daily_stats": records},
                view={
                    "nutrition_refresh": {
                        "daily_stats": {
                            day: {"freshness": "current", "fetched_at": fetched_at}
                        }
                    }
                },
                windows={"daily_stats": (refreshed, refreshed)},
                allowed=frozenset({"daily_stats"}),
            )

    def test_an_empty_targeted_read_keeps_the_days_stored_energy(self) -> None:
        # An empty read never empties a stored daily value (#3157, as #3128):
        # the window no longer prunes the stored summary, and a refresh mark
        # never hides a stored Garmin total. Before #3157 this read dropped it.
        self.store_garmin(
            {"daily_stats": [{"date": "2026-09-23", "totalKilocalories": 2000}]}
        )
        before = self.service.sync_status.get_day_summary("2026-09-23")[
            "energy_expenditure"
        ]
        self.assertEqual(before["total_kcal"], 2000)

        self.refresh_daily_stats([], "2026-09-23")

        after = self.service.sync_status.get_day_summary("2026-09-23")[
            "energy_expenditure"
        ]
        self.assertEqual(after["total_kcal"], 2000)
        self.assertEqual(after["status"], "measured")
        week = self.service.sync_status.get_week_summary(7)
        (day,) = [item for item in week["days"] if item["date"] == "2026-09-23"]
        self.assertEqual(day["expenditure_kcal"], 2000)

    def test_expenditure_gaps_count_why_closed_days_lack_a_total(self) -> None:
        later = {"freshness": "current", "fetched_at": "2026-09-24T10:00:00+00:00"}
        self.store_garmin(
            {
                "daily_stats": [
                    {"date": "2026-09-20", "totalKilocalories": 2000},
                    {"date": "2026-09-21", "activeKilocalories": 500},
                    # Stored at 08:00; a refresh mark from 10:00 no longer hides it.
                    {"date": "2026-09-22", "totalKilocalories": 2100},
                    # Today is not closed yet.
                    {"date": "2026-09-24", "totalKilocalories": 900},
                ],
                "nutrition_refresh": {"daily_stats": {"2026-09-22": later}},
            }
        )
        with self.manager.reader() as db:
            gaps = self.service.sync_status.expenditure_gaps(db)
        self.assertEqual(
            gaps,
            {
                "days": 90,
                "with_total": 2,
                "emptied_by_refresh_mark": 0,
                "without_total": 88,
                "with_active_only": 1,
            },
        )

    def test_a_later_refresh_mark_never_hides_a_stored_total(self) -> None:
        # Stored at 08:00; a refresh mark from 10:00 without a newer retrieval
        # (the 2026-11-07 case of #3157): the stored total still shows.
        later = {"freshness": "current", "fetched_at": "2026-09-24T10:00:00+00:00"}
        self.store_garmin(
            {
                "daily_stats": [{"date": "2026-09-22", "totalKilocalories": 2100}],
                "nutrition_refresh": {"daily_stats": {"2026-09-22": later}},
            }
        )
        week = self.service.sync_status.get_week_summary(7)
        (day,) = [item for item in week["days"] if item["date"] == "2026-09-22"]
        self.assertEqual(day["expenditure_kcal"], 2100)

    def test_a_targeted_read_with_the_day_keeps_its_energy(self) -> None:
        self.store_garmin(
            {"daily_stats": [{"date": "2026-09-23", "totalKilocalories": 2000}]}
        )
        self.refresh_daily_stats(
            [{"date": "2026-09-23", "totalKilocalories": 2200}], "2026-09-23"
        )

        after = self.service.sync_status.get_day_summary("2026-09-23")[
            "energy_expenditure"
        ]
        self.assertEqual(after["total_kcal"], 2200)
        self.assertEqual(after["status"], "measured")


if __name__ == "__main__":
    unittest.main()
