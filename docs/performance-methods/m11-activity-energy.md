# M11 — Own activity energy, net of resting metabolism

- **Card:** M11
- **Card version:** 1
- **Method ID:** activity-energy v1 (#3100)
- **Status:** methods implemented (`backend/performance/activity_energy.py`); daily wiring, adaptive TDEE and display follow (#3100, #3099)
- **Classification:** model estimate
- **Literature checked:** 2026-11-15
- **Review:** blocked

## Target construct

The energy the athlete spends on activity, on top of the resting metabolic rate,
per session and per day, computed from the athlete's own data instead of a
device's calorie estimate. Daily model (athlete decision 2026-11-15, #3100):
`TDEE = RMR + TEF + Σ session net cost + NEAT`, with one shrunken calibration
factor fitted against the adaptive TDEE (energy balance, #3100). This card
covers the activity terms and the thermic effect of food; the resting rate and
the calibration are separate. Garmin's or Wahoo's active calories are shown
only for comparison and never feed these terms.

## Formula and units

All session terms are **net** of the resting rate, so they add to RMR without
counting it twice. 1 kcal = 4184 J.

1. **Running:** `net = m × Σ d_s × C_r(i_s)` with mass `m` (kg), segment run
   `d_s` (m) and the Minetti cost `C_r(i)` (J/kg/m) at the segment's gradient
   `i_s`, clamped to ±0.45. Segments merge samples until the run reaches 20 m,
   so altitude noise does not become a steep gradient. On the flat
   `C_r(0) = 3.6 J/kg/m` (0.86 kcal/kg/km). Running power is never read as
   energy.
2. **Cycling with power:** `gross = W / 4.184 / GE` (W in kJ, GE = 0.20),
   `net = gross − RMR × duration`.
3. **Endurance without power:** the athlete's own flex-HR line
   `net kcal/min = a + b × HR + c × elapsed min`, fitted by least squares on
   steady segments (first 3 min skipped) of their power sessions whose net
   cost comes from item 2. With fewer than 20 segments, or all at one heart
   rate, Keytel's equation without VO2max is the fallback, net of RMR.
4. **Strength:** `net = (MET − 1) × RMR/min × duration` with the Compendium
   2024 MET 3.5 (general) or 6.0 (vigorous), i.e. the MET corrected to the
   measured resting rate instead of 1 MET = 3.5 ml/kg/min. Heart rate is not
   used for strength.
5. **NEAT:** steps outside recorded sessions × step length × Minetti's level
   walking cost `C_w(0) = 2.5 J/kg/m` × mass, plus floors as vertical work
   `m × g × 3 m / 0.25`. Step length from recorded walks (speed ÷ cadence),
   else 0.415 × height.
6. **TEF:** `0.25 × 4 × protein_g + 0.10 × 4 × carbohydrate_g + 0.015 × 9 × fat_g`.
7. **EPOC (optional, off by default):** 6 % of the net cost of a session above
   the threshold.

## Sources

- R44 (Minetti et al. 2002): the running and walking cost polynomials, net
  metabolic power per unit distance; coefficients checked in the paper's Fig. 2.
- R41, R42, R43: cycling gross efficiency (19.8 % in trained men, work-rate and
  training dependent).
- R63 (Keytel et al. 2005): heart-rate equation without VO2max (fallback).
- R64 (Herrmann et al. 2024, 2024 Adult Compendium): resistance-training METs.
- R67 (Byrne et al. 2005): 1 MET overestimates measured resting VO2, the reason
  to correct METs to the measured resting rate.
- R65 (Joosen & Westerterp 2006): thermic effect per macronutrient.
- R66 (LaForgia et al. 2006): EPOC 6-15 % of the net exercise oxygen cost.

## Population and reference measurement

Minetti: 10 elite mountain runners on a treadmill, indirect calorimetry.
Moseley & Jeukendrup: 17 trained men, cycle ergometer. Keytel: 115 regularly
exercising adults aged 18-45, gas exchange. The Compendium values are adult
averages. None of them is this athlete; the calibration against the adaptive
TDEE carries the individual offset.

## Protocol and data window

Per completed session from its canonical summary and stored streams (distance,
altitude, power, heart rate); per day from Garmin steps and floors. The flex-HR
line is refitted on the last 4-8 weeks of power sessions.

## Measurement error and uncertainty

Expected error (method comment, #3100): running ±8-10 %, cycling ±10 %,
flex-HR ±10-15 % individually calibrated and ±20-30 % or more with Keytel,
strength ±30-50 % (small absolute share), step length ±10-15 %, TEF ±30-80
kcal/day against a flat 10 %. Sums are reported with bands, not points.

## Bias and transferability

Laboratory treadmill and ergometer values transfer imperfectly to outdoor
terrain, wind and stops. GPS altitude is noisy; the 20 m merge limits it.
Gross efficiency varies with work rate (lower below about 100 W). The flex-HR
line is calibrated on power-derived cost, not calorimetry (weak evidence).

## Deviations from the studied procedure

- Minetti's polynomial is applied per GPS segment of field runs, not on a
  treadmill at constant speed.
- GE is one constant instead of a per-work-rate value.
- Flex-HR uses power-derived net cost as its reference instead of calorimetry.
- Floors use an efficiency of 0.25 taken from Minetti's statement that steep
  uphill walking reaches concentric muscle efficiency.

## Minimum data and stop rules

- Running needs distance and altitude streams; without altitude the grade is 0.
- Cycling needs the session's work (kJ) or a power stream.
- Flex-HR needs 20 steady segments over at least two heart-rate levels, else
  Keytel; Keytel needs weight, age and sex, else no value.
- NEAT needs the day's steps; without a measured or height-based step length
  there is no value.
- A missing input gives "unknown", never a device value in its place.

## Numeric rules

| Rule | Value | Tag | Rationale |
| --- | --- | --- | --- |
| Running cost polynomial | 155.4, −30.4, −43.3, 46.3, 19.5, 3.6 J/kg/m | source | R44, Fig. 2 |
| Walking cost polynomial | 280.5, −58.7, −76.8, 51.9, 19.6, 2.5 J/kg/m | source | R44, Fig. 2 |
| Gradient clamp | ±0.45 | source | R44 measured range |
| Grade segment length | 20 m | product assumption | limits GPS altitude noise |
| Cycling gross efficiency | 0.20 | source | R42 (19.8 % measured), varies per R41, R43 |
| Strength MET general / vigorous | 3.5 / 6.0 | source | R64 codes 02054 / 02050 |
| MET correction to own RMR | (MET − 1) × RMR | source | R67 |
| Keytel coefficients (kJ/min) | men −55.0969, 0.6309, 0.1988, 0.2017; women −20.4022, 0.4472, −0.1263, 0.074 | source | R63, as reproduced by secondary sources |
| Flex-HR skip / minimum segments | 180 s / 20 | product assumption | steady state; enough spread for three terms |
| Floor height / uphill efficiency | 3 m / 0.25 | product assumption | Garmin floor; R44 concentric efficiency statement |
| Step length from height | 0.415 × height | product assumption | common heuristic, weak evidence |
| TEF protein / carbohydrate / fat | 0.25 / 0.10 / 0.015 | source | R65 midpoints |
| EPOC share | 0.06 | source | R66 low end |

## Allowed statements

- "Eigener Aktivitätsverbrauch (Netto, über dem Ruheumsatz): X kcal, Methode M11", with its band.
- Which method a session used (Minetti, Leistung, eigene Herzfrequenz-Kalibrierung, Keytel, MET).

## Forbidden statements

- A session value without its method or band.
- Garmin's or Wahoo's active calories as part of the own estimate.
- Running power as energy.
- A strength estimate from heart rate.

## Open points

- Daily wiring, the adaptive TDEE and the calibration factor (#3100).
- Imbach 2020 and Cerezuela-Espejo 2021 are not cited until identified.
- Keytel's coefficients are checked against secondary sources only.
- EPOC stays off until the calibration shows a need.

<!-- BEGIN GENERATED CONSTANTS -->
## Code constants

| Constant | Value | Module | Code comment |
| --- | --- | --- | --- |
| `METHOD_ID` | `'activity-energy'` | `backend/performance/activity_energy.py` |  |
| `METHOD_VERSION` | `1` | `backend/performance/activity_energy.py` | Bump when a formula or constant below changes the estimate. |
| `J_PER_KCAL` | `4184.0` | `backend/performance/activity_energy.py` | Joules per kilocalorie (thermochemical calorie). |
| `GRAVITY` | `9.81` | `backend/performance/activity_energy.py` | Standard gravity, m/s^2, for the vertical work of floors. |
| `MINETTI_RUNNING` | `(155.4, -30.4, -43.3, 46.3, 19.5, 3.6)` | `backend/performance/activity_energy.py` | Minetti et al. 2002 (J Appl Physiol 93:1039, Fig. 2): net metabolic cost of running per unit distance, J/kg/m, as a 5th-order polynomial of the gradient (rise/run, -0.45 to +0.45); coefficients from i^5 down to the constant. |
| `MINETTI_WALKING` | `(280.5, -58.7, -76.8, 51.9, 19.6, 2.5)` | `backend/performance/activity_energy.py` | Minetti et al. 2002: the same fit for walking. |
| `GRADE_LIMIT` | `0.45` | `backend/performance/activity_energy.py` | The gradient range Minetti et al. 2002 measured; steeper segments are clamped. |
| `GRADE_MIN_RUN_M` | `20.0` | `backend/performance/activity_energy.py` | Segments shorter than this are merged for the grade, so GPS altitude noise on a few metres does not become a steep gradient. |
| `CYCLING_GROSS_EFFICIENCY` | `0.2` | `backend/performance/activity_energy.py` | Gross efficiency of cycling (mechanical / metabolic energy): 19.8 % measured in 17 trained men by Moseley & Jeukendrup 2001 (R42); it varies with work rate and training (R41, R43), and the one calibration factor absorbs an individual offset (weak evidence). |
| `STRENGTH_MET_GENERAL` | `3.5` | `backend/performance/activity_energy.py` | 2024 Adult Compendium of Physical Activities (Herrmann et al., J Sport Health Sci 13:6): code 02054, resistance training, multiple exercises, 8-15 reps. |
| `STRENGTH_MET_VIGOROUS` | `6.0` | `backend/performance/activity_energy.py` | Compendium 2024 code 02050: free weights, power lifting, vigorous effort. |
| `KEYTEL_MALE` | `(-55.0969, 0.6309, 0.1988, 0.2017)` | `backend/performance/activity_energy.py` | Keytel et al. 2005 (J Sports Sci 23:289), model without VO2max: gross energy expenditure in kJ/min from heart rate (bpm), weight (kg) and age (years): intercept, HR, weight, age. Coefficients as reproduced by several secondary sources; fallback only (about +-20-30 % individually). |
| `KEYTEL_FEMALE` | `(-20.4022, 0.4472, -0.1263, 0.074)` | `backend/performance/activity_energy.py` |  |
| `FLEX_HR_SKIP_S` | `180.0` | `backend/performance/activity_energy.py` | Flex-HR: the first minutes of a session are not steady state and are not fitted. |
| `FLEX_HR_MIN_SEGMENTS` | `20` | `backend/performance/activity_energy.py` | Flex-HR needs at least this many steady segments from power sessions. |
| `FLOOR_HEIGHT_M` | `3.0` | `backend/performance/activity_energy.py` | Vertical metres per counted floor (Garmin counts about 3 m of ascent). |
| `UPHILL_WORK_EFFICIENCY` | `0.25` | `backend/performance/activity_energy.py` | Efficiency of vertical work when walking up steep gradients: Minetti et al. 2002 found uphill walking above +0.15 reaches concentric muscle efficiency, taken here as 0.25 (assumption from that statement). |
| `STEP_LENGTH_PER_HEIGHT` | `0.415` | `backend/performance/activity_energy.py` | Step length from body height when no recorded walk measured it; a common heuristic, weak evidence (+-10-15 %). |
| `TEF_PROTEIN` | `0.25` | `backend/performance/activity_energy.py` | Thermic effect of food as a share of each macronutrient's energy: midpoints of protein 20-30 %, carbohydrate 5-15 %, fat 0-3 % (Joosen & Westerterp 2006, Nutr Metab 3:25). Alcohol is not stored, so it has no term. |
| `TEF_CARBOHYDRATE` | `0.1` | `backend/performance/activity_energy.py` |  |
| `TEF_FAT` | `0.015` | `backend/performance/activity_energy.py` |  |
| `KCAL_PER_G_PROTEIN` | `4.0` | `backend/performance/activity_energy.py` | Atwater energy per gram: protein, carbohydrate, fat (kcal/g). |
| `KCAL_PER_G_CARBOHYDRATE` | `4.0` | `backend/performance/activity_energy.py` |  |
| `KCAL_PER_G_FAT` | `9.0` | `backend/performance/activity_energy.py` |  |
| `EPOC_SHARE` | `0.06` | `backend/performance/activity_energy.py` | Optional excess post-exercise oxygen consumption for sessions above the threshold: 6 % of the net session cost, the low end of LaForgia et al. 2006 (J Sports Sci 24:1247, 6-15 %). Off unless asked for; weak evidence. |
<!-- END GENERATED CONSTANTS -->
