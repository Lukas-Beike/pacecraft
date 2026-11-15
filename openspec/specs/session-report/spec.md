# session-report Specification

## Purpose
Code computes every deterministic check of one completed session (value, rule, threshold, basis, verdict and severity), so the Coach's session analysis explains verdicts instead of computing them (#3084).

## Requirements

### Requirement: Deterministic session report
The system SHALL add to every `get_activity_details` result a `session_report` (method `session-report-v1`), computed on read from the session's existing derivations without a stored payload or model call. Thresholds are the athlete's decisions on #3084 (2026-11-15).

#### Scenario: Check shape and ranking
- **WHEN** the report is built
- **THEN** each check carries:
  - its value and the rule;
  - the threshold as a named constant;
  - the basis: `personal` against the athlete's own comparable values (at least 5), `plan` against the paired planned unit, else `fixed`;
  - the verdict: `ok`, `notable`, or `unknown` with a reason;
  - a deterministic severity.

  The report lists every notable check by severity in `notable`, the first four in `lead`, and every unknown check in `unknown`.

#### Scenario: Plan against actual
- **WHEN** the session has a paired planned unit
- **THEN** the report judges for runs and rides the duration (elapsed for interval sessions, whose planned rests the estimate counts, else moving) and the local TSS-scale load (M6, the scale of the plan estimate; never a provider load) against the unit's estimate, notable beyond ±20 %. For detected intervals it judges the planned work steps against the executed work laps (notable on missing or extra steps, with the mapping confidence); without a plan-to-lap mapping that check is unknown with the mapping's reason.

#### Scenario: Easy, recovery and long sessions
- **WHEN** the session focus is recovery, easy, endurance or long
- **THEN** the report judges:
  - aerobic decoupling, notable above 5 %;
  - the share of heart-rate zone time above zone 2, notable above 10 %; the paired plan's own seconds above zone 2 (strides, a fast finish) are taken off, unplanned surges count;
  - a stored heart-rate cap by the verdict the planned targets computed (mean above the cap or more than 10 % of the time above it);
  - for rides the variability index, notable above 1.05.

#### Scenario: Intervals, threshold sessions and tests
- **WHEN** the execution found intervals, or the focus is tempo, threshold, vo2max, anaerobic, sweet_spot or test and the execution is not continuous
- **THEN** the report judges:
  - fade across reps: the weakest rep against the median of the largest group of reps within 35 % of one duration, as speed or power, with lap pace for reps under 2 min; notable at 3 %; unknown for a test session or reps with different planned targets;
  - fade within reps: second against first half, notable at 3 %;
  - a too-fast start, notable at 3 %;
  - cardiac drift between the first and last rep at equal output, notable above 5 %;
  - time in target per rep, notable for a rep in the poor adherence band (below 50 %);
  - the recovery length against the plan, notable beyond ±20 %;
  - the mean HR drop 60 s into the recoveries against the median of the athlete's comparable sessions, notable 5 bpm below it, and unknown with fewer than 5 of them.

  Time in target and recovery length are unknown when the planned work steps and the executed work laps differ in number, because steps and laps pair in order. Cardiac drift compares only reps of one length. Without detected reps the fade check is unknown with the execution's reason.

#### Scenario: Continuous sessions
- **WHEN** the session has no intervals and is neither easy nor a race
- **THEN** the report judges a ride's variability index, and for a tempo or threshold focus the second against the first half of the main part, notable at 3 % slower.

#### Scenario: Race
- **WHEN** the session focus or purpose is race
- **THEN** the report judges the second half of the main part against the first, notable at 2 % slower. The halves leave out a warm-up lap or else the first 10 min of a session of 30 min or more, so the rule names that a fast start is not part of it.

#### Scenario: Data quality gates verdicts
- **WHEN** the heart-rate data shows spikes or a cadence lock or was not checked, or the speed of a speed-based output (a run, a ride without power) jumps
- **THEN** every check that reads those data is unknown with the data-quality reason instead of notable. Other unknown reasons pass on the owner's reason (comparable sessions, halves).

Evidence:
- `backend/performance/session_report.py:session_report`
- `backend/coach/activity_read_tools.py:CoachActivityReadToolService._with_session_context`
- `tests/test_session_report.py`
