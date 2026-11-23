"""Skinfold measurements stored as athlete-entered observations (#3099)."""

import json
import unittest
from datetime import date, timedelta

from support import temp_database

from backend.athlete.body_composition import Sex
from backend.athlete.skinfolds import SkinfoldService
from backend.errors import AppError

TODAY = date(2026, 11, 15)
MEN = {"chest": 16, "abdomen": 20, "thigh": 17}


class SkinfoldServiceTests(unittest.TestCase):
    def setUp(self):
        # The app's connections return rows as dicts, not sqlite3.Row.
        self.manager = temp_database(
            self,
            schema=True,
        )
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

    def test_an_age_outside_the_fitted_range_is_flagged_not_refused(self):
        self.profile = {"birth_year": "1960"}  # 66 in 2026, above the men's 61
        latest = self.save("2026-11-10", MEN)["measurements"][0]
        self.assertIsNotNone(latest["body_fat_pct"])
        self.assertTrue(latest["age_outside_fit"])
        self.profile = {"birth_year": "1986"}
        self.assertFalse(self.on(TODAY).age_outside_fit)

    def test_the_measurement_in_effect_is_the_latest_on_or_before_the_day(self):
        self.save("2026-10-01", {"chest": 10, "abdomen": 15, "thigh": 12})
        self.save("2026-11-10", MEN)
        self.assertEqual(self.on(date(2026, 10, 31)).measured_on, date(2026, 10, 1))
        self.assertEqual(self.on(TODAY).measured_on, date(2026, 11, 10))
        self.assertIsNone(self.on(date(2026, 9, 30)))

    def test_a_day_counts_as_measured_beyond_the_listed_newest_twenty(self):
        # The Coach's replace guard (#3959, Gitar on #4076): the read lists only
        # the newest 20, but an older day still holds its measurement.
        for offset in range(22):
            self.save((date(2026, 10, 1) + timedelta(days=offset)).isoformat(), MEN)
        listed = {item["date"] for item in self.service.read()["measurements"]}
        self.assertNotIn("2026-10-01", listed)
        self.assertTrue(self.service.measured_on("2026-10-01"))
        self.assertFalse(self.service.measured_on("2026-09-30"))

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
            # #3556: one to three readings per site, each within bounds.
            ("2026-11-10", {**MEN, "chest": [16, 17, 18, 19]}),
            ("2026-11-10", {**MEN, "chest": []}),
            ("2026-11-10", {**MEN, "chest": [16, 1]}),
        ):
            with self.subTest(day=day, folds=folds), self.assertRaises(AppError):
                self.save(day, folds)

    def test_comma_decimals_are_read(self):
        read = self.save("2026-11-10", {"chest": "16,5", "abdomen": 20, "thigh": 17})
        self.assertEqual(read["measurements"][0]["folds_mm"][0]["mm"], 16.5)

    def test_three_readings_give_their_median_and_stay_stored(self):
        # #3556: the site's value is the median, the readings stay in details;
        # a single reading reads as before, with itself as its only reading.
        read = self.save("2026-11-10", {**MEN, "chest": [16, "17,5", 17]})
        folds = read["measurements"][0]["folds_mm"]
        self.assertEqual(
            (17.0, [16.0, 17.5, 17.0]), (folds[0]["mm"], folds[0]["readings"])
        )
        self.assertEqual((20, [20]), (folds[1]["mm"], folds[1]["readings"]))
        # The body fat comes from the medians 17/20/17 mm (16/20/17 gave 17.1).
        self.assertEqual(17.4, self.on(TODAY).body_fat_pct)

    def test_repeatability_needs_ten_sites_with_repeats(self):
        # Each day: chest 16/17/18, abdomen 20/20/22, thigh 17/18/19 (medians
        # 17, 20, 18). Within-site squares 2 + 2.667 + 2 = 6.667 on 6 degrees of
        # freedom per day; three days give 9 sites, four give 12.
        repeated = {
            "chest": [16, 17, 18],
            "abdomen": [20, 20, 22],
            "thigh": [17, 18, 19],
        }
        for day in ("2026-10-20", "2026-10-27", "2026-11-03"):
            read = self.save(day, repeated)
        self.assertEqual(
            {"repeat_sites": 9, "needed_sites": 10, "sd_pct": None},
            read["repeatability"],
        )
        read = self.save("2026-11-10", repeated)
        # Pooled SD sqrt(26.667 / 24) = 1.054 mm; at S = 55 mm and 18.2 % body
        # fat, dBF/dS = 495 / D^2 x (b - 2cS) = 0.287 %/mm; x 1.054 x sqrt(3)
        # = 0.523 points for one reading per site, x 0.67 for the median of
        # three (Gitar on #3622) = 0.35 for the newest measurement.
        self.assertEqual(
            {"repeat_sites": 12, "needed_sites": 10, "sd_pct": 0.4},
            read["repeatability"],
        )


if __name__ == "__main__":
    unittest.main()
