# M12 — Adaptive daily expenditure from intake and the weight trend

- **Card:** M12
- **Card version:** 12
- **Method ID:** adaptive-expenditure v12 (#3100, #3187, #3255, #3552, #3551, #3553, #3555, #3554, #3586, #3603, #3612, #3614)
- **Status:** method implemented (`backend/nutrition/energy_balance.py`); the reader, diagnostics and display follow (#3100, #3099)
- **Classification:** model estimate
- **Literature checked:** 2026-11-15
- **Review:** blocked

## Target construct

The athlete's total daily energy expenditure (TDEE) over rolling 4-week
windows, estimated from their own logged intake and the change in stored
energy shown by the weigh-ins. From it:

- the one activity calibration factor for M11;
- the derived resting rate, `TDEE − own activity − TEF`;
- the systematic bias of meals flagged as uncertain (restaurant, friends).

It is information for energy-balance and fuelling advice, labelled as an
estimate. It never rewrites logged values and never falls back to a formula.

## Formula and units

1. **Current weight:** per day from the first weigh-in on, the least-squares
   line through the weigh-ins of the last 28 days, read at the latest of them,
   so a steady change has no lag (#3551; an exponential trend lagged it by
   about 14 weigh-ins). With fewer than 3 weigh-ins or a span below 7 days it
   is their mean; a day with none in those 28 days carries the previous value.
   It gives the mass of the formula rows' lean mass, of the activity pricing
   and of the energy density, and the resting reference uses the same weight.
2. **Energy density of a weight change:**
   `ρ = p × 9,441 + (1 − p) × 1,816` kcal/kg (39.5 MJ/kg fat and 7.6 MJ/kg lean,
   R68). The fat share `p` blends a prior 0.70 ± 0.15 with the measured share
   by their precisions (athlete decision, #3555): the slope of the fat mass
   (current weight × body fat) over the slope of the weight, through all
   dated body fat values of one method up to the latest window's end, each with a
   repeatability of 2 percentage points; for skinfolds the athlete's own once
   10 sites have repeat readings (#3556: the pooled within-site SD through
   the protocol's sensitivity at the latest measurement, times each
   measurement's own median gain: 1, 0.71 or 0.67 for one, two or three
   readings per site), each fat mass weighted by its own SD (weighted least
   squares). Its SD is the fat mass's SD over
   `sqrt(Σ(t − t̄)²)` and the weight slope; the posterior SD enters the band.
   With fewer than two values 14 days apart, or under 0.1 kg of weight
   change between them, `p = 0.70` (about 7,154 kcal/kg). Every window of
   the view takes this one phase-wide share, so a measurement that arrives
   mid-phase moves all windows alike and puts no step into the windows
   that `k` and `b` are fitted over (#3554).
3. **Stored energy per day:** `ρ × slope`, the least-squares slope of the
   window's own weigh-ins in kg/day. The current weight only supplies the
   mass for `ρ`.
4. **Adaptive TDEE:** mean intake of the window's complete days minus the stored
   energy per day. All complete days count; flagged entries only widen the
   band (athlete decision, #3187 item 14). The value "ohne Schätzung" comes
   only from a window without any flagged day; dropping a flagged day's
   intake while keeping the window's stored energy would bias it.
5. **Band (95 %):**
   `t × √(SD²_intake / n + Σ(0.25 × flagged kcal)² / n² + SE²_store)`, with
   the flagged kcal the logged kcal of each day's flagged entries and `t`
   the two-sided 95 % Student quantile for the smaller of `n − 1` and
   `weigh-ins − 2` degrees of freedom (1.96 beyond 120). `SE_store` combines
   `ρ` times the slope's standard error with the slope times the density's
   SD, `0.144 × (9,441 − 1,816)` kcal/kg (fat share between 0.5 and 1.0 as
   uniform). The slope's standard error carries the AR(1) factor
   `√((1 + r) / (1 − r))` of the lag-1 autocorrelation `r` of the weigh-ins
   around their line, clamped to 0–0.8: scatter that persists from one
   weigh-in to the next, as water and glycogen do, is fewer independent
   readings (#3614). The deficit card's band is `t × SE_store` with the same
   `t`, from `weigh-ins − 2` alone for a window without enough logged days
   (#3614).
5a. **Stability (#3614):** a weight step from water or glycogen is priced at
   tissue density and enters a window's slope at up to 1.45 × its size (a
   1 kg step at mid-window reads as about 380 kcal/day), with different
   amounts in the overlapping windows. Two windows that share days
   (7 to 21 days apart) may differ, net of their measured own activity, only
   by the noise of the days they do not share: each day's intake and
   weigh-in enters their difference with the difference of its two
   coefficients, at the pair's pooled intake scatter (plus the flagged
   entries' 25 %) and weigh-in scatter (half the mean squared change between
   consecutive weigh-ins, which a single step barely moves). A difference
   beyond `2.576 / 1.96 × t` times that SD, about the 99 % band, marks both
   windows unstable; each unstable window's band, the derived resting rate's
   and `SE_store` take its largest such difference in quadrature
   (`SE_store` as difference / `t`). The deficit card then reads
   "Geschätzter Verbrauch" and names "Gewichtssprung (Wasser, Glykogen)" as
   the main uncertainty. `is_stable(window)` is the check a fit over windows
   uses to skip unstable ones.
6. **Calibration factor `k`:** the least-squares slope of `TDEE − TEF` on the
   uncalibrated M11 activity over the windows, combined with the prior 1 ± 0.10
   as a precision-weighted mean, then clamped to 0.85–1.15. It stays 1 with
   fewer than 4 windows or no spread in activity. Neighbouring windows
   share 21 of their 28 days, so the slope's variance is multiplied by
   28 / 7 = 4 before the shrinkage; the bias `b` multiplies its slope
   variance by the same 4, so its band doubles (#3554).
7. **Derived resting rate:** `TDEE − k × activity − TEF`. Its band adds `k`
   times the window's mean M11 activity error (each part's method share, not
   averaged out across days) and the TEF error of 80 kcal/day, in quadrature
   (#3552).
   The activity is shown in two parts (split A, #3100): NEAT (steps outside
   sessions and floors) and the priced sessions, each as the mean over the
   same days as the activity, raw and times `k`. They add up to the
   activity; a day missing either part leaves both unknown, never 0, and a
   day without sessions has 0 session kcal.
   **Plausibility gate (#3255):** the residual carries every error of the
   logged intake and the modelled activity. When its whole band lies below
   the resting reference band (own Katch-McArdle from skinfold lean mass
   with its M13 band, else Garmin's mean resting value without a band), it
   is shown as "nicht plausibel" instead of a value; TDEE stays shown.
   Overlapping bands, or no reference, keep the value (athlete decision
   2026-11-16), with its short reason in the row.
   **Unmeasured everyday energy (#3550):** NEAT counts steps and floors
   only, so standing, housework and unrecorded movement end up in the
   residual. Where the residual exceeds that same resting reference, the
   own row shows Ruhe at the reference (with its band) and the excess as
   "nicht erfasster Alltag" with the residual's band; the total and the
   other rows are unchanged. It is a named remainder, not an estimate: it
   also carries logging and activity-model errors. Learning it as an offset
   for the formula rows is a later step (athlete decision 2026-11-18).
   The table's small "i"
   shows NEAT's inputs over the window: steps per day, the steps sessions
   take out, the step length with its source and the floors outside
   sessions.
8. **Flagged-meal bias `b`:** across the windows, `TDEE = T − b × x + c × a` by
   least squares, with `x` the flagged entries' logged kcal per complete day
   and `a` the window's raw M11 activity (#3586): `b` is the flagged effect at
   fixed activity, so a change in training volume is not read as a logging
   bias. Without spread in `a` the activity term drops out. `b` is reported
   in % with its band: positive means flagged meals were larger than logged.
9. **Learned correction:** once `b` is estimated, each window also shows
   `TDEE + b × x` (the flagged kcal counted at `1 + b`), labelled, with `x`
   times b's band added. Logged entries are never rewritten.

## Sources

- R68 (Hall 2008): energy content of fat and lean tissue change, and Forbes'
  body-composition curve.
- M11 and its sources for the activity and TEF terms.
- The method comment and athlete decisions on #3100 and #3099.

## Population and reference measurement

R68 models adults in weight change. The adaptive estimate itself is the
athlete's own data; it is not validated against doubly labelled water.
Under-reported intake (in athletes often 10–20 %) biases it low, and weight
noise widens it.

## Protocol and data window

28-day windows ending weekly over the last 12 weeks. Intake comes from the
diary, the weight from canonical weigh-ins, body fat from skinfold
measurements (#3099), and the activity and TEF terms from M11. A day whose
Garmin summary ended before midnight holds part of its steps, so its activity
is unknown and it leaves the window's activity means; its intake still counts
(#3612). The diagnostics count such past days of the latest window
(`latest_window_unfinished_days`).

Weigh-ins come only from the app (athlete decision, #3557): weights entered
in the app and the profile's weights from the change history, dated by the
day of the save. Garmin's weights are copies of them and count only on a day
without an app weight; a provider's stored profile weight (Wahoo `user_read`)
and any other provider never count. The profile's body fat values are dated
3-site skinfold readings and count as skinfolds; Garmin's body fat values fill
only days without an app value. The diagnostics count the latest window's
weigh-in days per source and the dropped days per provider
(`latest_window_weighin_sources`).

## Measurement error and uncertainty

Shown as a 95 % band. The intake scatter, the flagged-entry uncertainty
(25 % of the flagged entries' kcal) and the weight-trend error are combined. A logging
bias that is the same on every day is not visible in the band.

## Bias and transferability

- Logging bias and expenditure bias cannot be separated (weak evidence).
- The windows overlap, so their calibration points are correlated; the
  fits inflate their slope variance by 4 for it, an approximation (#3554).
- Water and glycogen shifts move the weight without stored energy. A steady
  drift is damped, but a step inside the window enters the slope at up to
  1.45 × its size and is priced at tissue density; the residual
  autocorrelation and the neighbour check widen the band for it instead of a
  second glycogen model (#3614). In synthetic runs a 1 kg step at mid-window
  is covered when the daily scatter is small; with a daily scatter of 0.5 kg
  it is often indistinguishable from noise and only partly covered.

## Deviations from the studied procedure

- R68 runs a dynamic model. This uses its tissue energy densities on the
  window's net change only.
- The prior fat share 0.70 ± 0.15 is a product assumption between Forbes'
  curve (about 0.55 at this body fat) and 0.80 for strength training with
  high protein (#3555 review).

## Minimum data and stop rules

- A day is complete with at least 2 entries and an intake of at least 70 % of
  the resting rate plus the day's TEF (#3553). The day's training does not
  raise the threshold, so a fully logged long day counts while the window's
  stored energy covers it anyway. The diagnostics count the complete days the
  former rule (60 % of resting + own activity + TEF) would have dropped.
- A window needs 14 complete days, 8 weigh-ins and 21 days between its first
  and last weigh-in. Otherwise it reports `intake_days`, `weighins` or
  `weight_span` and shows "Datenbasis zu gering".
- The stored energy needs the weigh-ins only ("Nur Waage", athlete decision,
  #3603): with 8 weigh-ins over 21 days it is computed with its band whether or
  not the window has complete logging days, so the Verlauf deficit card stays.
  The TDEE, the derived resting rate and the calibration still need the
  complete days; without them the card names the missing expenditure.
- The bias needs 4 windows with flagged days and spread in their share.
- The first read uses the existing diary and weigh-in history
  (`history_start`), not only days logged afterwards. With fewer than 4
  windows the calibration stays at 1 and each window carries how many windows
  the fit used (`calibration_windows`), so the display can say so.

## Numeric rules

| Rule | Value | Tag | Rationale |
| --- | --- | --- | --- |
| Fat / lean energy density | 9,441 / 1,816 kcal/kg | source | R68 (39.5 / 7.6 MJ/kg) |
| Prior fat share | 0.70 ± 0.15 | product assumption | between Forbes' curve and 0.80 for strength training with high protein (#3555) |
| Body fat repeatability | 2 percentage points | product assumption | upper end of the review's 1.5-2 for self-measured skinfolds (#3555) |
| Minimum weight change for a measured share | 0.1 kg | product assumption | below it the share divides by noise |
| Current weight | 28-day line at the latest weigh-in; mean below 3 weigh-ins or 7 days | product assumption | no lag on a steady change, damps single readings (#3551) |
| Window / step | 28 / 7 days | product assumption | #3099, #3100 |
| Complete day | ≥ 2 entries, ≥ 70 % of resting + TEF | product assumption | excludes clearly partial logging without dropping long training days (#3553) |
| Minimum basis | 14 complete days, 8 weigh-ins, 21-day span | product assumption | stop rule, no formula fallback |
| Flagged-entry uncertainty | 25 % of the flagged entries' kcal | product assumption | #3100 athlete decision, #3187 |
| Calibration prior / bounds | 1 ± 0.10 / 0.85–1.15 | product assumption | method comment, #3100 |
| Band | t quantile, 1.96 beyond 120 dof | product assumption | two-sided 95 %, standard t table (#3187 item 10); the deficit card uses the same (#3614) |
| Residual autocorrelation cap | 0.8 (factor 3 at most) | product assumption | AR(1) factor for persisting weigh-in scatter (#3614) |
| Stability band | 2.576 / 1.96 × t | product assumption | about 99 % per pair, so flat series are rarely flagged across up to 6 neighbour pairs (#3614) |
| Fat share SD for the density | 0.144 | product assumption | share 0.5-1.0 uniform; the density's uncertainty in the band |

## Allowed statements

- "Verbrauch aus deinen Daten: X kcal/Tag, Spanne L–H (Z vollständige Tage)",
  with L = max(0, X − Y) and H = X + Y; the app shows the span under the
  estimate and never as "± Y" (#3478).
- "Ohne geschätzte Mahlzeiten: X (L–H)", only from windows without any flagged day.
- "Mit gelernter Korrektur geschätzter Mahlzeiten: X (L–H)", once `b` exists.
- "Geschätzte Mahlzeiten wirken im Mittel X % niedriger/höher als erfasst (± Y)".
- "Datenbasis zu gering", with the reason.
- "Geschätzter Verbrauch" instead of "Echter Verbrauch" for an unstable window, with "Gewichtssprung (Wasser, Glykogen)" as the main uncertainty (#3614).

## Forbidden statements

- A value without its band and its basis.
- A formula value in place of a missing estimate.
- Rewritten intake.

## Open points

- The reader (`backend/nutrition/energy_inputs.py`) reads the diary, the
  canonical weigh-ins, daily steps and floors, the sessions and their streams
  (`backend/nutrition/wiring.py`); the diagnostics show its counts under
  `energy_balance`.
- The resting reference for the net terms and the completeness rule is
  Katch-McArdle from lean mass: first the skinfold body fat (#3099,
  `katch_mcardle_skinfold`), then the latest measured body fat
  (`katch_mcardle_body_fat`). Each needs a weigh-in or body fat within 90
  days. Garmin's resting value is only the last fallback (`garmin`). The
  skinfold body fat also sets the fat share of the energy density.
- Sessions without power or a fitted HR line use Keytel, with the age from
  the profile's birth year and the profile's sex.
- The meal flag is stored as `"uncertain": true` in the diary entry's existing
  `nutrition_basis` JSON and read as `estimate_uncertain` (#3100, #3182); no
  schema change.
- Wishnofsky 1958 is not cited.

<!-- BEGIN GENERATED CONSTANTS -->
## Code constants

| Constant | Value | Module | Code comment |
| --- | --- | --- | --- |
| `METHOD_ID` | `'adaptive-expenditure'` | `backend/nutrition/energy_balance.py` |  |
| `METHOD_VERSION` | `12` | `backend/nutrition/energy_balance.py` | Bump when a formula or constant below changes the estimate. |
| `WINDOW_DAYS` | `28` | `backend/nutrition/energy_balance.py` | Days per window and windows per week of the rolling view (#3099, #3100). |
| `CURRENT_WEIGHT_DAYS` | `28` | `backend/nutrition/energy_balance.py` | A day's current weight is the least-squares line through the weigh-ins of this many days up to it, at its latest weigh-in, so a steady change has no lag (#3551); with fewer weigh-ins or a shorter span, their mean. |
| `MIN_LINE_WEIGHINS` | `3` | `backend/nutrition/energy_balance.py` |  |
| `MIN_LINE_SPAN_DAYS` | `7` | `backend/nutrition/energy_balance.py` |  |
| `MIN_COMPLETE_DAYS` | `14` | `backend/nutrition/energy_balance.py` | A window needs this many complete logging days, weigh-ins and days between its first and last weigh-in; otherwise it gives its reason, never a formula. |
| `MIN_WEIGHINS` | `8` | `backend/nutrition/energy_balance.py` |  |
| `MIN_WEIGHT_SPAN_DAYS` | `21` | `backend/nutrition/energy_balance.py` |  |
| `MIN_DAY_ENTRIES` | `2` | `backend/nutrition/energy_balance.py` | A day counts as complete with this many diary entries. |
| `MIN_DAY_REST_SHARE` | `0.7` | `backend/nutrition/energy_balance.py` | A complete day also needs an intake of at least this share of the resting rate plus the day's TEF (#3553): independent of its training, so a fully logged long day counts; an intake below it lies far beneath any planned deficit, so meals are missing (product assumption). |
| `FORMER_DAY_SHARE` | `0.6` | `backend/nutrition/energy_balance.py` | The former rule's share of the day's whole expenditure (resting + own activity + TEF); only the diagnostics count the complete days it dropped. |
| `FAT_KCAL_PER_KG` | `9441.0` | `backend/nutrition/energy_balance.py` | Hall 2008 (Int J Obes 32:573): 39.5 MJ per kg fat and 7.6 MJ per kg lean tissue, in kcal/kg. |
| `LEAN_KCAL_PER_KG` | `1816.0` | `backend/nutrition/energy_balance.py` |  |
| `FAT_SHARE_PRIOR` | `0.7` | `backend/nutrition/energy_balance.py` | The fat share of a weight change is a precision-weighted blend of a prior and the slope of the phase's measured fat mass (athlete decision, #3555): the prior 0.70 +- 0.15 lies between Forbes' curve (about 0.55 at this body fat) and the 0.80 of strength training with high protein, and covers both. |
| `FAT_SHARE_PRIOR_SD` | `0.15` | `backend/nutrition/energy_balance.py` |  |
| `BODY_FAT_REPEATABILITY_PCT` | `2.0` | `backend/nutrition/energy_balance.py` | Repeatability of one self-measured body fat value (percentage points, SD): the upper end of the review's 1.5-2 for skinfolds (#3555). |
| `MIN_FAT_SHARE_WEIGHT_CHANGE_KG` | `0.1` | `backend/nutrition/energy_balance.py` | Below this weight change between the first and last measurement the measured share divides by noise, so the prior stands alone (product assumption). |
| `FLAGGED_DAY_UNCERTAINTY` | `0.25` | `backend/nutrition/energy_balance.py` | Extra uncertainty of a flagged (estimated) meal: a share of its logged kcal. |
| `BAND_Z` | `1.96` | `backend/nutrition/energy_balance.py` | Two-sided 95 % band multiplier. |
| `T_QUANTILE_95` | `{1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2... (sha256 e00a81c5)` | `backend/nutrition/energy_balance.py` | Two-sided 95 % Student t quantiles by degrees of freedom (standard table); a window's band uses the smaller of its intake (days - 1) and weight-slope (weigh-ins - 2) degrees of freedom, BAND_Z beyond the table (#3187 item 10). |
| `MIN_FAT_SPAN_DAYS` | `14` | `backend/nutrition/energy_balance.py` | The shortest span between the first and last measurement that informs the share: a shorter one divides the measurement noise by too few days. |
| `CALIBRATION_PRIOR_SD` | `0.1` | `backend/nutrition/energy_balance.py` | The one activity calibration factor: prior 1 +- 0.10, clamped to 0.85-1.15, fitted on complete windows of the last 12 weeks (method comment, #3100). |
| `CALIBRATION_BOUNDS` | `(0.85, 1.15)` | `backend/nutrition/energy_balance.py` |  |
| `MIN_CALIBRATION_WINDOWS` | `4` | `backend/nutrition/energy_balance.py` | Windows needed before the factor moves from 1, and before a flagged-meal bias is estimated. |
| `MIN_BIAS_WINDOWS` | `4` | `backend/nutrition/energy_balance.py` |  |
| `MAX_RESIDUAL_AUTOCORRELATION` | `0.8` | `backend/nutrition/energy_balance.py` | A weight step from water or glycogen is not tissue (#3614): weigh-ins whose scatter persists widen the slope's error by the AR(1) factor, with the lag-1 autocorrelation capped here (factor 3 at most); overlapping windows that differ beyond the 99 % band of the days they do not share are unstable and their band holds the difference. 2.576 / 1.96 turns the 95 % t into 99 %. |
| `STABILITY_BAND_RATIO` | `2.576 / 1.96` | `backend/nutrition/energy_balance.py` |  |
<!-- END GENERATED CONSTANTS -->
