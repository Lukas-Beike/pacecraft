"""Running dynamics and form drift on long runs (#1870).

Garmin summary values come from synthetic rows shaped like the observed list
entries; the FIT file is built with the struct-based builder of ``test_fit_file``.
Expected values are worked out by hand in the comments, not recomputed.
"""

import json
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from backend.activities.detail_contract import _channels, _coverage, _streams
from backend.activities.detail_merge import _populated_channel_count, merge_details
from backend.canonical.activity_detail import CHANNEL_BOUNDS, ActivityStreamSet
from backend.canonical.mapping import IssueReason, MappingIssue
from backend.canonical.records import ActivityRecord, Provenance
from backend.canonical.vocabulary import (
    RUNNING_DYNAMICS_CHANNELS,
    ActivityDetailOrigin,
    Environment,
    LoadModel,
    ProviderId,
    RecordingOrigin,
    SourceKind,
    Sport,
    StreamChannel,
    StreamResolution,
)
from backend.errors import AppError
from backend.http_api.analysis import AnalysisRoutes
from backend.performance.analysis_input import analysis_input
from backend.performance.canonical_session_analysis import session_analysis
from backend.performance.form_drift import FLAG_LABEL, form_drift
from backend.performance.report_service import TrainingProfileSelectionService
from backend.performance.running_dynamics import (
    garmin_band,
    monthly_trend,
    pace_bands,
    session_rating,
)
from backend.providers.fit_activity_detail_mapping import map_fit_activity_detail
from backend.providers.garmin_activity_mapping import map_garmin_activities
from tests import test_fit_file as fit_builder
from tests.session_stream_support import synthetic_session
from tests.support import dispatch_route
from tests.test_activity_detail_merge import candidate
from tests.test_activity_detail_store import GARMIN, provenance

UINT8, UINT16, UINT32 = fit_builder.UINT8, fit_builder.UINT16, fit_builder.UINT32
RETRIEVED = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
ZONE = ZoneInfo("Europe/Berlin")
FIT_PROVENANCE = Provenance(
    provider=ProviderId.GARMIN,
    source_kind=SourceKind.PROVIDER_REPORTED,
    source_record_id=None,
    recorded_by=RecordingOrigin.GARMIN,
    retrieved_at=RETRIEVED,
    label=None,
)
# stance_time (41), stance_time_balance (84), vertical_oscillation (39),
# vertical_ratio (83), step_length (85)
DYNAMICS_FIELDS = [(41, UINT16), (84, UINT16), (39, UINT16), (83, UINT16), (85, UINT16)]
INVALID_UINT16 = 0xFFFF


def garmin_run(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "activityId": 910042,
        "activityName": "Synthetic long run",
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-11-03 11:47:18",
        "distance": 9780.0,
        "duration": 2975.0,
        "avgGroundContactTime": 264.5,
        "avgGroundContactBalance": 49.63,
        "avgVerticalOscillation": 9.4,
        "avgVerticalRatio": 8.04,
        "avgStrideLength": 116.36,
    }
    row.update(overrides)
    return row


def map_run(**overrides: object):
    result = map_garmin_activities(
        [garmin_run(**overrides)], zone=ZONE, retrieved_at=RETRIEVED
    )
    return result.records[0], result.issues


class GarminRunningDynamicsTests(unittest.TestCase):
    def test_summary_values_are_mapped_in_explicit_units(self) -> None:
        record, issues = map_run()

        self.assertEqual((), issues)
        self.assertEqual(264.5, record.ground_contact_time_ms)
        self.assertEqual(49.63, record.ground_contact_balance_pct)
        self.assertEqual(9.4, record.vertical_oscillation_cm)
        self.assertEqual(8.04, record.vertical_ratio_pct)
        # Garmin sends the step length in centimetres: 116.36 cm is 1.1636 m.
        self.assertAlmostEqual(1.1636, record.step_length_m)

    def test_the_left_balance_spelling_is_read_when_the_plain_key_is_absent(
        self,
    ) -> None:
        record, _ = map_run(
            avgGroundContactBalance=None, avgGroundContactBalanceLeft=48.2
        )

        self.assertEqual(48.2, record.ground_contact_balance_pct)

    def test_other_sports_carry_no_dynamics_and_no_issue(self) -> None:
        for type_key, expected in (
            ("cycling", False),
            ("lap_swimming", False),
            ("walking", True),
            ("hiking", True),
        ):
            with self.subTest(type_key):
                record, issues = map_run(
                    activityType={"typeKey": type_key}, avgGroundContactTime=5000.0
                )

                self.assertIsNone(record.ground_contact_time_ms)
                self.assertEqual(expected, bool(issues))
        ride, issues = map_run(activityType={"typeKey": "cycling"})
        self.assertEqual((), issues)
        self.assertIsNone(ride.step_length_m)
        walk, _ = map_run(activityType={"typeKey": "walking"})
        self.assertEqual(264.5, walk.ground_contact_time_ms)

    def test_a_run_without_a_sensor_has_no_dynamics_and_no_issue(self) -> None:
        record, issues = map_run(
            avgGroundContactTime=None,
            avgGroundContactBalance=None,
            avgVerticalOscillation=None,
            avgVerticalRatio=None,
            avgStrideLength=None,
        )

        self.assertEqual((), issues)
        for field in (
            "ground_contact_time_ms",
            "ground_contact_balance_pct",
            "vertical_oscillation_cm",
            "vertical_ratio_pct",
            "step_length_m",
        ):
            self.assertIsNone(getattr(record, field), field)

    def test_zero_means_not_measured_and_is_no_issue(self) -> None:
        # Observed: a run with a vertical ratio of 8.04 but an oscillation of 0.
        record, issues = map_run(avgVerticalOscillation=0, avgStrideLength=0)

        self.assertEqual((), issues)
        self.assertIsNone(record.vertical_oscillation_cm)
        self.assertIsNone(record.step_length_m)
        self.assertEqual(8.04, record.vertical_ratio_pct)

    def test_an_implausible_value_is_dropped_with_one_issue(self) -> None:
        record, issues = map_run(avgGroundContactTime=5000.0)

        self.assertIsNone(record.ground_contact_time_ms)
        self.assertEqual(
            (MappingIssue("910042", "avgGroundContactTime", IssueReason.OUT_OF_RANGE),),
            issues,
        )


def fit_with_dynamics(rows: list[dict[int, int]]) -> bytes:
    builder = fit_builder.FitBuilder()
    builder.define(0, 20, [(253, UINT32), (3, UINT8), *DYNAMICS_FIELDS])
    for offset, values in enumerate(rows):
        builder.data(0, {253: 1000 + offset, 3: 140, **values})
    return builder.build()


def map_fit(rows: list[dict[int, int]]):
    return map_fit_activity_detail(
        fit_with_dynamics(rows), provider_activity_id="fit-1", provenance=FIT_PROVENANCE
    )


def stream_channels(result) -> dict[StreamChannel, tuple[float | None, ...]]:
    return dict(result.records[0].channels)


class FitRunningDynamicsTests(unittest.TestCase):
    def test_record_fields_become_channels_in_canonical_units(self) -> None:
        row = {41: 2645, 84: 4963, 39: 935, 83: 804, 85: 11636}
        channels = stream_channels(map_fit([row, row, row]))

        # stance_time /10 -> 264.5 ms; balance /100 -> 49.63 %;
        # oscillation 93.5 mm -> 9.35 cm; ratio /100 -> 8.04 %;
        # step length 1163.6 mm -> 1.1636 m.
        self.assertEqual((264.5,) * 3, channels[StreamChannel.GROUND_CONTACT_TIME_MS])
        self.assertEqual(
            (49.63,) * 3, channels[StreamChannel.GROUND_CONTACT_BALANCE_PCT]
        )
        self.assertEqual((9.35,) * 3, channels[StreamChannel.VERTICAL_OSCILLATION_CM])
        self.assertEqual((8.04,) * 3, channels[StreamChannel.VERTICAL_RATIO_PCT])
        self.assertEqual((1.1636,) * 3, channels[StreamChannel.STEP_LENGTH_M])

    def test_invalid_and_zero_readings_are_gaps_without_an_issue(self) -> None:
        good = {41: 2645, 84: 4963, 39: 935, 83: 804, 85: 11636}
        invalid = {41: INVALID_UINT16, 84: INVALID_UINT16}
        zero = {41: 0, 39: 0}
        result = map_fit([good, invalid, zero])

        channels = stream_channels(result)
        self.assertEqual(
            (264.5, None, None), channels[StreamChannel.GROUND_CONTACT_TIME_MS]
        )
        self.assertEqual(
            (9.35, None, None), channels[StreamChannel.VERTICAL_OSCILLATION_CM]
        )
        self.assertEqual((), result.issues)

    def test_a_file_without_a_sensor_has_no_dynamics_channels(self) -> None:
        result = map_fit([{}, {}, {}])

        self.assertEqual((), result.issues)
        self.assertTrue(RUNNING_DYNAMICS_CHANNELS.isdisjoint(stream_channels(result)))

    def test_an_implausible_reading_is_a_gap_with_one_issue(self) -> None:
        # 65000 / 10 = 6500 ms is above the 2000 ms bound.
        result = map_fit([{41: 65000}, {41: 2645}, {41: 2645}])

        self.assertEqual(
            (None, 264.5, 264.5),
            stream_channels(result)[StreamChannel.GROUND_CONTACT_TIME_MS],
        )
        self.assertEqual(
            (
                MappingIssue(
                    "fit-1", "ground_contact_time_ms", IssueReason.OUT_OF_RANGE
                ),
            ),
            result.issues,
        )

    def test_every_dynamics_channel_has_bounds(self) -> None:
        self.assertEqual(
            RUNNING_DYNAMICS_CHANNELS, RUNNING_DYNAMICS_CHANNELS & CHANNEL_BOUNDS.keys()
        )


def dynamics_set(**series: tuple[float, ...]) -> ActivityStreamSet:
    channels: dict[StreamChannel, tuple[float | None, ...]] = {
        StreamChannel.TIME_S: (0.0, 1.0, 2.0),
        StreamChannel.HEART_RATE_BPM: (140.0, 141.0, 142.0),
    }
    channels.update({StreamChannel(name): values for name, values in series.items()})
    return ActivityStreamSet(
        provider_activity_id="g1",
        provenance=provenance(GARMIN),
        origin=ActivityDetailOrigin.FETCH,
        resolution=StreamResolution.FULL,
        channels=channels,
    )


def run_record() -> ActivityRecord:
    return ActivityRecord(
        activity_id="g1",
        provenance=provenance(GARMIN),
        sport=Sport.RUN,
        environment=Environment.OUTDOOR,
        started_at=RETRIEVED,
        duration_s=3600.0,
        distance_m=12000.0,
        load=None,
        load_model=LoadModel.UNKNOWN,
        avg_hr=140.0,
        avg_power=None,
        name=None,
    )


class DynamicsReadModelTests(unittest.TestCase):
    def test_only_a_run_analysis_carries_the_drift_key(self) -> None:
        merged = merge_details(
            [candidate(dynamics_set(ground_contact_time_ms=(251.0, 252.0, 253.0)))]
        )
        ride = replace(run_record(), sport=Sport.RIDE)

        run_analysis = session_analysis([run_record()], merged, None)
        ride_analysis = session_analysis([ride], merged, None)

        self.assertEqual("insufficient_data", run_analysis["form_drift"]["status"])
        self.assertNotIn("form_drift", ride_analysis)

    def test_the_main_streams_never_carry_the_series(self) -> None:
        detail = dynamics_set(
            ground_contact_time_ms=(251.0, 252.0, 253.0),
            vertical_oscillation_cm=(9.11, 9.12, 9.13),
        )
        merged = merge_details([candidate(detail)])

        served = _streams(merged.streams)
        assert served is not None
        text = json.dumps(served, sort_keys=True)

        self.assertEqual({"heart_rate_bpm"}, set(served["channels"]))
        for needle in ("ground_contact", "vertical_oscillation", "251.0", "9.11"):
            self.assertNotIn(needle, text)
        # The calendar charts the series apart from the main channels (#2258),
        # so the coverage names them; the Coach drops them (test_coach_activity_read_tools).
        dynamics = _channels(merged.streams, running_dynamics=True)
        assert dynamics is not None
        self.assertEqual(
            {"ground_contact_time_ms", "vertical_oscillation_cm"}, set(dynamics)
        )
        coverage = _coverage([candidate(detail).stored])
        self.assertEqual(
            {"heart_rate_bpm", "ground_contact_time_ms", "vertical_oscillation_cm"},
            set(coverage),
        )

    def test_dynamics_never_decide_the_primary_stream(self) -> None:
        plain = dynamics_set()
        with_dynamics = dynamics_set(
            ground_contact_time_ms=(251.0, 252.0, 253.0),
            vertical_ratio_pct=(8.0, 8.0, 8.0),
            step_length_m=(1.2, 1.2, 1.2),
        )

        self.assertEqual(
            _populated_channel_count(plain), _populated_channel_count(with_dynamics)
        )

    def test_the_analysis_input_carries_the_series_for_the_drift_check(self) -> None:
        detail = dynamics_set(
            ground_contact_time_ms=(251.0, 252.0, 253.0),
            step_length_m=(1.2, 1.2, 1.2),
        )
        merged = merge_details([candidate(detail)])

        fields = analysis_input(run_record(), merged.streams, None).activity["streams"]

        self.assertEqual([251.0, 252.0, 253.0], fields["ground_contact_time"])
        self.assertEqual([1.2, 1.2, 1.2], fields["step_length"])
        self.assertNotIn("vertical_oscillation", fields)


SECONDS = 3601  # a 60-minute run on a 1 Hz axis: two thirds of exactly 1200 s


def steady_run(
    *,
    contact: tuple[float, float, float] | None = (250.0, 255.0, 265.0),
    oscillation: tuple[float, float, float] | None = (9.0, 9.2, 9.45),
    step: tuple[float, float, float] | None = (1.2, 1.18, 1.16),
    speed: tuple[float, float, float] = (3.3, 3.3, 3.3),
    seconds: int = SECONDS,
    **extra: object,
) -> dict[str, Any]:
    """A run whose thirds hold the given values; the sample after t belongs to t."""
    third = (seconds - 1) // 3

    def series(values: tuple[float, float, float]) -> list[float]:
        return [values[min(index // third, 2)] for index in range(seconds)]

    streams: dict[str, Any] = {
        "time": [float(index) for index in range(seconds)],
        "velocity_smooth": series(speed),
        "moving": [True] * seconds,
    }
    for key, values in (
        ("ground_contact_time", contact),
        ("vertical_oscillation", oscillation),
        ("step_length", step),
    ):
        if values is not None:
            streams[key] = series(values)
    return {"sport": "run", "trail": False, "streams": streams, **extra}


def drift_or_none(activity: dict[str, Any]) -> dict[str, Any] | None:
    return form_drift(activity, source_label="Garmin Connect")


def drift(activity: dict[str, Any]) -> dict[str, Any]:
    result = drift_or_none(activity)
    assert result is not None
    return result


class FormDriftTests(unittest.TestCase):
    def test_a_rising_contact_time_flags_the_run(self) -> None:
        result = drift(steady_run())

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["flagged"])
        self.assertEqual("Laufstil lässt nach", result["label"])
        self.assertEqual(FLAG_LABEL, result["label"])
        contact = result["metrics"]["contact_time"]
        # 250 ms -> 265 ms is +6.0 %, above the 5 % threshold.
        self.assertEqual(250.0, contact["first"])
        self.assertEqual(265.0, contact["last"])
        self.assertEqual(6.0, contact["change_percent"])
        self.assertTrue(contact["exceeds"])
        # 9.0 -> 9.45 cm is +5 %, below the 10 % threshold: reported, not flagged.
        self.assertEqual(
            5.0, result["metrics"]["vertical_oscillation"]["change_percent"]
        )
        self.assertFalse(result["metrics"]["vertical_oscillation"]["exceeds"])
        # 1.20 -> 1.16 m is -3.33 %, above the -5 % threshold.
        self.assertEqual(-3.33, result["metrics"]["step_length"]["change_percent"])
        self.assertFalse(result["metrics"]["step_length"]["exceeds"])
        self.assertEqual(0.0, result["pace_change_percent"])
        self.assertEqual(1200.0, result["third_seconds"])
        self.assertEqual(
            "derived from Garmin Connect original streams", result["source"]
        )
        self.assertIn("heuristics", result["interpretation"])

    def test_the_threshold_itself_flags_and_below_it_does_not(self) -> None:
        # 250 -> 262.5 ms is exactly +5 %.
        at_threshold = drift(steady_run(contact=(250.0, 255.0, 262.5)))
        # 250 -> 256 ms is +2.4 %.
        below = drift(steady_run(contact=(250.0, 255.0, 256.0)))

        self.assertTrue(at_threshold["flagged"])
        self.assertEqual("ok", below["status"])
        self.assertFalse(below["flagged"])
        self.assertIsNone(below["label"])

    def test_a_shorter_step_and_more_oscillation_are_reported_without_a_flag(
        self,
    ) -> None:
        result = drift(
            steady_run(contact=(250.0, 250.0, 250.0), oscillation=(9.0, 9.5, 10.0))
        )

        # 9.0 -> 10.0 cm is +11.1 %, above the 10 % oscillation threshold.
        self.assertTrue(result["metrics"]["vertical_oscillation"]["exceeds"])
        self.assertFalse(result["flagged"])

    def test_a_pace_that_differs_between_the_thirds_is_not_compared(self) -> None:
        # 3.3 -> 3.0 m/s is -9.1 %, beyond the 5 % tolerance.
        result = drift(steady_run(speed=(3.3, 3.2, 3.0)))

        self.assertEqual("insufficient_data", result["status"])
        self.assertIn("Tempo", result["reason"])
        self.assertNotIn("flagged", result)

    def test_only_the_contact_time_metric_counts_for_the_flag(self) -> None:
        result = drift(steady_run(oscillation=None, step=None))

        self.assertEqual({"contact_time"}, set(result["metrics"]))
        self.assertTrue(result["flagged"])

    def test_missing_dynamics_are_normal_not_an_error(self) -> None:
        result = drift(steady_run(contact=None, oscillation=None, step=None))

        self.assertEqual("insufficient_data", result["status"])
        self.assertEqual("Keine Laufdynamik aufgezeichnet.", result["reason"])

    def test_a_trail_run_or_other_sport_has_no_drift_key_at_all(self) -> None:
        trail = drift_or_none(steady_run(trail=True))
        ride = drift_or_none({**steady_run(), "sport": "ride"})

        self.assertIsNone(trail)
        self.assertIsNone(ride)

    def test_an_ineligible_run_gets_a_minimal_result(self) -> None:
        short = drift(steady_run(seconds=2400))
        pace = drift(steady_run(speed=(3.3, 3.2, 3.0)))
        absent = drift(steady_run(contact=None, oscillation=None, step=None))

        self.assertEqual("Die Einheit ist kürzer als 45 Minuten.", short["reason"])
        for result in (short, pace, absent):
            self.assertEqual({"method", "status", "reason"}, set(result))
            self.assertEqual("insufficient_data", result["status"])

    def test_a_third_without_enough_measured_time_is_not_compared(self) -> None:
        activity = steady_run()
        # The pod dropped out after the first third: the last third has no reading.
        activity["streams"]["ground_contact_time"] = [
            value if index < 1200 else None
            for index, value in enumerate(activity["streams"]["ground_contact_time"])
        ]

        result = drift(activity)

        self.assertEqual(
            {"vertical_oscillation", "step_length"}, set(result["metrics"])
        )
        self.assertFalse(result["flagged"])

    def test_stops_do_not_count(self) -> None:
        activity = steady_run()
        # Paused for the first 300 s with a contact time that must be ignored.
        activity["streams"]["moving"] = [index >= 300 for index in range(SECONDS)]
        activity["streams"]["ground_contact_time"][:300] = [900.0] * 300

        result = drift(activity)

        self.assertEqual(250.0, result["metrics"]["contact_time"]["first"])


# Running dynamics against the athlete's own runs at the same pace (#2829).
DAY = date(2026, 11, 10)
# 4:00/km and 6:00/km: two pace bands of 15 s/km.
FAST, SLOW = 1000 / 240, 1000 / 360
# Fast runs touch the ground shorter than slow ones, as speed demands.
HISTORY: dict[float, list[float]] = {
    FAST: [226, 228, 230, 232, 234, 236],
    SLOW: [274, 276, 278, 280, 282, 284],
}


def _run(segments: list[tuple[int, float]], **dynamics: float) -> dict[str, Any]:
    """A 1 Hz run of (seconds, speed) parts with constant running dynamics."""
    session = synthetic_session(
        [("lap", seconds, speed, (140.0, 150.0)) for seconds, speed in segments],
        kinds=False,
    )
    length = len(session["streams"]["time"])
    values = {
        "ground_contact_time": 250.0,
        "vertical_oscillation": 8.5,
        "vertical_ratio": 7.6,
        "ground_contact_balance": 50.3,
        "step_length": 1.15,
        **dynamics,
    }
    for key, value in values.items():
        session["streams"][key] = [value] * length
    return session


def _record(days_before: int, bands: dict[str, Any] | None, key: str) -> dict[str, Any]:
    return {
        "activity_id": key,
        "date": (DAY - timedelta(days=days_before)).isoformat(),
        "sport": "run",
        "running_dynamics": bands,
    }


def _history(contact_times: dict[float, list[float]]) -> list[dict[str, Any]]:
    """Earlier runs, one per contact time, at each speed's own pace."""
    records = []
    for speed, values in contact_times.items():
        for index, value in enumerate(values):
            bands = pace_bands(_run([(600, speed)], ground_contact_time=value))
            records.append(_record(index + 1, bands, f"act_{speed:.2f}_{index}"))
    return records


class PaceBandTests(unittest.TestCase):
    def test_a_run_keeps_time_and_means_per_pace_band(self) -> None:
        bands = pace_bands(_run([(600, FAST), (600, SLOW), (300, 1.5)]))
        assert bands is not None
        # 1.5 m/s is walking and left out; cadence 85 rpm is 170 steps per minute.
        self.assertEqual(sorted(bands["bands"]), ["240", "360"])
        slow = bands["bands"]["360"]
        self.assertEqual(slow["seconds"], 600)
        self.assertEqual(
            (
                slow["ground_contact_time_ms"],
                slow["cadence_spm"],
                slow["step_length_m"],
            ),
            (250.0, 170.0, 1.15),
        )

    def test_rides_trail_runs_and_runs_without_dynamics_have_none(self) -> None:
        trail = {**_run([(600, FAST)]), "trail": True}
        plain = synthetic_session([("lap", 600, FAST, (140.0, 150.0))], kinds=False)
        ride = {**_run([(600, FAST)]), "sport": "ride"}
        short = _run([(100, FAST)])
        for activity in (trail, plain, ride, short):
            self.assertIsNone(pace_bands(activity))


class SessionRatingTests(unittest.TestCase):
    def _rate(self, speed: float, **dynamics: float) -> dict[str, Any]:
        own = _record(0, pace_bands(_run([(600, speed)], **dynamics)), "act_own")
        return session_rating(own, [*_history(HISTORY), own])

    def test_a_slow_run_is_not_worse_for_being_slow(self) -> None:
        # 279 ms is long against the fast runs but usual at 6:00/km.
        slow = self._rate(SLOW, ground_contact_time=279)
        fast = self._rate(FAST, ground_contact_time=231)
        for result in (slow, fast):
            self.assertEqual(
                result["metrics"]["ground_contact_time_ms"]["verdict"], "usual"
            )
        band = slow["bands"][0]
        self.assertEqual(band["band_s_per_km"], 360)
        contact = band["metrics"]["ground_contact_time_ms"]
        self.assertEqual((contact["peers"], contact["usual"]), (6, [277.33, 280.67]))

    def test_each_value_is_rated_in_its_own_direction(self) -> None:
        result = self._rate(
            SLOW,
            ground_contact_time=290,
            vertical_oscillation=7.0,
            ground_contact_balance=50.0,
            step_length=1.30,
        )["metrics"]
        verdicts = {metric: item["verdict"] for metric, item in result.items()}
        # The history has one value per metric except contact time: its middle
        # third is that value, so lower oscillation is better, a longer step
        # only longer, a balance at 50/50 better than 50.3.
        self.assertEqual(verdicts["ground_contact_time_ms"], "worse")
        self.assertEqual(verdicts["vertical_oscillation_cm"], "better")
        self.assertEqual(verdicts["step_length_m"], "higher")
        self.assertEqual(verdicts["ground_contact_balance_pct"], "better")
        self.assertEqual(verdicts["cadence_spm"], "usual")
        self.assertEqual(result["ground_contact_time_ms"]["garmin"], "orange")

    def test_too_few_or_out_of_window_runs_leave_the_band_unrated(self) -> None:
        own = _record(0, pace_bands(_run([(600, SLOW)])), "act_own")
        earlier = [
            _record(days, pace_bands(_run([(600, SLOW)])), f"act_{days}")
            for days in (1, 2, 3, 4, 85, -1)
        ]
        result = session_rating(own, [*earlier, own])
        contact = result["bands"][0]["metrics"]["ground_contact_time_ms"]
        # Days 85 and the day after the run are outside the 84 days before it.
        self.assertEqual((contact["peers"], contact["verdict"]), (4, "too_few"))
        self.assertEqual(
            result["metrics"]["ground_contact_time_ms"]["verdict"], "too_few"
        )
        self.assertEqual(
            session_rating(_record(0, None, "act_own"), [])["status"], "not_covered"
        )


class GarminBandTests(unittest.TestCase):
    def test_bands_follow_garmins_manual(self) -> None:
        # EPIX (Gen 2) manual, chest strap: contact time purple < 218, blue 218-248,
        # green 249-277, orange 278-308, red > 308 ms; cadence purple > 183,
        # blue 174-183, green 164-173, orange 153-163, red < 153 spm;
        # balance green up to 50.7 %, orange 50.8-52.2 %, red beyond.
        cases = [
            ("ground_contact_time_ms", 217, "purple"),
            ("ground_contact_time_ms", 218, "blue"),
            ("ground_contact_time_ms", 249, "green"),
            ("ground_contact_time_ms", 308, "orange"),
            ("ground_contact_time_ms", 309, "red"),
            ("cadence_spm", 152, "red"),
            ("cadence_spm", 153, "orange"),
            ("cadence_spm", 183, "blue"),
            ("cadence_spm", 184, "purple"),
            ("vertical_oscillation_cm", 8.2, "green"),
            ("vertical_ratio_pct", 6.0, "purple"),
            ("ground_contact_balance_pct", 49.3, "green"),
            ("ground_contact_balance_pct", 50.8, "orange"),
            ("ground_contact_balance_pct", 52.3, "red"),
            ("step_length_m", 1.2, None),
        ]
        for metric, value, colour in cases:
            with self.subTest(metric=metric, value=value):
                self.assertEqual(garmin_band(metric, value), colour)


class MonthlyTrendTests(unittest.TestCase):
    def test_monthly_medians_per_band_need_enough_time(self) -> None:
        records = [
            {
                "date": "2026-10-03",
                "running_dynamics": {
                    "bands": {"360": {"seconds": 400, "ground_contact_time_ms": 280}}
                },
            },
            {
                "date": "2026-10-20",
                "running_dynamics": {
                    "bands": {"360": {"seconds": 400, "ground_contact_time_ms": 270}}
                },
            },
            {
                "date": "2026-11-02",
                "running_dynamics": {
                    "bands": {"360": {"seconds": 300, "ground_contact_time_ms": 260}}
                },
            },
            {
                "date": "2025-01-02",
                "running_dynamics": {
                    "bands": {"360": {"seconds": 900, "ground_contact_time_ms": 300}}
                },
            },
        ]
        trend = monthly_trend(records, DAY)
        self.assertEqual(
            (trend["months"][0], trend["months"][-1]), ("2025-12", "2026-11")
        )
        series = trend["bands"][0]["series"]["ground_contact_time_ms"]
        # October: median of 280 and 270 over 800 s; November: 300 s is too little.
        self.assertEqual((series[-2], series[-1]), (275, None))


class RunningDynamicsReadTests(unittest.TestCase):
    def _service(self, every: Any) -> TrainingProfileSelectionService:
        service = TrainingProfileSelectionService(lambda: DAY)
        service.all_time_records = every  # type: ignore[method-assign]
        return service

    def test_the_read_waits_for_the_background_records(self) -> None:
        self.assertEqual(
            self._service(lambda: None).running_dynamics(), {"status": "pending"}
        )
        self.assertEqual(
            self._service(Mock(side_effect=RuntimeError)).running_dynamics(),
            {"status": "failed"},
        )
        own = _record(0, pace_bands(_run([(600, SLOW)])), "act_own")
        result = self._service(lambda: ((own,), False)).running_dynamics("act_own")
        self.assertEqual((result["status"], result["stale"]), ("ok", True))
        self.assertEqual(result["session"]["status"], "ok")
        self.assertIsNone(
            self._service(lambda: ((own,), True)).running_dynamics()["session"]
        )

    def test_the_route_passes_the_activity_and_bounds_it(self) -> None:
        reports = Mock()
        reports.profiles.running_dynamics.return_value = {"status": "pending"}
        handler = Mock()
        routes = AnalysisRoutes(lambda: reports)
        self.assertTrue(
            dispatch_route(
                routes, handler, "/api/analysis/running-dynamics?activity_id=act_1"
            )
        )
        reports.profiles.running_dynamics.assert_called_once_with("act_1")
        handler.send_json.assert_called_once_with(200, {"status": "pending"})
        with self.assertRaises(AppError):
            dispatch_route(
                routes,
                Mock(),
                f"/api/analysis/running-dynamics?activity_id={'a' * 201}",
            )


if __name__ == "__main__":
    unittest.main()
