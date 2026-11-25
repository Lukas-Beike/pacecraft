# weekly-review Specification

## Purpose
Review one ISO week against the week before under that week in the calendar: plan, load, recovery, notable sessions and check-ins, computed locally from stored data without a model request (#2805, #3298).

## Requirements

### Requirement: Locally computed week review
The system SHALL compute the review of the current, the previous or a past ISO week (Monday to Sunday) of the 51 weeks before it whose check-ins are stored from existing local data only: the weekly progress, the plan compliance, the session summaries, the personal recovery baselines and the check-ins. It SHALL set each figure against the week before and SHALL make no model request.

#### Scenario: Review of the current week
- **WHEN** `GET /api/analysis/week-review?week=current` is read on a Thursday
- **THEN** the review covers Monday to Sunday of this week, `effective_end` is today, and every figure of the week before is given alongside for the change.

#### Scenario: Previous, past and unknown weeks
- **WHEN** `week=previous`, `week` as the ISO date of a Monday from this week back to 51 weeks before it, or no `week` is read
- **THEN** that week or, without the query, the current week is reviewed, its recovery set against the range as of the week's last day so far. Any other value (another day, a future week, an older week, another form) is refused with 400 "Ungültige Woche." and nothing is read.

#### Scenario: No model request
- **WHEN** the review is read
- **THEN** no Coach turn starts and no request reaches a model provider; the result carries `method` and no text written by a model.

### Requirement: Week review content
The review SHALL report the plan as completed, missed and still open units with the share of planned time done, the load as time, sessions and training load per week with the low, moderate and high intensity shares, the recovery as weekly means of sleep, HRV and resting heart rate against the range of the athlete's earlier values, the notable sessions and the check-ins.

#### Scenario: Plan and load
- **WHEN** a week had 4 planned units, 3 completed and 1 missed, and 5 sessions
- **THEN** `plan` reports `completed` 3, `missed` 1, `open` 0, the missed unit with its day and name and `duration_share` from planned and completed time. `load` reports `duration_s` (the time a session is shown with: moving time, else elapsed time, strength elapsed first; the calendar week header sums the same), `sessions` and `training_load` (whole points, as the header rounds it) for this and the previous week and the intensity `shares`.

#### Scenario: Recovery against the personal range
- **WHEN** the week has nights with sleep, HRV and resting heart rate of the preferred source
- **THEN** each metric reports the week's mean, the number of nights, the previous week's mean and `position` (`below`, `within` or `above`) against the range of the athlete's earlier values as of the week's last day so far when that range is known. A metric without nights is left out.

#### Scenario: Notable sessions and check-ins
- **WHEN** the week has sessions and check-ins
- **THEN** `notable` lists the session with the highest training load, every race and every missed planned unit, each with day and name. `checkins` reports the days entered of 7 (of the days passed in the current week), the days with pain or illness and the mean motivation, stress and soreness (0 to 10); the free-text day form is not averaged.

Evidence:
- `backend/http_api/analysis.py`
- `backend/performance/report_service.py:TrainingReportReadService.week_review`
- `backend/performance/week_review.py`
- `public/plan-views.js::fillWeekReview`
- `tests/test_week_review.py`

### Requirement: Week review card in the calendar
The calendar week view SHALL show a card "Wochenrückblick" below the seven days of the selected week when that week is the running one or one of the 51 before it, no sport filter is set, as approved by the athlete on 2026-11-16 (layout B, all tiles, always open). Its header reads "Wochenrückblick · KW nn", followed by two columns of tiles. Analyse SHALL have no "Woche" segment; an `analysis/week` link SHALL open the calendar.

#### Scenario: A past week
- **WHEN** the athlete steps to a past week
- **THEN** its card shows every tile with data in this order: Plan (completed of planned, missed units as a note), Einheiten (with the sessions outside the plan), Zeit and Belastung (each with the change against the week before), Schlaf Ø (with its change and source), Ruheherzfrequenz Ø and HRV Ø (with their position and source), Check-ins (entered of days, with the score means), "Besondere Einheiten" across the full width and Beschwerden (with structured complaints across the full width, one line per body area and side with its days and severity, a worsening from the first to the latest severity in red, then free-text pain days beyond them and illness days; otherwise the pain and illness day counts); the plan line under the week is not shown.

#### Scenario: A running week
- **WHEN** the selected week is the current one
- **THEN** the header reads "Wochenrückblick · KW nn · bis heute", the Plan tile names the units still open, and Zeit and Belastung show no change against the full week before.

#### Scenario: A future week, an old week or a week without data
- **WHEN** the selected week lies in the future or more than 51 weeks back, or its review has no data or cannot be read
- **THEN** no card is shown, and a week with planned units shows its plan line; a tile without data is never shown with a placeholder.

#### Scenario: Stepping weeks
- **WHEN** the athlete steps to another week while a review is still loading
- **THEN** the card shows the review of the newly selected week, and a late answer for the earlier week is dropped.

Evidence:
- `e2e/calendar-week-compact.spec.js`
- `public/plan-views.js::plannedWeekReview`
- `tests/plan-views-times.test.cjs`
