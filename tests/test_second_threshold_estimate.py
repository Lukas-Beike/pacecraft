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

from backend.athlete.zones import local_configuration
from backend.canonical.activity_detail import ActivityDfaAlpha1, DfaWindow
from backend.canonical.vocabulary import DfaQuality
from backend.coach.context import CoachStructuredContextService
from backend.db.repositories import KeyValueRepository
from backend.errors import AppError
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
        # A step test is hard at its end by design (#3833): the drift rule is off.
        windows += [
            DfaWindow(
                start_s=end - 30.0 * i,
                heart_rate_bpm=171.0 + i,
                alpha1=0.48 - i * 0.02,
                artifact_share=0.0,
            )
            for i in range(4, 6)
        ]
        self.assertIsNone(lt2.session_second_threshold(stored(tuple(windows)), TODAY))
        found = lt2.session_second_threshold(
            stored(tuple(windows)), TODAY, late_drift=False
        )
        assert found is not None
        self.assertAlmostEqual(found.bpm, 170.0, delta=1.0)

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

    def test_the_dfa_value_carries_the_cited_limits_of_agreement(self) -> None:
        # #3920 C7: Rogers et al. 2021 (R76), -24 to +16 bpm; unknown values carry none.
        result = lt2.dfa_second_threshold(self.runs(3), TODAY)
        self.assertEqual(result["range"]["heart_rate_bpm"], [-24, 16])
        self.assertIn("R76", result["range"]["basis"])
        self.assertNotIn("range", lt2.dfa_second_threshold(self.runs(2), TODAY))


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
        # No verified source states a CP/CS range (#3920 C7): none is shown.
        self.assertNotIn("range", result)

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

    def service(self, records=(), first_threshold_hr=None, lthr=160.0):
        profile = Mock()
        profile.get.return_value = (
            {}
            if first_threshold_hr is None
            else {"first_threshold_hr": first_threshold_hr}
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

    def test_the_shared_90_day_read_leaves_the_estimates_unchanged(self) -> None:
        # #3920: the curve records are read for 90 days, the one window the
        # performance context shares; rows older than 42 days stay out of the fit.
        days = []
        old = (TODAY - timedelta(days=75)).isoformat()
        points = [
            {
                "duration_seconds": seconds,
                "watts": watts,
                "start_seconds": 0.0,
                "coverage": 1.0,
                "status": "ok",
            }
            for seconds, watts in (
                (180, 330.0),
                (420, 290.0),
                (720, 275.0),
                (1200, 262.0),
            )
        ]
        row = {
            "sport": "ride",
            "environment": "outdoor",
            "date": old,
            "activity_id": "old",
            "power_profile": {"status": "ok", "duration_curve": points},
        }
        profile = Mock()
        profile.get.return_value = {}

        def estimates(records):
            return SecondThresholdService(
                self.manager,
                profile,
                lambda: lambda _day: 160.0,
                lambda window: days.append(window) or list(records),
                lambda: TODAY,
            ).estimates()

        self.assertEqual(estimates([row]), estimates([]))
        self.assertEqual(days, [90, 90])
        self.assertEqual(
            estimates([row])["ride"]["critical_power"]["status"], "unknown"
        )

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
        # A stored 171 bpm lies above the 170 bpm crossing (#3915: stored in bpm).
        result = self.service(first_threshold_hr="171", lthr=180.0).estimates()
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


RUN_ESTIMATES = {
    "run": {
        "dfa_alpha1": {
            "status": "ok",
            "heart_rate_bpm": 169,
            "pace_seconds_per_km": 256,
        },
        "critical_speed": {"status": "unknown", "reason": "x", "sessions": 0},
    },
    "ride": {
        "dfa_alpha1": {"status": "unknown", "reason": "nur aus Läufen", "sessions": 0},
        "critical_power": {"status": "ok", "power_w": 271},
    },
}


class FakeZones:
    """The zone service's two calls the adoption makes; saves are kept."""

    def __init__(self, *configurations):
        self.items = configurations
        self.saved = []

    def configurations(self):
        return self.items

    def save_adopted(self, payload):
        self.saved.append(payload)
        local_configuration({**payload, "valid_from": TODAY.isoformat()})
        return {"configuration": payload, "change": {"id": 1}}


def zone(
    metric,
    threshold,
    *,
    local=False,
    origin=None,
    limits=None,
    percent=None,
    maximum=None,
):
    payload = {
        "sport": "run" if metric != "power" else "ride",
        "metric": metric,
        "threshold": threshold,
        "maximum": maximum,
        "limits": limits,
        "percent_limits": percent,
        "threshold_origin": origin,
    }
    configuration = local_configuration(payload)
    if local:
        return configuration
    from dataclasses import replace

    from backend.canonical.records import Provenance
    from backend.canonical.vocabulary import ProviderId, RecordingOrigin, SourceKind

    return replace(
        configuration,
        provenance=Provenance(
            provider=ProviderId.GARMIN,
            source_kind=SourceKind.PROVIDER_REPORTED,
            source_record_id="zones",
            recorded_by=RecordingOrigin.GARMIN,
            retrieved_at=None,
            label=None,
        ),
    )


class AdoptTests(unittest.TestCase):
    def adopt(self, zones, **payload):
        service = SecondThresholdService(
            Mock(), Mock(), Mock(), lambda _days: [], lambda: TODAY, zones=lambda: zones
        )
        service.estimates = lambda: RUN_ESTIMATES  # type: ignore[method-assign]
        return service.adopt(payload)

    def test_one_value_is_adopted_with_its_origin_and_the_limits_in_effect(
        self,
    ) -> None:
        zones = FakeZones(
            zone("heart_rate", 165, percent=[80, 89, 94, 100, 103, 110], maximum=190)
        )
        self.adopt(
            zones, sport="run", source="dfa_alpha1", value="heart_rate", shown=169
        )
        (saved,) = zones.saved
        self.assertEqual(saved["threshold"], 169)
        self.assertEqual(saved["threshold_origin"], "dfa_alpha1")
        self.assertEqual(saved["percent_limits"], [80, 89, 94, 100, 103, 110])
        self.assertEqual(saved["maximum"], 190)

    def test_pace_is_stored_as_the_threshold_speed(self) -> None:
        zones = FakeZones(zone("pace", 3.8, percent=[78, 88, 94, 100, 106, 999]))
        self.adopt(zones, sport="run", source="dfa_alpha1", value="pace", shown=256)
        # 256 s per km is 1000 / 256 = 3.906 m/s.
        self.assertAlmostEqual(zones.saved[0]["threshold"], 3.906, places=3)

    def test_refusals_name_their_reason(self) -> None:
        hr = zone("heart_rate", 165, percent=[80, 89, 94, 100, 103, 110])
        cases = [
            ({"shown": 170}, (hr,), "lt2_changed"),
            ({}, (), "lt2_no_zones"),
            (
                {},
                (
                    zone(
                        "heart_rate",
                        168,
                        local=True,
                        percent=[80, 89, 94, 100, 103, 110],
                    ),
                ),
                "lt2_replace_local",
            ),
            (
                {},
                (zone("heart_rate", 158, percent=[80, 89, 94, 100, 103, 110]),),
                "lt2_deviation",
            ),
        ]
        for extra, configurations, reason in cases:
            with self.subTest(reason=reason):
                payload = {
                    "sport": "run",
                    "source": "dfa_alpha1",
                    "value": "heart_rate",
                    "shown": 169,
                    **extra,
                }
                with self.assertRaises(AppError) as raised:
                    self.adopt(FakeZones(*configurations), **payload)
                self.assertEqual(
                    (raised.exception.status, raised.exception.reason), (409, reason)
                )

    def test_confirmations_let_the_same_requests_through(self) -> None:
        # 169 against 158 bpm is 7 % (F9); an entered value needs replace_local.
        for configuration, flag in (
            (
                zone("heart_rate", 158, percent=[80, 89, 94, 100, 103, 110]),
                "confirm_deviation",
            ),
            (
                zone(
                    "heart_rate", 168, local=True, percent=[80, 89, 94, 100, 103, 110]
                ),
                "replace_local",
            ),
        ):
            with self.subTest(flag=flag):
                zones = FakeZones(configuration)
                self.adopt(
                    zones,
                    sport="run",
                    source="dfa_alpha1",
                    value="heart_rate",
                    shown=169,
                    **{flag: True},
                )
                self.assertEqual(len(zones.saved), 1)

    def test_an_adopted_estimate_is_replaced_without_asking(self) -> None:
        zones = FakeZones(
            zone(
                "heart_rate",
                167,
                local=True,
                origin="dfa_alpha1",
                percent=[80, 89, 94, 100, 103, 110],
            )
        )
        self.adopt(
            zones, sport="run", source="dfa_alpha1", value="heart_rate", shown=169
        )
        self.assertEqual(len(zones.saved), 1)

    def test_absolute_limits_that_no_longer_fit_are_refused_with_the_reason(
        self,
    ) -> None:
        # A maximum of 168 bpm below the new 169 bpm threshold fails the HR rules.
        zones = FakeZones(
            zone("heart_rate", 165, limits=[130, 145, 155, 165, 166, 168], maximum=168)
        )
        with self.assertRaises(AppError) as raised:
            self.adopt(
                zones, sport="run", source="dfa_alpha1", value="heart_rate", shown=169
            )
        self.assertEqual(raised.exception.reason, "lt2_zone_limits")

    def test_a_good_step_test_lt2_is_adopted_only_on_request_with_the_dfa_origin(
        self,
    ) -> None:
        # #3833: the run step test's LT2 goes through the same adoption path.
        tests = {
            "good": {
                "lt2": {
                    "status": "ok",
                    "heart_rate_bpm": 171,
                    "pace_seconds_per_km": 265,
                    "quality": "good",
                }
            },
            "low": {
                "lt2": {
                    "status": "ok",
                    "heart_rate_bpm": 171,
                    "pace_seconds_per_km": 265,
                    "quality": "low",
                }
            },
        }
        reads = []

        def reader(activity_id):
            reads.append(activity_id)
            return tests.get(activity_id)

        zones = FakeZones(
            zone("heart_rate", 165, percent=[80, 89, 94, 100, 103, 110], maximum=190)
        )
        service = SecondThresholdService(
            Mock(), Mock(), Mock(), lambda _days: [], lambda: TODAY, zones=lambda: zones
        )
        self.assertEqual(zones.saved, [])
        service.adopt(
            {
                "sport": "run",
                "source": "step_test",
                "value": "heart_rate",
                "shown": 171,
                "activity_id": "good",
            },
            reader,
        )
        (saved,) = zones.saved
        self.assertEqual(saved["threshold"], 171)
        self.assertEqual(saved["threshold_origin"], "dfa_alpha1")
        for payload, reason in (
            ({"activity_id": "low", "shown": 171}, "lt2_unknown"),
            ({"activity_id": "good", "shown": 170}, "lt2_changed"),
            ({"activity_id": "none", "shown": 171}, "lt2_unknown"),
        ):
            with self.subTest(payload=payload), self.assertRaises(AppError) as raised:
                service.adopt(
                    {
                        "sport": "run",
                        "source": "step_test",
                        "value": "heart_rate",
                        **payload,
                    },
                    reader,
                )
            self.assertEqual(raised.exception.reason, reason)
        with self.assertRaises(AppError) as raised:
            service.adopt(
                {
                    "sport": "ride",
                    "source": "step_test",
                    "value": "power",
                    "shown": 1,
                    "activity_id": "good",
                },
                reader,
            )
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(len(zones.saved), 1)

    def test_unknown_sources_values_and_estimates_are_refused(self) -> None:
        zones = FakeZones(zone("power", 265, percent=[55, 75, 90, 105, 120, 150, 999]))
        for payload, status in (
            ({"sport": "ride", "source": "dfa_alpha1", "value": "heart_rate"}, 400),
            (
                {
                    "sport": "run",
                    "source": "critical_speed",
                    "value": "pace",
                    "shown": 250,
                },
                409,
            ),
        ):
            with self.subTest(payload=payload), self.assertRaises(AppError) as raised:
                self.adopt(zones, **payload)
            self.assertEqual(raised.exception.status, status)


class NoticeBaselineTests(unittest.TestCase):
    """F10: the values the athlete last saw are stored on the server, per sport."""

    manager: object
    counter: int

    def setUp(self) -> None:
        open_database(self)
        self.service = SecondThresholdService(
            self.manager,
            Mock(),
            Mock(),
            lambda _days: [],
            lambda: TODAY,
            key_values=KeyValueRepository(lambda: "2026-11-16T18:00:00+00:00"),
        )

    def test_seen_values_are_kept_per_sport_with_unknown_as_null(self) -> None:
        self.assertEqual(self.service.notice_baselines(), {})
        self.service.save_notice_baseline(
            {
                "sport": "run",
                "values": {"provider_hr": 171, "cs_pace": None, "other": 5},
            }
        )
        stored = self.service.save_notice_baseline(
            {"sport": "ride", "values": {"cp_power": 271}}
        )
        self.assertEqual(stored["run"]["provider_hr"], 171.0)
        self.assertIsNone(stored["run"]["cs_pace"])
        self.assertNotIn("other", stored["run"])
        self.assertEqual(self.service.notice_baselines()["ride"]["cp_power"], 271.0)

    def test_bad_sports_and_values_are_refused(self) -> None:
        for payload in (
            {"sport": "swim", "values": {}},
            {"sport": "run", "values": []},
            {"sport": "run", "values": {"provider_hr": "171"}},
            {"sport": "run", "values": {"provider_hr": True}},
            {"sport": "run", "values": {"provider_hr": -1}},
        ):
            with self.subTest(payload=payload), self.assertRaises(AppError) as raised:
                self.service.save_notice_baseline(payload)
            self.assertEqual(raised.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
