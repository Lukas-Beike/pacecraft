"""Skinfold body fat and resting energy from lean mass (#3099)."""

import unittest

from backend.athlete.body_composition import (
    RestingFormula,
    Sex,
    body_density,
    lean_mass_kg,
    resting_energy,
    siri_body_fat_pct,
)


class SkinfoldBodyFatTests(unittest.TestCase):
    def test_men_match_the_published_example(self):
        # Livestrong worked example (Jackson & Pollock 1978): 40-year-old man,
        # chest 16, abdomen 20, thigh 17 mm -> density 1.0597633, Siri 17.1 %.
        density = body_density(Sex.MALE, {"chest": 16, "abdomen": 20, "thigh": 17}, 40)
        self.assertAlmostEqual(density, 1.0597633, places=7)
        self.assertAlmostEqual(siri_body_fat_pct(density), 17.1, places=1)

    def test_women_use_their_own_sites_and_coefficients(self):
        # Jackson, Pollock & Ward (1980): 1.0994921 - 0.0009929*47 + 0.0000023*47^2
        # - 0.0001392*30 = 1.0537305 by hand.
        folds = {"triceps": 15, "suprailiac": 12, "thigh": 20}
        density = body_density(Sex.FEMALE, folds, 30)
        self.assertAlmostEqual(density, 1.0537305, places=7)
        self.assertAlmostEqual(siri_body_fat_pct(density), 19.76, places=2)

    def test_wrong_sites_or_values_are_refused(self):
        for sex, folds, age in (
            (Sex.MALE, {"triceps": 15, "suprailiac": 12, "thigh": 20}, 30),
            (Sex.MALE, {"chest": 16, "abdomen": 20}, 30),
            (Sex.MALE, {"chest": 1, "abdomen": 20, "thigh": 17}, 30),
            (Sex.MALE, {"chest": 16, "abdomen": 95, "thigh": 17}, 30),
            (Sex.FEMALE, {"triceps": 15, "suprailiac": 12, "thigh": 20}, 12),
        ):
            with (
                self.subTest(sex=sex, folds=folds, age=age),
                self.assertRaises(ValueError),
            ):
                body_density(sex, folds, age)


class RestingEnergyTests(unittest.TestCase):
    def test_lean_mass_is_the_fat_free_share(self):
        self.assertAlmostEqual(lean_mass_kg(80, 15), 68.0, places=9)
        for fat in (0, 70):
            with self.subTest(fat=fat), self.assertRaises(ValueError):
                lean_mass_kg(80, fat)

    def test_katch_mcardle_matches_the_published_examples(self):
        # 80 kg at 15 % -> 68 kg lean -> 370 + 21.6*68 = 1838.8 kcal; at 20 % -> 1752.4.
        for fat, expected in ((15, 1838.8), (20, 1752.4)):
            with self.subTest(fat=fat):
                lean = lean_mass_kg(80, fat)
                self.assertAlmostEqual(
                    resting_energy(RestingFormula.KATCH_MCARDLE, lean),
                    expected,
                    places=6,
                )

    def test_cunningham_uses_its_own_coefficients(self):
        # Cunningham 1980: 500 + 22 x LBM; 68 kg -> 1996 kcal.
        self.assertAlmostEqual(resting_energy(RestingFormula.CUNNINGHAM, 68), 1996.0)


if __name__ == "__main__":
    unittest.main()
