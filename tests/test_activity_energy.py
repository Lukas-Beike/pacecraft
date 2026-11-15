"""Own activity energy (M11, #3100) against worked examples computed by hand."""

from __future__ import annotations

import unittest

from backend.performance import activity_energy as energy


class RunningAndWalkingCostTests(unittest.TestCase):
    def test_minetti_running_cost_on_the_flat_and_uphill(self) -> None:
        # Flat: the polynomial's constant, 3.6 J/kg/m (Minetti 2002, Fig. 2).
        self.assertAlmostEqual(energy.running_cost_j_per_kg_m(0.0), 3.6)
        # +10 %: 155.4e-5 - 30.4e-4 - 43.3e-3 + 46.3e-2 + 1.95 + 3.6, by hand.
        self.assertAlmostEqual(energy.running_cost_j_per_kg_m(0.10), 5.968214)
        # Steeper than the measured range is clamped to +-0.45.
        self.assertEqual(
            energy.running_cost_j_per_kg_m(0.6), energy.running_cost_j_per_kg_m(0.45)
        )
        self.assertAlmostEqual(energy.walking_cost_j_per_kg_m(0.0), 2.5)

    def test_flat_10_km_run_for_70_kg(self) -> None:
        # 70 kg x 10,000 m x 3.6 J/kg/m = 2,520,000 J = 602.29 kcal net.
        self.assertAlmostEqual(
            energy.running_net_kcal(70.0, [(10_000.0, 0.0)]), 602.294, places=2
        )

    def test_segments_merge_short_runs_and_skip_gaps(self) -> None:
        distance = [0.0, 10.0, 20.0, None, 30.0, 40.0]
        altitude = [0.0, 1.0, 2.0, 5.0, 3.0, 4.0]
        self.assertEqual(
            energy.graded_segments(distance, altitude),  # type: ignore[arg-type]
            [(20.0, 0.1), (20.0, 0.1)],
        )


class CyclingStrengthAndHeartRateTests(unittest.TestCase):
    def test_one_hour_at_200_w(self) -> None:
        # 720 kJ / 4.184 = 172.084 kcal mechanical, / 0.20 = 860.421 gross,
        # minus 1,680 kcal/day x 1/24 = 70 kcal resting: 790.421 net.
        self.assertAlmostEqual(
            energy.cycling_net_kcal(720.0, 3600.0, 1680.0), 790.421, places=3
        )

    def test_strength_corrected_to_own_resting_rate(self) -> None:
        # 1,440 kcal/day is 60 kcal/h: (3.5 - 1) x 60 and (6 - 1) x 60.
        self.assertAlmostEqual(energy.strength_net_kcal(3600.0, 1440.0), 150.0)
        self.assertAlmostEqual(
            energy.strength_net_kcal(3600.0, 1440.0, vigorous=True), 300.0
        )

    def test_keytel_fallback_for_a_man(self) -> None:
        # -55.0969 + 0.6309 x 150 + 0.1988 x 70 + 0.2017 x 30 = 59.5051 kJ/min;
        # x 60 / 4.184 = 853.32 kcal gross, minus 60 kcal resting = 793.32.
        self.assertAlmostEqual(
            energy.keytel_net_kcal(150, 3600, 70, 30, True, 1440), 793.32, places=2
        )

    def test_flex_hr_recovers_a_known_line_and_needs_enough_segments(self) -> None:
        segments = [
            energy.FlexHrSegment(hr, -5.0 + 0.1 * hr + 0.01 * minutes, minutes)
            for hr in range(110, 170, 3)
            for minutes in (10.0, 40.0)
        ]
        fit = energy.fit_flex_hr(segments)
        assert fit is not None
        self.assertAlmostEqual(fit.intercept, -5.0, places=6)
        self.assertAlmostEqual(fit.per_bpm, 0.1, places=6)
        self.assertAlmostEqual(fit.per_min, 0.01, places=6)
        # 60 min at 140 bpm: (-5 + 14 + 0.01 x 30) x 60 = 558 kcal.
        self.assertAlmostEqual(energy.flex_hr_net_kcal(fit, 140, 3600), 558.0)
        self.assertIsNone(energy.fit_flex_hr(segments[:5]))
        same_hr = [energy.FlexHrSegment(140, 9.0, m) for m in range(20)]
        self.assertIsNone(energy.fit_flex_hr(same_hr))


class DailyTermsTests(unittest.TestCase):
    def test_step_length_from_walks_else_height(self) -> None:
        # 1.4 m/s at 112 steps/min is 0.75 m per step.
        self.assertAlmostEqual(energy.step_length_m([1.4], [112.0], 1.80), 0.75)
        self.assertAlmostEqual(energy.step_length_m([], [], 1.80), 0.747)
        self.assertIsNone(energy.step_length_m([], [], None))

    def test_neat_counts_steps_outside_sessions_and_floors(self) -> None:
        # 70 x 8,000 x 0.75 x 2.5 = 1,050,000 J walking; floors 70 x 9.81 x 3 x
        # 10 = 20,601 J / 0.25 = 82,404 J; total 1,132,404 J = 270.65 kcal.
        self.assertAlmostEqual(
            energy.neat_net_kcal(70.0, 10_000, 2_000, 0.75, floors=10), 270.65, places=2
        )
        self.assertEqual(energy.neat_net_kcal(70.0, 1_000, 2_000, 0.75), 0.0)

    def test_tef_from_macros(self) -> None:
        # 0.25 x 600 + 0.10 x 1,200 + 0.015 x 720 = 280.8 kcal.
        self.assertAlmostEqual(energy.tef_kcal(150, 300, 80), 280.8)

    def test_epoc_is_optional(self) -> None:
        self.assertAlmostEqual(energy.epoc_kcal(500, True), 30.0)
        self.assertEqual(energy.epoc_kcal(500, False), 0.0)


if __name__ == "__main__":
    unittest.main()
