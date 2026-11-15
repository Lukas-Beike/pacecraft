# weekly-review Specification

## Purpose
Review one ISO week against the week before in Analyse → Woche: plan, load, recovery, notable sessions and check-ins, computed locally from stored data without a model request (#2805).

## Requirements

### Requirement: Locally computed week review
The system SHALL compute the review of the current or the previous ISO week (Monday to Sunday) from existing local data only: the weekly progress, the plan compliance, the session summaries, the personal recovery baselines and the check-ins. It SHALL set each figure against the week before and SHALL make no model request.

#### Scenario: Review of the current week
- **WHEN** `GET /api/analysis/week-review?week=current` is read on a Thursday
- **THEN** the review covers Monday to Sunday of this week, `effective_end` is today, and every figure of the week before is given alongside for the change.

#### Scenario: Previous week and an unknown week
- **WHEN** `week=previous` is read, or `week` is missing
- **THEN** the previous or, without the query, the current week is reviewed. Any other value is refused with 400 "Ungültige Woche." and nothing is read.

#### Scenario: No model request
- **WHEN** the review is read
- **THEN** no Coach turn starts and no request reaches a model provider; the result carries `method` and no text written by a model.

### Requirement: Week review content
The review SHALL report the plan as completed, missed and still open units with the share of planned time done, the load as time, sessions and training load per week with the low, moderate and high intensity shares, the recovery as weekly means of sleep, HRV and resting heart rate against the range of the athlete's earlier values, the notable sessions and the check-ins.

#### Scenario: Plan and load
- **WHEN** a week had 4 planned units, 3 completed and 1 missed, and 5 sessions
- **THEN** `plan` reports `completed` 3, `missed` 1, `open` 0, the missed unit with its day and name and `duration_share` from planned and completed time. `load` reports `duration_s`, `sessions` and `training_load` for this and the previous week and the intensity `shares`.

#### Scenario: Recovery against the personal range
- **WHEN** the week has nights with sleep, HRV and resting heart rate of the preferred source
- **THEN** each metric reports the week's mean, the number of nights, the previous week's mean and `position` (`below`, `within` or `above`) against the current range of the athlete's earlier values when that range is known. A metric without nights is left out.

#### Scenario: Notable sessions and check-ins
- **WHEN** the week has sessions and check-ins
- **THEN** `notable` lists the session with the highest training load, every race and every missed planned unit, each with day and name. `checkins` reports the days entered of 7 (of the days passed in the current week), the days with pain or illness and the mean motivation, stress and soreness (0 to 10); the free-text day form is not averaged.

Evidence:
- `backend/http_api/analysis.py`
- `backend/performance/report_service.py:TrainingReportReadService.week_review`
- `backend/performance/week_review.py`
- `public/analysis.js::renderWeekReview`
- `tests/test_week_review.py`

### Requirement: Analyse segment "Woche"
The Analyse view SHALL offer the segment "Woche" with a switch "Diese / Letzte" and one card each for Plan, Belastung, Erholung, Besondere Einheiten and Check-ins, as chosen by the athlete on 2026-11-14 (layout A).

#### Scenario: Switching weeks
- **WHEN** the athlete opens Analyse → Woche and taps "Letzte"
- **THEN** the previous week's review is read and shown, and "Diese" returns to the current week.

#### Scenario: Empty week
- **WHEN** a week has no plan, sessions, recovery data or check-ins
- **THEN** each card without data is left out, and the segment shows its empty state below the week's header, whose switch leads back to the other week.

Evidence:
- `e2e/analysis-on-demand.spec.js`
- `public/analysis.js::renderWeekReview`
