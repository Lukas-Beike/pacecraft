"""Canonical activity detail: types, stream codec, store and schema guards."""

import dataclasses
import json
import sqlite3
import unittest
import zlib
from datetime import UTC, datetime

from backend.canonical.activity_detail import (
    LAP_METRIC_FIELDS,
    MAX_STREAM_POINTS,
    MAX_WORKOUT_STEPS,
    ActivityLap,
    ActivityLapSet,
    ActivityStreamSet,
    DeviceZoneBasis,
    WorkoutStep,
    ZoneBucket,
    ZoneDistribution,
    clean_series,
)
from backend.canonical.records import Provenance
from backend.canonical.vocabulary import (
    ZONE_DETAIL_KINDS,
    ActivityDetailKind,
    ActivityDetailOrigin,
    DeviceHeartRateCalc,
    DevicePowerCalc,
    LapKind,
    LapSource,
    LapTrigger,
    ProviderId,
    RecordingOrigin,
    SourceKind,
    StreamChannel,
    StreamResolution,
    WorkoutDurationType,
    WorkoutStepIntensity,
    WorkoutTargetType,
    WorkoutTargetUnit,
    ZoneBoundarySource,
    ZoneMetric,
)
from backend.db import row_factory
from backend.db.activity_detail_store import (
    _child_rows,
    _content_hash,
    _header,
    analysis_rows_for,
    decode_series,
    detail_sets_for,
    detail_sets_for_session,
    encode_series,
    replace_detail_set,
    save_analysis,
    stream_export_rows,
)
from backend.db.canonical_store import WriteOutcome
from backend.db.schema import initialize_schema

STAMP = "2026-10-18T08:00:00+00:00"
LATER = "2026-10-18T09:00:00+00:00"
RETRIEVED = datetime(2026, 10, 18, 7, 30, tzinfo=UTC)
INTERVALS = ProviderId.INTERVALS
GARMIN = ProviderId.GARMIN
DIGEST = "a" * 64


def provenance(provider=INTERVALS, **overrides):
    values = {
        "provider": provider,
        "source_kind": SourceKind.PROVIDER_REPORTED,
        "source_record_id": None,
        "recorded_by": RecordingOrigin.GARMIN,
        "retrieved_at": RETRIEVED,
        "label": None,
    }
    values.update(overrides)
    return Provenance(**values)


def stream_set(activity_id="i1", provider=INTERVALS, power=(100.0, None, 120.0)):
    return ActivityStreamSet(
        provider_activity_id=activity_id,
        provenance=provenance(provider),
        origin=ActivityDetailOrigin.FETCH,
        resolution=StreamResolution.FULL,
        channels={
            StreamChannel.TIME_S: (0.0, 1.0, 2.0),
            StreamChannel.POWER_W: power,
            StreamChannel.HEART_RATE_BPM: (120.0, 121.0, 122.5),
        },
    )


def lap_set(activity_id="i1", provider=INTERVALS):
    return ActivityLapSet(
        provider_activity_id=activity_id,
        provenance=provenance(provider),
        origin=ActivityDetailOrigin.FETCH,
        lap_source=LapSource.DEVICE_LAPS,
        laps=(
            ActivityLap(0, LapKind.WARMUP, 0.0, 600.0, name="Warm up", avg_hr=120.0),
            ActivityLap(
                1,
                LapKind.WORK,
                600.0,
                900.0,
                avg_power=280.0,
                load=12.5,
                avg_speed_mps=8.0,
                max_speed_mps=11.5,
            ),
        ),
    )


WATCH_ZONES = DeviceZoneBasis(
    max_hr=190.0,
    threshold_hr=172.0,
    ftp_w=250.0,
    hr_calc=DeviceHeartRateCalc.PERCENT_LTHR,
    power_calc=DevicePowerCalc.PERCENT_FTP,
)
WATCH_STEPS = (
    WorkoutStep(
        0, WorkoutStepIntensity.WARMUP, WorkoutDurationType.TIME, duration_value=600.0
    ),
    WorkoutStep(
        1,
        WorkoutStepIntensity.INTERVAL,
        WorkoutDurationType.DISTANCE,
        duration_value=1000.0,
        target_type=WorkoutTargetType.SPEED,
        target_low=3.5,
        target_high=3.8,
        target_unit=WorkoutTargetUnit.MPS,
    ),
    WorkoutStep(
        2,
        WorkoutStepIntensity.RECOVERY,
        WorkoutDurationType.HR_BELOW,
        duration_value=120.0,
        target_type=WorkoutTargetType.HEART_RATE,
        target_low=60.0,
        target_high=70.0,
        target_unit=WorkoutTargetUnit.PCT_MAX_HR,
    ),
    WorkoutStep(
        3,
        WorkoutStepIntensity.ACTIVE,
        WorkoutDurationType.REPEAT,
        repeat_from=1,
        repeat_count=4,
    ),
    WorkoutStep(
        4,
        WorkoutStepIntensity.COOLDOWN,
        WorkoutDurationType.OPEN,
        target_type=WorkoutTargetType.POWER,
        target_zone=2,
    ),
)


def watch_stream_set(activity_id="i1", provider=INTERVALS):
    """A stream set with a breathing rate channel and the zone basis of the watch."""
    base = stream_set(activity_id, provider)
    return dataclasses.replace(
        base,
        channels={
            **base.channels,
            StreamChannel.RESPIRATION_RATE_BRPM: (16.0, 17.5, None),
        },
        device_zones=WATCH_ZONES,
    )


def watch_lap_set(activity_id="i1", provider=INTERVALS):
    """A lap set whose laps name their workout step, trigger, breathing and GAP."""
    base = lap_set(activity_id, provider)
    laps = (
        dataclasses.replace(
            base.laps[0],
            workout_step_index=0,
            lap_trigger=LapTrigger.MANUAL,
            avg_respiration_rate_brpm=14.5,
            max_respiration_rate_brpm=21.25,
            avg_grade_adjusted_speed_mps=3.25,
        ),
        dataclasses.replace(
            base.laps[1], workout_step_index=1, lap_trigger=LapTrigger.DISTANCE
        ),
    )
    return dataclasses.replace(base, laps=laps, workout_steps=WATCH_STEPS)


def zone_set(activity_id="i1", provider=INTERVALS):
    return ZoneDistribution(
        provider_activity_id=activity_id,
        provenance=provenance(provider),
        origin=ActivityDetailOrigin.FETCH,
        metric=ZoneMetric.POWER,
        boundary_source=ZoneBoundarySource.PROVIDER_REPORTED,
        buckets=(
            ZoneBucket(1, 300.0, lower=0.0, upper=150.0, label="Z1"),
            ZoneBucket(2, 600.0, lower=150.0, upper=None),
        ),
    )


class ActivityDetailTypeTests(unittest.TestCase):
    def test_clean_series_replaces_unusable_points_with_gaps(self):
        values, rejected = clean_series(
            StreamChannel.HEART_RATE_BPM,
            [120, None, True, "x", float("nan"), 9999, 10**400],
        )
        self.assertEqual(values, (120.0, None, None, None, None, None, None))
        self.assertEqual(rejected, 5)

    def test_every_zone_metric_has_its_own_detail_kind(self):
        self.assertEqual(set(ZONE_DETAIL_KINDS), set(ZoneMetric))
        self.assertEqual(len(set(ZONE_DETAIL_KINDS.values())), len(ZoneMetric))

    def test_lap_metric_fields_are_lap_attributes_and_table_columns(self):
        lap_fields = {field.name for field in dataclasses.fields(ActivityLap)}
        self.assertLessEqual(set(LAP_METRIC_FIELDS), lap_fields)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        initialize_schema(db)
        columns = {row[1] for row in db.execute("PRAGMA table_info(activity_laps)")}
        self.assertLessEqual(set(LAP_METRIC_FIELDS), columns)

    def test_stream_set_requires_a_strict_complete_time_axis(self):
        invalid = (
            {StreamChannel.POWER_W: (1.0,)},
            {StreamChannel.TIME_S: (0.0, 1.0)},
            {StreamChannel.TIME_S: (0.0, None), StreamChannel.POWER_W: (1.0, 2.0)},
            {StreamChannel.TIME_S: (1.0, 1.0), StreamChannel.POWER_W: (1.0, 2.0)},
            {StreamChannel.TIME_S: (0.0, 1.0), StreamChannel.POWER_W: (1.0,)},
            {StreamChannel.TIME_S: (0.0,), StreamChannel.POWER_W: (-5.0,)},
        )
        for channels in invalid:
            with self.subTest(channels=channels), self.assertRaises(ValueError):
                ActivityStreamSet(
                    provider_activity_id="i1",
                    provenance=provenance(),
                    origin=ActivityDetailOrigin.FETCH,
                    resolution=StreamResolution.FULL,
                    channels=channels,
                )

    def test_stream_set_channels_are_read_only(self):
        detail = stream_set()
        with self.assertRaises(TypeError):
            detail.channels[StreamChannel.POWER_W] = (1.0, 2.0, 3.0)
        self.assertIs(detail.kind, ActivityDetailKind.STREAMS)

    def test_details_require_a_serving_provider(self):
        with self.assertRaises(ValueError):
            ActivityLapSet(
                provider_activity_id="i1",
                provenance=provenance(None, source_kind=SourceKind.DERIVED),
                origin=ActivityDetailOrigin.FETCH,
                lap_source=LapSource.SPLITS,
                laps=(ActivityLap(0, LapKind.LAP, 0.0, 1.0),),
            )

    def test_laps_and_zones_must_be_consecutive(self):
        with self.assertRaises(ValueError):
            ActivityLapSet(
                provider_activity_id="i1",
                provenance=provenance(),
                origin=ActivityDetailOrigin.FETCH,
                lap_source=LapSource.SPLITS,
                laps=(ActivityLap(1, LapKind.LAP, 0.0, 1.0),),
            )
        with self.assertRaises(ValueError):
            ZoneDistribution(
                provider_activity_id="i1",
                provenance=provenance(),
                origin=ActivityDetailOrigin.FETCH,
                metric=ZoneMetric.HEART_RATE,
                boundary_source=ZoneBoundarySource.UNKNOWN,
                buckets=(ZoneBucket(2, 10.0),),
            )
        self.assertIs(zone_set().kind, ActivityDetailKind.POWER_ZONES)

    def test_new_lap_values_read_as_none_instead_of_raising(self):
        lap = {"index": 0, "kind": LapKind.LAP, "start_offset_s": 0.0}
        lap["end_offset_s"] = 60.0
        for name, value in (
            ("avg_respiration_rate_brpm", 0.5),
            ("max_respiration_rate_brpm", 100.5),
            ("avg_respiration_rate_brpm", float("nan")),
            ("avg_respiration_rate_brpm", "18"),
            ("avg_respiration_rate_brpm", True),
            ("avg_grade_adjusted_speed_mps", -0.1),
            ("avg_grade_adjusted_speed_mps", 100.5),
            ("workout_step_index", 1000),
            ("workout_step_index", -1),
            ("workout_step_index", True),
            ("workout_step_index", 2.0),
            ("lap_trigger", "manual"),
            ("lap_trigger", 3),
        ):
            with self.subTest(name=name, value=value):
                self.assertIsNone(getattr(ActivityLap(**lap, **{name: value}), name))
        kept = ActivityLap(
            **lap,
            avg_respiration_rate_brpm=1,
            max_respiration_rate_brpm=100,
            avg_grade_adjusted_speed_mps=0,
            workout_step_index=999,
            lap_trigger=LapTrigger.SESSION_END,
        )
        self.assertEqual(
            (1.0, 100.0, 0.0, 999, LapTrigger.SESSION_END),
            (
                kept.avg_respiration_rate_brpm,
                kept.max_respiration_rate_brpm,
                kept.avg_grade_adjusted_speed_mps,
                kept.workout_step_index,
                kept.lap_trigger,
            ),
        )
        # An existing field still fails fast.
        with self.assertRaises(ValueError):
            ActivityLap(**lap, avg_hr=9999.0)

    def test_workout_step_needs_its_structure_and_forgives_its_numbers(self):
        step = {
            "index": 0,
            "intensity": WorkoutStepIntensity.ACTIVE,
            "duration_type": WorkoutDurationType.TIME,
        }
        for overrides, error in (
            ({"index": 1000}, ValueError),
            ({"index": -1}, ValueError),
            ({"index": True}, TypeError),
            ({"intensity": "active"}, TypeError),
            ({"duration_type": "time"}, TypeError),
            ({"target_type": "speed"}, TypeError),
            ({"target_unit": "mps"}, TypeError),
        ):
            with self.subTest(overrides=overrides), self.assertRaises(error):
                WorkoutStep(**{**step, **overrides})
        for duration_type, value in (
            (WorkoutDurationType.TIME, -1.0),
            (WorkoutDurationType.TIME, 10**9),
            (WorkoutDurationType.DISTANCE, float("inf")),
            (WorkoutDurationType.HR_BELOW, 300.0),
            (WorkoutDurationType.OPEN, 5.0),
            (WorkoutDurationType.REPEAT, 5.0),
        ):
            with self.subTest(duration_type=duration_type, value=value):
                made = WorkoutStep(
                    **{**step, "duration_type": duration_type}, duration_value=value
                )
                self.assertIsNone(made.duration_value)
        # Repeat values belong to a repeat step only.
        repeat = {**step, "duration_type": WorkoutDurationType.REPEAT}
        made = WorkoutStep(**repeat, repeat_from=1, repeat_count=4)
        self.assertEqual((1, 4), (made.repeat_from, made.repeat_count))
        made = WorkoutStep(**repeat, repeat_from=1000, repeat_count=0)
        self.assertEqual((None, None), (made.repeat_from, made.repeat_count))
        made = WorkoutStep(**step, repeat_from=1, repeat_count=4)
        self.assertEqual((None, None), (made.repeat_from, made.repeat_count))

    def test_workout_step_target_keeps_only_a_range_its_type_measures(self):
        step = {
            "index": 0,
            "intensity": WorkoutStepIntensity.ACTIVE,
            "duration_type": WorkoutDurationType.OPEN,
        }
        hr_range = {"target_low": 140.0, "target_high": 160.0}
        for overrides, expected in (
            # A range in a unit of its type is kept.
            (
                {
                    "target_type": WorkoutTargetType.HEART_RATE,
                    "target_unit": WorkoutTargetUnit.BPM,
                    **hr_range,
                },
                (WorkoutTargetUnit.BPM, 140.0, 160.0, None),
            ),
            # A unit of another type drops the range.
            (
                {
                    "target_type": WorkoutTargetType.HEART_RATE,
                    "target_unit": WorkoutTargetUnit.MPS,
                    **hr_range,
                },
                (None, None, None, None),
            ),
            # A bound that is out of range alone becomes None; the other stays.
            (
                {
                    "target_type": WorkoutTargetType.HEART_RATE,
                    "target_unit": WorkoutTargetUnit.BPM,
                    "target_low": 10.0,
                    "target_high": 160.0,
                },
                (WorkoutTargetUnit.BPM, None, 160.0, None),
            ),
            # A reversed range is no range.
            (
                {
                    "target_type": WorkoutTargetType.POWER,
                    "target_unit": WorkoutTargetUnit.W,
                    "target_low": 300.0,
                    "target_high": 250.0,
                },
                (WorkoutTargetUnit.W, None, None, None),
            ),
            # A zone replaces the range.
            (
                {
                    "target_type": WorkoutTargetType.POWER,
                    "target_unit": WorkoutTargetUnit.W,
                    **hr_range,
                    "target_zone": 3,
                },
                (None, None, None, 3),
            ),
            # An open target has neither range nor zone.
            (
                {
                    "target_type": WorkoutTargetType.OPEN,
                    "target_unit": WorkoutTargetUnit.W,
                    **hr_range,
                    "target_zone": 3,
                },
                (None, None, None, None),
            ),
            (
                {"target_type": WorkoutTargetType.SPEED, "target_zone": 21},
                (None, None, None, None),
            ),
        ):
            with self.subTest(overrides=overrides):
                made = WorkoutStep(**step, **overrides)
                self.assertEqual(
                    expected,
                    (
                        made.target_unit,
                        made.target_low,
                        made.target_high,
                        made.target_zone,
                    ),
                )

    def test_lap_set_drops_unusable_steps_but_never_its_laps(self):
        def step(index):
            return WorkoutStep(
                index, WorkoutStepIntensity.ACTIVE, WorkoutDurationType.OPEN
            )

        capped = tuple(step(index) for index in range(MAX_WORKOUT_STEPS))
        for steps, kept in (
            ((step(0), step(5), step(9)), 3),
            (capped, MAX_WORKOUT_STEPS),
            ((), 0),
            ([step(0)], 0),
            ((step(1), step(1)), 0),
            ((step(2), step(1)), 0),
            ((*capped, step(MAX_WORKOUT_STEPS)), 0),
            ((step(0), "step"), 0),
        ):
            with self.subTest(kept=kept):
                made = dataclasses.replace(lap_set(), workout_steps=steps)
                self.assertEqual(kept, len(made.workout_steps))
                self.assertEqual(2, len(made.laps))

    def test_device_zone_basis_keeps_only_usable_values(self):
        made = DeviceZoneBasis(
            max_hr=190,
            threshold_hr=10,
            ftp_w=0,
            hr_calc="custom",
            power_calc=DevicePowerCalc.CUSTOM,
        )
        self.assertEqual(
            (190.0, None, None, None, DevicePowerCalc.CUSTOM),
            (made.max_hr, made.threshold_hr, made.ftp_w, made.hr_calc, made.power_calc),
        )
        self.assertFalse(made.is_empty)
        self.assertTrue(DeviceZoneBasis(max_hr=True, ftp_w=5001.0).is_empty)
        # A stream set keeps a basis only when it holds a value.
        for zones, kept in (
            (DeviceZoneBasis(), None),
            (DeviceZoneBasis(max_hr=500.0), None),
            ({"max_hr": 190.0}, None),
            (WATCH_ZONES, WATCH_ZONES),
        ):
            with self.subTest(zones=zones):
                self.assertEqual(
                    kept,
                    dataclasses.replace(stream_set(), device_zones=zones).device_zones,
                )

    def test_respiration_channel_is_bounded_to_breaths_per_minute(self):
        values, rejected = clean_series(
            StreamChannel.RESPIRATION_RATE_BRPM, [0.5, 1, 18.5, 100, 100.5, None]
        )
        self.assertEqual((None, 1.0, 18.5, 100.0, None, None), values)
        self.assertEqual(2, rejected)
        with self.assertRaises(ValueError):
            dataclasses.replace(
                stream_set(),
                channels={
                    StreamChannel.TIME_S: (0.0, 1.0),
                    StreamChannel.RESPIRATION_RATE_BRPM: (18.0, 101.0),
                },
            )


class StreamCodecTests(unittest.TestCase):
    def test_round_trip_keeps_gaps_and_counts_valid_points(self):
        values = (0.0, None, 1.5, -3.25, None)
        blob, valid = encode_series(values)
        self.assertEqual(valid, 3)
        self.assertEqual(decode_series(blob, len(values)), values)

    def test_decode_rejects_mismatched_corrupt_or_oversized_data(self):
        blob, _ = encode_series((1.0, 2.0))
        bomb = zlib.compress(b"\x00" * (8 * 1000))
        cases = (
            (blob, 3),
            (blob, 1),
            (b"not zlib", 2),
            (bomb, 2),
            (blob + b"trailing", 2),
            (blob, 0),
            (blob, MAX_STREAM_POINTS + 1),
            ("text", 2),
        )
        for data, count in cases:
            with (
                self.subTest(count=count, size=len(data)),
                self.assertRaises(ValueError),
            ):
                decode_series(data, count)

    def test_decode_rejects_a_valid_count_that_disagrees_with_the_gaps(self):
        blob, valid = encode_series((1.0, None, 2.0))
        self.assertEqual(decode_series(blob, 3, valid), (1.0, None, 2.0))
        for wrong in (valid - 1, valid + 1):
            with self.subTest(valid_count=wrong), self.assertRaises(ValueError):
                decode_series(blob, 3, wrong)


class ActivityDetailStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = row_factory
        self.addCleanup(self.db.close)
        initialize_schema(self.db)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.add_session("act_1")
        self.add_record(INTERVALS, "i1", "act_1")

    def add_session(self, activity_id, state="active", merged_into=None):
        self.db.execute(
            "INSERT INTO activity_sessions(activity_id, primary_provider, "
            "primary_provider_activity_id, state, merged_into, started_at, "
            "created_at, updated_at) VALUES (?, 'intervals', ?, ?, ?, ?, ?, ?)",
            (activity_id, activity_id, state, merged_into, STAMP, STAMP, STAMP),
        )

    def add_record(self, provider, provider_activity_id, activity_id, state="active"):
        self.db.execute(
            "INSERT INTO activity_records(provider, provider_activity_id, activity_id, "
            "state, mapper_version, source_kind, recorded_by, sport, environment, "
            "started_at, load_model, fetched_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, 'provider_reported', 'garmin', 'ride', 'outdoor', "
            "?, 'unknown', ?, ?, ?)",
            (
                provider.value,
                provider_activity_id,
                activity_id,
                state,
                STAMP,
                STAMP,
                STAMP,
                STAMP,
            ),
        )

    def store(self, detail, **overrides):
        options = {"mapper_version": 1, "summary_sha256": DIGEST, "now": STAMP}
        options.update(overrides)
        return replace_detail_set(self.db, detail, **options)

    def count(self, table):
        return self.db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def test_every_kind_round_trips_with_provenance(self):
        details = (stream_set(), lap_set(), zone_set())
        for detail in details:
            self.assertIs(self.store(detail), WriteOutcome.WRITTEN)
        stored = detail_sets_for(self.db, "intervals", "i1")
        self.assertEqual(
            {item.detail.kind for item in stored},
            {detail.kind for detail in details},
        )
        by_kind = {item.detail.kind: item.detail for item in stored}
        for detail in details:
            with self.subTest(kind=detail.kind):
                self.assertEqual(by_kind[detail.kind], detail)
        self.assertEqual(stored[0].activity_id, "act_1")
        self.assertEqual(stored[0].summary_sha256, DIGEST)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_watch_workout_data_round_trips_as_numbers_and_names_only(self):
        for detail in (watch_stream_set(), watch_lap_set()):
            self.assertIs(self.store(detail), WriteOutcome.WRITTEN)
        stored = {
            item.detail.kind: item.detail
            for item in detail_sets_for(self.db, "intervals", "i1")
        }
        self.assertEqual(watch_stream_set(), stored[ActivityDetailKind.STREAMS])
        self.assertEqual(watch_lap_set(), stored[ActivityDetailKind.LAPS])
        row = self.db.execute(
            "SELECT workout_step_index, lap_trigger, avg_respiration_rate_brpm, "
            "max_respiration_rate_brpm, avg_grade_adjusted_speed_mps FROM activity_laps "
            "WHERE lap_index = 0"
        ).fetchone()
        self.assertEqual((0, "manual", 14.5, 21.25, 3.25), tuple(row.values()))
        sets = {
            row["detail_kind"]: row
            for row in self.db.execute(
                "SELECT detail_kind, workout_steps, device_zones FROM activity_detail_sets"
            )
        }
        # Every key and value is a number or a vocabulary name: the allowlist holds.
        self.assertEqual(
            {
                "index": 3,
                "intensity": "active",
                "duration_type": "repeat",
                "duration_value": None,
                "repeat_from": 1,
                "repeat_count": 4,
                "target_type": "open",
                "target_low": None,
                "target_high": None,
                "target_unit": None,
                "target_zone": None,
            },
            json.loads(sets["laps"]["workout_steps"])[3],
        )
        self.assertEqual(5, len(json.loads(sets["laps"]["workout_steps"])))
        self.assertIsNone(sets["laps"]["device_zones"])
        self.assertEqual(
            {
                "max_hr": 190.0,
                "threshold_hr": 172.0,
                "ftp_w": 250.0,
                "hr_calc": "percent_lthr",
                "power_calc": "percent_ftp",
            },
            json.loads(sets["streams"]["device_zones"]),
        )
        self.assertIsNone(sets["streams"]["workout_steps"])
        # The same content changes nothing; a changed step or none replaces them.
        self.assertIs(self.store(watch_lap_set()), WriteOutcome.UNCHANGED)
        fewer = dataclasses.replace(watch_lap_set(), workout_steps=WATCH_STEPS[:2])
        self.assertIs(self.store(fewer, now=LATER), WriteOutcome.UPDATED)
        self.assertIs(self.store(lap_set(), now=LATER), WriteOutcome.UPDATED)
        (laps,) = (
            item.detail
            for item in detail_sets_for(self.db, "intervals", "i1")
            if item.detail.kind is ActivityDetailKind.LAPS
        )
        self.assertEqual(lap_set(), laps)
        self.assertEqual((), laps.workout_steps)
        self.assertIsNone(
            self.db.execute(
                "SELECT workout_steps FROM activity_detail_sets WHERE detail_kind = 'laps'"
            ).fetchone()["workout_steps"]
        )

    def test_sets_without_watch_data_read_as_none_and_keep_their_content_hash(self):
        self.store(stream_set())
        self.store(lap_set())
        stored = {
            item.detail.kind: item.detail
            for item in detail_sets_for(self.db, "intervals", "i1")
        }
        streams = stored[ActivityDetailKind.STREAMS]
        laps = stored[ActivityDetailKind.LAPS]
        self.assertIsNone(streams.device_zones)
        self.assertEqual((), laps.workout_steps)
        for lap in laps.laps:
            self.assertEqual(
                (None,) * 5,
                (
                    lap.workout_step_index,
                    lap.lap_trigger,
                    lap.avg_respiration_rate_brpm,
                    lap.max_respiration_rate_brpm,
                    lap.avg_grade_adjusted_speed_mps,
                ),
            )
        # A set without the new data hashes as before schema v31 (hashes taken
        # from the build before it); only the lap rows gained values to hash.
        for detail, expected in (
            (
                stream_set(),
                "0ea6d2790ca5a2390b8281d510f538250e89f4496b16855f4d1b7672bbb39810",
            ),
            (
                zone_set(),
                "e38211c95bb8c3e9a309e5f30b42b87dbee5594de19da44ccd1bf5a666e70585",
            ),
        ):
            with self.subTest(kind=detail.kind):
                self.assertEqual(
                    expected, _content_hash(_header(detail, 1), _child_rows(detail))
                )
        # The new data enters the hash.
        hashes = {
            _content_hash(_header(detail, 1), _child_rows(detail))
            for detail in (
                stream_set(),
                watch_stream_set(),
                dataclasses.replace(
                    watch_stream_set(),
                    device_zones=dataclasses.replace(WATCH_ZONES, ftp_w=260.0),
                ),
                lap_set(),
                watch_lap_set(),
                dataclasses.replace(watch_lap_set(), workout_steps=WATCH_STEPS[:2]),
            )
        }
        self.assertEqual(6, len(hashes))

    def test_unusable_stored_watch_values_read_as_none_and_the_sets_still_load(self):
        self.store(watch_stream_set())
        self.store(watch_lap_set())
        self.db.execute(
            "UPDATE activity_laps SET avg_respiration_rate_brpm = 500, "
            "max_respiration_rate_brpm = 'fast', avg_grade_adjusted_speed_mps = -3, "
            "workout_step_index = 5.5, lap_trigger = 'bogus' WHERE lap_index = 0"
        )
        self.db.execute(
            "UPDATE activity_detail_sets SET workout_steps = ? WHERE detail_kind = 'laps'",
            ('[{"index": 1, "intensity": "active"}]',),
        )
        self.db.execute(
            "UPDATE activity_detail_sets SET device_zones = ? "
            "WHERE detail_kind = 'streams'",
            ('{"max_hr": 9999, "ftp_w": 250, "hr_calc": "bogus", "power_calc": 7}',),
        )
        stored = {
            item.detail.kind: item.detail
            for item in detail_sets_for(self.db, "intervals", "i1")
        }
        laps = stored[ActivityDetailKind.LAPS]
        self.assertEqual(2, len(laps.laps))
        self.assertEqual(120.0, laps.laps[0].avg_hr)
        self.assertEqual(
            (None,) * 5,
            (
                laps.laps[0].avg_respiration_rate_brpm,
                laps.laps[0].max_respiration_rate_brpm,
                laps.laps[0].avg_grade_adjusted_speed_mps,
                laps.laps[0].workout_step_index,
                laps.laps[0].lap_trigger,
            ),
        )
        self.assertEqual(
            (1, LapTrigger.DISTANCE),
            (laps.laps[1].workout_step_index, laps.laps[1].lap_trigger),
        )
        self.assertEqual((), laps.workout_steps)
        streams = stored[ActivityDetailKind.STREAMS]
        self.assertEqual(
            (None, 250.0, None, None),
            (
                streams.device_zones.max_hr,
                streams.device_zones.ftp_w,
                streams.device_zones.hr_calc,
                streams.device_zones.power_calc,
            ),
        )
        self.assertEqual(
            (16.0, 17.5, None), streams.channels[StreamChannel.RESPIRATION_RATE_BRPM]
        )
        # A basis without one usable value reads as none.
        for text in ('{"max_hr": 9999, "hr_calc": "bogus"}', "{}"):
            with self.subTest(text=text):
                self.db.execute(
                    "UPDATE activity_detail_sets SET device_zones = ? "
                    "WHERE detail_kind = 'streams'",
                    (text,),
                )
                (streams,) = (
                    item.detail
                    for item in detail_sets_for(self.db, "intervals", "i1")
                    if item.detail.kind is ActivityDetailKind.STREAMS
                )
                self.assertIsNone(streams.device_zones)

    def test_watch_header_columns_belong_to_their_own_set_kind(self):
        self.store(watch_stream_set())
        self.store(watch_lap_set())
        update = "UPDATE activity_detail_sets SET {} = ? WHERE detail_kind = ?"
        for column, kind, value in (
            ("workout_steps", "streams", "[]"),
            ("workout_steps", "laps", "{}"),
            ("device_zones", "laps", "{}"),
            ("device_zones", "streams", "[]"),
        ):
            with (
                self.subTest(column=column, kind=kind, value=value),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.db.execute(update.format(column), (value, kind))

    def test_equal_content_only_refreshes_fetch_bookkeeping(self):
        self.store(stream_set())
        later = stream_set()
        object.__setattr__(
            later,
            "provenance",
            provenance(retrieved_at=datetime(2026, 10, 18, 9, tzinfo=UTC)),
        )
        self.assertIs(
            self.store(later, summary_sha256=None, now=LATER), WriteOutcome.UNCHANGED
        )
        row = self.db.execute(
            "SELECT summary_sha256, fetched_at, updated_at FROM activity_detail_sets"
        ).fetchone()
        self.assertEqual(
            (row["summary_sha256"], row["fetched_at"], row["updated_at"]),
            (None, "2026-10-18T09:00:00+00:00", STAMP),
        )

    def test_changed_content_replaces_every_child_row(self):
        self.store(stream_set())
        changed = ActivityStreamSet(
            provider_activity_id="i1",
            provenance=provenance(),
            origin=ActivityDetailOrigin.FETCH,
            resolution=StreamResolution.DOWNSAMPLED,
            channels={
                StreamChannel.TIME_S: (0.0, 5.0),
                StreamChannel.CADENCE_RPM: (80.0, 82.0),
            },
        )
        self.assertIs(self.store(changed, now=LATER), WriteOutcome.UPDATED)
        self.assertEqual(self.count("activity_stream_channels"), 2)
        (stored,) = detail_sets_for(self.db, "intervals", "i1")
        self.assertEqual(stored.detail, changed)
        row = self.db.execute(
            "SELECT created_at, updated_at FROM activity_detail_sets"
        ).fetchone()
        self.assertEqual((row["created_at"], row["updated_at"]), (STAMP, LATER))

    def test_providers_keep_alternative_sets_for_one_session(self):
        self.add_session("act_2", state="duplicate", merged_into="act_1")
        self.add_record(GARMIN, "g1", "act_2")
        self.store(stream_set())
        self.store(stream_set("g1", GARMIN, power=(101.0, 105.0, 119.0)))
        stored = detail_sets_for_session(self.db, "act_1")
        self.assertEqual(
            [(item.provider, item.activity_id) for item in stored],
            [("garmin", "act_2"), ("intervals", "act_1")],
        )
        self.assertEqual(
            detail_sets_for_session(self.db, "act_2")[0].provider, "garmin"
        )

    def test_tombstoned_records_and_sessions_hide_details_without_deleting(self):
        self.store(stream_set())
        self.db.execute("UPDATE activity_records SET state = 'deleted'")
        self.assertEqual(detail_sets_for(self.db, "intervals", "i1"), [])
        self.db.execute("UPDATE activity_records SET state = 'active'")
        self.db.execute("UPDATE activity_sessions SET state = 'deleted'")
        self.assertEqual(detail_sets_for_session(self.db, "act_1"), [])
        self.assertEqual(self.count("activity_stream_channels"), 3)

    def test_unreadable_stored_set_is_skipped(self):
        self.store(stream_set())
        self.store(lap_set())
        self.db.execute(
            "UPDATE activity_stream_channels SET data = ? WHERE channel = 'power_w'",
            (b"corrupt",),
        )
        with self.assertLogs("intervals_coach", "WARNING") as logs:
            stored = detail_sets_for(self.db, "intervals", "i1")
        self.assertEqual(
            [item.detail.kind for item in stored], [ActivityDetailKind.LAPS]
        )
        (record,) = logs.records
        self.assertEqual(record.event, "activity_detail_unreadable")
        self.assertEqual(
            record.context,
            {"provider": "intervals", "detail_kind": "streams", "error": "ValueError"},
        )

    def test_detail_requires_an_existing_activity_record(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store(stream_set("missing"))

    def test_store_rejects_invalid_bookkeeping(self):
        for options, error in (
            ({"mapper_version": 0}, ValueError),
            ({"mapper_version": True}, TypeError),
            ({"summary_sha256": "short"}, ValueError),
        ):
            with self.subTest(options=options), self.assertRaises(error):
                self.store(stream_set(), **options)
        self.assertEqual(self.count("activity_detail_sets"), 0)

    def test_deleting_a_set_cascades_to_its_children(self):
        self.store(stream_set())
        self.store(lap_set())
        self.store(zone_set())
        self.db.execute("DELETE FROM activity_detail_sets")
        for table in (
            "activity_stream_channels",
            "activity_laps",
            "activity_zone_times",
        ):
            with self.subTest(table=table):
                self.assertEqual(self.count(table), 0)

    def test_set_columns_must_match_their_detail_kind(self):
        insert = (
            "INSERT INTO activity_detail_sets(provider, provider_activity_id, "
            "detail_kind, mapper_version, origin, source_kind, recorded_by, "
            "resolution, lap_source, zone_boundary_source, content_sha256, "
            "fetched_at, created_at, updated_at) "
            "VALUES ('intervals', 'i1', ?, 1, 'fetch', 'provider_reported', "
            "'garmin', ?, ?, ?, ?, ?, ?, ?)"
        )
        for kind, resolution, lap_source, boundary in (
            ("streams", None, None, None),
            ("laps", "full", "splits", None),
            ("power_zones", None, None, None),
            ("unknown", "full", None, None),
        ):
            with self.subTest(kind=kind), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(
                    insert,
                    (
                        kind,
                        resolution,
                        lap_source,
                        boundary,
                        DIGEST,
                        STAMP,
                        STAMP,
                        STAMP,
                    ),
                )

    def test_analysis_is_stored_once_per_visible_session(self):
        payload = {"target": {"kind": "ftp"}, "score": 0.8}
        save = {"analysis_version": 1, "input_sha256": DIGEST, "payload": payload}
        self.assertIs(
            save_analysis(self.db, "act_1", now=STAMP, **save), WriteOutcome.WRITTEN
        )
        self.assertIs(
            save_analysis(self.db, "act_1", now=LATER, **save), WriteOutcome.UNCHANGED
        )
        save["input_sha256"] = "b" * 64
        self.assertIs(
            save_analysis(self.db, "act_1", now=LATER, **save), WriteOutcome.UPDATED
        )
        rows = analysis_rows_for(self.db, ["act_1", "act_1", ""])
        self.assertEqual(rows["act_1"]["payload"], payload)
        self.assertEqual(rows["act_1"]["computed_at"], LATER)
        self.db.execute("UPDATE activity_sessions SET state = 'deleted'")
        self.assertEqual(analysis_rows_for(self.db, ["act_1"]), {})

    def test_stream_export_decodes_points_and_flags_unreadable_channels(self):
        self.store(stream_set())
        self.db.execute(
            "UPDATE activity_stream_channels SET data = ? WHERE channel = 'heart_rate_bpm'",
            (b"corrupt",),
        )
        rows = {row["channel"]: row for row in stream_export_rows(self.db)}
        self.assertEqual(rows["power_w"]["values"], [100.0, None, 120.0])
        self.assertEqual(
            (rows["power_w"]["point_count"], rows["power_w"]["valid_count"]), (3, 2)
        )
        self.assertIsNone(rows["heart_rate_bpm"]["values"])

    def test_analysis_rejects_invalid_input(self):
        for overrides, error in (
            ({"analysis_version": 0}, ValueError),
            ({"input_sha256": "A" * 64}, ValueError),
            ({"payload": ["not", "a", "mapping"]}, TypeError),
        ):
            values = {"analysis_version": 1, "input_sha256": DIGEST, "payload": {}}
            values.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(error):
                save_analysis(self.db, "act_1", now=STAMP, **values)


if __name__ == "__main__":
    unittest.main()
