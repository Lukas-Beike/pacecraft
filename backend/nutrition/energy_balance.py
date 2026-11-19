"""Adaptive daily expenditure from intake and the weight trend (#3100).

Pure functions over one ``EnergyInputs``: the lag-free current weight, rolling
4-week windows with the adaptive TDEE (mean logged intake minus the change in
stored energy), the one shrunken activity calibration factor, the derived
resting rate and the bias of meals flagged as uncertain. Results are bands, not
points, and a window without enough data says why instead of falling back to
a formula. The activity and TEF terms come from M11
(``backend/performance/activity_energy.py``).
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, NamedTuple, cast

from backend.performance.activity_energy import TEF_ERROR_KCAL, tef_kcal

METHOD_ID = "adaptive-expenditure"
# Bump when a formula or constant below changes the estimate.
METHOD_VERSION = 7
# Days per window and windows per week of the rolling view (#3099, #3100).
WINDOW_DAYS = 28
# A day's current weight is the least-squares line through the weigh-ins of
# this many days up to it, at its latest weigh-in, so a steady change has no
# lag (#3551); with fewer weigh-ins or a shorter span, their mean.
CURRENT_WEIGHT_DAYS = 28
MIN_LINE_WEIGHINS = 3
MIN_LINE_SPAN_DAYS = 7
# A window needs this many complete logging days, weigh-ins and days between
# its first and last weigh-in; otherwise it gives its reason, never a formula.
MIN_COMPLETE_DAYS = 14
MIN_WEIGHINS = 8
MIN_WEIGHT_SPAN_DAYS = 21
# A day counts as complete with this many diary entries.
MIN_DAY_ENTRIES = 2
# A complete day also needs an intake of at least this share of the resting rate plus the day's
# TEF (#3553): independent of its training, so a fully logged long day counts;
# an intake below it lies far beneath any planned deficit, so meals are missing
# (product assumption).
MIN_DAY_REST_SHARE = 0.7
# The former rule's share of the day's whole expenditure (resting + own
# activity + TEF); only the diagnostics count the complete days it dropped.
FORMER_DAY_SHARE = 0.6
# Hall 2008 (Int J Obes 32:573): 39.5 MJ per kg fat and 7.6 MJ per kg lean
# tissue, in kcal/kg.
FAT_KCAL_PER_KG = 9441.0
LEAN_KCAL_PER_KG = 1816.0
# The fat share of a weight change is a precision-weighted blend of a prior
# and the slope of the phase's measured fat mass (athlete decision, #3555):
# the prior 0.70 +- 0.15 lies between Forbes' curve (about 0.55 at this body
# fat) and the 0.80 of strength training with high protein, and covers both.
FAT_SHARE_PRIOR = 0.70
FAT_SHARE_PRIOR_SD = 0.15
# Repeatability of one self-measured body fat value (percentage points, SD):
# the upper end of the review's 1.5-2 for skinfolds (#3555).
BODY_FAT_REPEATABILITY_PCT = 2.0
# Below this weight change between the first and last measurement the measured
# share divides by noise, so the prior stands alone (product assumption).
MIN_FAT_SHARE_WEIGHT_CHANGE_KG = 0.1
# Extra uncertainty of a flagged (estimated) meal: a share of its logged kcal.
FLAGGED_DAY_UNCERTAINTY = 0.25
# Two-sided 95 % band multiplier.
BAND_Z = 1.96
# Two-sided 95 % Student t quantiles by degrees of freedom (standard table);
# a window's band uses the smaller of its intake (days - 1) and weight-slope
# (weigh-ins - 2) degrees of freedom, BAND_Z beyond the table (#3187 item 10).
T_QUANTILE_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
    14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
    20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042, 40: 2.021,
    60: 2.000, 120: 1.980,
}  # fmt: skip
# The shortest span between the first and last measurement that informs the
# share: a shorter one divides the measurement noise by too few days.
MIN_FAT_SPAN_DAYS = 14
# The one activity calibration factor: prior 1 +- 0.10, clamped to 0.85-1.15,
# fitted on complete windows of the last 12 weeks (method comment, #3100).
CALIBRATION_PRIOR_SD = 0.10
CALIBRATION_BOUNDS = (0.85, 1.15)
# Windows needed before the factor moves from 1, and before a flagged-meal
# bias is estimated.
MIN_CALIBRATION_WINDOWS = 4
MIN_BIAS_WINDOWS = 4


@dataclass(frozen=True)
class Estimate:
    kcal: float
    band_kcal: float  # half-width of the shown band


@dataclass(frozen=True)
class DayInputs:
    """One day: logged intake, its macros and flags, weigh-in and own activity."""

    intake_kcal: float | None = None
    protein_g: float = 0.0
    carbohydrate_g: float = 0.0
    fat_g: float = 0.0
    entries: int = 0
    flagged: bool = False
    # Logged kcal of the day's flagged (estimated) entries (#3187 item 13).
    flagged_kcal: float = 0.0
    weight_kg: float | None = None
    activity_kcal: float | None = None  # M11 session net cost + NEAT, uncalibrated
    # Its 95 % error from each part's M11 method share (#3552); 0 without it.
    activity_error_kcal: float = 0.0
    # Its parts (split A, #3100): NEAT net of rest, and the day's priced
    # sessions (0.0 without any); None where unknown, never 0 (#3187).
    neat_kcal: float | None = None
    sport_kcal: float | None = None
    # NEAT's inputs, shown in the split's basis (#3255): the device's day
    # steps, the steps its sessions take out, and the floors outside them.
    steps: float | None = None
    session_steps: float = 0.0
    floors: float | None = None


@dataclass(frozen=True)
class EnergyInputs:
    days: Mapping[date, DayInputs]
    body_fat_pct: Mapping[date, float]  # skinfold measurements by date
    # Resting reference for the net terms and the completeness rule: own
    # Katch-McArdle from skinfold lean mass first, Garmin only as the last
    # fallback (orchestrator, 2026-11-15); the source is shown with it.
    rmr_kcal: float | None
    rmr_source: str | None = None  # 'katch_mcardle' | 'garmin' | None
    # NEAT's step length and where it comes from: 'walks' or 'height' (#3255).
    step_m: float | None = None
    step_source: str | None = None
    # The formula rows' body fat without a skinfold (#3362): a device's or
    # scale's per day with its provider, then the profile's.
    device_body_fat: Mapping[date, tuple[float, Any]] = field(default_factory=dict)
    profile_body_fat_pct: float | None = None


class StoredEnergy(NamedTuple):
    """Energy per day that went into (+) or came from (-) the body stores, its
    standard error and how fat and lean mass were split, with the terms the
    Verlauf deficit card shows (#3548)."""

    kcal_per_day: float
    sd: float
    method: str
    # Days between the first and the last weigh-in or measurement used.
    days: int
    weight_start_kg: float
    weight_end_kg: float
    fat_change_kg: float
    # The first and last measurement that informed the share, else only the
    # latest one, if any.
    fat_pct_start: float | None
    fat_pct_end: float | None
    # The main source of the error: "fat_share" (the blended split) or
    # "weight_scatter" (the weigh-ins around their line).
    uncertainty: str
    # The blended fat share and the measurements' weight in it, 0 to 1 (#3555).
    fat_share: float = FAT_SHARE_PRIOR
    measured_weight: float = 0.0


@dataclass(frozen=True)
class EnergyWindow:
    end: date
    days: int
    complete_days: int
    flagged_days: int
    weighins: int
    activity_kcal: float | None
    activity_kcal_raw: float | None
    tef_kcal: float | None
    # All complete days count; flagged entries widen the band (#3187 item 14).
    adaptive_tdee: Estimate | None
    # Only from a window without any flagged day, else None.
    adaptive_tdee_unflagged: Estimate | None
    derived_rmr: Estimate | None
    insufficient: str | None
    rmr_source: str | None = None
    # Windows the calibration fit used; below MIN_CALIBRATION_WINDOWS the
    # factor is still the prior 1 and the display says so.
    calibration_windows: int = 0
    # Flagged entries' logged kcal per complete day of the window.
    flagged_kcal_per_day: float = 0.0
    # With the learned flagged-meal bias b: flagged kcal x (1 + b), labelled;
    # None until the bias is estimated. Logged entries are never rewritten.
    adaptive_tdee_corrected: Estimate | None = None
    # Split A (#3100), kcal/day over the same days as activity_kcal_raw, so
    # neat + sport is the activity; None when any of those days lacks a part.
    neat_kcal_raw: float | None = None
    sport_kcal_raw: float | None = None
    # The same parts times the calibration factor k, like activity_kcal.
    neat_kcal: float | None = None
    sport_kcal: float | None = None
    # The k applied to this window's activity (1.0 while it stays the prior).
    calibration_factor: float = 1.0
    # Its terms for the deficit card (#3548); None without an estimate.
    store: StoredEnergy | None = None
    # The mean of the days' activity errors, uncalibrated (#3552): a method's
    # error is the same on every day, so it does not average out.
    activity_error_kcal_raw: float = 0.0


def weight_trend(weights_kg: Mapping[date, float]) -> dict[date, float]:
    """The current weight per day from the first weigh-in on, without a lag (#3551).

    A day's value is the least-squares line through the weigh-ins of the
    ``CURRENT_WEIGHT_DAYS`` up to it, read at the latest of them; with fewer
    than ``MIN_LINE_WEIGHINS`` or a span below ``MIN_LINE_SPAN_DAYS``, their
    mean. A day without a weigh-in in that span carries the previous value.
    """
    if not weights_kg:
        return {}
    weighed = sorted(weights_kg)
    trend: dict[date, float] = {}
    day = weighed[0]
    while day <= weighed[-1]:
        since = day - timedelta(days=CURRENT_WEIGHT_DAYS - 1)
        recent = weighed[bisect_left(weighed, since) : bisect_right(weighed, day)]
        trend[day] = (
            _line_at_latest([(d, weights_kg[d]) for d in recent])
            if recent
            else trend[day - timedelta(days=1)]
        )
        day += timedelta(days=1)
    return trend


def _line_at_latest(points: Sequence[tuple[date, float]]) -> float:
    """The least-squares line's value at the latest point, else the mean."""
    ys = [kg for _, kg in points]
    latest = points[-1][0]
    if len(points) < MIN_LINE_WEIGHINS or (
        (latest - points[0][0]).days < MIN_LINE_SPAN_DAYS
    ):
        return statistics.fmean(ys)
    xs = [float((d - latest).days) for d, _ in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return mean_y - sxy / sxx * mean_x


def energy_density(share: float) -> float:
    """kcal per kg of weight change with this fat share (Hall 2008)."""
    return share * FAT_KCAL_PER_KG + (1.0 - share) * LEAN_KCAL_PER_KG


def day_tef(day: DayInputs) -> float:
    return tef_kcal(day.protein_g, day.carbohydrate_g, day.fat_g)


def is_complete(day: DayInputs, rmr_kcal: float | None) -> bool:
    """Enough entries, and an intake of at least ``MIN_DAY_REST_SHARE`` of the
    resting rate plus the day's TEF, whatever the day's training (#3553)."""
    if day.intake_kcal is None or day.entries < MIN_DAY_ENTRIES:
        return False
    if rmr_kcal is None:
        return True
    return day.intake_kcal >= MIN_DAY_REST_SHARE * (rmr_kcal + day_tef(day))


def below_former_share(day: DayInputs, rmr_kcal: float | None) -> bool:
    """Whether the former activity-scaled rule would have dropped the day: its
    intake below ``FORMER_DAY_SHARE`` of resting + activity + TEF (#3553)."""
    if day.intake_kcal is None or rmr_kcal is None or day.activity_kcal is None:
        return False
    expected = rmr_kcal + day.activity_kcal + day_tef(day)
    return day.intake_kcal < FORMER_DAY_SHARE * expected


def _latest_body_fat(inputs: EnergyInputs, day: date) -> float | None:
    measured = [d for d in inputs.body_fat_pct if d <= day]
    return inputs.body_fat_pct[max(measured)] if measured else None


def t_quantile(dof: int) -> float:
    """The 95 % two-sided t quantile, from the next smaller tabled dof."""
    tabled = [d for d in T_QUANTILE_95 if d <= max(1, dof)]
    return BAND_Z if dof > max(T_QUANTILE_95) else T_QUANTILE_95[max(tabled)]


def _tdee(
    intakes: Sequence[tuple[float, float]],
    stored_per_day: float,
    store_sd: float,
    weighins: int = 3,
) -> Estimate | None:
    """Mean intake minus stored energy per day, with its 95 % band.

    Each intake is (day kcal, its flagged entries' kcal); the flagged part
    widens the band by ``FLAGGED_DAY_UNCERTAINTY`` of itself.
    """
    if not intakes:
        return None
    values = [kcal for kcal, _ in intakes]
    mean = statistics.fmean(values)
    spread = statistics.stdev(values) if len(values) > 1 else 0.0
    flagged_var = sum((FLAGGED_DAY_UNCERTAINTY * k) ** 2 for _, k in intakes)
    intake_var = spread**2 / len(values) + flagged_var / len(values) ** 2
    dof = min(len(values) - 1, weighins - 2)
    band = t_quantile(dof) * math.sqrt(intake_var + store_sd**2)
    return Estimate(mean - stored_per_day, band)


def _insufficient(complete_days: int, weighins: Sequence[date]) -> str | None:
    """Why a window has no estimate, or None when its basis is enough."""
    if complete_days < MIN_COMPLETE_DAYS:
        return "intake_days"
    if len(weighins) < MIN_WEIGHINS:
        return "weighins"
    if (weighins[-1] - weighins[0]).days < MIN_WEIGHT_SPAN_DAYS:
        return "weight_span"
    return None


def _fat_series(inputs: EnergyInputs) -> list[dict[date, float]]:
    """The dated body fat measurements per method: skinfold, entered, device."""
    entered = {d: v for d, (v, p) in inputs.device_body_fat.items() if p is None}
    device = {d: v for d, (v, p) in inputs.device_body_fat.items() if p is not None}
    return [dict(inputs.body_fat_pct), entered, device]


class FatShare(NamedTuple):
    """The blended fat share of a weight change, its SD, the measurements'
    weight in it (0 to 1) and the first and last measurement used (#3555)."""

    share: float
    sd: float
    measured_weight: float = 0.0
    pct_first: float | None = None
    pct_last: float | None = None


def _slope(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """The least-squares slope and the spread of ``xs`` (sum of squares)."""
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return sxy / sxx, sxx


def fat_share(inputs: EnergyInputs, trend: Mapping[date, float], end: date) -> FatShare:
    """The fat share of a weight change up to ``end``: the prior blended with
    the measured one by their precisions (athlete decision, #3555).

    The measured share is the slope of the fat mass (the day's current weight x
    body fat) over the slope of the weight, through all measurements of one
    method up to ``end`` that have a current weight; each fat mass carries the
    repeatability ``BODY_FAT_REPEATABILITY_PCT``. Its weight grows with the
    number of measurements and their span; with fewer than two at least
    ``MIN_FAT_SPAN_DAYS`` apart, or no weight change, the prior stands alone.
    """
    prior = FatShare(FAT_SHARE_PRIOR, FAT_SHARE_PRIOR_SD)
    for series in _fat_series(inputs):
        days = sorted(d for d in series if d <= end and d in trend)
        if len(days) >= 2 and (days[-1] - days[0]).days >= MIN_FAT_SPAN_DAYS:
            break
    else:
        return prior._replace(pct_last=_latest_body_fat(inputs, end))
    xs = [float((d - days[0]).days) for d in days]
    weights = [trend[d] for d in days]
    weight_slope, sxx = _slope(xs, weights)
    if abs(weight_slope) * (xs[-1] - xs[0]) < MIN_FAT_SHARE_WEIGHT_CHANGE_KG:
        return prior._replace(pct_first=series[days[0]], pct_last=series[days[-1]])
    fat_slope, _ = _slope(
        xs, [w * series[d] / 100.0 for w, d in zip(weights, days, strict=True)]
    )
    fat_sd = statistics.fmean(weights) * BODY_FAT_REPEATABILITY_PCT / 100.0
    measured_sd = fat_sd / math.sqrt(sxx) / abs(weight_slope)
    precision = 1.0 / measured_sd**2
    total = precision + 1.0 / FAT_SHARE_PRIOR_SD**2
    weight = precision / total
    share = weight * fat_slope / weight_slope + (1.0 - weight) * FAT_SHARE_PRIOR
    return FatShare(
        min(1.0, max(0.0, share)),
        1.0 / math.sqrt(total),
        weight,
        series[days[0]],
        series[days[-1]],
    )


def _stored_energy(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    weights: Mapping[date, float],
    end: date,
) -> StoredEnergy:
    """Stored energy per day over the weigh-ins, its standard error and how fat
    and lean mass were split.

    The least-squares slope of the window's own weigh-ins, at the energy
    density of the blended fat share (#3555). The error combines the slope's
    standard error with the share's. Unlike the trend's change the slope has
    no warm-up lag after the first weigh-in.
    """
    weighins = sorted(weights)
    xs = [float((d - weighins[0]).days) for d in weighins]
    ys = [weights[d] for d in weighins]
    slope, sxx = _slope(xs, ys)
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    residuals = [
        y - (mean_y + slope * (x - mean_x)) for x, y in zip(xs, ys, strict=True)
    ]
    slope_se = math.sqrt(sum(r * r for r in residuals) / max(1, len(xs) - 2) / sxx)
    share = fat_share(inputs, trend, end)
    density = energy_density(share.share)
    density_sd = share.sd * (FAT_KCAL_PER_KG - LEAN_KCAL_PER_KG)
    scatter, split = abs(density * slope_se), abs(slope * density_sd)
    span = xs[-1] - xs[0]
    # The fitted line's weights at the first and the last weigh-in.
    first = mean_y + slope * (xs[0] - mean_x)
    return StoredEnergy(
        density * slope,
        math.hypot(scatter, split),
        # The same whole percent the label shows (Gitar on #3581).
        "measured" if round(100 * share.measured_weight) >= 1 else "prior",
        round(span),
        first,
        first + slope * span,
        slope * span * share.share,
        share.pct_first,
        share.pct_last,
        "fat_share" if split >= scatter else "weight_scatter",
        share.share,
        share.measured_weight,
    )


def _window(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    end: date,
    factor: float,
    calibration_windows: int = 0,
    length: int = WINDOW_DAYS,
) -> EnergyWindow:
    start = end - timedelta(days=length - 1)
    span = [start + timedelta(days=n) for n in range(length)]
    days = {d: inputs.days[d] for d in span if d in inputs.days}
    complete = {d: v for d, v in days.items() if is_complete(v, inputs.rmr_kcal)}
    flagged = sum(1 for v in complete.values() if v.flagged)
    weights = {d: v.weight_kg for d, v in days.items() if v.weight_kg is not None}
    weighins = sorted(weights)
    active = [v for v in days.values() if v.activity_kcal is not None]
    raw = statistics.fmean(v.activity_kcal or 0.0 for v in active) if active else None
    raw_error = sum(v.activity_error_kcal for v in active) / max(1, len(active))
    neat_raw, sport_raw = _split(active)
    tef = (
        statistics.fmean([day_tef(v) for v in complete.values()]) if complete else None
    )
    reason = _insufficient(len(complete), weighins)
    tdee = unflagged = derived = store = None
    if reason is None:
        store = _stored_energy(inputs, trend, weights, end)
        everything = [(v.intake_kcal or 0.0, v.flagged_kcal) for v in complete.values()]
        tdee = _tdee(everything, store.kcal_per_day, store.sd, len(weighins))
        unflagged = tdee if flagged == 0 else None
        if tdee is not None and raw is not None and tef is not None:
            # Activity and digestion carry their M11 errors (#3552).
            band = math.hypot(tdee.band_kcal, raw_error * factor, TEF_ERROR_KCAL)
            derived = Estimate(tdee.kcal - raw * factor - tef, band)
    return EnergyWindow(
        end=end,
        days=length,
        complete_days=len(complete),
        flagged_days=flagged,
        weighins=len(weighins),
        activity_kcal=None if raw is None else raw * factor,
        activity_kcal_raw=raw,
        activity_error_kcal_raw=raw_error,
        tef_kcal=tef,
        adaptive_tdee=tdee,
        adaptive_tdee_unflagged=unflagged,
        derived_rmr=derived,
        insufficient=reason,
        rmr_source=inputs.rmr_source,
        calibration_windows=calibration_windows,
        neat_kcal_raw=neat_raw,
        sport_kcal_raw=sport_raw,
        neat_kcal=None if neat_raw is None else neat_raw * factor,
        sport_kcal=None if sport_raw is None else sport_raw * factor,
        calibration_factor=factor,
        store=store,
        flagged_kcal_per_day=statistics.fmean(
            [v.flagged_kcal for v in complete.values()]
        )
        if complete
        else 0.0,
    )


def period_window(
    inputs: EnergyInputs, end: date, days: int, factor: float
) -> EnergyWindow:
    """One window of ``days`` ending on ``end``, by the same rules and calibration.

    The fit scale's periods (#3397); a period too short for the rules has no
    own estimate, as any window.
    """
    weights = {
        d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None
    }
    return _window(inputs, weight_trend(weights), end, factor, length=days)


def _split(active: Sequence[DayInputs]) -> tuple[float | None, float | None]:
    """Mean NEAT and session kcal over the days with a known activity.

    Both stay None unless every one of those days has both parts, so the two
    always add up to the window's activity (split A, #3100).
    """
    neat = [v.neat_kcal for v in active]
    sport = [v.sport_kcal for v in active]
    if not active or None in neat or None in sport:
        return None, None
    return (
        statistics.fmean(n for n in neat if n is not None),
        statistics.fmean(s for s in sport if s is not None),
    )


def _calibration_points(windows: Sequence[EnergyWindow]) -> list[tuple[float, float]]:
    """(raw activity, TDEE - TEF) of every window with an estimate."""
    return [
        (w.activity_kcal_raw, w.adaptive_tdee.kcal - w.tef_kcal)
        for w in windows
        if w.adaptive_tdee is not None
        and w.activity_kcal_raw is not None
        and w.tef_kcal is not None
    ]


def calibration_factor(windows: Sequence[EnergyWindow]) -> float:
    """The one activity factor: slope of (TDEE - TEF) on activity, shrunk to 1.

    Ordinary least squares over the windows with a basis gives the slope and
    its variance; the posterior mean with the prior 1 +- 0.10 is clamped to
    0.85-1.15. Without enough windows, or without spread in activity, it is 1.
    """
    points = _calibration_points(windows)
    if len(points) < MIN_CALIBRATION_WINDOWS:
        return 1.0
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return 1.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx
    residual = [y - (mean_y + slope * (x - mean_x)) for x, y in points]
    dof = max(1, len(points) - 2)
    slope_var = sum(r * r for r in residual) / dof / sxx
    prior_precision = 1.0 / CALIBRATION_PRIOR_SD**2
    if slope_var <= 0:
        posterior = slope
    else:
        precision = 1.0 / slope_var
        posterior = (slope * precision + prior_precision) / (
            precision + prior_precision
        )
    low, high = CALIBRATION_BOUNDS
    return max(low, min(high, posterior))


def energy_windows(
    inputs: EnergyInputs, end: date, weeks: int = 12
) -> tuple[EnergyWindow, ...]:
    """One window per week ending on ``end``, ``end`` - 7, ...; oldest first.

    It works on whatever history ``inputs`` holds, so logged days from before
    the first read count at once; older windows without a basis give reasons.
    """
    weights = {
        d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None
    }
    trend = weight_trend(weights)
    ends = [end - timedelta(days=7 * n) for n in range(weeks)][::-1]
    uncalibrated = [_window(inputs, trend, e, 1.0) for e in ends]
    factor = calibration_factor(uncalibrated)
    used = len(_calibration_points(uncalibrated))
    windows = [_window(inputs, trend, e, factor, used) for e in ends]
    bias = _bias(windows)
    if bias is None:
        return tuple(windows)
    return tuple(_corrected(window, *bias) for window in windows)


def _corrected(window: EnergyWindow, bias: float, band: float) -> EnergyWindow:
    """The window with flagged kcal counted at (1 + b); its band adds b's."""
    tdee = window.adaptive_tdee
    if tdee is None:
        return window
    extra = window.flagged_kcal_per_day
    corrected = Estimate(
        tdee.kcal + bias * extra, math.hypot(tdee.band_kcal, band * extra)
    )
    return cast(EnergyWindow, replace(window, adaptive_tdee_corrected=corrected))


def history_start(end: date, weeks: int = 12) -> date:
    """The first day the oldest of ``weeks`` windows ending on ``end`` reads."""
    return end - timedelta(days=7 * (weeks - 1) + WINDOW_DAYS - 1)


def flagged_meal_bias(inputs: EnergyInputs, end: date) -> tuple[float, float] | None:
    """Mean misestimate of flagged entries, in % of their logged kcal, with its band."""
    return windows_bias(energy_windows(inputs, end))


def windows_bias(windows: Sequence[EnergyWindow]) -> tuple[float, float] | None:
    """The flagged-entry bias in % with its band, from windows already built."""
    bias = _bias(windows)
    return None if bias is None else (bias[0] * 100.0, bias[1] * 100.0)


def _bias(windows: Sequence[EnergyWindow]) -> tuple[float, float] | None:
    """The flagged-entry bias b and its band, as fractions (#3187 items 13, 14).

    Across the windows, the adaptive TDEE is regressed on the flagged entries'
    logged kcal per day x: TDEE = T - b x. If flagged meals were in fact
    larger than logged by b, more flagged intake shows as a lower
    expenditure, so b = -slope; positive b means they were larger.
    """
    points = [
        (w.flagged_kcal_per_day, w.adaptive_tdee.kcal)
        for w in windows
        if w.adaptive_tdee is not None
    ]
    usable = [p for p in points if p[0] > 0]
    if len(usable) < MIN_BIAS_WINDOWS or len({round(x, 6) for x, _ in points}) < 3:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx
    residual = [y - (mean_y + slope * (x - mean_x)) for x, y in points]
    slope_sd = math.sqrt(sum(r * r for r in residual) / max(1, len(points) - 2) / sxx)
    return -slope, BAND_Z * slope_sd
