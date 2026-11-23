# M10 — First threshold heart rate from chest-strap RR intervals (DFA-α1)

- **Card:** M10
- **Card version:** 6
- **Method ID:** dfa-alpha1-first-threshold v3 (`backend/canonical/dfa_alpha1.py:METHOD_VERSION`), athlete-level value in `backend/performance/first_threshold_estimate.py` (#2131, C-008), running step test run-step-test-v3 in `backend/performance/step_test.py` (#3833, #3916, #3915)
- **Status:** implemented (`backend/providers/fit_rr_mapping.py` reads the RR intervals); per-session result stored with the stream set, athlete-level estimate shown in the profile and the Coach context, adopted only by the athlete
- **Classification:** model estimate
- **Literature checked:** 2026-11-21
- **Review:** blocked

## Target construct

The heart rate at which the athlete's short-term scaling exponent alpha1 of
detrended fluctuation analysis (DFA-α1) of beat-to-beat intervals crosses 0.75,
read from chest-strap recordings of the athlete's own runs. In laboratory
incremental tests this heart-rate variability threshold lies close to the first
ventilatory threshold (R56, R58), the construct that LT1 stands for here
(R54). The result is a heart-rate estimate for the athlete to review, not a
ventilatory or lactate threshold measurement. Nothing writes
it into the profile except the athlete's explicit adoption, which stores it as
the first threshold with the origin `estimated` (`first_threshold_origin`).

Athlete decision (2026-11-10, #2131): write this card and list the method as
implemented, no longer as deferred.

Athlete decisions (2026-11-16, #3273, F1 to F3, F7): the stored window series
also gives a second threshold, the heart rate where alpha1 crosses 0.5 (the
VT2 analogue, Rogers et al. 2021), shown as a local LT2 estimate beside Garmin's
for runs only. The athlete may adopt it with an explicit action; it is stored
with its origin and never adopted automatically.

Athlete decisions (2026-11-21, #3833): a running step test yields both
thresholds from one chest-strap run, with protocol P1 (3-minute stages), a
result block on the completed test with "Übernehmen" per value through the
existing adoption paths, and the evaluation as above plus at least 6
analysable stages for `good` (section "Running step test").

## Formula and units

Per recording (`dfa_alpha1.first_threshold`, unit bpm):

1. **Segments:** the RR series (ms) arrives as gap-free segments; a dropout of
   the sensor starts a new one and a window never spans two segments.
2. **Artifacts:** a beat is dropped when it lies outside 300 to 2000 ms or
   differs by more than 20 % from the last accepted beat; after 5 beats in a
   row rejected only for that change, the new level is accepted.
3. **Detrending:** the artifact-free series of a segment is detrended with
   smoothness priors (R61), λ = 500, before windowing.
4. **Windows:** 120 s windows on the segment's own beat clock (running sum of
   the intervals, each limited to 0.3 to 2 s), starting every 30 s. A window is
   analysed when it holds at least 100 beats, at most 5 % of them artifacts, and
   at least 100 artifact-free beats, and when the mean heart rates of its two
   halves differ by at most 8 bpm.
5. **alpha1:** integrate the detrended series (minus its mean), cut it into
   boxes of n = 4 to 16 beats, remove a linear trend per box, take the
   root-mean-square fluctuation F(n) over all boxes of that size, and fit
   log F(n) against log n; alpha1 is the slope (R56, R60).
6. **Window heart rate:** 60 000 / mean RR (ms) of the window's artifact-free
   beats, in bpm.
7. **Crossing:** a least-squares line of alpha1 on window heart rate over all
   analysed windows; the estimate is the heart rate where it reaches 0.75,
   `(0.75 − intercept) / slope`, rounded to 0.1 bpm.
8. **Quality:** `good` with at least 20 windows, an artifact share (dropped
   beats of all beats) of at most 0.03 and R² of at least 0.5, else `low`.
9. **Window series (v3, card 2, #3273):** every analysed window is kept with
   its start on the recording's beat clock (dropouts left out), its mean heart
   rate, alpha1 and its own artifact share, also when the recording gives no
   0.75 crossing (then estimate and quality are empty). Later thresholds are
   computed from these stored windows; the 0.75 result is unchanged.

Per athlete (`first_threshold_estimate.first_threshold_estimate`, unit percent
of the run threshold HR, "% LSHF"):

1. Take one stored result per run session of the last 60 days (the better
   quality, then the more windows, wins among records of one session); only
   `good` sessions qualify.
2. Express each as `estimate_bpm / run threshold HR in effect on the session's
   day × 100`, rounded to 0.1, so a later threshold change does not move it;
   a session without a known threshold HR or with a percent outside 40 to 100
   does not qualify.
3. With at least 3 qualifying sessions the athlete-level value is their median,
   rounded to 0.1. Its bpm is that percent of today's run threshold HR,
   rounded half up, and absent when no threshold HR is known.
4. The athlete-level quality is `good` with at least 5 sessions whose values
   spread at most 5 percentage points, else `low`.

Second threshold per run (`second_threshold_estimate.session_second_threshold`):

1. Only runs whose stored series is readable (method 3); older rows are not
   evaluated, never "no crossing".
2. A least-squares line of alpha1 on window heart rate over the windows with
   alpha1 at or below 0.75 only, so the easy part does not place the crossing.
3. A result needs at least 10 such windows, a heart-rate spread of 10 bpm,
   windows reaching 0.40 and 0.60, a falling line, a crossing inside the
   observed heart rates and within 60 to 220 bpm, above the same run's 0.75
   crossing, and windows below 0.5 not only in the last quarter of the run
   (late drift).
4. Quality as for the first threshold: `good` with at least 20 windows, a
   recording artifact share of at most 0.03 and R² of at least 0.5.

Athlete level (`dfa_second_threshold`): the median in bpm over at least 3 good
runs of the last 60 days (F1); a percent of the threshold heart rate would move
with the value adopted from it. `good` with at least 5 runs whose spread is at
most 5 % of the median (F2, as for the first threshold, without a per-run
window rule). It must lie above the stored first threshold. The pace beside it
is the athlete's own steady-run pace at that heart rate (card M9).

## Running step test (protocol P1, #3833)

One rested chest-strap run that yields both thresholds: LT1 where alpha1
crosses 0.75 and LT2 where it crosses 0.5. Nothing is adopted automatically.

Protocol (`step_test.protocol_template`, a local library template the Coach
creates with `manage_training_templates` `step_test`; the explicit Garmin sync
sends its steps, nothing else writes remotely):

1. Warm-up 10 min from 1.5 to 0.5 km/h slower than stage 1 (slower than LT1).
2. Stages of 3 min with pace targets, each 0.5 km/h faster and held as a
   band of ±3 % around the stage pace (6:08–6:30 /km for 6:19; athlete
   decision), so the watch alerts only when clearly off. Stage 1 lies
   1.5 km/h below the current run LT1 pace (rounded to 0.1 km/h); without a
   plausible LT1 pace (2:30 to 9:00 /km) it is 9.5 km/h (6:19 /km).
3. The stages run to the second stage beyond the first one at or above the
   current LT2 (threshold) pace, at most 10 and at least 6 stages; without an
   LT2 pace all 10.
4. The run ends when a stage can no longer be held, at the latest after the
   last stage, then 10 min cool-down at the warm-up pace.
5. The unit names the preconditions: a chest strap that records RR intervals,
   HRV recording switched on in the watch, rested, not after a hard day, not
   ill. The Coach plans it only on the athlete's request and under the same
   preconditions; as a `test` unit it is hard for the plan checks. It does
   not reset the reminder for the run critical-speed test; each test keeps its
   own rhythm (athlete decision).

Why 3 minutes and +0.5 km/h: the VT2 validation of the 0.5 crossing (R76) used
3-minute treadmill stages. With the 120 s windows every 30 s and at most 8 bpm
between window halves, a small step keeps the heart-rate jump per stage near or
below 8 bpm, so windows across a stage change still count, and 30 min of
stages give about 50 windows (`good` needs 20). No study compared stage
lengths for running; the choice is a design decision.

Evaluation (`step_test.evaluate`), when the run is paired with a unit from the
step-test template:

1. Input: the run's stored window series (method 3). Without one there is no
   value ("Keine RR-Intervalle").
2. Stages: recorded laps of 150 to 210 s with heart rate and speed, warm-up,
   cool-down and recovery laps left out. A stage is analysable when an analysed
   window's midpoint (start + 60 s on the beat clock, dropouts left out) lies
   in it; the beat clock and the lap clock are taken as equal.
3. LT1: the 0.75 crossing and quality of the crossing rules above, fitted
   only over the windows whose midpoint lies in a stage lap and whose alpha1
   lies from 0.5 to 1.0 (#3916): warm-up windows on the flat upper plateau and
   cool-down windows with a low alpha1 at a low heart rate after the hardest
   stages would move the line in a direction that varies per run. "Nicht
   erreicht" when no window's alpha1 lies below 0.75; "nicht bestimmbar" when a
   window does but the stop rules give no crossing.
4. LT2: the 0.5 crossing of the second threshold per run over the stage
   windows with alpha1 from 0.3 to 0.75, above this LT1 and without the
   late-drift rule (a step test is hard at its end by design). "Nicht erreicht"
   when no window reaches alpha1 0.5; "nicht bestimmbar" otherwise without a
   crossing.
5. Pace: the speed at the value's heart rate, linear between the two stage laps
   around it (none outside them), as s/km. A stage's heart rate is the mean of
   its last 60 s from the heart-rate stream, else the lap mean: the lap mean
   holds the rise at the start of the stage and maps a threshold to too fast a
   pace (#3916).
6. Quality: `good` when the crossing is `good` by the rules above and at least
   6 stages are analysable, else `low`. Only a `good` value is offered for
   adoption: LT1 as percent of today's run threshold HR with the origin
   `estimated` (the first threshold adoption), LT2 heart rate and pace as the
   run zone thresholds from today with the origin `dfa_alpha1` (the LT2
   adoption). The result, labelled `abgeleitet`, is shown on the completed
   test in the calendar and given to the Coach with the session details.

Limits: the individual error of the 0.5 crossing is about ±10 bpm (R76), so
LT2 especially is an estimate to review, not a lab value; LT1 holds only when
rested (R77); the studies used treadmills, and outdoor wind, slope and pace
control add error. Place (decision for #3833): a treadmill at 1 % incline
first, else a 400 m track with the watch's track run mode, a flat loop only as
a fallback, and the same place for every repeat test, so tests stay
comparable.

The LT2 adoption row of the DFA-α1 estimate shows the R76 limits of agreement
"(−24/+16)" bpm beside the heart rate (#3920, C7). The LT1 adoption and the
step test show no range: no limit of agreement is cited for them.

## Sources

- **Threshold definition and agreement with VT1:** R56 (laboratory, running,
  gas exchange), R57 and R58 (perspective and update by the same group), R59
  (systematic review of DFA-α1 in endurance exercise).
- **Anaerobic threshold (alpha1 0.5) and the step test:** R76 (treadmill, 3-min
  stages, VT2); R77 (consumer chest belt, valid only when rested).
- **DFA method:** R60. **Smoothness-priors detrending:** R61.
- **Chest-strap RR quality:** R62.
- **Target construct (LT1 and VT1):** R54, R55.

## Population and reference measurement

- R56: 17 male volunteers aged 19 to 52 without medical history, 15 analysed
  (two excluded for frequent atrial ectopy), incremental treadmill run, ECG
  RR series, gas-exchange VT1 as reference. Mean VT1 heart rate 152 bpm
  against 154 bpm at DFA-α1 0.75; no female participants.
- R58 summarises further studies in elite, recreational and cardiac
  populations; it is a narrative update by the same group, not an independent
  validation.
- R62: 25 participants, Polar H10 against ECG in an exhaustive cycling ramp:
  RR and heart rate agree closely, DFA-α1 shows a wider bias and limits of
  agreement, especially at high intensity.
- No study validates a threshold read from uncontrolled outdoor sessions of one
  athlete over weeks against a laboratory VT1.

## Protocol and data window

- Input: the `hrv` messages (number 78) of the FIT file of a run or ride whose
  heart rate source is a chest strap (`backend/providers/fit_rr_mapping.py`,
  `fit_activity_detail_mapping._first_threshold`); optical heart rate gives no
  result. At most 150 000 intervals per file. The RR series is an input only
  and is never stored, logged, exported or sent to the Coach.
- A stretch where the file clock runs more than 10 s ahead of the intervals is
  a dropout and starts a new segment.
- The result is computed once when the stream set is mapped and stored with it
  (`activity_detail_sets.dfa_alpha1`: estimate, windows, artifact share,
  quality, method version and, from method 3, the window series).
- Method 3 came without a mapper version bump (#3273, the third clause of the
  mapper version guard): rows of method 2 hold no series and count as "not
  evaluated" until the next bump of the Garmin and FIT detail mappers replays
  them. The LT1 estimate reads methods 2 and 3 alike; within a session the
  better quality wins, and among equals a record with the series.
- Athlete level: run sessions of the last 60 days up to today. Rides keep a
  stored result but do not enter the athlete-level estimate.
- The protocol of R56 is a ramp to exhaustion; sessions here are whatever the
  athlete ran. A recording gives a result only when its windows cross
  alpha1 0.75 at all (stop rules below).

## Measurement error and uncertainty

- DFA-α1 is biased by artifacts. R58 reports minimal bias below 3 % corrected
  artifacts and a negligible shift of the threshold at 6 %; the per-window
  limit of 5 % and the `good` limit of 3 % follow that range.
- The Polar H10 DFA-α1 bias against ECG is larger than that of RR and heart
  rate and grows with intensity (R62); the threshold region sits at moderate
  intensity, but the error is not quantified for this athlete or other straps.
- The estimate comes from a regression over all steady windows, not from the
  rapid-change section used in R56, and from sessions of varying structure and
  fatigue; no uncertainty interval is computed. The 5 percentage point
  spread of `good` sessions is a consistency check, not a confidence interval.
- Heat, fatigue, hydration, caffeine, hard days before and cardiac drift shift
  heart rate and autonomic activity; they are not corrected.

## Bias and transferability

- R56 included only men and one running protocol; transfer to this athlete,
  to outdoor running, to hills and to cycling is not shown.
- Intervals, recoveries and steps larger than 8 bpm per half window are
  removed, so the regression sees mostly steady and ramped efforts; a recording
  without effort above LT1 yields no result rather than a biased one.
- A person with frequent ectopic beats can give an artifact-corrected series
  with distorted DFA-α1 (excluded in R56); the artifact share only partly
  catches this.
- A weak or absent alpha1 response in some people is not excluded; the
  falling-line and crossing rules reject such recordings but cannot detect a
  wrong crossing that looks plausible.

## Deviations from the studied procedure

- Windows start every 30 s instead of every 5 s (R56).
- The line is fitted over all steady windows, not over the section from 1.0 to
  below 0.5 (R56), so the estimate is a recording-wide regression.
- Artifact correction is a fixed 300 to 2000 ms and 20 % rule, not the Kubios
  "automatic method" (R56); windows above 5 % artifacts are rejected instead
  of corrected by interpolation.
- Detrending is the smoothness-priors method of Kubios with its default
  λ = 500 (R56, R61), implemented with a banded solver.
- The athlete-level value is a median of per-session estimates expressed as
  percent of the threshold HR; the literature reports single laboratory tests.
- Field recordings instead of a laboratory ramp with a metabolic cart.

## Minimum data and stop rules

- Per recording nothing is stored with fewer than 10 analysed windows. With
  10 or more the window series is stored; the 0.75 estimate is kept only
  when: at least 10 analysed windows; the highest
  window heart rate at least 10 bpm above the lowest; the lowest alpha1 at most
  0.65 and the highest at least 0.85 (0.75 ∓ 0.1); a fitted slope below zero;
  and an estimate inside the range of the observed window heart rates and
  within 60 to 220 bpm (outside it no estimate is kept).
- Athlete level: fewer than 3 qualifying `good` sessions in 60 days give no
  estimate, nothing is shown and nothing is sent to the Coach.
- A stored estimate is replaced only through the athlete's adoption; a changed
  estimate between display and adoption is refused (409).

## Numeric rules

| Rule | Value | Tag | Rationale |
| --- | --- | --- | --- |
| Threshold alpha1 | 0.75 | source | Midpoint between 1.0 (fractal) and 0.5 (uncorrelated) used as the first threshold marker in R56; supported by R58. `dfa_alpha1.py:THRESHOLD_ALPHA1`. |
| Window length | 120 s | source | R56 used 2-minute windows. `WINDOW_S`. |
| Window stride | 30 s | product assumption | R56 recalculated every 5 s; 30 s keeps windows overlapping but cheaper. `STRIDE_S`. |
| Box sizes | 4 to 16 beats | source | R56 set the DFA-α1 window width to 4 ≤ n ≤ 16; method from R60. `BOX_SIZES`. |
| Detrending | smoothness priors, λ = 500 | source | R61 method; R56 kept the Kubios default λ = 500. `DETREND_LAMBDA`. |
| Linear trend per box | order 1 | product assumption | Usual DFA-1; the order is not stated in the accessible text of R56. `alpha1`. |
| Valid RR range | 300 to 2000 ms | product assumption | Physiological plausibility guard; not taken from a cited threshold. `MIN_RR_MS`, `MAX_RR_MS`. |
| Beat-to-beat change | more than 20 % from the last accepted beat | product assumption | Common ectopic-beat guard; value not verified in R56/R58. `MAX_BEAT_CHANGE`. |
| Re-anchor after rejected beats | 5 in a row | product assumption | One bad first beat must not reject a recording. `REANCHOR_AFTER`. |
| Maximum artifact share per window | 0.05 | product assumption | Between the 3 % and 6 % levels R58 reports as low bias. `MAX_WINDOW_ARTIFACT_SHARE`. |
| Minimum beats per window | 100 | product assumption | Enough beats for boxes up to 16 within a 2-minute window at the lowest plausible rates. `MIN_WINDOW_BEATS`. |
| Heart-rate step between window halves | at most 8 bpm | product assumption | Keeps steady and ramped windows, drops intervals and recoveries. `MAX_HALF_HR_STEP_BPM`. |
| Minimum analysed windows | 10 | product assumption | Fewer windows cannot give a stable line. `MIN_WINDOWS`. |
| Minimum heart-rate spread | 10 bpm | product assumption | A flat line cannot locate a crossing. `MIN_HR_SPREAD_BPM`. |
| Crossing margin | alpha1 reaches 0.65 and 0.85 | product assumption | A recording that never goes below and above 0.75 gives no extrapolated crossing. `CROSSING_MARGIN`. |
| Stored estimate bounds | 60 to 220 bpm, rounded to 0.1 | product assumption | `DFA_ESTIMATE_BOUNDS_BPM` of the canonical record. |
| RR input bounds | at most 150 000 intervals; dropout when the file clock is 10 s ahead | product assumption | `fit_rr_mapping.MAX_RR_INTERVALS`, `MAX_UNACCOUNTED_S`. |
| Heart rate source | chest strap only | product assumption | The validation in R62 covers a chest strap against ECG; optical heart-rate sources are not covered by the cited evidence. |
| Recording quality `good`, artifact share | at most 0.03 | source | R58: minimal DFA-α1 bias below 3 % artifacts. `GOOD_MAX_ARTIFACT_SHARE`. |
| Recording quality `good`, windows and fit | at least 20 windows, R² at least 0.5 | product assumption | `GOOD_MIN_WINDOWS`, `GOOD_MIN_R_SQUARED`. |
| Athlete-level window | 60 days | product assumption | Same window as M9 (`ESTIMATE_DAYS`); one training state. |
| Athlete-level sessions | at least 3 `good` run sessions | product assumption | `MIN_SESSIONS`; one session is not an estimate. |
| Athlete-level aggregate | median of percent of the run threshold HR on each day | product assumption | Robust to one bad session; unaffected by later threshold changes. |
| Athlete-level `good` | at least 5 sessions, spread at most 5 percentage points | product assumption | `GOOD_MIN_SESSIONS`, `GOOD_MAX_SPREAD_PCT`. |
| Stored percent bounds | 40 to 100 % LSHF | product assumption | `FIRST_THRESHOLD_PERCENT_BOUNDS` of the profile value. |
| Step test stage length | 3 min | source | R76 validated the 0.5 crossing with 3-minute stages; athlete decision P1 (#3833). `step_test.py:STAGE_MIN`. |
| Step test speed step | 0.5 km/h | product assumption | Keeps the heart-rate jump per stage near the 8 bpm window rule. `STEP_KMH`. |
| Step test start | 1.5 km/h below LT1, else 9.5 km/h, at most 3.5 km/h below LT2 | product assumption | Athlete decision P1; the LT2 bound keeps two stages beyond LT2 when LT1 is far below it (#3915). `BELOW_LT1_KMH`, `FALLBACK_START_KMH`, `BELOW_LT2_KMH`. |
| Step test stages | 2 beyond LT2, 6 to 10 | product assumption | Athlete decision P1; 6 is the analysable minimum of `good`. `STAGES_BEYOND_LT2`, `MAX_STAGES`, `MIN_GOOD_STAGES`. |
| Step test warm-up and cool-down | 10 min each, 1.5 to 0.5 km/h below stage 1 | product assumption | Athlete decision P1. `WARMUP_MIN`, `COOLDOWN_MIN`, `EASY_RANGE_KMH`. |
| Step test stage laps | 150 to 210 s | product assumption | A 3-minute step lap; a shorter last stage was not held. `STAGE_LAP_S`. |
| Step test `good` | at least 6 analysable stages | product assumption | Athlete decision (#3833), on top of the recording rules. `MIN_GOOD_STAGES`. |
| Step test plausible paces | 2:30 to 9:00 /km | product assumption | Another LT1 or LT2 pace counts as unknown. `PACE_BOUNDS_S`. |
| Step test fit windows | stage-lap midpoints; alpha1 0.5 to 1.0 (LT1), 0.3 to 0.75 (LT2) | product assumption | Keeps warm-up and cool-down out of both lines (#3916). `LT1_ALPHA1_RANGE`, `LT2_ALPHA1_RANGE`. |
| Step test stage heart rate | last 60 s of each stage | product assumption | Leaves the rise at the stage start out of the pace mapping (#3916). `STAGE_HR_TAIL_S`. |

## Allowed statements

- "Estimate from chest-strap RR intervals (DFA-α1, research stage), not a
  laboratory value: 150 bpm, 87,2 % LSHF, 6 sessions, quality good."
- "Your runs of the last 60 days give an estimate of your first threshold; you
  can adopt it as the stored value in the profile."
- "No estimate yet: fewer than three good chest-strap runs with a clear
  alpha1 crossing in the last 60 days."
- A change of the adopted value is the athlete's own action with receipt and
  undo.

## Forbidden statements

- The estimate as a measured, laboratory or validated first threshold or VT1.
- An automatic adoption or a Coach adoption; the Coach may only suggest it.
- Zones, heart-rate caps or plans changed from the estimate without the
  athlete's adoption; after adoption the caps carry the source `estimated`.
- A statement on fitness, form, readiness or injury risk from alpha1; the
  anaerobic threshold (alpha1 0.5) only as the labelled local LT2 estimate
  (#3273).
- An estimate from optical heart rate, from fewer sessions than the minimum or
  from a cycling session (LT1 and LT2 alike, F3).

## Open points

- Validation check (#3920): one laboratory or lactate step test of the
  athlete. Its LT1 is compared with this card's estimate from the same
  weeks, and the difference is recorded here; the review stays blocked
  until then.
- No comparison with a laboratory VT1, a lactate step test or a field test of
  this athlete; until then the athlete's review of this card stays open.
- R56 tested men on a treadmill ramp only; female participants, outdoor running
  and cycling remain unchecked.
- The artifact rules, window stride and regression over all windows are
  implementation choices; their effect on the estimate against the Kubios
  procedure of R56 is not measured.
- Quality limits (20 windows, R² 0.5, 5 sessions, 5 percentage points) are
  untested thresholds; real coverage from the athlete's recordings is not yet
  counted.
- Cycling results are stored but unused: no decision yet on a ride estimate.
- The step test's stage length, speed step and analysable-stage minimum are
  untested for running; no study compared stage lengths, and the lap and beat
  clocks are taken as equal.

<!-- BEGIN GENERATED CONSTANTS -->
## Code constants

| Constant | Value | Module | Code comment |
| --- | --- | --- | --- |
| `HRV_MESSAGE` | `78` | `backend/providers/fit_rr_mapping.py` |  |
| `MAX_RR_INTERVALS` | `150000` | `backend/providers/fit_rr_mapping.py` | A day of beats at 100 bpm; longer series belong to no single activity. |
| `MAX_UNACCOUNTED_S` | `10.0` | `backend/providers/fit_rr_mapping.py` | The file clock may run this far ahead of the intervals before a dropout is assumed. |
| `METHOD_VERSION` | `3` | `backend/canonical/dfa_alpha1.py` |  |
| `MIN_RR_MS` | `300.0` | `backend/canonical/dfa_alpha1.py` |  |
| `MAX_RR_MS` | `2000.0` | `backend/canonical/dfa_alpha1.py` |  |
| `MAX_BEAT_CHANGE` | `0.2` | `backend/canonical/dfa_alpha1.py` |  |
| `REANCHOR_AFTER` | `5` | `backend/canonical/dfa_alpha1.py` | After this many beats in a row rejected only for their change, the new level is taken as real: a bad first beat must not reject the whole recording. |
| `DETREND_LAMBDA` | `500.0` | `backend/canonical/dfa_alpha1.py` | Smoothness priors detrending strength (Kubios and the DFA-a1 studies use 500). |
| `WINDOW_S` | `120.0` | `backend/canonical/dfa_alpha1.py` |  |
| `STRIDE_S` | `30.0` | `backend/canonical/dfa_alpha1.py` |  |
| `BOX_SIZES` | `tuple(range(4, 17))` | `backend/canonical/dfa_alpha1.py` |  |
| `MAX_WINDOW_ARTIFACT_SHARE` | `0.05` | `backend/canonical/dfa_alpha1.py` |  |
| `MIN_WINDOW_BEATS` | `100` | `backend/canonical/dfa_alpha1.py` |  |
| `MAX_HALF_HR_STEP_BPM` | `8.0` | `backend/canonical/dfa_alpha1.py` |  |
| `THRESHOLD_ALPHA1` | `0.75` | `backend/canonical/dfa_alpha1.py` |  |
| `CROSSING_MARGIN` | `0.1` | `backend/canonical/dfa_alpha1.py` | The lowest alpha1 must reach this far below the threshold and the highest this far above it, so a recording that never crosses 0.75 gives no estimate. |
| `MIN_WINDOWS` | `10` | `backend/canonical/dfa_alpha1.py` |  |
| `MIN_HR_SPREAD_BPM` | `10.0` | `backend/canonical/dfa_alpha1.py` |  |
| `GOOD_MIN_WINDOWS` | `20` | `backend/canonical/dfa_alpha1.py` |  |
| `GOOD_MAX_ARTIFACT_SHARE` | `0.03` | `backend/canonical/dfa_alpha1.py` |  |
| `GOOD_MIN_R_SQUARED` | `0.5` | `backend/canonical/dfa_alpha1.py` |  |
| `ESTIMATE_DAYS` | `60` | `backend/performance/first_threshold_estimate.py` |  |
| `MIN_SESSIONS` | `3` | `backend/performance/first_threshold_estimate.py` |  |
| `GOOD_MIN_SESSIONS` | `5` | `backend/performance/first_threshold_estimate.py` | The estimate is good with this many sessions that agree within this spread (percentage points of the threshold HR); otherwise it is low. |
| `GOOD_MAX_SPREAD_PCT` | `5.0` | `backend/performance/first_threshold_estimate.py` |  |
| `SOURCE_LABEL` | `'Schätzung aus Brustgurt-RR-Intervallen (DFA-α1, Forschungsstand), kein Laborwert'` | `backend/performance/first_threshold_estimate.py` |  |
| `METHOD` | `'run-step-test-v3'` | `backend/performance/step_test.py` | v1 fitted the crossings over the whole run (#3833), v2 over the stage windows (#3916), v3 starts stage 1 at most 3.5 km/h below LT2 (#3915). |
| `PROTOCOL` | `'run_step_test'` | `backend/performance/step_test.py` |  |
| `NAME` | `'Stufentest Laufen (LT1 und LT2)'` | `backend/performance/step_test.py` |  |
| `WARMUP_MIN` | `10` | `backend/performance/step_test.py` |  |
| `COOLDOWN_MIN` | `10` | `backend/performance/step_test.py` |  |
| `STAGE_MIN` | `3` | `backend/performance/step_test.py` |  |
| `STEP_KMH` | `0.5` | `backend/performance/step_test.py` |  |
| `BELOW_LT1_KMH` | `1.5` | `backend/performance/step_test.py` |  |
| `BELOW_LT2_KMH` | `3.5` | `backend/performance/step_test.py` | Stage 1 lies at most this far below LT2, so two stages lie beyond it (#3915). |
| `FALLBACK_START_KMH` | `9.5` | `backend/performance/step_test.py` |  |
| `STAGES_BEYOND_LT2` | `2` | `backend/performance/step_test.py` |  |
| `MAX_STAGES` | `10` | `backend/performance/step_test.py` |  |
| `MIN_GOOD_STAGES` | `6` | `backend/performance/step_test.py` |  |
| `EASY_RANGE_KMH` | `(1.5, 0.5)` | `backend/performance/step_test.py` | The warm-up and cool-down range lies this far below stage 1 (km/h). |
| `STAGE_PACE_BAND` | `0.03` | `backend/performance/step_test.py` | Each stage's pace band, ±3 % around its pace (athlete decision, #3833). |
| `PACE_BOUNDS_S` | `(150.0, 540.0)` | `backend/performance/step_test.py` | Plausible run paces (s/km); another value counts as unknown. |
| `STAGE_LAP_S` | `(150.0, 210.0)` | `backend/performance/step_test.py` | A recorded lap of this length (s) is one stage; a shorter last one was not held. |
| `LT1_ALPHA1_RANGE` | `(0.5, 1.0)` | `backend/performance/step_test.py` | The alpha1 range of the stage windows that place LT1 (#3916). |
| `STAGE_HR_TAIL_S` | `60.0` | `backend/performance/step_test.py` | A stage's heart rate is the mean over its last part (s), after the rise. |
| `END_NOTE` | `'Ende: wenn die Stufe nicht mehr zu halten ist, spätestens nach Stufe {last}.'` | `backend/performance/step_test.py` |  |
| `SOURCE` | `'abgeleitet'` | `backend/performance/step_test.py` |  |
| `LT1_NOT_REACHED` | `'α1 blieb über 0,75. Die Stufen waren nicht schnell genug.'` | `backend/performance/step_test.py` |  |
| `LT2_NOT_REACHED` | `'α1 blieb über 0,5. Die letzten Stufen waren nicht schnell genug oder wurden abgebrochen.'` | `backend/performance/step_test.py` |  |
| `NO_CROSSING` | `'keine klare Kreuzung'` | `backend/performance/step_test.py` |  |
<!-- END GENERATED CONSTANTS -->
