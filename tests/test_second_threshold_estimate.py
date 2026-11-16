"""Local LT2 estimates (#3273): DFA-a1 at 0.5 and critical speed or power.

The DFA series are built so their crossing is known by design: alpha1 falls by
0.0125 per bpm from 0.75 at 150 bpm, so it reaches 0.5 at exactly 170 bpm. The
critical speed and power cases take their expected values from the M4 card's
reference cases (CS 4 m/s, CP 250 W).
"""

import unittest
from datetime import date, timedelta
from unittest.mock import Mock

from support import dispatch_route

from backend.canonical.activity_detail import ActivityDfaAlpha1, DfaWindow
from backend.canonical.vocabulary import DfaQuality
from backend.coach.context import CoachStructuredContextService
from backend.http_api.athlete_get import AthleteGetRoutes
from backend.performance import second_threshold_estimate as lt2
from backend.performance.second_threshold_service import SecondThresholdService
from tests.test_first_threshold_estimate import add_run, open_database

TODAY = date(2026, 11, 16)


def series(low=150.0, high=180.0, step=0.5, *, start=0.0, stride=30.0):
    """Windows whose alpha1 reaches 0.5 at 170 bpm by design."""
    windows, rate, clock = [], low, start
    while rate <= high:
        windows.append(
            DfaWindow(
                start_s=clock,
                heart_rate_bpm=rate,
                alpha1=round(0.75 - (rate - 150.0) * 0.0125, 4),
                artifact_share=0.0,
            )
        )
        rate += step
        clock += stride
    return tuple(windows)


def stored(windows, *, method=3, estimate=150.0, share=0.01):
    return ActivityDfaAlpha1(
        estimate_bpm=estimate,
        windows=max(len(windows), 1),
        artifact_share=share,
        quality=None if estimate is None else DfaQuality.GOOD,
        method=method,
        series=windows,
    )


class SessionCrossingTests(unittest.TestCase):
    def test_the_crossing_is_the_heart_rate_where_alpha1_reaches_one_half(self) -> None:
        found = lt2.session_second_threshold(stored(series()), TODAY)
        assert found is not None
        self.assertAlmostEqual(found.bpm, 170.0, delta=0.1)
        self.assertIs(found.quality, DfaQuality.GOOD)

    def test_easy_windows_above_075_do_not_move_the_crossing(self) -> None:
        # A long easy cool-down with alpha1 1.2 would pull one line over all windows.
        hard = series()
        easy = tuple(
            DfaWindow(
                start_s=hard[-1].start_s + 30.0 * (i + 1),
                heart_rate_bpm=120.0 + i * 0.2,
                alpha1=1.2,
                artifact_share=0.0,
            )
            for i in range(60)
        )
        found = lt2.session_second_threshold(stored(hard + easy), TODAY)
        assert found is not None
        self.assertAlmostEqual(found.bpm, 170.0, delta=0.1)

    def test_a_row_without_the_series_is_not_evaluated(self) -> None:
        self.assertIsNone(lt2.session_second_threshold(stored((), method=2), TODAY))

    def test_windows_that_never_reach_040_give_no_crossing(self) -> None:
        # Up to 166 bpm alpha1 stays at 0.55 or above.
        self.assertIsNone(
            lt2.session_second_threshold(stored(series(high=166.0)), TODAY)
        )

    def test_a_crossing_below_the_runs_own_first_threshold_is_refused(self) -> None:
        self.assertIsNone(
            lt2.session_second_threshold(stored(series(), estimate=172.0), TODAY)
        )

    def test_low_alpha1_only_in_the_last_quarter_is_drift(self) -> None:
        # The run holds 150 to 168 bpm for its first 75 %, then drifts past 170.
        windows = list(series(high=168.0))
        end = windows[-1].start_s / 0.75
        windows += [
            DfaWindow(
                start_s=end - 30.0 * i,
                heart_rate_bpm=171.0 + i,
                alpha1=0.48 - i * 0.02,
                artifact_share=0.0,
            )
            for i in range(4)
        ]
        self.assertIsNone(lt2.session_second_threshold(stored(tuple(windows)), TODAY))

    def test_artifacts_lower_the_quality(self) -> None:
        found = lt2.session_second_threshold(stored(series(), share=0.04), TODAY)
        assert found is not None
        self.assertIs(found.quality, DfaQuality.LOW)


class AthleteLevelTests(unittest.TestCase):
    def runs(self, count, *, start=1):
        return [
            (TODAY - timedelta(days=start + i), stored(series())) for i in range(count)
        ]

    def test_three_good_runs_give_their_median_in_bpm(self) -> None:
        result = lt2.dfa_second_threshold(self.runs(3), TODAY)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["heart_rate_bpm"], 170)
        self.assertEqual(result["sessions"], 3)
        self.assertEqual(result["quality"], "low")  # five runs make it good
        self.assertEqual(
            lt2.dfa_second_threshold(self.runs(5), TODAY)["quality"], "good"
        )

    def test_two_runs_are_unknown_and_name_runs_not_yet_evaluated(self) -> None:
        older = [(TODAY - timedelta(days=9), stored((), method=2))]
        result = lt2.dfa_second_threshold(self.runs(2) + older, TODAY)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(
            result["reason"],
            "nur 2 von 3 nötigen Läufen mit Brustgurt in 60 Tagen (1 noch nicht ausgewertet)",
        )
        self.assertNotIn("heart_rate_bpm", result)

    def test_runs_outside_60_days_do_not_count(self) -> None:
        result = lt2.dfa_second_threshold(self.runs(3, start=58), TODAY)
        self.assertEqual(result["status"], "unknown")

    def test_a_median_not_above_the_first_threshold_is_unknown(self) -> None:
        result = lt2.dfa_second_threshold(
            self.runs(3), TODAY, first_threshold_bpm=171.0
        )
        self.assertEqual(result["status"], "unknown")


def fit(sport, environment, value, *, residual=0.01, loo=()):
    key = "cs_mps" if sport == "run" else "cp_watts"
    return {
        "sport": sport,
        "environment": environment,
        "status": "ok",
        key: value,
        "efforts": [{"duration_seconds": s} for s in (180, 420, 720)],
        "residuals": [{"duration_seconds": 180, "relative": residual}],
        "sensitivity": {"leave_one_out": [{key: item} for item in loo]},
    }


class CriticalTests(unittest.TestCase):
    def test_critical_speed_is_shown_as_pace(self) -> None:
        # 4 m/s is 250 s per km.
        result = lt2.critical_second_threshold(
            [fit("run", "outdoor", 4.0, loo=(4.05,))], "run"
        )
        self.assertEqual(result["pace_seconds_per_km"], 250)
        self.assertEqual(result["quality"], "good")
        self.assertEqual(result["window_days"], 42)
        self.assertNotIn("heart_rate_bpm", result)

    def test_rides_take_the_outdoor_fit_before_the_indoor_one(self) -> None:
        fits = [fit("ride", "indoor", 240.0), fit("ride", "outdoor", 250.0)]
        self.assertEqual(lt2.critical_second_threshold(fits, "ride")["power_w"], 250)

    def test_quality_needs_close_efforts_and_a_stable_leave_one_out(self) -> None:
        for kwargs in ({"residual": 0.016}, {"loo": (258.0,)}):
            with self.subTest(**kwargs):
                result = lt2.critical_second_threshold(
                    [fit("ride", "outdoor", 250.0, **kwargs)], "ride"
                )
                self.assertEqual(result["quality"], "low")

    def test_a_withheld_fit_is_unknown_with_its_reason(self) -> None:
        withheld = {
            "sport": "run",
            "environment": "outdoor",
            "status": "insufficient_data",
            "reason": "Keine unabhängigen 3, 7, 12-Minuten-Belastungen in 42 Tagen.",
        }
        result = lt2.critical_second_threshold([withheld], "run")
        self.assertEqual(
            result, {"status": "unknown", "reason": withheld["reason"], "sessions": 0}
        )


class ServiceTests(unittest.TestCase):
    manager: object
    counter: int

    def setUp(self) -> None:
        open_database(self)

    def service(self, records=(), first_threshold_pct=None, lthr=160.0):
        profile = Mock()
        profile.get.return_value = (
            {}
            if first_threshold_pct is None
            else {"first_threshold_pct": first_threshold_pct}
        )
        return SecondThresholdService(
            self.manager,
            profile,
            lambda: lambda _day: lthr,
            lambda _days: list(records),
            lambda: TODAY,
        )

    def test_stored_series_give_the_dfa_value_and_rides_name_runs_only(self) -> None:
        for days in (2, 5, 9):
            add_run(self, days, stored(series()))
        result = self.service().estimates()
        self.assertEqual(result["run"]["dfa_alpha1"]["heart_rate_bpm"], 170)
        # No steady runs near 170 bpm: the pace stays unknown, the pulse shows.
        self.assertIsNone(result["run"]["dfa_alpha1"]["pace_seconds_per_km"])
        self.assertEqual(result["ride"]["dfa_alpha1"]["reason"], "nur aus Läufen")
        self.assertEqual(result["run"]["critical_speed"]["status"], "unknown")
        self.assertIn("42 Tagen", result["run"]["critical_speed"]["reason"])
        self.assertEqual(set(result["ride"]), {"dfa_alpha1", "critical_power"})

    def test_a_session_uses_its_series_record_and_old_rows_are_not_evaluated(
        self,
    ) -> None:
        shared = add_run(self, 3, stored((), method=2, estimate=150.0))
        add_run(self, 3, stored(series()), provider="wahoo", session=shared)
        add_run(self, 4, stored((), method=2, estimate=151.0))
        result = self.service().estimates()["run"]["dfa_alpha1"]
        self.assertEqual(
            result["reason"],
            "nur 1 von 3 nötigen Läufen mit Brustgurt in 60 Tagen (1 noch nicht ausgewertet)",
        )

    def test_the_stored_first_threshold_bounds_the_value(self) -> None:
        for days in (2, 5, 9):
            add_run(self, days, stored(series()))
        # 95 % of 180 bpm is 171 bpm, above the 170 bpm crossing.
        result = self.service(first_threshold_pct="95", lthr=180.0).estimates()
        self.assertEqual(result["run"]["dfa_alpha1"]["status"], "unknown")


ESTIMATES = {"run": {"dfa_alpha1": {"status": "unknown", "reason": "x", "sessions": 0}}}


class VisibilityTests(unittest.TestCase):
    def read_profile(self, service):
        handler, profile, competitions = Mock(), Mock(), Mock()
        profile.get.return_value = {}
        competitions.list.return_value = []
        routes = AthleteGetRoutes(
            Mock,
            lambda: profile,
            lambda: competitions,
            Mock,
            Mock,
            Mock(),
            second_threshold_service=service,
        )
        self.assertTrue(dispatch_route(routes, handler, "/api/profile"))
        return handler.send_json.call_args.args[1]

    def test_the_profile_read_carries_the_estimates_and_survives_a_failure(
        self,
    ) -> None:
        service = Mock()
        service.estimates.return_value = ESTIMATES
        self.assertEqual(self.read_profile(lambda: service)["lt2_estimates"], ESTIMATES)
        service.estimates.side_effect = RuntimeError("synthetic failure")
        self.assertIsNone(self.read_profile(lambda: service)["lt2_estimates"])

    def test_the_coach_sees_the_estimates_and_a_failure_leaves_them_out(self) -> None:
        performance = Mock()
        performance.current_performance.return_value = {"ftp": 260}

        def current(read):
            return CoachStructuredContextService(
                Mock(), Mock(), Mock(), Mock(), performance, lt2_estimates=read
            )._current_performance(None)

        self.assertEqual(current(lambda: ESTIMATES)["lt2_estimates"], ESTIMATES)

        def failing():
            raise RuntimeError("synthetic failure")

        self.assertNotIn("lt2_estimates", current(failing))


if __name__ == "__main__":
    unittest.main()
