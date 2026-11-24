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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, NamedTuple, cast

from backend.athlete.body_records import body_fat_on, weight_trend
from backend.performance.activity_energy import (
    TEF_ERROR_KCAL,
    TEF_UNCOVERED,
    tef_kcal,
)

METHOD_ID = "adaptive-expenditure"
# Bump when a formula or constant below changes the estimate.
METHOD_VERSION = 26
# Days per window and windows per week of the rolling view (#3099, #3100).
WINDOW_DAYS = 28
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
# and the slope of the phase's measured fat mass (athlete decision, #3555).
# The prior follows Forbes' curve at the current fat mass, FM / (FM + 10.4 kg)
# (Forbes 1987, Hall 2008), clamped to 0.40-0.85, with SD 0.15; without a
# body fat it falls back to 0.70 (athlete decision, #3684).
FAT_SHARE_PRIOR = 0.70
FAT_SHARE_PRIOR_SD = 0.15
FORBES_FAT_MASS_KG = 10.4
# Water, glycogen and gut content the weigh-ins cannot tell from tissue: about
# 1 kg (95 %) over a window, as early in a cut about 2 of 3.4 kg in 27 days
# can be (Fable review, #3690); its SD, priced at the tissue density over the
# window's span, enters the stored energy's error in quadrature.
WATER_KG_SD = 1.0 / 1.96
# A period longer than one window prices its weight change from the mean of the
# weigh-ins in its first and last this many days, not a line through all of
# them, which a flat-then-steep course drags far past any weigh-in (athlete
# decision, #3739).
EDGE_DAYS = 14
# The 4 weeks keep the line for their deficit and band, but their card shows
# the mean weigh-ins of their first and last this many days as start, end and
# change (athlete decision, #3739).
SHORT_EDGE_DAYS = 7
FAT_SHARE_PRIOR_BOUNDS = (0.40, 0.85)
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
# The flagged-meal bias b (#3616): shrunk toward 0 by the prior 0 +- 0.25 and
# kept at or above -1 (a flagged meal cannot be less than nothing); reported
# and applied only once its 95 % band spans at most 50 percentage points
# (half-width 0.25) and its flagged days lie in at least 2 windows that share
# no day (product assumptions).
BIAS_PRIOR_SD = 0.25
BIAS_MIN = -1.0
BIAS_MAX_BAND = 0.25
MIN_INDEPENDENT_BIAS_WINDOWS = 2
# Neighbouring windows share 21 of their 28 days, so a fit over them holds about
# a quarter of the information its window count suggests: the variance of the
# k and b slopes is inflated by WINDOW_DAYS / 7 before the shrinkage (#3554).
WINDOW_OVERLAP_INFLATION = WINDOW_DAYS / 7
# The daily estimate rests on the estimated windows of the last 12 weeks, each
# weighted by its precision and by its age, halved every 6 weeks (athlete
# decision, #3687): a water or glycogen step largely cancels out over them.
BASE_DAYS = 84
BASE_HALF_LIFE_DAYS = 42
# A weight step from water or glycogen is not tissue (#3614): weigh-ins whose
# scatter persists widen the slope's error by the AR(1) factor, with the lag-1
# autocorrelation capped here (factor 3 at most); overlapping windows that
# differ beyond the 99 % band of the days they do not share are unstable and
# their band holds the difference. 2.576 / 1.96 turns the 95 % t into 99 %.
MAX_RESIDUAL_AUTOCORRELATION = 0.8
STABILITY_BAND_RATIO = 2.576 / 1.96
# The learned everyday term of a formula row (#3577): the own total's excess over
# the row, shrunk towards 0 with this SD (kcal/day, product assumption).
LEARNED_EVERYDAY_PRIOR_SD = 250.0
# A formula can overshoot too, so the term may reduce a row, down to this floor
# (kcal/day); it is offered and applied only with this many stable windows that
# share no day, or a posterior SD up to this (kcal/day) (#3689).
LEARNED_EVERYDAY_FLOOR = -250.0
LEARNED_EVERYDAY_MIN_INDEPENDENT = 3
LEARNED_EVERYDAY_MAX_SD = 150.0


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
    # The day summary ended before midnight (#3612): its activity is unknown,
    # its NEAT holds only today's part so far.
    unfinished: bool = False
    # Logged kcal its entries' macros do not cover (#3617 item 3).
    uncovered_kcal: float = 0.0
    # The known salt of its entries and the share of their values that report it,
    # and whether a session of the day was long: only for the water hint (#4047).
    salt_g: float | None = None
    salt_known_share: float = 0.0
    long_session: bool = False


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
    # scale's per day with its provider; never the profile's (#3640).
    device_body_fat: Mapping[date, tuple[float, Any]] = field(default_factory=dict)
    # The athlete's own SD of one skinfold body fat, from repeat readings;
    # None until there are enough, then the default applies (#3556).
    # Per measurement day, with its own median gain (Gitar on #3622).
    skinfold_sd_pct: Mapping[date, float] = field(default_factory=dict)


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
    # The days of fat_pct_start and fat_pct_end: the phase's span (#3598).
    fat_date_start: date | None = None
    fat_date_end: date | None = None
    # The window's last weigh-in, which the weight chart ends on (#3598).
    weight_last_kg: float | None = None
    # The prior of the fat share and its source (#3684).
    fat_prior: float = FAT_SHARE_PRIOR
    fat_prior_source: str = "fallback"
    # The first and last weigh-in the stored energy rests on (#3704); with edge
    # means, the mean dates of the two edge windows (#3739).
    weight_date_start: date | None = None
    weight_date_end: date | None = None
    # A long period's weigh-ins in its first and last edge window, whose means
    # are weight_start_kg and weight_end_kg; None for the line (#3739).
    edge_weighins: tuple[int, int] | None = None


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
    # False when an overlapping window differs by more than the days they do
    # not share can explain (#3614); the band then holds that difference.
    stable: bool = True
    # The mean logged intake of its complete days: TDEE plus the stored energy
    # of their span, which can differ from the whole window's (#3667).
    intake_kcal: float | None = None
    # The weigh-ins inside that span, which set the TDEE band's dof (#3667).
    logged_weighins: int = 0


def energy_density(share: float) -> float:
    """kcal per kg of weight change with this fat share (Hall 2008)."""
    return share * FAT_KCAL_PER_KG + (1.0 - share) * LEAN_KCAL_PER_KG


def day_tef(day: DayInputs) -> float:
    """The macros' thermic effect plus 10 % of the kcal they do not cover."""
    macros = tef_kcal(day.protein_g, day.carbohydrate_g, day.fat_g)
    return macros + TEF_UNCOVERED * day.uncovered_kcal


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


def _dof(complete_days: int, weighins: int) -> int:
    """A window's band dof: the smaller of its intake and weight-slope dof."""
    return min(complete_days - 1, weighins - 2)


def window_t(window: EnergyWindow) -> float:
    """The t quantile of a window's bands; the deficit card uses it too (#3614).

    A window with weigh-ins but too few logged days ("Nur Waage", #3603) has
    only the weight slope's dof.
    """
    if window.adaptive_tdee is None:
        return t_quantile(window.weighins - 2)
    # The TDEE's own dof: the weigh-ins of its logged span (#3667).
    weighins = window.logged_weighins or window.weighins
    return t_quantile(_dof(window.complete_days, weighins))


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
    band = t_quantile(_dof(len(values), weighins)) * math.sqrt(intake_var + store_sd**2)
    return Estimate(mean - stored_per_day, band)


def _insufficient(complete_days: int, weighins: Sequence[date]) -> str | None:
    """Why a window has no estimate, or None when its basis is enough."""
    if complete_days < MIN_COMPLETE_DAYS:
        return "intake_days"
    return _weighins_short(weighins)


def logged_weights(
    weights: Mapping[date, float], complete: Iterable[date]
) -> dict[date, float]:
    """The weigh-ins between the first and the last complete day: the span
    the intake mean covers, so the TDEE never prices a phase it did not log
    (#3667); all of them without a complete day."""
    days = sorted(complete)
    if not days:
        return dict(weights)
    return {d: w for d, w in weights.items() if days[0] <= d <= days[-1]}


def _scale_store(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    weights: Mapping[date, float],
    end: date,
    share: FatShare | None,
    start: date | None = None,
) -> StoredEnergy | None:
    """The window's stored energy whenever its weigh-ins carry it, logged meals
    or not ("Nur Waage", #3603); ``start``, its first day, sets its edge means (#3739)."""
    if _weighins_short(sorted(weights)):
        return None
    return _stored_energy(inputs, trend, weights, end, share, start)


def _weighins_short(weighins: Sequence[date]) -> str | None:
    """Why the weigh-ins cannot carry the stored energy, or None. The store
    needs no logged meals ("Nur Waage", athlete decision, #3603)."""
    if len(weighins) < MIN_WEIGHINS:
        return "weighins"
    if (weighins[-1] - weighins[0]).days < MIN_WEIGHT_SPAN_DAYS:
        return "weight_span"
    return None


def fat_series(inputs: EnergyInputs) -> list[dict[date, float]]:
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
    # Their days: the phase's span the card names, not the window's (#3598).
    day_first: date | None = None
    day_last: date | None = None
    # The prior it was blended with and where it came from: "forbes" or
    # "fallback" (#3684).
    prior: float = FAT_SHARE_PRIOR
    prior_source: str = "fallback"


def fat_share_prior(
    inputs: EnergyInputs, trend: Mapping[date, float], end: date
) -> tuple[float, str]:
    """Forbes' fat share at the current fat mass on ``end``: the trend weight x
    the body fat the view uses (skinfold, entered, device), clamped to
    FAT_SHARE_PRIOR_BOUNDS; FAT_SHARE_PRIOR without either (#3684)."""
    fat = body_fat_on(inputs.body_fat_pct, inputs.device_body_fat, end)
    days = [d for d in trend if d <= end]
    if fat is None or not days:
        return FAT_SHARE_PRIOR, "fallback"
    mass = trend[max(days)] * fat["body_fat_pct"] / 100.0
    low, high = FAT_SHARE_PRIOR_BOUNDS
    return min(high, max(low, mass / (mass + FORBES_FAT_MASS_KG))), "forbes"


def _slope(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """The least-squares slope and the spread of ``xs`` (sum of squares)."""
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return sxy / sxx, sxx


def _weighted_slope(
    xs: Sequence[float], ys: Sequence[float], precisions: Sequence[float]
) -> tuple[float, float]:
    """The weighted least-squares slope and its precision, Σ w (x − x̄)²."""
    total = sum(precisions)
    mean_x = sum(w * x for w, x in zip(precisions, xs, strict=True)) / total
    mean_y = sum(w * y for w, y in zip(precisions, ys, strict=True)) / total
    leverage = sum(w * (x - mean_x) ** 2 for w, x in zip(precisions, xs, strict=True))
    sxy = sum(
        w * (x - mean_x) * (y - mean_y)
        for w, x, y in zip(precisions, xs, ys, strict=True)
    )
    return sxy / leverage, leverage


def fat_share(inputs: EnergyInputs, trend: Mapping[date, float], end: date) -> FatShare:
    """The fat share of a weight change up to ``end``: the prior blended with
    the measured one by their precisions (athlete decision, #3555).

    The measured share is the slope of the fat mass (the day's current weight x
    body fat) over the slope of the weight, through all measurements of one
    method up to ``end`` that have a current weight; each fat mass carries the
    repeatability ``BODY_FAT_REPEATABILITY_PCT``, for skinfolds the athlete's
    own from repeat readings once known (#3556). Its weight grows with the
    number of measurements and their span; with fewer than two at least
    ``MIN_FAT_SPAN_DAYS`` apart, or no weight change, the prior stands alone.
    """
    value, source = fat_share_prior(inputs, trend, end)
    prior = FatShare(value, FAT_SHARE_PRIOR_SD, prior=value, prior_source=source)
    for method, series in enumerate(fat_series(inputs)):
        days = sorted(d for d in series if d <= end and d in trend)
        if len(days) >= 2 and (days[-1] - days[0]).days >= MIN_FAT_SPAN_DAYS:
            break
    else:
        measured = [d for d in inputs.body_fat_pct if d <= end]
        return prior._replace(
            pct_last=_latest_body_fat(inputs, end),
            day_last=max(measured) if measured else None,
        )
    xs = [float((d - days[0]).days) for d in days]
    weights = [trend[d] for d in days]
    weight_slope, _ = _slope(xs, weights)
    if abs(weight_slope) * (xs[-1] - xs[0]) < MIN_FAT_SHARE_WEIGHT_CHANGE_KG:
        return prior._replace(
            pct_first=series[days[0]],
            pct_last=series[days[-1]],
            day_first=days[0],
            day_last=days[-1],
        )
    # Each fat mass weighs by its own SD: the skinfold day's own repeatability
    # once known (#3556), else the default (weighted least squares).
    own = inputs.skinfold_sd_pct if method == 0 else {}
    precisions = [
        (100.0 / (w * own.get(d, BODY_FAT_REPEATABILITY_PCT))) ** 2
        for w, d in zip(weights, days, strict=True)
    ]
    fats = [w * series[d] / 100.0 for w, d in zip(weights, days, strict=True)]
    fat_slope, fat_leverage = _weighted_slope(xs, fats, precisions)
    measured_sd = 1.0 / math.sqrt(fat_leverage) / abs(weight_slope)
    precision = 1.0 / measured_sd**2
    total = precision + 1.0 / FAT_SHARE_PRIOR_SD**2
    weight = precision / total
    share = weight * fat_slope / weight_slope + (1.0 - weight) * value
    return FatShare(
        min(1.0, max(0.0, share)),
        1.0 / math.sqrt(total),
        weight,
        series[days[0]],
        series[days[-1]],
        days[0],
        days[-1],
        value,
        source,
    )


class WeightChange(NamedTuple):
    """A weight course's slope (kg/day) with its standard error, the weight it
    starts from, the days it spans and their first and last day; with edge
    means, the weigh-ins in each edge (#3739)."""

    slope: float
    slope_se: float
    first_kg: float
    span: float
    day_first: date
    day_last: date
    edges: tuple[int, int] | None = None


def _line_change(weights: Mapping[date, float]) -> WeightChange:
    """The Theil-Sen line of the weigh-ins (#3690): its slope, the slope's
    standard error scaled for persisting scatter (#3614), and its weight at the
    first weigh-in. Unlike the trend's change it has no warm-up lag."""
    weighins = sorted(weights)
    xs = [float((d - weighins[0]).days) for d in weighins]
    ys = [weights[d] for d in weighins]
    _, sxx = _slope(xs, ys)
    slope, intercept = _theil_sen(xs, ys)
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=True)]
    slope_se = math.sqrt(sum(r * r for r in residuals) / max(1, len(xs) - 2) / sxx)
    return WeightChange(
        slope,
        slope_se * _persistence(residuals),
        intercept,
        xs[-1],
        weighins[0],
        weighins[-1],
    )


def _edge(weighins: Sequence[date], low: date, high: date) -> list[date]:
    return [d for d in weighins if low <= d <= high]


def edge_change(
    weights: Mapping[date, float], start: date, end: date, days: int
) -> WeightChange | None:
    """The change between the mean weigh-ins of the window's first and last
    ``days`` (athlete decision, #3739); None when the first edge does not end
    before the last begins.

    An edge without a weigh-in starts at the period's first weigh-in, or ends
    at its last. The rate is the difference over the days between the edges'
    mean dates. Each mean's standard error is the edges' pooled SD (one
    weigh-in's ``WATER_KG_SD`` without a spread to pool) over the root of its
    count, so a sparse edge widens the band.
    """
    weighins = sorted(weights)
    width = timedelta(days=days - 1)
    first = _edge(weighins, start, start + width) or _edge(
        weighins, weighins[0], weighins[0] + width
    )
    last = _edge(weighins, end - width, end) or _edge(
        weighins, weighins[-1] - width, weighins[-1]
    )
    if first[-1] >= last[0]:
        return None
    edges = [[weights[d] for d in first], [weights[d] for d in last]]
    means = [statistics.fmean(edge) for edge in edges]
    dof = sum(len(edge) - 1 for edge in edges)
    squares = sum(
        (y - mean) ** 2 for edge, mean in zip(edges, means, strict=True) for y in edge
    )
    sd = math.sqrt(squares / dof) if dof else WATER_KG_SD
    # Each edge's mean day, in days after the first weigh-in.
    at = [statistics.fmean((d - weighins[0]).days for d in e) for e in (first, last)]
    span = at[1] - at[0]
    se = math.hypot(sd / math.sqrt(len(first)), sd / math.sqrt(len(last)))
    return WeightChange(
        (means[1] - means[0]) / span,
        se / span,
        means[0],
        span,
        weighins[0] + timedelta(days=math.floor(at[0] + 0.5)),
        weighins[0] + timedelta(days=math.floor(at[1] + 0.5)),
        (len(first), len(last)),
    )


def _stored_energy(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    weights: Mapping[date, float],
    end: date,
    share: FatShare | None = None,
    start: date | None = None,
) -> StoredEnergy:
    """Stored energy per day over the weigh-ins, its standard error and how fat
    and lean mass were split.

    The weight change of the window's own weigh-ins: a period longer than one
    window (from ``start``) takes the means of its first and last
    ``EDGE_DAYS`` (#3739), else the Theil-Sen line (#3690). It is priced at
    the energy density of the blended fat share
    (#3555): ``share``, the phase's one share the windows of a view share
    (#3554), else the one up to ``end``. The error combines the change's
    standard error with the share's and an unmodelled water term
    (``WATER_KG_SD``, #3690).
    """
    edges = (
        edge_change(weights, start, end, EDGE_DAYS)
        if start is not None and (end - start).days >= WINDOW_DAYS
        else None
    )
    change = edges or _line_change(weights)
    slope, slope_se, span = change.slope, change.slope_se, change.span
    share = share or fat_share(inputs, trend, end)
    density = energy_density(share.share)
    density_sd = share.sd * (FAT_KCAL_PER_KG - LEAN_KCAL_PER_KG)
    scatter, split = abs(density * slope_se), abs(slope * density_sd)
    # Water the weigh-ins cannot tell from tissue widens the error; the label
    # still names the larger of the measured terms (#3690).
    water = abs(density) * WATER_KG_SD / max(1.0, span)
    # The line's weight at the first weigh-in, or the first edge's mean.
    first = change.first_kg
    return StoredEnergy(
        density * slope,
        math.hypot(scatter, split, water),
        # The same whole percent the label shows (Gitar on #3581).
        "measured" if round(100 * share.measured_weight) >= 1 else "prior",
        # The days between the dates the card names (#3739).
        (change.day_last - change.day_first).days,
        first,
        first + slope * span,
        slope * span * share.share,
        share.pct_first,
        share.pct_last,
        "fat_share" if split >= scatter else "weight_scatter",
        share.share,
        share.measured_weight,
        share.day_first,
        share.day_last,
        weights[max(weights)],
        share.prior,
        share.prior_source,
        change.day_first,
        change.day_last,
        change.edges,
    )


def _theil_sen(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """The Theil-Sen line: the median of the pairwise slopes and the median
    intercept under it. A single bad weigh-in or a step near a window's edge
    moves it far less than least squares (#3690)."""
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    ]
    slope = statistics.median(slopes)
    return slope, statistics.median(y - slope * x for x, y in zip(xs, ys, strict=True))


def _persistence(residuals: Sequence[float]) -> float:
    """How much more uncertain the slope is because the weigh-ins' scatter
    persists from one weigh-in to the next, as water and glycogen do (#3614).

    The lag-1 autocorrelation r of the residuals around the line, clamped to
    0 to ``MAX_RESIDUAL_AUTOCORRELATION``, scales the slope's standard error
    by sqrt((1 + r) / (1 - r)), the AR(1) factor; 1 for independent scatter.
    """
    total = sum(r * r for r in residuals)
    if total <= 0:
        return 1.0
    lagged = sum(a * b for a, b in pairwise(residuals)) / total
    r = min(MAX_RESIDUAL_AUTOCORRELATION, max(0.0, lagged))
    return math.sqrt((1.0 + r) / (1.0 - r))


def _logged_basis(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    window: tuple[Mapping[date, float], Mapping[date, DayInputs], StoredEnergy | None],
    end: date,
    share: FatShare | None,
    start: date | None = None,
) -> tuple[dict[date, float], str | None, StoredEnergy | None]:
    """The weigh-ins of the complete days' span, why the window has no
    expenditure (the minimum basis on that span, #3667) and the store the
    expenditure prices over that span."""
    weights, complete, store = window
    logged = logged_weights(weights, complete)
    reason = _insufficient(len(complete), sorted(logged))
    days = sorted(complete)
    if (
        reason in {"weighins", "weight_span"}
        and not _weighins_short(sorted(weights))
        and (days[-1] - days[0]).days < MIN_WEIGHT_SPAN_DAYS
    ):
        # The weigh-ins carry the window; only its logged days span too little
        # (Gitar on #3676: too few weigh-ins inside a long span keep their reason).
        reason = "logged_span"
    if len(logged) == len(weights):
        return logged, reason, store
    return logged, reason, _scale_store(inputs, trend, logged, end, share, start)


def _window(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    end: date,
    factor: float,
    calibration_windows: int = 0,
    length: int = WINDOW_DAYS,
    share: FatShare | None = None,
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
    # The stored energy needs weigh-ins only; the expenditure also logged meals
    # (#3603): the deficit card keeps the window's whole span; its start sets
    # the edge means (#3739).
    store = _scale_store(inputs, trend, weights, end, share, start)
    logged, reason, intake_store = _logged_basis(
        inputs, trend, (weights, complete, store), end, share, start
    )
    tdee = unflagged = derived = intake = None
    if reason is None and intake_store is not None:
        everything = [(v.intake_kcal or 0.0, v.flagged_kcal) for v in complete.values()]
        intake = statistics.fmean(kcal for kcal, _ in everything)
        tdee = _tdee(
            everything, intake_store.kcal_per_day, intake_store.sd, len(logged)
        )
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
        intake_kcal=intake,
        logged_weighins=len(logged),
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
    own estimate, as any window. One longer than a window prices its weight
    change from its edge means (#3739).
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
    its variance, inflated for the windows' overlap (#3554); the posterior mean
    with the prior 1 +- 0.10 is clamped to 0.85-1.15. Without enough windows, or
    without spread in activity, it is 1.
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
    slope_var = sum(r * r for r in residual) / dof / sxx * WINDOW_OVERLAP_INFLATION
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
    Every window takes the phase's one fat share up to ``end``, so a measurement
    that arrives mid-phase does not put a step into the windows that ``k`` and
    ``b`` are fitted over (#3554).
    """
    weights = {
        d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None
    }
    trend = weight_trend(weights)
    share = fat_share(inputs, trend, end)
    ends = [end - timedelta(days=7 * n) for n in range(weeks)][::-1]
    uncalibrated = [_window(inputs, trend, e, 1.0, share=share) for e in ends]
    factor = calibration_factor(uncalibrated)
    used = len(_calibration_points(uncalibrated))
    windows = [_window(inputs, trend, e, factor, used, share=share) for e in ends]
    spreads = unexplained_spreads(inputs, windows)
    windows = [_widened(w, s) for w, s in zip(windows, spreads, strict=True)]
    bias = _bias(windows)
    if bias is None:
        return tuple(windows)
    return tuple(_corrected(window, *bias) for window in windows)


def is_stable(window: EnergyWindow) -> bool:
    """Whether the window agrees with its overlapping neighbours (#3614); a fit
    over windows, such as the learned everyday term, uses only stable ones."""
    return window.stable


class Base(NamedTuple):
    """The daily estimate's own resting rate (#3687): its estimate, the windows
    it averages, the sum of their age weights, and ``unstable`` when every one
    of them failed the neighbour check (#3614)."""

    estimate: Estimate
    windows: int
    weight_sum: float
    unstable: bool


def age_weight(window_end: date, end: date) -> float:
    """A window's weight by its age: halved every ``BASE_HALF_LIFE_DAYS`` from
    its end to the view's end (#3687)."""
    return 0.5 ** ((end - window_end).days / BASE_HALF_LIFE_DAYS)


def weighted_mean(points: Sequence[tuple[float, float, float]]) -> tuple[float, float]:
    """The mean of (value, SD, age weight) points weighted by precision times age,
    and its SD, inflated for the windows' overlap like k and b (#3554)."""
    weights = [age / sd**2 for _, sd, age in points]
    total = sum(weights)
    mean = sum(w * value for w, (value, _, _) in zip(weights, points, strict=True))
    variance = (
        sum((w * sd) ** 2 for w, (_, sd, _) in zip(weights, points, strict=True))
        / total**2
    )
    return mean / total, math.sqrt(variance * WINDOW_OVERLAP_INFLATION)


def twelve_week_base(windows: Sequence[EnergyWindow]) -> Base | None:
    """The daily estimate's base (athlete decision, #3687, amended): the own
    resting rate of every estimated window ending within ``BASE_DAYS`` of the
    latest, each weighted by its precision and its age. An unstable window
    (#3614) carries its widened band, so it weighs less without being dropped:
    a water step averages out, a real change arrives within weeks. Flagged
    ``unstable`` when every window is; None without an estimate."""
    end = windows[-1].end if windows else None
    used = [
        w
        for w in windows
        if w.derived_rmr is not None and (cast(date, end) - w.end).days < BASE_DAYS
    ]
    if not used:
        return None
    points = [
        (
            cast(Estimate, w.derived_rmr).kcal,
            cast(Estimate, w.derived_rmr).band_kcal / BAND_Z,
            age_weight(w.end, cast(date, end)),
        )
        for w in used
    ]
    mean, sd = weighted_mean(points)
    return Base(
        Estimate(mean, BAND_Z * sd),
        len(used),
        sum(age for _, _, age in points),
        not any(is_stable(w) for w in used),
    )


def _difference_terms(
    inputs: EnergyInputs, window: EnergyWindow
) -> tuple[dict[date, float], float, dict[date, float], float]:
    """Each day's coefficient in the window's TDEE, for its intake and its
    weigh-in, with the intake and weigh-in variance around them (#3614)."""
    span = [window.end - timedelta(days=n) for n in range(window.days)]
    days = {d: inputs.days[d] for d in span if d in inputs.days}
    intake = {
        d: v.intake_kcal or 0.0
        for d, v in days.items()
        if is_complete(v, inputs.rmr_kcal)
    }
    # The same span the window's TDEE prices (#3667).
    weights = logged_weights(
        {d: v.weight_kg for d, v in days.items() if v.weight_kg is not None}, intake
    )
    first = min(weights)
    xs = {d: float((d - first).days) for d in weights}
    _, sxx = _slope(list(xs.values()), list(weights.values()))
    mean_x = statistics.fmean(xs.values())
    ordered = [weights[d] for d in sorted(weights)]
    residual = sum((b - a) ** 2 for a, b in pairwise(ordered)) / (
        2 * max(1, len(ordered) - 1)
    )
    density = energy_density(
        window.store.fat_share if window.store else FAT_SHARE_PRIOR
    )
    return (
        {d: 1.0 / len(intake) for d in intake},
        statistics.variance(intake.values()),
        {d: density * (x - mean_x) / sxx for d, x in xs.items()},
        residual,
    )


def unexplained_spreads(
    inputs: EnergyInputs, windows: Sequence[EnergyWindow]
) -> list[float]:
    """Per window, the largest difference to an overlapping window that the
    days they do not share cannot explain, else 0 (#3614).

    A step in weight from water or glycogen enters a window's slope at up to
    1.45 x its size and moves its overlapping neighbours by different amounts.
    Two windows' expenditure net of their own activity may differ only by the
    noise of the days they do not share: each day's intake and weigh-in enters
    the difference with the difference of its two coefficients, at the pair's
    pooled intake and weigh-in scatter. A difference beyond the t band of that
    marks both windows unstable.
    """
    estimated = [(i, w) for i, w in enumerate(windows) if w.adaptive_tdee is not None]
    terms = {i: _difference_terms(inputs, w) for i, w in estimated}
    spreads = [0.0] * len(windows)
    for i, a in estimated:
        for j, b in estimated:
            if not 0 < (b.end - a.end).days < a.days:
                continue
            activity = (
                (a.activity_kcal_raw or 0.0) - (b.activity_kcal_raw or 0.0)
                if a.activity_kcal_raw is not None and b.activity_kcal_raw is not None
                else 0.0
            )
            difference = abs(
                cast(Estimate, a.adaptive_tdee).kcal
                - cast(Estimate, b.adaptive_tdee).kcal
                - activity
            )
            if difference > STABILITY_BAND_RATIO * min(
                window_t(a), window_t(b)
            ) * _difference_sd(inputs, terms[i], terms[j]):
                spreads[i] = max(spreads[i], difference)
                spreads[j] = max(spreads[j], difference)
    return spreads


def _difference_sd(
    inputs: EnergyInputs,
    a: tuple[dict[date, float], float, dict[date, float], float],
    b: tuple[dict[date, float], float, dict[date, float], float],
) -> float:
    """The SD of two windows' TDEE difference from the days they do not share."""
    intake_var, weight_var = (a[1] + b[1]) / 2, (a[3] + b[3]) / 2
    variance = sum(
        (a[0].get(d, 0.0) - b[0].get(d, 0.0)) ** 2
        * (intake_var + (FLAGGED_DAY_UNCERTAINTY * inputs.days[d].flagged_kcal) ** 2)
        for d in a[0].keys() | b[0].keys()
    ) + weight_var * sum(
        (a[2].get(d, 0.0) - b[2].get(d, 0.0)) ** 2 for d in a[2].keys() | b[2].keys()
    )
    return math.sqrt(variance)


def _widened(window: EnergyWindow, spread: float) -> EnergyWindow:
    """An unstable window: its store error and bands hold the spread (#3614)."""
    if not spread or window.store is None or window.adaptive_tdee is None:
        return window
    store = window.store._replace(
        sd=math.hypot(window.store.sd, spread / window_t(window))
    )
    tdee = Estimate(
        window.adaptive_tdee.kcal, math.hypot(window.adaptive_tdee.band_kcal, spread)
    )
    derived = window.derived_rmr and Estimate(
        window.derived_rmr.kcal, math.hypot(window.derived_rmr.band_kcal, spread)
    )
    return cast(
        EnergyWindow,
        replace(
            window,
            store=store,
            adaptive_tdee=tdee,
            adaptive_tdee_unflagged=window.adaptive_tdee_unflagged and tdee,
            derived_rmr=derived,
            stable=False,
        ),
    )


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


class LearnedEveryday(NamedTuple):
    """A formula row's learned everyday term (#3577): the shrunken fit with its
    sign kept (``raw_kcal``), the value shown and applied (floored at
    ``LEARNED_EVERYDAY_FLOOR``, #3689), its 95 % band, the windows it was
    fitted over, how many of the stable ones share no day, and whether it
    qualifies to be offered and applied."""

    raw_kcal: float
    kcal: float
    band_kcal: float
    windows: int
    independent: int = 0
    qualified: bool = False


def fit_learned_everyday(
    points: Sequence[tuple[float, float, float]], independent: int = 0
) -> LearnedEveryday | None:
    """The mean of each window's excess (value, SD, age weight), weighted by
    precision and age as the 12-week base (#3687), its variance inflated for
    the windows' overlap and shrunk towards 0 with ``LEARNED_EVERYDAY_PRIOR_SD``
    by the conjugate normal update (#3577); None without a window. It
    qualifies with ``LEARNED_EVERYDAY_MIN_INDEPENDENT`` stable windows that
    share no day, or a posterior SD of ``LEARNED_EVERYDAY_MAX_SD`` (#3689)."""
    usable = [point for point in points if point[1] > 0]
    if not usable:
        return None
    mean, sd = weighted_mean(usable)
    precision = 1.0 / sd**2
    posterior_var = 1.0 / (precision + 1.0 / LEARNED_EVERYDAY_PRIOR_SD**2)
    raw = posterior_var * precision * mean
    return LearnedEveryday(
        raw,
        max(LEARNED_EVERYDAY_FLOOR, raw),
        BAND_Z * math.sqrt(posterior_var),
        len(usable),
        independent,
        independent >= LEARNED_EVERYDAY_MIN_INDEPENDENT
        or math.sqrt(posterior_var) <= LEARNED_EVERYDAY_MAX_SD,
    )


def independent_windows(windows: Sequence[EnergyWindow]) -> int:
    """The stable estimated windows that share no day, counted from the latest
    back (#3689): the next counts only once it ends a full window earlier."""
    count, before = 0, None
    for window in reversed(windows):
        if window.derived_rmr is None or not is_stable(window):
            continue
        if before is None or (before - window.end).days >= window.days:
            count, before = count + 1, window.end
    return count


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

    The TDEE also follows the window's own activity, so when that spreads
    across the windows it is the fit's second term (#3586): both TDEE and x
    are taken net of their line on the raw activity, and b is the flagged
    slope at fixed activity (Frisch-Waugh), with one degree of freedom less.
    """
    points = [
        (w.flagged_kcal_per_day, w.adaptive_tdee.kcal, w.activity_kcal_raw)
        for w in windows
        if w.adaptive_tdee is not None
    ]
    usable = [p for p in points if p[0] > 0]
    if len(usable) < MIN_BIAS_WINDOWS or len({round(p[0], 6) for p in points}) < 3:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    activity = [p[2] for p in points]
    dof = len(points) - 2
    if (
        all(a is not None for a in activity)
        and len({round(a or 0.0, 6) for a in activity}) > 1
    ):
        known = [a or 0.0 for a in activity]
        xs, ys = _net_of(known, xs), _net_of(known, ys)
        dof -= 1
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 1e-9:  # flagged kcal moved only with the activity
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    residual = [
        y - (mean_y + slope * (x - mean_x)) for x, y in zip(xs, ys, strict=True)
    ]
    # The windows overlap, so their slope is less certain than OLS says (#3554);
    # its band takes t at the fit's dof, not z (#3616).
    slope_var = sum(r * r for r in residual) / max(1, dof) / sxx
    data_band = t_quantile(dof) * math.sqrt(slope_var * WINDOW_OVERLAP_INFLATION)
    bias, band = _shrunk_bias(-slope, data_band)
    if (
        band > BIAS_MAX_BAND
        or _independent_windows(windows) < MIN_INDEPENDENT_BIAS_WINDOWS
    ):
        return None
    return bias, band


def _shrunk_bias(raw: float, band: float) -> tuple[float, float]:
    """``raw`` with its 95 % band, shrunk toward 0 by the prior 0 +- 0.25 and
    kept at or above -1 (#3616): the data's SD is its band / 1.96."""
    if band <= 0:
        return max(BIAS_MIN, raw), 0.0
    precision = (BAND_Z / band) ** 2
    prior = 1.0 / BIAS_PRIOR_SD**2
    shrunk = raw * precision / (precision + prior)
    return max(BIAS_MIN, shrunk), BAND_Z / math.sqrt(precision + prior)


def _independent_windows(windows: Sequence[EnergyWindow]) -> int:
    """Estimated windows with flagged entries that share no day, counted from
    the latest back (#3616)."""
    count, last = 0, None
    for window in sorted(windows, key=lambda w: w.end, reverse=True):
        if window.adaptive_tdee is None or window.flagged_kcal_per_day <= 0:
            continue
        if last is None or (last - window.end).days >= window.days:
            count, last = count + 1, window.end
    return count


def _net_of(regressor: Sequence[float], values: Sequence[float]) -> list[float]:
    """``values`` minus their least-squares line on ``regressor`` (#3586)."""
    slope, _ = _slope(regressor, values)
    mean_r, mean_v = statistics.fmean(regressor), statistics.fmean(values)
    return [
        v - mean_v - slope * (r - mean_r)
        for r, v in zip(regressor, values, strict=True)
    ]
