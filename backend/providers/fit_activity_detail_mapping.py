"""Map FIT activity files to canonical detail sets (origin FETCH).

The input is the raw bytes of one FIT activity file, decoded by ``fit_file``.
Field numbers follow the FIT SDK profile: record messages (20) provide streams,
lap messages (19) provide device laps and set messages (225) provide executed
strength sets. Values are converted to the canonical
units: metres, metres per second, beats or revolutions per minute, watts,
degrees Celsius and seconds.

Streams (``StreamResolution.FULL``) use these record fields: timestamp (253,
time zero is the first usable one), distance (5, /100 m), speed (enhanced 73,
else base 6, /1000 m/s), altitude (enhanced 78, else base 2, /5 - 500 m), heart
rate (3), cadence (4, plus the fractional cadence 53 /128 when present), power (7)
and temperature (13). The enhanced field is used per record when it holds a
usable value; an invalid sentinel falls back to the base field.

Running dynamics streams, present only with a supporting sensor and taken from the
FIT SDK profile (not checked against a recorded file): stance_time (41, /10 ms) as
ground contact time, stance_time_balance (84, /100 percent) as the share on the
left foot, vertical_oscillation (39, /10 mm, shown in cm), vertical_ratio (83,
/100 percent) and step_length (85, /10 mm, shown in m). A reading of 0 means not
measured and is a gap.

Device laps (``LapSource.DEVICE_LAPS``) use these lap fields: start_time (2),
total_elapsed_time (7, /1000 s), total_timer_time (8, /1000 s, moving duration),
total_distance (9, /100 m), average speed (enhanced 110, else base 13, /1000
m/s), average and maximum heart rate (15, 16), average cadence (17), average and
maximum power (19, 20), total ascent (21), normalized power (33) and intensity
(23: 0 active, 1 rest and 4 recovery, 2 warmup, 3 cooldown, 5 interval as
``WORK``; other values map to ``LAP``).

Strength sets (``StrengthSetSource.FIT_SET_MESSAGES``) use these set fields, in
file order: set_type (5: 0 rest, 1 active; a set without one is dropped),
repetitions (3), weight (4, /16 kg; 0 means no external load), duration (0,
/1000 s), start_time (6, seconds from time zero; before it, unknown), category
(7, the first valid entry of the array: the FIT ``exercise_category`` enum 0-32,
a newer number maps to ``other``, 65534 unknown to none) and wkt_step_index
(11, the step number without the message-index flag bits). The per-category
exercise subtype (8) is not mapped, so FIT sets carry no exercise name.

Breathing rate (record 108, lap 136 and 137, all /100 breaths per minute) and the
device's grade-adjusted lap speed (161, /1000 m/s) are read like the other
metrics; a value outside the canonical bounds is a gap or None, silently, since
these fields never make a file incomplete. Garmin's undocumented performance
condition (record 90) and stamina fields (record 137 and 138) are read the same
way, unscaled, as unofficial channels (#3374). A lap also keeps its workout step
index (71, the number without the message-index flag bits) and trigger (24: the
FIT ``lap_trigger`` enum 0-8; a newer number is None).

The executed workout steps (``workout_step`` 27) go to the lap set by ascending
index, at most ``MAX_WORKOUT_STEPS``. Only numbers are read: duration type (1:
0 time /1000 s, 1 distance /100 m, 2 and 3 heart rate below and above, 5 open,
6 repeat until steps complete, 14 and 15 power below and above, 29 reps; any
other type is ``other`` without a value) with its value (2), intensity (7), target
type (3: 0 speed, 1 heart rate, 2 open, 3 cadence, 4 power) and either a zone
(4) or a custom range (5, 6: speed /1000 m/s; heart rate, above 100 is bpm
plus 100, up to 100 percent of the maximum; power, above 1000 is watts plus
1000, up to 1000 percent of the FTP). A repeat step carries the step to return to
(2) and the repeat count (4). Step names (27.0) and notes (27.8) and the workout
message (26, name and description) are free text of the file and never read; a
step without a usable index is dropped. The zone basis of the watch comes from
``zones_target`` (7): maximum and threshold heart rate (1, 2), FTP (3) and the
calculation types (5, 7); it is shown, never used in a calculation.

Device information (23) yields the heart rate and power source of the stream set
and its external heart rate and power sensors, with the serial only as a pseudonymous hash
(``fit_sensor_mapping``). The hrv messages (78) of a chest-strap recording give
the first threshold estimate of the stream set (``dfa_alpha1``); only that result
is kept, never the RR intervals (``fit_rr_mapping``). Not mapped: session and
activity messages, developer fields, and lap run cadence.

The mapper is pure: it performs no I/O, logs nothing and never raises for
malformed bytes. Issues name the activity, the FIT field and a reason only.
Caller arguments are checked and may raise, as in the Garmin mapper.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Final, cast

from backend.canonical.activity_detail import (
    MAX_LAPS,
    MAX_STREAM_POINTS,
    MAX_STREAM_SPAN_S,
    MAX_STRENGTH_SETS,
    MAX_WORKOUT_STEP_INDEX,
    MAX_WORKOUT_STEPS,
    ActivityDetailSet,
    ActivityDfaAlpha1,
    ActivityLap,
    ActivityLapSet,
    ActivityStreamSet,
    ActivityStrengthSet,
    ActivityStrengthSetSet,
    DeviceZoneBasis,
    WorkoutStep,
    clean_series,
    strength_set_accepts,
)
from backend.canonical.dfa_alpha1 import first_threshold
from backend.canonical.mapping import IssueReason, MappingIssue, MappingResult
from backend.canonical.records import Provenance
from backend.canonical.validation import require_instance, require_text
from backend.canonical.vocabulary import (
    ActivityDetailOrigin,
    DeviceHeartRateCalc,
    DevicePowerCalc,
    ExerciseCategory,
    HeartRateSource,
    LapKind,
    LapSource,
    LapTrigger,
    RecordingOrigin,
    StreamChannel,
    StreamResolution,
    StrengthSetSource,
    StrengthSetType,
    WorkoutDurationType,
    WorkoutStepIntensity,
    WorkoutTargetType,
    WorkoutTargetUnit,
)
from backend.providers.fit_file import (
    FitDecodeError,
    FitMessage,
    fit_field,
    fit_messages,
)
from backend.providers.fit_rr_mapping import HRV_MESSAGE, rr_intervals_ms
from backend.providers.fit_sensor_mapping import DEVICE_INFO_MESSAGE, sensor_profile

# Version 1: first mapping of FIT activity detail sets (streams and device laps).
# Version 2: maximum speed per lap.
# Version 3: executed strength sets from set messages.
# Version 4: GPS position channels from the record semicircles.
# Version 5: heart rate and power sources and the external sensors of a stream
# set, from the device information messages.
# Version 6: the first threshold estimate (DFA-a1) of a chest-strap recording
# that carries RR intervals (hrv messages); the intervals are not stored.
# Version 7: Bluetooth LE sensor roles with the FIT SDK source type numbers.
# Version 8: running dynamics channels from the record messages (#1870).
# Version 9: ``active`` laps are work laps when the file has rest or recovery laps,
# and records with a compressed timestamp header keep their timestamp (#1936).
# Version 10: breathing rate (record and lap), the device's lap grade-adjusted
# speed, the lap's workout step and trigger, the executed workout steps and the
# watch's zone basis (#2220).
# Version 11: files with an unset (zero) header CRC and chained files decode,
# and a file that does not names why in its issue field (#3337).
# Version 12: Garmin's undocumented performance condition (record 90) and its two
# stamina fields (record 137 and 138) as unofficial channels (#3374).
# Version 13: DFA-a1 method 4 detrends the 4 Hz resampled RR series (#3919).
MAPPER_VERSION: Final = 13

# Same limit as the Garmin and Intervals mappers, so issues stay bounded.
MAX_FIT_ACTIVITY_ID_LENGTH: Final = 200

_RECORD_MESSAGE: Final = 20
_LAP_MESSAGE: Final = 19
_TIMESTAMP_FIELD: Final = 253
_CADENCE_FIELD: Final = 4
_FRACTIONAL_CADENCE_FIELD: Final = 53
_LAP_START_FIELD: Final = 2
_LAP_ELAPSED_FIELD: Final = 7
_LAP_INTENSITY_FIELD: Final = 23
_SET_MESSAGE: Final = 225
_SET_DURATION_FIELD: Final = 0
_SET_REPETITIONS_FIELD: Final = 3
_SET_WEIGHT_FIELD: Final = 4
_SET_TYPE_FIELD: Final = 5
_SET_START_FIELD: Final = 6
_SET_CATEGORY_FIELD: Final = 7
_SET_STEP_FIELD: Final = 11
_SET_TYPES: Final[Mapping[int, StrengthSetType]] = {
    0: StrengthSetType.REST,
    1: StrengthSetType.ACTIVE,
}
# FIT ``exercise_category`` numbers 0-32, in profile order.
_FIT_EXERCISE_CATEGORIES: Final[tuple[ExerciseCategory, ...]] = (
    ExerciseCategory.BENCH_PRESS,
    ExerciseCategory.CALF_RAISE,
    ExerciseCategory.CARDIO,
    ExerciseCategory.CARRY,
    ExerciseCategory.CHOP,
    ExerciseCategory.CORE,
    ExerciseCategory.CRUNCH,
    ExerciseCategory.CURL,
    ExerciseCategory.DEADLIFT,
    ExerciseCategory.FLYE,
    ExerciseCategory.HIP_RAISE,
    ExerciseCategory.HIP_STABILITY,
    ExerciseCategory.HIP_SWING,
    ExerciseCategory.HYPEREXTENSION,
    ExerciseCategory.LATERAL_RAISE,
    ExerciseCategory.LEG_CURL,
    ExerciseCategory.LEG_RAISE,
    ExerciseCategory.LUNGE,
    ExerciseCategory.OLYMPIC_LIFT,
    ExerciseCategory.PLANK,
    ExerciseCategory.PLYO,
    ExerciseCategory.PULL_UP,
    ExerciseCategory.PUSH_UP,
    ExerciseCategory.ROW,
    ExerciseCategory.SHOULDER_PRESS,
    ExerciseCategory.SHOULDER_STABILITY,
    ExerciseCategory.SHRUG,
    ExerciseCategory.SIT_UP,
    ExerciseCategory.SQUAT,
    ExerciseCategory.TOTAL_BODY,
    ExerciseCategory.TRICEPS_EXTENSION,
    ExerciseCategory.WARM_UP,
    ExerciseCategory.RUN,
)
_FIT_UNKNOWN_CATEGORY: Final = 65534
# message_index values carry flag bits above the 12-bit index.
_MESSAGE_INDEX_MASK: Final = 0x0FFF
# Record field per channel; speed and altitude prefer their enhanced fields.
_RECORD_CHANNEL_FIELDS: Final[tuple[tuple[StreamChannel, int], ...]] = (
    (StreamChannel.DISTANCE_M, 5),
    (StreamChannel.SPEED_MPS, 6),
    (StreamChannel.ALTITUDE_M, 2),
    (StreamChannel.HEART_RATE_BPM, 3),
    (StreamChannel.POWER_W, 7),
    (StreamChannel.TEMPERATURE_C, 13),
)
# Running dynamics: channel, record field and the divisor from the scaled FIT unit
# to the canonical one (oscillation mm -> cm, step length mm -> m).
_DYNAMICS_FIELDS: Final[tuple[tuple[StreamChannel, int, float], ...]] = (
    (StreamChannel.GROUND_CONTACT_TIME_MS, 41, 1.0),
    (StreamChannel.GROUND_CONTACT_BALANCE_PCT, 84, 1.0),
    (StreamChannel.VERTICAL_OSCILLATION_CM, 39, 10.0),
    (StreamChannel.VERTICAL_RATIO_PCT, 83, 1.0),
    (StreamChannel.STEP_LENGTH_M, 85, 1000.0),
)
# FIT positions are signed 32-bit semicircles: 2**31 semicircles are 180 degrees.
_SEMICIRCLE_DEG: Final = 180.0 / 2**31
_POSITION_FIELDS: Final[tuple[tuple[StreamChannel, int], ...]] = (
    (StreamChannel.LATITUDE_DEG, 0),
    (StreamChannel.LONGITUDE_DEG, 1),
)
# Lap metric -> lap field number. Scaling and enhanced preference are in fit_file.
_LAP_METRIC_FIELDS: Final[tuple[tuple[str, int], ...]] = (
    ("moving_duration_s", 8),
    ("distance_m", 9),
    ("avg_speed_mps", 13),
    ("max_speed_mps", 14),
    ("avg_hr", 15),
    ("max_hr", 16),
    ("avg_cadence", 17),
    ("avg_power", 19),
    ("max_power", 20),
    ("elevation_gain_m", 21),
    ("normalized_power", 33),
)
# FIT lap intensity: 0 active, 1 rest, 4 recovery.
_ACTIVE_INTENSITY: Final = 0
_REST_INTENSITIES: Final = frozenset({1, 4})
_LAP_KINDS: Final[Mapping[int, LapKind]] = {
    0: LapKind.LAP,
    1: LapKind.RECOVERY,
    2: LapKind.WARMUP,
    3: LapKind.COOLDOWN,
    4: LapKind.RECOVERY,
    5: LapKind.WORK,
}
# Breathing rate (record 108, lap 136 and 137) and the device's lap GAP (161), each
# with its divisor to the canonical unit. Their bounds are the canonical types'.
_RESPIRATION_FIELD: Final = 108
_RESPIRATION_DIVISOR: Final = 100.0
# Garmin's undocumented record fields (#3374): performance condition (90, points
# -20..+20) and two stamina fields (137, 138, percent), all unscaled. A probe of
# the athlete's files found them only in runs and rides and always in range.
_UNOFFICIAL_FIELDS: Final[tuple[tuple[StreamChannel, int], ...]] = (
    (StreamChannel.PERFORMANCE_CONDITION, 90),
    (StreamChannel.STAMINA_137_PCT, 137),
    (StreamChannel.STAMINA_138_PCT, 138),
)
_LAP_DEVICE_FIELDS: Final[tuple[tuple[str, int, float], ...]] = (
    ("avg_respiration_rate_brpm", 136, 100.0),
    ("max_respiration_rate_brpm", 137, 100.0),
    ("avg_grade_adjusted_speed_mps", 161, 1000.0),
)
_LAP_TRIGGER_FIELD: Final = 24
_LAP_STEP_FIELD: Final = 71
# FIT ``lap_trigger`` numbers 0-8, in profile order.
_FIT_LAP_TRIGGERS: Final[tuple[LapTrigger, ...]] = (
    LapTrigger.MANUAL,
    LapTrigger.TIME,
    LapTrigger.DISTANCE,
    LapTrigger.POSITION_START,
    LapTrigger.POSITION_LAP,
    LapTrigger.POSITION_WAYPOINT,
    LapTrigger.POSITION_MARKED,
    LapTrigger.SESSION_END,
    LapTrigger.FITNESS_EQUIPMENT,
)
_WORKOUT_MESSAGE: Final = 26
_WORKOUT_STEP_MESSAGE: Final = 27
_STEP_INDEX_FIELD: Final = 254
_STEP_DURATION_TYPE_FIELD: Final = 1
_STEP_DURATION_VALUE_FIELD: Final = 2
_STEP_TARGET_TYPE_FIELD: Final = 3
_STEP_TARGET_VALUE_FIELD: Final = 4
_STEP_TARGET_LOW_FIELD: Final = 5
_STEP_TARGET_HIGH_FIELD: Final = 6
_STEP_INTENSITY_FIELD: Final = 7
# FIT ``wkt_step_duration``; the repeat_until_* types other than steps complete
# have no count to show and map to ``other``.
_REPEAT_UNTIL_STEPS_COMPLETE: Final = 6
_STEP_DURATIONS: Final[Mapping[int, WorkoutDurationType]] = {
    0: WorkoutDurationType.TIME,
    1: WorkoutDurationType.DISTANCE,
    2: WorkoutDurationType.HR_BELOW,
    3: WorkoutDurationType.HR_ABOVE,
    5: WorkoutDurationType.OPEN,
    _REPEAT_UNTIL_STEPS_COMPLETE: WorkoutDurationType.REPEAT,
    14: WorkoutDurationType.POWER_BELOW,
    15: WorkoutDurationType.POWER_ABOVE,
    29: WorkoutDurationType.REPS,
}
_STEP_INTENSITIES: Final[Mapping[int, WorkoutStepIntensity]] = {
    0: WorkoutStepIntensity.ACTIVE,
    1: WorkoutStepIntensity.REST,
    2: WorkoutStepIntensity.WARMUP,
    3: WorkoutStepIntensity.COOLDOWN,
    4: WorkoutStepIntensity.RECOVERY,
    5: WorkoutStepIntensity.INTERVAL,
}
_STEP_TARGETS: Final[Mapping[int, WorkoutTargetType]] = {
    0: WorkoutTargetType.SPEED,
    1: WorkoutTargetType.HEART_RATE,
    2: WorkoutTargetType.OPEN,
    3: WorkoutTargetType.CADENCE,
    4: WorkoutTargetType.POWER,
}
# A heart rate target or duration above this value is bpm plus this offset, up to
# it a percent of the maximum; power counts the same way in watts and FTP.
_HEART_RATE_OFFSET: Final = 100
_POWER_OFFSET: Final = 1000
_ZONES_TARGET_MESSAGE: Final = 7
_HR_CALCS: Final[Mapping[int, DeviceHeartRateCalc]] = {
    0: DeviceHeartRateCalc.CUSTOM,
    1: DeviceHeartRateCalc.PERCENT_MAX_HR,
    2: DeviceHeartRateCalc.PERCENT_HRR,
    3: DeviceHeartRateCalc.PERCENT_LTHR,
}
_POWER_CALCS: Final[Mapping[int, DevicePowerCalc]] = {
    0: DevicePowerCalc.CUSTOM,
    1: DevicePowerCalc.PERCENT_FTP,
}


def map_fit_activity_detail(
    data: object,
    *,
    provider_activity_id: str,
    provenance: Provenance,
    strength_sets: bool = True,
) -> MappingResult[ActivityDetailSet]:
    """Map one FIT activity file into stream, lap and strength sets (origin FETCH).

    ``strength_sets=False`` skips the set messages, so a caller with a better
    strength source gets neither their set nor their issues.
    """
    ref = require_text(
        provider_activity_id,
        "provider_activity_id",
        max_length=MAX_FIT_ACTIVITY_ID_LENGTH,
    )
    require_instance(provenance, Provenance, "provenance")
    if provenance.provider is None:
        raise ValueError("provenance.provider is required for activity details")
    # The whole file is skipped; the field names why, as a bounded code (#3337).
    if not isinstance(data, bytes):
        return MappingResult(
            issues=(MappingIssue(ref, "fit_not_bytes", IssueReason.INVALID_TYPE),)
        )
    try:
        messages = fit_messages(data)
    except FitDecodeError as error:
        failed = MappingIssue(ref, f"fit_{error.code}", IssueReason.INVALID_TYPE)
        return MappingResult(issues=(failed,))
    issues: list[MappingIssue] = []
    records: list[ActivityDetailSet] = []
    stream_set = _stream_set(ref, provenance, messages, issues)
    if stream_set is not None:
        records.append(stream_set)
    lap_set = _lap_set(ref, provenance, messages, issues)
    if lap_set is not None:
        records.append(lap_set)
    strength_set = (
        _strength_set_set(ref, provenance, messages, issues) if strength_sets else None
    )
    if strength_set is not None:
        records.append(strength_set)
    return MappingResult(records=tuple(records), issues=tuple(issues))


def _add_issue(
    issues: list[MappingIssue], ref: str, field: str, reason: IssueReason
) -> None:
    """Append one issue; identical (field, reason) pairs are reported once."""
    issue = MappingIssue(ref, field, reason)
    if issue not in issues:
        issues.append(issue)


def _first_record_timestamp(messages: list[FitMessage]) -> float | None:
    for number, fields in messages:
        if number == _RECORD_MESSAGE:
            stamp = fit_field(fields, _RECORD_MESSAGE, _TIMESTAMP_FIELD)
            if stamp is not None:
                return stamp
    return None


def _kept_records(
    ref: str, messages: list[FitMessage], issues: list[MappingIssue]
) -> list[tuple[float, Mapping[int, object]]]:
    """Return (seconds from time zero, fields) for each usable record.

    A record is skipped when it has no usable timestamp, when its timestamp is
    not strictly after the previous kept one, or when it lies beyond the stream
    span. The timestamp field gets at most one issue per reason: MISSING when any
    record lacks a usable timestamp, OUT_OF_RANGE when any record was skipped.
    """
    zero: float | None = None
    last: float | None = None
    missing = False
    out_of_range = False
    kept: list[tuple[float, Mapping[int, object]]] = []
    for number, fields in messages:
        if number != _RECORD_MESSAGE:
            continue
        stamp = fit_field(fields, _RECORD_MESSAGE, _TIMESTAMP_FIELD)
        if stamp is None:
            missing = True
            continue
        if zero is None:
            zero = stamp
        if last is not None and stamp <= last:
            out_of_range = True
            continue
        offset = stamp - zero
        if offset > MAX_STREAM_SPAN_S:
            out_of_range = True
            continue
        last = stamp
        kept.append((offset, fields))
    if missing:
        _add_issue(issues, ref, "timestamp", IssueReason.MISSING)
    if out_of_range:
        _add_issue(issues, ref, "timestamp", IssueReason.OUT_OF_RANGE)
    return kept


def _cadence(fields: Mapping[int, object]) -> float | None:
    """Whole cadence plus the fractional part when the device records one."""
    whole = fit_field(fields, _RECORD_MESSAGE, _CADENCE_FIELD)
    if whole is None:
        return None
    fraction = fit_field(fields, _RECORD_MESSAGE, _FRACTIONAL_CADENCE_FIELD)
    return whole + (fraction or 0.0)


def _measured(value: float | None, divisor: float) -> float | None:
    """A running dynamics reading in canonical units; 0 means not measured."""
    return value / divisor if value else None


def _scaled(fields: Mapping[int, object], field: int, divisor: float) -> float | None:
    """One raw field in its canonical unit; a missing, invalid or array value is None."""
    value = fields.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value / divisor if math.isfinite(value) else None


def _add_channel(
    channels: dict[StreamChannel, tuple[float | None, ...]],
    ref: str,
    channel: StreamChannel,
    values: list[float | None],
    issues: list[MappingIssue],
    *,
    report: bool = True,
) -> None:
    """Clean one channel; a channel without any usable point is dropped.

    ``report=False`` leaves out the issue for rejected points: an issue makes a
    Garmin detail fetch partial, and a channel added later must not do that.
    """
    points, rejected = clean_series(channel, values)
    if rejected and report:
        _add_issue(issues, ref, channel.value, IssueReason.OUT_OF_RANGE)
    if any(point is not None for point in points):
        channels[channel] = points


def _add_sensor_channels(
    channels: dict[StreamChannel, tuple[float | None, ...]],
    ref: str,
    rows: list[Mapping[int, object]],
    issues: list[MappingIssue],
) -> None:
    """Add the running dynamics and the GPS position channels of the records."""
    for channel, field_number, divisor in _DYNAMICS_FIELDS:
        measured = [
            _measured(fit_field(fields, _RECORD_MESSAGE, field_number), divisor)
            for fields in rows
        ]
        _add_channel(channels, ref, channel, measured, issues)
    for channel, field_number in _POSITION_FIELDS:
        degrees = [
            None if raw is None else raw * _SEMICIRCLE_DEG
            for raw in (
                fit_field(fields, _RECORD_MESSAGE, field_number) for fields in rows
            )
        ]
        _add_channel(channels, ref, channel, degrees, issues)
    _add_channel(
        channels,
        ref,
        StreamChannel.RESPIRATION_RATE_BRPM,
        [_scaled(fields, _RESPIRATION_FIELD, _RESPIRATION_DIVISOR) for fields in rows],
        issues,
        report=False,
    )


def _stream_set(
    ref: str,
    provenance: Provenance,
    messages: list[FitMessage],
    issues: list[MappingIssue],
) -> ActivityStreamSet | None:
    kept = _kept_records(ref, messages, issues)
    if len(kept) > MAX_STREAM_POINTS:
        _add_issue(issues, ref, "record", IssueReason.OUT_OF_RANGE)
        return None
    # One record has no rate to show, and a time axis alone is not a stream set.
    if len(kept) < 2:
        return None
    rows = [fields for _, fields in kept]
    channels: dict[StreamChannel, tuple[float | None, ...]] = {
        StreamChannel.TIME_S: tuple(offset for offset, _ in kept)
    }
    for channel, field_number in _RECORD_CHANNEL_FIELDS:
        values = [fit_field(fields, _RECORD_MESSAGE, field_number) for fields in rows]
        _add_channel(channels, ref, channel, values, issues)
    _add_channel(
        channels,
        ref,
        StreamChannel.CADENCE_RPM,
        [_cadence(fields) for fields in rows],
        issues,
    )
    _add_sensor_channels(channels, ref, rows, issues)
    # The undocumented fields mean what the probe found only in a Garmin recording.
    if provenance.recorded_by is RecordingOrigin.GARMIN:
        for channel, field_number in _UNOFFICIAL_FIELDS:
            values = [_scaled(fields, field_number, 1.0) for fields in rows]
            _add_channel(channels, ref, channel, values, issues, report=False)
    if len(channels) < 2:
        return None
    hr_source, power_source, sensors = sensor_profile(
        messages,
        provider=provenance.provider,
        has_heart_rate=StreamChannel.HEART_RATE_BPM in channels,
        has_power=StreamChannel.POWER_W in channels,
    )
    try:
        return ActivityStreamSet(
            provider_activity_id=ref,
            provenance=provenance,
            origin=ActivityDetailOrigin.FETCH,
            resolution=StreamResolution.FULL,
            channels=channels,
            hr_source=hr_source,
            power_source=power_source,
            sensors=sensors,
            dfa_alpha1=_first_threshold(messages, hr_source),
            device_zones=_device_zones(messages),
        )
    except TypeError, ValueError:
        _add_issue(issues, ref, "record", IssueReason.OUT_OF_RANGE)
        return None


def _first_threshold(
    messages: list[FitMessage], hr_source: HeartRateSource | None
) -> ActivityDfaAlpha1 | None:
    """The DFA-a1 estimate of a chest-strap recording; only this result is kept."""
    if hr_source is not HeartRateSource.CHEST_STRAP:
        return None
    return first_threshold(rr_intervals_ms(messages))


def _lap_set(
    ref: str,
    provenance: Provenance,
    messages: list[FitMessage],
    issues: list[MappingIssue],
) -> ActivityLapSet | None:
    rows: list[Mapping[int, object]] = [
        fields for number, fields in messages if number == _LAP_MESSAGE
    ]
    if not rows:
        return None
    starts: list[tuple[float, Mapping[int, object]]] = []
    for fields in rows:
        start = fit_field(fields, _LAP_MESSAGE, _LAP_START_FIELD)
        if start is None:
            _add_issue(issues, ref, "start_time", IssueReason.MISSING)
            continue
        starts.append((start, fields))
    starts.sort(key=lambda item: item[0])
    # Time zero is the first record timestamp; without records, the first lap.
    zero = _first_record_timestamp(messages)
    if zero is None:
        if not starts:
            return None
        zero = starts[0][0]
    # An ``active`` lap only means a work step beside rest or recovery laps.
    active_is_work = any(
        _lap_intensity(fields) in _REST_INTENSITIES for _, fields in starts
    )
    kept: list[ActivityLap] = []
    for start, fields in starts:
        lap = _lap(ref, start - zero, fields, issues, active_is_work=active_is_work)
        if lap is not None:
            kept.append(lap)
    if len(kept) > MAX_LAPS:
        _add_issue(issues, ref, "lap", IssueReason.OUT_OF_RANGE)
        del kept[MAX_LAPS:]
    if not kept:
        return None
    try:
        return ActivityLapSet(
            provider_activity_id=ref,
            provenance=provenance,
            origin=ActivityDetailOrigin.FETCH,
            lap_source=LapSource.DEVICE_LAPS,
            laps=tuple(
                cast(ActivityLap, replace(lap, index=position))
                for position, lap in enumerate(kept)
            ),
            workout_steps=_workout_steps(messages),
        )
    except TypeError, ValueError:
        _add_issue(issues, ref, "lap", IssueReason.OUT_OF_RANGE)
        return None


def _lap(
    ref: str,
    start_offset: float,
    fields: Mapping[int, object],
    issues: list[MappingIssue],
    *,
    active_is_work: bool = False,
) -> ActivityLap | None:
    """Build one lap, or return None with the issue that drops it."""
    if start_offset < 0:
        _add_issue(issues, ref, "start_time", IssueReason.OUT_OF_RANGE)
        return None
    elapsed = fit_field(fields, _LAP_MESSAGE, _LAP_ELAPSED_FIELD)
    if elapsed is None:
        _add_issue(issues, ref, "total_elapsed_time", IssueReason.MISSING)
        return None
    metrics: dict[str, Any] = {}
    for name, field_number in _LAP_METRIC_FIELDS:
        value = fit_field(fields, _LAP_MESSAGE, field_number)
        if value is None:
            continue
        if _lap_accepts(name, value):
            metrics[name] = value
        else:
            _add_issue(issues, ref, name, IssueReason.OUT_OF_RANGE)
    # The device's own values never raise and never make an issue: out of bounds is None.
    device: dict[str, Any] = {
        name: _scaled(fields, field_number, divisor)
        for name, field_number, divisor in _LAP_DEVICE_FIELDS
    }
    try:
        return ActivityLap(
            index=0,
            kind=_lap_kind(fields, active_is_work),
            start_offset_s=start_offset,
            end_offset_s=start_offset + elapsed,
            workout_step_index=_step_index(fields.get(_LAP_STEP_FIELD)),
            lap_trigger=_lap_trigger(fields.get(_LAP_TRIGGER_FIELD)),
            **metrics,
            **device,
        )
    except TypeError, ValueError:
        _add_issue(issues, ref, "lap", IssueReason.OUT_OF_RANGE)
        return None


def _lap_intensity(fields: Mapping[int, object]) -> int | None:
    intensity = fields.get(_LAP_INTENSITY_FIELD)
    if isinstance(intensity, int) and not isinstance(intensity, bool):
        return intensity
    return None


def _lap_kind(fields: Mapping[int, object], active_is_work: bool) -> LapKind:
    intensity = _lap_intensity(fields)
    if intensity == _ACTIVE_INTENSITY and active_is_work:
        return LapKind.WORK
    if intensity is None:
        return LapKind.LAP
    return _LAP_KINDS.get(intensity, LapKind.LAP)


def _lap_accepts(name: str, value: float) -> bool:
    """Whether ActivityLap accepts one metric, so its bounds are not repeated here."""
    metric: dict[str, Any] = {name: value}
    try:
        ActivityLap(
            index=0,
            kind=LapKind.LAP,
            start_offset_s=0.0,
            end_offset_s=0.0,
            **metric,
        )
    except TypeError, ValueError:
        return False
    return True


def _strength_set_set(
    ref: str,
    provenance: Provenance,
    messages: list[FitMessage],
    issues: list[MappingIssue],
) -> ActivityStrengthSetSet | None:
    rows = [fields for number, fields in messages if number == _SET_MESSAGE]
    if not rows:
        return None
    zero = _first_record_timestamp(messages)
    if zero is None:
        starts = [
            start
            for fields in rows
            if (start := fit_field(fields, _SET_MESSAGE, _SET_START_FIELD)) is not None
        ]
        zero = min(starts) if starts else None
    kept: list[ActivityStrengthSet] = []
    for fields in rows:
        if len(kept) == MAX_STRENGTH_SETS:
            _add_issue(issues, ref, "set", IssueReason.OUT_OF_RANGE)
            break
        item = _strength_set(ref, len(kept), zero, fields, issues)
        if item is not None:
            kept.append(item)
    if not kept:
        return None
    return ActivityStrengthSetSet(
        provider_activity_id=ref,
        provenance=provenance,
        origin=ActivityDetailOrigin.FETCH,
        source=StrengthSetSource.FIT_SET_MESSAGES,
        sets=tuple(kept),
    )


def _strength_set(
    ref: str,
    index: int,
    zero: float | None,
    fields: Mapping[int, object],
    issues: list[MappingIssue],
) -> ActivityStrengthSet | None:
    """Build one set, or return None with the issue that drops it."""
    raw_type = _whole(fields.get(_SET_TYPE_FIELD))
    set_type = None if raw_type is None else _SET_TYPES.get(raw_type)
    if set_type is None:
        _add_issue(issues, ref, "set_type", IssueReason.MISSING)
        return None
    values: dict[str, Any] = {
        "reps": _whole(fields.get(_SET_REPETITIONS_FIELD)),
        # 0 kg is no external load, never a stand-in value.
        "weight_kg": fit_field(fields, _SET_MESSAGE, _SET_WEIGHT_FIELD) or None,
        "duration_s": fit_field(fields, _SET_MESSAGE, _SET_DURATION_FIELD),
        "workout_step_index": _step_index(fields.get(_SET_STEP_FIELD)),
        "start_offset_s": None,
    }
    start = fit_field(fields, _SET_MESSAGE, _SET_START_FIELD)
    if start is not None and zero is not None and start >= zero:
        values["start_offset_s"] = start - zero
    for name, value in values.items():
        if value is not None and not strength_set_accepts(name, value):
            _add_issue(issues, ref, name, IssueReason.OUT_OF_RANGE)
            values[name] = None
    return ActivityStrengthSet(
        index=index,
        set_type=set_type,
        exercise_category=_category(fields.get(_SET_CATEGORY_FIELD)),
        **values,
    )


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _step_index(value: object) -> int | None:
    step = _whole(value)
    return None if step is None else step & _MESSAGE_INDEX_MASK


def _category(value: object) -> ExerciseCategory | None:
    """The first valid category of the FIT array (a single value decodes bare)."""
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        number = _whole(entry)
        if number is None or number == _FIT_UNKNOWN_CATEGORY:
            continue
        if number < len(_FIT_EXERCISE_CATEGORIES):
            return _FIT_EXERCISE_CATEGORIES[number]
        return ExerciseCategory.OTHER
    return None


def _lap_trigger(value: object) -> LapTrigger | None:
    number = _whole(value)
    if number is None or not 0 <= number < len(_FIT_LAP_TRIGGERS):
        return None
    return _FIT_LAP_TRIGGERS[number]


def _workout_steps(messages: list[FitMessage]) -> tuple[WorkoutStep, ...]:
    """The workout steps of the file by ascending index, at most MAX_WORKOUT_STEPS.

    A step without a usable index is dropped, and so is a later one that repeats
    an index. This never raises.
    """
    steps: dict[int, WorkoutStep] = {}
    for number, fields in messages:
        if number != _WORKOUT_STEP_MESSAGE or len(steps) == MAX_WORKOUT_STEPS:
            continue
        step = _workout_step(fields)
        if step is not None:
            steps.setdefault(step.index, step)
    return tuple(steps[index] for index in sorted(steps))


def _workout_step(fields: Mapping[int, object]) -> WorkoutStep | None:
    """One step from its numeric fields; the name (0) and notes (8) are never read."""
    index = _step_index(fields.get(_STEP_INDEX_FIELD))
    if index is None or index > MAX_WORKOUT_STEP_INDEX:
        return None
    duration_type = _choice(
        _STEP_DURATIONS,
        fields.get(_STEP_DURATION_TYPE_FIELD),
        WorkoutDurationType.OTHER,
    )
    values: dict[str, Any] = {
        "index": index,
        "intensity": _choice(
            _STEP_INTENSITIES,
            fields.get(_STEP_INTENSITY_FIELD),
            WorkoutStepIntensity.OTHER,
        ),
        "duration_type": duration_type,
    }
    duration = fields.get(_STEP_DURATION_VALUE_FIELD)
    zone = _whole(fields.get(_STEP_TARGET_VALUE_FIELD))
    if duration_type is WorkoutDurationType.REPEAT:
        # A repeat step names the step to return to and, as its target value, the count.
        values["repeat_from"] = _step_index(duration)
        values["repeat_count"] = zone
        values["target_type"] = WorkoutTargetType.OPEN
    else:
        values["duration_value"] = _duration_value(duration_type, _whole(duration))
        values.update(_step_target(fields, zone))
    try:
        return WorkoutStep(**values)
    except TypeError, ValueError:
        return None


def _choice[T](table: Mapping[int, T], value: object, default: T) -> T:
    """The member a FIT enum number names, else the default (also for no number)."""
    number = _whole(value)
    return default if number is None else table.get(number, default)


def _duration_value(kind: WorkoutDurationType, raw: int | None) -> float | None:
    if raw is None:
        return None
    if kind is WorkoutDurationType.TIME:
        return raw / 1000
    if kind is WorkoutDurationType.DISTANCE:
        return raw / 100
    if kind is WorkoutDurationType.REPS:
        return float(raw)
    if kind in (WorkoutDurationType.HR_BELOW, WorkoutDurationType.HR_ABOVE):
        return raw - _HEART_RATE_OFFSET if raw > _HEART_RATE_OFFSET else None
    if kind in (WorkoutDurationType.POWER_BELOW, WorkoutDurationType.POWER_ABOVE):
        return raw - _POWER_OFFSET if raw > _POWER_OFFSET else None
    return None


def _step_target(fields: Mapping[int, object], zone: int | None) -> dict[str, Any]:
    """The target type with its zone (target value 4) or custom range (5 and 6)."""
    target_type = _choice(
        _STEP_TARGETS, fields.get(_STEP_TARGET_TYPE_FIELD), WorkoutTargetType.OTHER
    )
    target: dict[str, Any] = {"target_type": target_type}
    if target_type in (WorkoutTargetType.OPEN, WorkoutTargetType.OTHER):
        return target
    if zone:
        target["target_zone"] = zone
        return target
    raw_bounds = (
        _whole(fields.get(_STEP_TARGET_LOW_FIELD)),
        _whole(fields.get(_STEP_TARGET_HIGH_FIELD)),
    )
    # Zero on both sides is the device's way of saying that no range is set.
    if not any(raw_bounds):
        return target
    bounds = [
        None if raw is None else _target_bound(target_type, raw) for raw in raw_bounds
    ]
    units = {bound[0] for bound in bounds if bound is not None}
    # One unit per range: a bpm low with a percent high is not a range.
    if len(units) == 1:
        target["target_unit"] = units.pop()
        target["target_low"], target["target_high"] = (
            None if bound is None else bound[1] for bound in bounds
        )
    return target


def _target_bound(
    target_type: WorkoutTargetType, raw: int
) -> tuple[WorkoutTargetUnit, float]:
    """One bound of a custom target range in its canonical unit."""
    if target_type is WorkoutTargetType.SPEED:
        return WorkoutTargetUnit.MPS, raw / 1000
    if target_type is WorkoutTargetType.HEART_RATE:
        if raw > _HEART_RATE_OFFSET:
            return WorkoutTargetUnit.BPM, float(raw - _HEART_RATE_OFFSET)
        return WorkoutTargetUnit.PCT_MAX_HR, float(raw)
    if target_type is WorkoutTargetType.POWER:
        if raw > _POWER_OFFSET:
            return WorkoutTargetUnit.W, float(raw - _POWER_OFFSET)
        return WorkoutTargetUnit.PCT_FTP, float(raw)
    return WorkoutTargetUnit.RPM, float(raw)


def _device_zones(messages: list[FitMessage]) -> DeviceZoneBasis | None:
    """The watch's zone basis from the first zones_target message that holds one."""
    for number, fields in messages:
        if number != _ZONES_TARGET_MESSAGE:
            continue
        zones = DeviceZoneBasis(
            max_hr=_whole(fields.get(1)),
            threshold_hr=_whole(fields.get(2)),
            ftp_w=_whole(fields.get(3)),
            hr_calc=_choice(_HR_CALCS, fields.get(5), None),
            power_calc=_choice(_POWER_CALCS, fields.get(7), None),
        )
        if not zones.is_empty:
            return zones
    return None


# Fields each mapped message is read for (#2220); 253 (timestamp) and 254
# (message index) are structural and never counted.
_STRUCTURAL_FIELDS: Final = frozenset({253, 254})
MAPPED_FIELDS: Final[Mapping[int, frozenset[int]]] = {
    _RECORD_MESSAGE: frozenset(
        {_CADENCE_FIELD, _FRACTIONAL_CADENCE_FIELD, 73, 78, _RESPIRATION_FIELD}
        | {field for _channel, field in _RECORD_CHANNEL_FIELDS}
        | {field for _channel, field, _divisor in _DYNAMICS_FIELDS}
        | {field for _channel, field in _POSITION_FIELDS}
        | {field for _channel, field in _UNOFFICIAL_FIELDS}
    ),
    _LAP_MESSAGE: frozenset(
        {_LAP_START_FIELD, _LAP_ELAPSED_FIELD, _LAP_INTENSITY_FIELD, 110, 111}
        | {_LAP_TRIGGER_FIELD, _LAP_STEP_FIELD}
        | {field for _name, field in _LAP_METRIC_FIELDS}
        | {field for _name, field, _divisor in _LAP_DEVICE_FIELDS}
    ),
    _SET_MESSAGE: frozenset(
        {
            _SET_DURATION_FIELD,
            _SET_REPETITIONS_FIELD,
            _SET_WEIGHT_FIELD,
            _SET_TYPE_FIELD,
            _SET_START_FIELD,
            _SET_CATEGORY_FIELD,
            _SET_STEP_FIELD,
        }
    ),
    DEVICE_INFO_MESSAGE: frozenset({0, 1, 2, 3, 4, 25}),
    _ZONES_TARGET_MESSAGE: frozenset({1, 2, 3, 5, 7}),
    # The step name (0) and notes (8) stay unread: free text of the file.
    _WORKOUT_STEP_MESSAGE: frozenset(
        {
            _STEP_DURATION_TYPE_FIELD,
            _STEP_DURATION_VALUE_FIELD,
            _STEP_TARGET_TYPE_FIELD,
            _STEP_TARGET_VALUE_FIELD,
            _STEP_TARGET_LOW_FIELD,
            _STEP_TARGET_HIGH_FIELD,
            _STEP_INTENSITY_FIELD,
        }
    ),
}
# Field description (206): developer data index (0) and field number (1).
_FIELD_DESCRIPTION_MESSAGE: Final = 206


def fit_inventory(data: object) -> frozenset[str] | None:
    """What one FIT file holds, as count keys only (#2220); ``None`` if unreadable.

    ``m<number>`` per unmapped message number present, ``f<message>.<field>`` per field of
    a mapped message with a usable value that the mapper does not read, and
    ``d<index>.<field>`` per declared developer field. Numbers only: never a
    value, name, timestamp or position. Pure and never raises for bad bytes.
    """
    if not isinstance(data, bytes):
        return None
    try:
        messages = fit_messages(data)
    except ValueError:
        return None
    keys: set[str] = set()
    for number, fields in messages:
        # The hrv and workout (name, description) messages are never read whole.
        if number not in MAPPED_FIELDS and number not in (
            HRV_MESSAGE,
            _WORKOUT_MESSAGE,
        ):
            keys.add(f"m{number}")
        if number == _FIELD_DESCRIPTION_MESSAGE:
            index, field = _whole(fields.get(0)), _whole(fields.get(1))
            if index is not None and field is not None:
                keys.add(f"d{index}.{field}")
        mapped = MAPPED_FIELDS.get(number)
        if mapped is None:
            continue
        keys.update(
            f"f{number}.{field}"
            for field, value in fields.items()
            if field not in mapped
            and field not in _STRUCTURAL_FIELDS
            and _usable(value)
        )
    return frozenset(keys)


def _usable(value: object) -> bool:
    if isinstance(value, list | tuple):
        return any(item is not None for item in value)
    return value is not None
