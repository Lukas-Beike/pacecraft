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
        # Level walking: ACSM 0.1 mL O2/kg/m x 20.1 J/mL = 2.01 J/kg/m (#3187).
        self.assertAlmostEqual(energy.walking_cost_j_per_kg_m(0.0), 2.01)
        # Minetti's walking shape, scaled by 2.01 / 2.5 = 0.804: at +10 %,
        # 280.5e-5 - 58.7e-4 - 76.8e-3 + 51.9e-2 + 1.96 + 2.5 = 4.899135 J/kg/m,
        # x 0.804 = 3.9389045.
        self.assertAlmostEqual(energy.walking_cost_j_per_kg_m(0.10), 3.9389045)

    def test_flat_10_km_run_for_70_kg(self) -> None:
        # 70 kg x 10,000 m x 3.6 J/kg/m = 2,520,000 J = 602.29 kcal net.
        self.assertAlmostEqual(
            energy.running_net_kcal(70.0, [(10_000.0, 0.0)]), 602.294, places=2
        )

    def test_segments_merge_to_the_200_m_span_and_keep_the_tail(self) -> None:
        # The grade is taken over 200 m, as in grade-adjusted pace (#3187 item 12):
        # 0-200 m climbs 20 m (+10 %), 200-400 m climbs 20 m (+10 %), the last
        # 50 m are kept as flat instead of dropped.
        distance = [0.0, 100.0, 200.0, None, 300.0, 400.0, 450.0]
        altitude = [0.0, 10.0, 20.0, 50.0, 30.0, 40.0, 45.0]
        self.assertEqual(
            energy.graded_segments(distance, altitude),
            [(200.0, 0.1), (200.0, 0.1), (50.0, 0.0)],
        )

    def test_a_run_without_altitude_counts_as_flat(self) -> None:
        # A treadmill run: no altitude stream, so every segment is grade 0.
        distance = [0.0, 100.0, 200.0, 300.0, 400.0]
        segments = energy.graded_segments(distance, [])
        self.assertEqual(segments, [(200.0, 0.0)] * 2)
        # 70 kg x 400 m x 3.6 J/kg/m = 100,800 J = 24.092 kcal.
        self.assertAlmostEqual(
            energy.running_net_kcal(70.0, segments), 24.092, places=3
        )
        # A late fix at 500 m: the leading gap is flat at 500 m, not a climb from 0.
        self.assertEqual(
            energy.graded_segments(distance, [None, None, 500.0, 500.0, 500.0]),
            [(200.0, 0.0)] * 2,
        )
        # Missing samples repeat the last altitude instead of dropping distance:
        # 0-200 m stays at 10 m, 200-400 m rises 2 m (+1 %).
        self.assertEqual(
            energy.graded_segments(distance, [10.0, None, None, 12.0, 12.0]),
            [(200.0, 0.0), (200.0, 0.01)],
        )
        # A run shorter than the span keeps its distance, as flat.
        self.assertEqual(energy.graded_segments([0.0, 150.0], []), [(150.0, 0.0)])


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
            energy.strength_net_kcal(3600.0, 1440.0, energy.STRENGTH_MET_VIGOROUS),
            300.0,
        )

    def test_strength_met_rises_with_the_session_rpe(self) -> None:
        # #3530 (athlete): 3.5 up to RPE 5, +0.5 per point, 6.0 at RPE 10.
        self.assertEqual(
            [3.5, 3.5, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0],
            [energy.strength_met(r) for r in (None, 0, 5, 6, 7, 8, 9, 10)],
        )
        self.assertEqual(4.75, energy.strength_met(7.5))

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
        # #3611: total distance over total steps of the plausible walks (0.30 to
        # 0.55 x 1.80 m = 0.54 to 0.99 m): 4,500 m over 6,000 steps = 0.75 m.
        measured = energy.step_length_m([(3000.0, 4000.0), (1500.0, 2000.0)], 1.80)
        self.assertAlmostEqual(0.75, measured or 0.0)
        # One walk at 2.0 m per step (the former bug's size) does not count at
        # all; it neither enters the sum nor pulls a mean.
        robust = energy.step_length_m([(3000.0, 4000.0), (4000.0, 2000.0)], 1.80)
        self.assertAlmostEqual(0.75, robust or 0.0)
        # No plausible walk: 0.415 x 1.80 m.
        self.assertAlmostEqual(
            0.747, energy.step_length_m([(4000.0, 2000.0)], 1.80) or 0.0
        )
        self.assertAlmostEqual(0.747, energy.step_length_m([], 1.80) or 0.0)
        self.assertIsNone(energy.step_length_m([], None))
        # Without a height the bounds are 0.45 to 1.10 m.
        self.assertTrue(energy.plausible_step(0.75, None))
        self.assertFalse(energy.plausible_step(2.0, None))

    def test_neat_counts_steps_outside_sessions_and_floors(self) -> None:
        # 70 x 8,000 x 0.75 x 2.01 = 844,200 J walking; floors 70 x 9.81 x 3 x
        # 10 = 20,601 J / 0.25 = 82,404 J; total 926,604 J = 221.46 kcal.
        self.assertAlmostEqual(
            energy.neat_net_kcal(70.0, 10_000, 2_000, 0.75, floors=10), 221.46, places=2
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
