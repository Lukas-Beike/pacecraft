# M12 — Adaptive daily expenditure from intake and the weight trend

- **Card:** M12
- **Card version:** 3
- **Method ID:** adaptive-expenditure v3 (#3100, #3187, #3255)
- **Status:** method implemented (`backend/nutrition/energy_balance.py`); the reader, diagnostics and display follow (#3100, #3099)
- **Classification:** model estimate
- **Literature checked:** 2026-11-15
- **Review:** blocked

## Target construct

The athlete's total daily energy expenditure (TDEE) over rolling 4-week
windows, estimated from their own logged intake and the change in stored
energy shown by the smoothed weight trend. From it:

- the one activity calibration factor for M11;
- the derived resting rate, `TDEE − own activity − TEF`;
- the systematic bias of meals flagged as uncertain (restaurant, friends).

It is information for energy-balance and fuelling advice, labelled as an
estimate. It never rewrites logged values and never falls back to a formula.

## Formula and units

1. **Weight trend:** exponentially weighted, half-life 10 days, from the first
   weigh-in on; days without a weigh-in carry it.
2. **Energy density of a weight change:**
   `ρ = p × 9,441 + (1 − p) × 1,816` kcal/kg (39.5 MJ/kg fat and 7.6 MJ/kg lean,
   R68). The fat share `p = FM / (FM + 10.4 kg)` follows Forbes' curve as used by
   R68, with FM from the latest skinfold body fat. Without skinfolds,
   `p = 0.75` (about 7,535 kcal/kg).
3. **Stored energy per day:** `ρ × slope`, the least-squares slope of the
   window's own weigh-ins in kg/day. Unlike the trend's change, it has no
   warm-up lag after the first weigh-in. The trend only supplies the mass for
   `ρ`.
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
   uniform).
6. **Calibration factor `k`:** the least-squares slope of `TDEE − TEF` on the
   uncalibrated M11 activity over the windows, combined with the prior 1 ± 0.10
   as a precision-weighted mean, then clamped to 0.85–1.15. It stays 1 with
   fewer than 4 windows or no spread in activity.
7. **Derived resting rate:** `TDEE − k × activity − TEF`. Its band adds 10 % of
   the activity term.
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
   2026-11-16). The basis shows NEAT's inputs over the window: steps per
   day, the steps sessions take out, the step length with its source and
   the floors outside sessions.
8. **Flagged-meal bias `b`:** across the windows, `TDEE = T − b × x` by least
   squares, with `x` the flagged entries' logged kcal per complete day. `b` is
   reported in % with its band: positive means flagged meals were larger than
   logged.
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
measurements (#3099), and the activity and TEF terms from M11.

## Measurement error and uncertainty

Shown as a 95 % band. The intake scatter, the flagged-entry uncertainty
(25 % of the flagged entries' kcal) and the weight-trend error are combined. A logging
bias that is the same on every day is not visible in the band.

## Bias and transferability

- Logging bias and expenditure bias cannot be separated (weak evidence).
- The windows overlap, so their calibration points are correlated.
- Water and glycogen shifts move the weight without stored energy; the 10-day
  trend and the 28-day window damp them.

## Deviations from the studied procedure

- R68 runs a dynamic model. This uses its tissue energy densities and Forbes'
  share on the window's net change only.
- The fat share without skinfolds is a fixed assumption.

## Minimum data and stop rules

- A day is complete with at least 2 entries and at least 60 % of its own
  expenditure estimate (resting + own activity + TEF).
- A window needs 14 complete days, 8 weigh-ins and 21 days between its first
  and last weigh-in. Otherwise it reports `intake_days`, `weighins` or
  `weight_span` and shows "Datenbasis zu gering".
- The bias needs 4 windows with flagged days and spread in their share.
- The first read uses the existing diary and weigh-in history
  (`history_start`), not only days logged afterwards. With fewer than 4
  windows the calibration stays at 1 and each window carries how many windows
  the fit used (`calibration_windows`), so the display can say so.

## Numeric rules

| Rule | Value | Tag | Rationale |
| --- | --- | --- | --- |
| Fat / lean energy density | 9,441 / 1,816 kcal/kg | source | R68 (39.5 / 7.6 MJ/kg) |
| Forbes constant | 10.4 kg | source | R68, Forbes' curve |
| Fat share without skinfolds | 0.75 | product assumption | about 7,535 kcal/kg, near the common 7,700 |
| Trend half-life | 10 days | product assumption | damps water shifts, follows real change |
| Window / step | 28 / 7 days | product assumption | #3099, #3100 |
| Complete day | ≥ 2 entries, ≥ 60 % of own estimate | product assumption | excludes clearly partial logging |
| Minimum basis | 14 complete days, 8 weigh-ins, 21-day span | product assumption | stop rule, no formula fallback |
| Flagged-entry uncertainty | 25 % of the flagged entries' kcal | product assumption | #3100 athlete decision, #3187 |
| Calibration prior / bounds | 1 ± 0.10 / 0.85–1.15 | product assumption | method comment, #3100 |
| Band | t quantile, 1.96 beyond 120 dof | product assumption | two-sided 95 %, standard t table (#3187 item 10) |
| Fat share SD for the density | 0.144 | product assumption | share 0.5-1.0 uniform; the density's uncertainty in the band |

## Allowed statements

- "Verbrauch aus deinen Daten: X ± Y kcal/Tag (Z vollständige Tage)".
- "Ohne geschätzte Mahlzeiten: X ± Y", only from windows without any flagged day.
- "Mit gelernter Korrektur geschätzter Mahlzeiten: X ± Y", once `b` exists.
- "Geschätzte Mahlzeiten wirken im Mittel X % niedriger/höher als erfasst (± Y)".
- "Datenbasis zu gering", with the reason.

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
| `METHOD_VERSION` | `3` | `backend/nutrition/energy_balance.py` | Bump when a formula or constant below changes the estimate. |
| `WINDOW_DAYS` | `28` | `backend/nutrition/energy_balance.py` | Days per window and windows per week of the rolling view (#3099, #3100). |
| `TREND_HALF_LIFE_DAYS` | `10.0` | `backend/nutrition/energy_balance.py` | Half-life in days of the exponentially weighted weight trend. |
| `MIN_COMPLETE_DAYS` | `14` | `backend/nutrition/energy_balance.py` | A window needs this many complete logging days, weigh-ins and days between its first and last weigh-in; otherwise it gives its reason, never a formula. |
| `MIN_WEIGHINS` | `8` | `backend/nutrition/energy_balance.py` |  |
| `MIN_WEIGHT_SPAN_DAYS` | `21` | `backend/nutrition/energy_balance.py` |  |
| `MIN_DAY_ENTRIES` | `2` | `backend/nutrition/energy_balance.py` | A day counts as complete with this many diary entries and at least this share of the day's own expenditure estimate (resting + own activity + TEF). |
| `MIN_DAY_SHARE` | `0.6` | `backend/nutrition/energy_balance.py` |  |
| `FAT_KCAL_PER_KG` | `9441.0` | `backend/nutrition/energy_balance.py` | Hall 2008 (Int J Obes 32:573): 39.5 MJ per kg fat and 7.6 MJ per kg lean tissue, in kcal/kg. |
| `LEAN_KCAL_PER_KG` | `1816.0` | `backend/nutrition/energy_balance.py` |  |
| `FORBES_CONSTANT_KG` | `10.4` | `backend/nutrition/energy_balance.py` | Forbes' curve as used by Hall 2008: the fat share of a weight change is FM / (FM + 10.4 kg), FM the fat mass in kg. |
| `DEFAULT_FAT_SHARE` | `0.75` | `backend/nutrition/energy_balance.py` | Fat share of a weight change without a body-fat measurement: 0.75 gives about 7,500 kcal/kg, near the common 7,700 (product assumption). |
| `FLAGGED_DAY_UNCERTAINTY` | `0.25` | `backend/nutrition/energy_balance.py` | Extra uncertainty of a flagged (estimated) meal: a share of its logged kcal. |
| `BAND_Z` | `1.96` | `backend/nutrition/energy_balance.py` | Two-sided 95 % band multiplier. |
| `T_QUANTILE_95` | `{1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2... (sha256 e00a81c5)` | `backend/nutrition/energy_balance.py` | Two-sided 95 % Student t quantiles by degrees of freedom (standard table); a window's band uses the smaller of its intake (days - 1) and weight-slope (weigh-ins - 2) degrees of freedom, BAND_Z beyond the table (#3187 item 10). |
| `FAT_SHARE_SD` | `0.144` | `backend/nutrition/energy_balance.py` | Uncertainty of the fat share of a weight change (SD), so the energy density enters the band: a share between 0.5 and 1.0 taken as uniform, 0.5 / sqrt(12) (product assumption, #3187 item 10). |
| `CALIBRATION_PRIOR_SD` | `0.1` | `backend/nutrition/energy_balance.py` | The one activity calibration factor: prior 1 +- 0.10, clamped to 0.85-1.15, fitted on complete windows of the last 12 weeks (method comment, #3100). |
| `CALIBRATION_BOUNDS` | `(0.85, 1.15)` | `backend/nutrition/energy_balance.py` |  |
| `MIN_CALIBRATION_WINDOWS` | `4` | `backend/nutrition/energy_balance.py` | Windows needed before the factor moves from 1, and before a flagged-meal bias is estimated. |
| `MIN_BIAS_WINDOWS` | `4` | `backend/nutrition/energy_balance.py` |  |
<!-- END GENERATED CONSTANTS -->
