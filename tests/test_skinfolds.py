"""Skinfold measurements stored as athlete-entered observations (#3099)."""

import json
import unittest
from datetime import date

from support import temp_database

from backend.athlete.body_composition import Sex
from backend.athlete.skinfolds import SkinfoldService
from backend.errors import AppError

TODAY = date(2026, 11, 15)
MEN = {"chest": 16, "abdomen": 20, "thigh": 17}


class SkinfoldServiceTests(unittest.TestCase):
    def setUp(self):
        self.manager = temp_database(self, schema=True)
        self.profile = {"birth_year": "1986"}
        self.service = SkinfoldService(
            self.manager,
            lambda _db: json.dumps(self.profile),
            now=lambda: "2026-11-15T10:00:00+00:00",
            today=lambda: TODAY,
        )

    def save(self, day, folds):
        return self.service.save({"date": day, "folds_mm": folds})

    def on(self, day):
        with self.manager.reader() as db:
            return self.service.skinfold_on(db, day)

    def test_a_measurement_keeps_its_folds_and_recomputes_body_fat(self):
        # Published example: 40-year-old man, 16/20/17 mm -> 17.1 % (Siri).
        read = self.save("2026-11-10", MEN)
        latest = read["measurements"][0]
        self.assertEqual(
            (latest["date"], latest["sex"], latest["body_fat_pct"]),
            ("2026-11-10", "male", 17.1),
        )
        self.assertEqual([fold["mm"] for fold in latest["folds_mm"]], [16, 20, 17])
        measurement = self.on(date(2026, 11, 12))
        self.assertEqual((measurement.sex, dict(measurement.folds_mm)), (Sex.MALE, MEN))

    def test_without_a_birth_year_the_folds_stay_and_body_fat_is_unknown(self):
        self.profile = {}
        read = self.save("2026-11-10", MEN)
        self.assertIsNone(read["measurements"][0]["body_fat_pct"])
        self.assertFalse(read["birth_year_known"])
        self.profile = {"birth_year": "1986"}
        self.assertEqual(self.on(TODAY).body_fat_pct, 17.1)

    def test_the_measurement_in_effect_is_the_latest_on_or_before_the_day(self):
        self.save("2026-10-01", {"chest": 10, "abdomen": 15, "thigh": 12})
        self.save("2026-11-10", MEN)
        self.assertEqual(self.on(date(2026, 10, 31)).measured_on, date(2026, 10, 1))
        self.assertEqual(self.on(TODAY).measured_on, date(2026, 11, 10))
        self.assertIsNone(self.on(date(2026, 9, 30)))

    def test_a_save_replaces_that_day_and_a_confirmed_remove_retires_it(self):
        self.save("2026-11-10", MEN)
        women = {"triceps": 15, "suprailiac": 12, "thigh": 20}
        read = self.save("2026-11-10", women)
        self.assertEqual(
            [(m["date"], m["sex"]) for m in read["measurements"]],
            [("2026-11-10", "female")],
        )
        with self.assertRaises(AppError):
            self.service.remove({"date": "2026-11-10"})
        read = self.service.remove({"date": "2026-11-10", "confirmed": True})
        self.assertEqual(read["measurements"], [])
        with self.assertRaises(AppError) as missing:
            self.service.remove({"date": "2026-11-10", "confirmed": True})
        self.assertEqual(missing.exception.status, 404)
        self.save("2026-11-10", MEN)  # a retired day can be measured again
        self.assertEqual(self.on(TODAY).sex, Sex.MALE)

    def test_bad_input_is_refused(self):
        for day, folds in (
            ("2026-11-16", MEN),  # future
            ("", MEN),
            ("2026-11-10", {"chest": 16, "abdomen": 20}),
            ("2026-11-10", {"chest": 16, "triceps": 20, "thigh": 17}),
            ("2026-11-10", {**MEN, "chest": 1}),
            ("2026-11-10", {**MEN, "chest": "abc"}),
        ):
            with self.subTest(day=day, folds=folds), self.assertRaises(AppError):
                self.save(day, folds)

    def test_comma_decimals_are_read(self):
        read = self.save("2026-11-10", {"chest": "16,5", "abdomen": 20, "thigh": 17})
        self.assertEqual(read["measurements"][0]["folds_mm"][0]["mm"], 16.5)


if __name__ == "__main__":
    unittest.main()
