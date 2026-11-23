# nutrition-diary-and-targets Specification

## Purpose

Describe the athlete's local nutrition diary, nutrient completeness, expenditure comparisons, and body-history presentation. Re-validated against application revision `d1beefd77`. Evidence below is source/test inspection unless execution is recorded separately. The inspected nutrition flow has no saved calorie/macro target or adaptive-target calculation; its running-day expenditure projection is not a target. Food definitions belong to `food-catalog-and-meal-library`, external writes to `nutrition-provider-sync`, and loading to `view-refresh-and-client-state`.

## Requirements

### Requirement: Protected diary reads and explicit local mutations
The system SHALL expose day and history reads only to an authenticated athlete and process nutrition mutations through the authenticated, CSRF-protected API boundary.

#### Scenario: Read a selected day
- **WHEN** an authenticated athlete requests a valid diary date
- **THEN** the response contains that date's local entries, counts, totals, and expenditure metadata; an omitted date uses the athlete-local day.

#### Scenario: Reject unauthenticated reads
- **WHEN** an unauthenticated caller requests nutrition history
- **THEN** authentication fails before accessing the nutrition service.

#### Scenario: Reject an unauthorized mutation
- **WHEN** a nutrition POST or PUT lacks the required session or CSRF proof
- **THEN** the shared HTTP boundary rejects it before dispatching a durable nutrition mutation.

Evidence: `backend/http_api/nutrition.py::NutritionGetRoutes.routes`, `backend/http_api/handler.py::RequestHandler._post`; `tests/test_server_http.py::ServerHttpTests.test_every_table_route_checks_access_before_its_handler_runs`. Mutation protection also uses the shared HTTP authentication contract; these route-unit tests do not exercise a real browser session.

### Requirement: Validated local entries with trustworthy provenance
The system SHALL validate diary entry data before persistence, retain an entry identity and source provenance, and derive nutrition provenance from the accepted input path rather than trusting a supplied provenance object.

#### Scenario: Record a manual entry
- **WHEN** the athlete submits a description, valid date, finite calories from 0 through 10000, and optional finite macros from 0 through 1000 grams
- **THEN** the entry is persisted locally with normalized calories/macros, source, and an identifier and can be read again.

#### Scenario: Reject invalid entry data
- **WHEN** required calories or description are missing, a date or meal type is invalid, or a nutrient is negative, nonfinite, or out of range
- **THEN** validation fails without saving the invalid entry.

#### Scenario: Distinguish estimates and authoritative food calculations
- **WHEN** an entry uses Coach/photo/voice estimates, packaging values, or database/product components
- **THEN** its basis identifies the accepted estimate, label, or frozen server calculation; a client-supplied basis does not establish database provenance.

Evidence: `backend/nutrition/models.py::normalize_nutrition_entry`, `backend/nutrition/meal_library.py::NutritionMealLibraryService.prepare_values`, `backend/nutrition/diary.py::NutritionDiaryService.log_meal`; `tests/test_nutrition.py::NutritionRepositoryAndServiceTests.test_log_and_get_meal`, `tests/test_nutrition_composition.py::NutritionCompositionTests.test_composite_rejects_forged_authoritative_values_and_unknown_macros_propagate`. See `food-catalog-and-meal-library` for component validation.

### Requirement: Athlete-local dates and explicit time knowledge
The system SHALL preserve the selected consumption date and distinguish a known meal time from an unknown time. Unknown time SHALL require an explicit meal type and use a neutral ordering anchor without presenting it as an observed time.

#### Scenario: Default at local midnight
- **WHEN** an entry omits the date after midnight in the athlete's configured timezone
- **THEN** the entry uses the athlete-local date, even if UTC is still the previous day.

#### Scenario: Log without a known time
- **WHEN** an entry declares `logged_time_known=false` and an explicit breakfast, lunch, dinner, or snack type
- **THEN** that type is retained, the stored ordering anchor is noon on the meal date, and the UI omits a clock-time claim.

#### Scenario: Correct a previously unknown time
- **WHEN** a correction explicitly supplies a meal time without overriding the time-knowledge flag
- **THEN** the entry becomes known-time and the supplied time is combined with its meal date.

Evidence: `backend/nutrition/models.py::_normalize_logged_time`, `backend/nutrition/diary.py::_names_time_without_flag`, `public/nutrition.js::nutritionCard`; `tests/test_nutrition.py::NutritionRepositoryAndServiceTests.test_meal_logged_after_athlete_local_midnight_uses_athlete_local_day`, `tests/test_nutrition_meal_times.py::NutritionMealTimeTests.test_unknown_time_keeps_explicit_meal_type_and_noon_anchor`, `tests/test_nutrition_meal_times.py::NutritionMealTimeTests.test_naming_a_time_makes_an_unknown_time_known`.

### Requirement: Targeted corrections and deletion
The system SHALL update or delete the identified local meal, and preserve omitted correction fields. Nothing is marked for an export, as nutrition is never sent to a provider. Invalid or missing targets SHALL produce an error.

#### Scenario: Move and correct a meal
- **WHEN** the athlete corrects calories and the meal date of an existing entry
- **THEN** its identity, source, and other omitted fields remain, and the meal sits on its new date.

#### Scenario: Add a product to a logged meal
- **WHEN** the athlete adds a saved product to a logged meal
- **THEN** the change gets a change-history record, so it can be undone.

#### Scenario: Invalid correction
- **WHEN** a correction is empty, contains unsupported fields or invalid nutrient values, or names a nonexistent entry
- **THEN** the service rejects it and retains the valid local state.

#### Scenario: No direct entry update route
- **WHEN** a client sends `PUT /api/nutrition/entry`
- **THEN** no nutrition route handles it and no entry changes; an entry is corrected only through the Coach correction with its field whitelist, `manual_correction` basis and undo reference, or through the component edit.

Evidence: `backend/http_api/nutrition.py::NutritionPutRoutes.routes`, `backend/nutrition/diary.py::NutritionDiaryService.correct_meal`, `backend/nutrition/diary.py::NutritionDiaryService.delete_meal`, `backend/nutrition/diary.py::NutritionDiaryService.add_product_to_meal`; `tests/test_nutrition_api_and_sync.py::NutritionHttpApiTests.test_put_entry_route_is_removed`, `tests/test_nutrition_history.py::NutritionHistoryTests.test_undo_of_an_added_product_restores_the_previous_meal`, `tests/test_nutrition.py::NutritionRepositoryAndServiceTests.test_correct_meal_preserves_omitted_fields_and_moves_the_day`, `tests/test_nutrition.py::NutritionRepositoryAndServiceTests.test_correct_meal_rejects_empty_invalid_or_missing_target`. Shared history/undo authorization is owned by the history and Coach contracts.

### Requirement: Saved meals and composed meals are managed in the UI
The system SHALL let the athlete in Ernährung › Produkte list the saved meals, log one for a number of portions on a day, rename it, delete it, compose a meal of several foods with amounts and keep or log it, and save a diary meal as a saved meal, each through the use cases of the Coach tools, with the server calculating every value and a receipt with undo.

#### Scenario: Saved meal routes
- **WHEN** the athlete lists, creates, renames, deletes or logs a saved meal, or saves a diary meal as one
- **THEN** `GET /api/nutrition/templates`, `POST /api/nutrition/templates`, `POST /api/nutrition/templates/delete` (`confirmed: true`), `POST /api/nutrition/templates/log` and `POST /api/nutrition/templates/from-entry` answer; the client sends names, amounts, ids, day, time and type, never nutrient values, a saved meal change never changes logged entries, and the response carries the undo reference

#### Scenario: Composed meal
- **WHEN** the athlete composes a meal from foods and amounts
- **THEN** it is logged with `POST /api/nutrition/entry` (`components`) or kept with `POST /api/nutrition/templates` (`components`), calculated by the server

Evidence: `backend/http_api/nutrition.py::NutritionPostRoutes._save_template`, `backend/nutrition/meal_library.py::NutritionMealLibraryService.save_template_from_meal`, `public/nutrition-templates.js`; `tests/test_nutrition_composition.py::NutritionCompositionTests::test_saved_meal_routes_keep_log_and_delete_with_the_server_values`.

### Requirement: A logged meal's components are edited directly
The system SHALL let the athlete remove a component of a logged meal, change its amount, or swap it for another database food or saved product, addressed by position. The server SHALL scale the stored snapshot or calculate from the food or product ID and the amount; a client SHALL send no nutrient values. The meal keeps its name and source, becomes a composite, gets a change-history record and has its date marked pending; templates stay unchanged.

#### Scenario: Change an amount
- **WHEN** the athlete sets a component to a new amount
- **THEN** its nutrients scale with the amount from its stored origin, the other components stay as stored, and the meal totals follow.

#### Scenario: Swap a component
- **WHEN** the athlete swaps a component for another food or product
- **THEN** its amount and unit stay and its nutrients and origin come from the new food; a food counted in another unit fails with `food_unit_mismatch` and nothing changes.

#### Scenario: The meal changed meanwhile
- **WHEN** an edit carries a `revision` that no longer matches the meal's components
- **THEN** it fails with 409 `meal_changed` and nothing changes.

#### Scenario: Remove the last component
- **WHEN** an edit would leave the meal without a component
- **THEN** it fails with `last_component`; the client deletes the meal after its own confirmation instead.

#### Scenario: Change the meal's day, type or time
- **WHEN** the same request carries `meal` with `meal_date`, `meal_type`, `meal_time` or `logged_time_known: false`, with or without component edits
- **THEN** both are saved as one change with one undo reference; a new type moves a default time to the type's default but never a named one, a new day moves the meal, an unknown time keeps only the type, and a `meal` without edits leaves the components and origin as stored; any other key, `logged_time_known: true`, or a time together with an unknown time fails with 400 and nothing changes.

Evidence: `backend/nutrition/meal_components.py::validated_meal_changes`, `tests/test_nutrition_meal_components.py::MealComponentEditTests.test_the_meal_change_and_the_component_edits_are_one_write`, `tests/test_nutrition_meal_components.py::MealComponentEditTests.test_a_new_day_moves_the_meal`, `tests/test_nutrition_meal_components.py::MealComponentEditTests.test_a_bad_meal_change_changes_nothing`, `backend/nutrition/meal_components.py::edited_components`, `backend/nutrition/meal_components.py::entry_components`, `backend/nutrition/diary.py::NutritionDiaryService.edit_components`, `backend/http_api/nutrition.py::NutritionPostRoutes._edit_components`; `tests/test_nutrition_meal_components.py::MealComponentEditTests.test_a_changed_amount_is_recalculated_and_the_others_stay_as_stored`, `tests/test_nutrition_meal_components.py::MealComponentEditTests.test_a_swap_keeps_the_amount_and_takes_the_new_origin`, `tests/test_nutrition_meal_components.py::MealComponentEditTests.test_a_removed_component_leaves_the_rest_and_the_last_one_stays`, `tests/test_nutrition_meal_components.py::MealComponentEditTests.test_a_swap_to_another_unit_is_refused_and_changes_nothing`, `tests/test_nutrition_meal_components.py::MealComponentEditTests.test_an_edit_of_a_meal_that_changed_meanwhile_is_refused`; `e2e/nutrition-meal-edit.spec.js`.

### Requirement: Diary changes are recorded and undoable
The system SHALL record every diary create, correction, added product and deletion as a `nutrition_entry` change in change history, with the audit-projected fields of the entry, and SHALL let the shared undo restore the recorded state only while the entry still matches the state the change produced. A Coach turn whose applied writes are all diary tools SHALL be undoable as a whole.

#### Scenario: Undo a logged meal
- **WHEN** the athlete undoes the change that logged a meal
- **THEN** the entry is removed.

#### Scenario: Undo an added product
- **WHEN** the athlete undoes the change that added a saved product to a logged meal
- **THEN** the meal returns to its previous values.

#### Scenario: Undo a correction or deletion
- **WHEN** the athlete undoes a correction or a deletion
- **THEN** the entry returns with its earlier values, source and nutrient basis, including its composite ingredient snapshots, and local sync state.

#### Scenario: Entry edited after the change
- **WHEN** an entry was edited again after the change being undone
- **THEN** the undo is refused with a conflict and the later edit stays.

#### Scenario: Coach turn receipt
- **WHEN** a Coach turn saved, updated or deleted diary entries or logged a saved meal
- **THEN** its change summary is marked undoable.

Evidence:
- `backend/coach/change_receipts.py::_FULLY_TRACKED_TOOLS`
- `backend/history/undo_service.py::HistoryUndoService`
- `backend/nutrition/diary.py::NutritionDiaryService._create`
- `backend/nutrition/diary.py::NutritionDiaryService._update`
- `backend/nutrition/diary.py::NutritionDiaryService.add_product_to_meal`
- `backend/nutrition/diary.py::NutritionDiaryService.delete_meal`
- `backend/nutrition/diary.py::NutritionDiaryService.restore_in_transaction`
- `tests/test_nutrition_history.py::NutritionHistoryTests.test_a_coach_turn_that_logs_meals_is_undoable`
- `tests/test_nutrition_history.py::NutritionHistoryTests.test_undo_is_refused_after_a_later_edit`
- `tests/test_nutrition_history.py::NutritionHistoryTests.test_undo_of_a_correction_restores_the_previous_values`
- `tests/test_nutrition_history.py::NutritionHistoryTests.test_undo_of_a_delete_brings_the_entry_back_and_a_later_log_undoes`
- `tests/test_nutrition_history.py::NutritionHistoryTests.test_undo_of_a_logged_meal_removes_it`
- `tests/test_nutrition_history.py::NutritionHistoryTests.test_undo_of_an_added_product_restores_the_previous_meal`

### Requirement: Daily macro completeness is distinct from known subtotals
The system SHALL sum calories and counts for recorded meals while preserving unknown macronutrients as unknown. It SHALL expose known macro subtotals and the number of entries with incomplete macros rather than treating omitted values as zero.

#### Scenario: Complete day
- **WHEN** every recorded entry supplies a macro
- **THEN** that macro's daily total is the sum of the recorded values.

#### Scenario: Partially known day
- **WHEN** one of two meals lacks macros
- **THEN** affected complete totals are null, the known meal contributes to `known_macro_totals`, and the incomplete-entry count increases.

#### Scenario: Range summary
- **WHEN** the athlete requests an inclusive valid range
- **THEN** returned days with entries preserve the same completeness rules; a reversed range, or one spanning more than 366 days, is rejected with 400 rather than silently swapped or cut.

Evidence: `backend/db/repositories.py::nutrition_macro_totals`, `backend/nutrition/diary.py::NutritionDiaryService.get_range_summary`; `tests/test_nutrition_meal_times.py::NutritionMealTimeTests.test_partial_macros_keep_daily_totals_unknown_but_expose_known_sums`, `tests/test_nutrition_meal_times.py::NutritionMealTimeTests.test_range_summary_matches_partial_day_totals`, `tests/test_nutrition.py::NutritionRepositoryAndServiceTests.test_day_summary`, `tests/test_nutrition.py::NutritionRepositoryAndServiceTests.test_range_summary_and_unsynced_dates`.

### Requirement: Historical sugar and salt derive from consumption snapshots
The system SHALL compute displayed sugar and salt from the quantity and nutrient data frozen with consumption, including scaled composite portions. Unknown contributions SHALL retain an incomplete total and a separate known subtotal.

#### Scenario: Product changes after consumption
- **WHEN** a product's current nutrient definition changes after a meal was recorded
- **THEN** historical sugar and salt continue to use the consumed product snapshot.

#### Scenario: Composite or database recipe
- **WHEN** a consumed recipe contains multiple frozen ingredients or scaled portions
- **THEN** sugar and salt are calculated from each consumed quantity, including nested provenance snapshots.

#### Scenario: Missing nutrient
- **WHEN** at least one component has no sugar or salt value
- **THEN** that complete total remains null, available values appear only as a known subtotal, and the UI marks incompleteness.

Evidence: `backend/nutrition/intake_totals.py::_snapshot_values`, `backend/nutrition/intake_totals.py::add_intake_totals`, `public/nutrition.js::nutritionTotalTile`; `tests/test_nutrition_composition.py::NutritionCompositionTests.test_sugar_and_salt_follow_consumption_snapshots_and_unknowns`, `tests/test_nutrition_composition.py::NutritionCompositionTests.test_composite_salt_sugar_scale_with_template_portions_and_week_coverage`.

### Requirement: Closed-day history preserves independent coverage
The system SHALL return 7, 14, or 84 closed days ending yesterday, keep absent intake and expenditure unknown, and calculate balances only for dates with both values. Each day's expenditure SHALL be its own "Aus deinen Daten" day total from the energy table, named by `expenditure_source` "own", with the wearable's day kept beside it (#3600). Each average SHALL report its own observed-day count.

#### Scenario: Day without an own day total
- **WHEN** the athlete recorded no meal on a historical day, or its everyday or sport part is unknown, while the wearable counted the day
- **THEN** the day's expenditure and balance remain null, the wearable's day stays readable, and the day counts toward neither expenditure nor balance coverage.

#### Scenario: Independently covered metrics
- **WHEN** intake, macros, and expenditure cover different days
- **THEN** each average uses only its known observations and the balance total counts only paired days.

#### Scenario: Unsupported period or empty history
- **WHEN** the requested period is not the integer 7, 14, or 84, or no measurements exist
- **THEN** invalid periods fail; an empty valid history contains dated gaps, null averages, and zero coverage instead of invented zero intake.

Evidence: `backend/nutrition/sync_status.py::NutritionSyncStatusService.get_week_summary`, `backend/nutrition/sync_status.py::_week_day`, `backend/nutrition/energy_view.py::own_day_totals`, `public/nutrition.js::nutritionChartDays`; `tests/test_nutrition_garmin_expenditure.py::NutritionGarminExpenditureTests.test_the_period_balance_takes_the_own_day_totals`, `tests/test_energy_view.py::SelectedDayTests.test_the_period_balance_takes_each_day_s_own_row_total`, `tests/test_nutrition_garmin_expenditure.py::NutritionGarminExpenditureTests.test_week_uses_closed_days_and_only_paired_values_for_balance`, `tests/test_nutrition_garmin_expenditure.py::NutritionGarminExpenditureTests.test_history_rejects_unsupported_periods`, `tests/nutrition-history.test.cjs::weekly chart averages observed days independently and preserves empty weeks`.

### Requirement: Measured expenditure retains date and freshness provenance
The system SHALL keep measured total, active, and resting energy distinct and return source, measurement date, sync time, and freshness for the requested day. It SHALL report a measured total only when supplied, without manufacturing it from components.

#### Scenario: Canonical measurements exist
- **WHEN** daily total, active or resting energy is available through the observation reader
- **THEN** those canonical measurements supply the day values and the running day's resting reference, while refresh status and the complete read windows remain tied to the Garmin snapshot.

#### Scenario: No canonical energy
- **WHEN** the stored Garmin snapshot has daily energy for a day but no canonical observation exists for it
- **THEN** the day's energy is unavailable; the snapshot's values are never read.

#### Scenario: Only components or malformed data exist
- **WHEN** a day has active/resting energy without a measured total, or provider data is malformed or absent
- **THEN** the measured total remains unavailable and the athlete's local diary is still readable.

#### Scenario: Targeted read that leaves a day out
- **WHEN** a targeted daily-energy refresh returns no record for a day of its range
- **THEN** the range counts as incomplete, so that day is neither marked read nor its stored summary pruned (#3157); other days retain their own freshness.

#### Scenario: A refresh mark never hides a stored total
- **WHEN** a day is marked read by a targeted refresh and a Garmin daily total is stored for it, whenever fetched
- **THEN** that total is shown; only a marked day without any stored Garmin total drops other sources' values (#3157).

Evidence: `backend/performance/daily_health.py::daily_energy_expenditure`, `backend/nutrition/sync_status.py::_filter_targeted_missing_energy`; `tests/test_nutrition_garmin_expenditure.py::NutritionGarminExpenditureTests.test_day_and_week_read_measured_energy_from_observations`, `tests/test_nutrition_garmin_expenditure.py::NutritionGarminExpenditureTests.test_malformed_garmin_snapshot_leaves_local_day_summary_available`, `tests/test_nutrition_garmin_expenditure.py::NutritionGarminExpenditureTests.test_targeted_nutrition_refresh_freshness_is_scoped_to_requested_day`, `tests/test_nutrition_garmin_expenditure.py::NutritionGarminExpenditureTests.test_a_later_refresh_mark_never_hides_a_stored_total`, `tests/test_nutrition_garmin_refresh.py::GarminNutritionRefreshTests.test_a_range_answer_without_a_day_is_incomplete`, `tests/test_performance_daily_health.py::CanonicalDailyHealthParityTests.test_expenditure_reads_every_component_from_the_observations`, `tests/test_performance_daily_health.py::CanonicalDailyHealthParityTests.test_snapshot_energy_without_observations_is_unavailable`.

### Requirement: Running-day projection is explicitly derived
The system SHALL expose a separate running-day full-day expenditure projection only when today's active energy and a valid completed resting-energy reference are available. It SHALL retain the reference date and keep this projection distinct from Garmin's measured total.

#### Scenario: Valid reference
- **WHEN** the latest positive resting-energy value within the preceding seven days lies in a fully current completed read window
- **THEN** today's active energy plus that reference produces the separate projection and reference metadata.

#### Scenario: Missing or inconsistent reference
- **WHEN** the reference is absent, outside a trusted current window, or gives a projection below today's already measured resting or total energy
- **THEN** no projected total is returned.

#### Scenario: Historical or future date
- **WHEN** the selected date is not the current athlete-local day
- **THEN** the running-day projection is absent; future dates do not acquire measured expenditure from past data.

Evidence: `backend/performance/daily_health.py::_projection_reference`, `backend/performance/daily_health.py::_projected_expenditure`, `public/nutrition.js::renderNutritionExpenditure`; `tests/test_nutrition_garmin_expenditure.py::NutritionGarminExpenditureTests.test_today_projects_persisted_resting_reference_with_current_active_energy`. Projection edge branches are inspected source evidence; this is not evidence of adaptive nutrition targets.

### Requirement: Nutrition body view preserves observations and gaps
The client SHALL expose body history under nutrition with the selectable periods 4 Wochen, 3, 6 and 12 Monate, render server-preferred body measurements, and preserve measurement dates and gaps. Detailed body-series and derived power-to-weight calculation remain governed by the performance domain.

#### Scenario: Open body history
- **WHEN** the athlete navigates to `#nutrition/body`
- **THEN** the body charts appear in nutrition with period controls and the preferred weight/body-fat series; the latest-values table keeps each original measurement date.

#### Scenario: Missing observations
- **WHEN** a date has no body measurement
- **THEN** the chart retains the gap rather than using a current profile value as historical observation.

#### Scenario: Twelve-week aggregation
- **WHEN** the athlete selects twelve weeks
- **THEN** each rolling week's chart point uses the week's latest measurement, retains its observation count and latest measurement date, draws a single measured week as a point, and leaves empty weeks as gaps; W/kg remains in the current cycling-performance table rather than a Body chart.

#### Scenario: Preferred measurements unavailable
- **WHEN** the response has no measured series from the server-preferred body source
- **THEN** the latest-values table shows missing values and empty metric charts are omitted rather than silently substituting another source or profile value.

Evidence: `backend/performance/body.py::body_history`, `public/analysis.js::renderBodyAnalysis`, `public/analysis.js::bodyWeeklyPoints`, `public/analysis.js::analysisPlottable`, `public/navigation.js::NAV_ROUTES`; `e2e/body-analysis.spec.js::@responsive Body keeps old measurements dated and long gaps empty`, `tests/analysis-presentation.test.cjs::12-week body charts show the latest reading of each week and draw a single week as a point`, `e2e/body-analysis.spec.js::@responsive Body plots measurements and W/kg appears only in the cycling table`, `tests/test_nutrition_garmin_refresh.py::GarminNutritionRefreshTests.test_remote_body_refresh_keeps_body_fat_only_measurements_and_normalizes_date`. Exact body-chart runtime conformance remains unexecuted in this worker; detailed body-series calculation belongs to the performance domain specification.

### Requirement: Daily macro comparison
The diary SHALL subtly color complete macro totals outside athlete-specific daily reference ranges using the latest available weight and training data.

#### Scenario: Protein target
- **WHEN** a current body weight is available
- **THEN** the protein reference is 2 g/kg with a 15 percent tolerance in either direction.

#### Scenario: Protein in a deficit
- **WHEN** a current body weight is available and the profile's energy goal is lose
- **THEN** the protein reference is 1.6 to 2.4 g/kg; every other goal keeps 1.7 to 2.3 g/kg. Evidence: `tests/test_nutrition_macro_ranges.py::MacroRangeTests::test_protein_widens_to_1_6_2_4_g_per_kg_only_in_a_deficit`.

#### Scenario: Training-adjusted carbohydrate range
- **WHEN** the day's training is known
- **THEN** the base range is 3–5 g/kg below 60 minutes, 5–7 g/kg from 60 minutes, and 6–10 g/kg from 90 minutes; at least ten minutes in zone 4 or above within one session raises it one range, capped at 6–10 g/kg; from 240 minutes it is 8–12 g/kg (Thomas, Erdman & Burke 2016, Table 1, #3920).

#### Scenario: Planned and completed sessions
- **WHEN** a completed activity is matched to a planned session
- **THEN** its actual duration (a strength session's total time, other sports' moving time) replaces the planned duration on the activity's actual date without double counting.

#### Scenario: Missed session
- **WHEN** a planned session is marked missed
- **THEN** it does not contribute training to the day's carbohydrate range.

#### Scenario: Fat reference
- **WHEN** complete fat and positive calorie totals are available
- **THEN** the reference is 20–30 percent of recorded energy as fat energy.

#### Scenario: Missing input
- **WHEN** intensity or training information is unavailable but current weight is known
- **THEN** carbohydrate comparison uses the 3–5 g/kg light-activity reference without claiming the training intensity is known.

#### Scenario: Missing weight or intake
- **WHEN** a required weight, calorie value or macro total is unknown
- **THEN** the affected comparison is not colored and no missing value is assumed.

#### Scenario: Intensity source
- **WHEN** a session of the day is read for its intensity
- **THEN** recorded power zone times decide, else pace zone times, else heart-rate zone times, else a planned duration profile; seconds in zone 4 or above are summed, and a recorded session without readable zone times has no usable intensity.

#### Scenario: Planned profile
- **WHEN** a planned session has a duration-axis profile with constant segments
- **THEN** its seconds in zone 4 or above count toward the ten-minute threshold; ramps, ranged labels or another axis make the intensity unknown.

#### Scenario: Several sessions
- **WHEN** a day has several sessions
- **THEN** their minutes add up, and one session with at least 600 seconds in zone 4 or above raises the level by one; a session without duration or usable intensity, or a calendar entry that cannot be dated, keeps the carbohydrate range at 3 to 5 g/kg.

#### Scenario: Low value on the running day
- **WHEN** a complete total lies below its range
- **THEN** it is marked low only for days before today; a value above its range is marked on every day.

Detailed contract: the fat range is 20 to 30 percent of the recorded kcal divided by 9 kcal per gram. The weight is the current performance weight; without one there is no weight-based range, never the profile's stored weight (#3640). A missed, archived or locally deleted calendar entry contributes nothing. The classes are `nutrition-above-range` and `nutrition-below-range`.

Evidence: `backend/nutrition/macro_ranges.py`, `backend/http_api/nutrition.py`, `public/nutrition.js`; `tests/test_nutrition_macro_ranges.py::test_ambiguous_zone_range_uses_light_fallback`, `tests/test_nutrition_macro_ranges.py::test_multiple_sessions_and_exact_high_intensity_cutoff`, `tests/test_nutrition_macro_ranges.py::test_completed_activity_uses_actual_day_and_skips_missed_plan`, `tests/test_nutrition_macro_ranges.py::test_context_keeps_the_weight_when_plan_is_unavailable`, `tests/test_nutrition_api_and_sync.py`, `tests/nutrition-macro-ranges.test.cjs::marks complete values outside a range and avoids premature low warnings`, `e2e/nutrition-macro-ranges.spec.js`.

### Requirement: Nutrition history shows average daily calorie balance
The nutrition history SHALL show a signed average daily calorie balance alongside the total balance for the 4-week, 3-month, 6-month and 12-month views (28, 91, 182 and 365 days), with the count of days used and the period's day count.

#### Scenario: Complete deficit period
- **WHEN** all 28 days have intake and expenditure and their total balance is -8,932 kcal
- **THEN** the total remains visible and the average displays -319 kcal per day with coverage of 28 of 28 days.

#### Scenario: Partial period
- **WHEN** only some days have both intake and expenditure in either period
- **THEN** the average is the sum of those paired daily balances divided by their count, and its coverage identifies the paired days out of 14 or 84.

#### Scenario: Surplus or balanced period
- **WHEN** the paired-day total is positive or zero
- **THEN** the average retains the positive sign for a surplus or displays zero for a balanced period.

#### Scenario: No paired days
- **WHEN** no day has both intake and expenditure
- **THEN** the average is shown as unavailable with zero coverage, rather than as zero.

Evidence: `backend/nutrition/sync_status.py::NutritionSyncStatusService.get_week_summary` supplies the paired balance sum and count (`balance_kcal`, `balance_days`); `public/nutrition.js::renderNutritionWeek` derives and displays the signed average beside the total. `tests/nutrition-history.test.cjs::history displays daily balance from paired days alongside the total` covers the complete, partial, surplus, balanced, rounding-to-zero and no-paired-day cases; `e2e/nutrition-week.spec.js::@responsive nutrition week shows coverage, signed balance and missing days without fueling` and `e2e/nutrition-week.spec.js::@responsive nutrition gaps keep expenditure visible without inventing intake` cover the rendered tiles at all viewports; `tests/test_nutrition_garmin_expenditure.py::NutritionGarminExpenditureTests.test_week_uses_closed_days_and_only_paired_values_for_balance` covers the paired-day summary.

### Requirement: Entry normalization rules
The system SHALL normalize an entry to `meal_date`, `logged_at`, `meal_type`, `description` (required, cut to 500 characters), whole `kcal`, macros with one decimal, `source`, `nutrition_basis`, `logged_time_known` and `sync_state` `local`. It SHALL accept the aliases `date`, `meal_time`, `calories`, `carbs`, `carbohydrates`, `protein` and `fat`, and SHALL reject ambiguous grouped-thousands text instead of reading it as a decimal.

#### Scenario: Meal type from the clock
- **WHEN** no meal type is given and the time is known
- **THEN** the type is breakfast from 05:00, lunch from 11:00, snack from 15:00, dinner from 18:00 and snack from 23:00 to 04:59.

#### Scenario: Main meal without a time
- **WHEN** a breakfast, lunch or dinner names no time and does not declare `logged_time_known=false`
- **THEN** it is stored at 09:00, 12:00 or 18:00 on the meal date as a known time, today and on any other day.

#### Scenario: Type change of a meal at its default time
- **WHEN** a change names a new meal type but no time, and the stored time is the old type's default
- **THEN** the time moves to the new type's default (kept when the new type has none); a named time is never moved.

#### Scenario: Meal for another day without a time
- **WHEN** an entry of another type for a day other than the athlete's today names no time
- **THEN** it is stored with unknown time (the noon anchor); for today the current local time is used.

#### Scenario: Time with another date
- **WHEN** a given timestamp carries a date other than the meal date
- **THEN** its clock time is kept and its date replaced by the meal date; an unparsable time is 400.

#### Scenario: Ambiguous number text
- **WHEN** a quantity arrives as text such as "1,200", or "1.200" for kcal
- **THEN** the entry is rejected with 400 instead of reading 1.2 or 1200.

#### Scenario: Source and sync state
- **WHEN** a client sends a source outside `manual`, `voice`, `photo`, `coach`, or a `sync_state`
- **THEN** the source becomes `manual` and the sync state stays `local`.

Detailed contract: `logged_time_known` must be a boolean; with an unknown time a meal type is required ("Bei unbekannter Uhrzeit ist der Mahlzeittyp erforderlich."). A correction that names a field and its alias with different values is 400 "Widersprüchliche Werte für {feld}."; equal values are accepted. For entries with a database basis the macro aliases are ignored.

Evidence: `backend/nutrition/models.py::normalize_nutrition_entry`, `backend/nutrition/models.py::meal_type_from_hour`, `backend/nutrition/models.py::_normalize_logged_time`, `backend/nutrition/models.py::default_meal_logged_at`, `backend/nutrition/diary.py::_merged_entry`, `backend/nutrition/diary.py::_canonical_meal_changes`; `tests/test_nutrition.py::test_meal_type_from_hour`, `tests/test_nutrition.py::test_grouped_thousands_text_is_rejected_instead_of_read_as_a_decimal`, `tests/test_nutrition.py::test_zero_values_are_preserved_and_client_cannot_claim_sync_state`, `tests/test_nutrition.py::test_conflicting_alias_and_field_are_rejected_and_equal_values_accepted`, `tests/test_nutrition_meal_times.py::test_meal_for_another_day_without_a_time_has_no_known_time`, `tests/test_nutrition_meal_times.py::test_main_meal_without_a_time_takes_its_type_default`, `tests/test_nutrition_meal_times.py::test_type_change_moves_only_a_default_time`, `tests/test_nutrition_meal_times.py::test_dated_time_is_rebased_onto_the_meal_date`, `tests/test_nutrition_meal_times.py::test_unparsable_time_is_rejected_instead_of_a_known_noon`.

### Requirement: Energy block of a diary day
The system SHALL return `energy_expenditure` with every day read, and SHALL keep the diary readable when its inputs fail. Request and response shapes of the day, week and refresh routes are in `docs/api-routes.md`.

#### Scenario: Energy block
- **WHEN** a day is read
- **THEN** `energy_expenditure` has `total_kcal`, `active_kcal`, `resting_kcal`, `projected_total_kcal`, `projection_resting_kcal`, `projection_reference_date`, `source`, `measured_date`, `freshness`, `fetched_at`, `synced_at`, `provisional`, `status` (`measured` or `unavailable`), `measurement_status` and `measurement_age_days`.

#### Scenario: Failed reference inputs
- **WHEN** the weight or training data for the macro ranges cannot be read
- **THEN** the day is answered without the unavailable ranges; `macro_ranges` omits what cannot be derived.

Evidence: `backend/performance/daily_health.py::daily_energy_expenditure`, `backend/http_api/nutrition.py::NutritionGetRoutes._day_summary`, `backend/nutrition/sync_status.py::NutritionSyncStatusService.get_week_summary`; `tests/test_nutrition_api_and_sync.py::test_get_day_route`, `tests/test_nutrition_api_and_sync.py::test_day_reference_failure_preserves_diary`, `tests/test_nutrition_api_and_sync.py::test_history_period_is_forwarded`, `tests/test_nutrition_meal_times.py::test_entry_route_returns_the_change_for_undo`.

### Requirement: The energy card guards the size of a deficit and shows the expected day total
The energy card SHALL show two non-blocking notes as its last lines and, for today, an expected day total beside the total so far. "Ziel heute" and "Offene Kalorien" SHALL stay on the total so far.

#### Scenario: Energy availability below the floor
- **WHEN** at least five of the last seven days are completely logged with priced sessions and a fat-free mass is known
- **THEN** the 7-day mean of the intake less the sessions' net exercise energy per kg fat-free mass is read as `energy_availability`, and below 30 kcal/kg FFM the card ends with "Energieverfügbarkeit (7 Tage) 27 kcal/kg FFM · unter 30"; with fewer days or without a fat-free mass there is no note. Evidence: `tests/test_energy_view.py::EnergyGuardTests::test_availability_is_the_net_intake_per_kg_fat_free_mass`, `tests/test_energy_view.py::EnergyGuardTests::test_it_needs_five_complete_days_among_seven_and_a_lean_mass`, `tests/test_energy_view.py::EnergyGuardTests::test_the_floor_fires_on_the_shown_whole_number`.

#### Scenario: Fast weight loss
- **WHEN** the 28-day weight line falls faster than 1 percent of body mass per week
- **THEN** `loss_rate` is fast and the card ends with "Gewicht fällt 1,3 %/Woche · schneller als 1 %". Evidence: `tests/test_energy_view.py::EnergyGuardTests::test_the_weight_line_falling_faster_than_one_percent_a_week_is_fast`.

#### Scenario: Expected day total
- **WHEN** today's read has every part known
- **THEN** `expected_total_kcal` is the outlined source's resting energy and everyday term, plus the latest window's mean steps and digestion, plus today's planned units costed from their planned duration, and the card shows "Tag erwartet ≈ N" directly under "Tag gesamt"; a past day, or an unknown part, shows no row. Evidence: `tests/test_energy_view.py::ExpectedTotalTests::test_it_adds_the_usual_steps_digestion_and_the_planned_units`, `tests/test_energy_view.py::ExpectedTotalTests::test_it_is_for_today_and_known_parts_only`, `tests/test_energy_view.py::ExpectedTotalTests::test_the_goal_stays_on_the_total_so_far`, `tests/test_energy_view.py::PlannedUnitCostTests::test_strength_takes_its_general_met_and_other_sports_their_own_rate`.

Evidence: `backend/nutrition/energy_guard.py`, `backend/nutrition/energy_expected.py`, `public/analysis.js::energyUsage`; `tests/analysis-presentation.test.cjs::the diary's usage card draws the outlined source's parts, its day total and the info behind the small i (#3252, #3703)`.

### Requirement: A marked weigh-in step is explained as probable water
The energy read SHALL carry `water_hint` for this morning's weigh-in (today's, read beside the history that ends yesterday): a hint only, with no kilogram figure, that changes no weight, trend or energy estimate. The weigh-in is marked when it lies beyond twice the robust scatter of the athlete's own weigh-ins around their trend, and at least 0.7 kg; the cause is read from yesterday against the athlete's usual 28 days, counting only completely logged days.

#### Scenario: A jump after salt or carbohydrates
- **WHEN** the weigh-in is marked upward and the previous day's logged salt or carbohydrates were clearly above the usual level
- **THEN** the hint names `up` with the causes that apply, and the weight row of Körper shows "Wahrscheinlich Wasser: gestern viel Salz und Kohlenhydrate" (or "viel Salz", "viele Kohlenhydrate") under its source line.

#### Scenario: A drop after a long session
- **WHEN** the weigh-in is marked downward after a session of at least 90 minutes and the previous day's carbohydrates were clearly below the usual level
- **THEN** the hint names `down`, and the row shows "Wahrscheinlich Wasser: gestern wenig Kohlenhydrate nach langer Einheit".

#### Scenario: Nothing to say
- **WHEN** the deviation is not marked, the direction does not match yesterday, yesterday is not completely logged, or its salt data is sparse (under half of its values report salt)
- **THEN** there is no hint, and without usable salt data the hint never names salt.

#### Scenario: The Coach reads the same hint
- **WHEN** the Coach reads the energy of a day
- **THEN** it receives the same `water_hint`, which carries `coach_note`: name the cause, give no kilogram figure, and salt binds water only for a short time and never causes tissue or fat gain.

Evidence: `backend/nutrition/water_hint.py`, `backend/nutrition/energy_view.py::read_energy_view`, `public/analysis.js::waterHintText`; `tests/test_water_hint.py`, `tests/test_coach_tool_coverage.py::test_the_energy_read_takes_the_asked_day`, `tests/analysis-presentation.test.cjs::body overview: the newest own body fat with its source, weight with its source, lean mass derived (#3402)`.

### Requirement: Correction and meal-extension semantics
A correction SHALL accept only meal date, time, time knowledge, meal type, description, kcal, macros (and their aliases), `food_ingredients`, `components` and `packaging_label`, SHALL keep the stored source, and SHALL mark hand-edited nutrients with the basis `manual_correction`. Adding a product to a meal SHALL keep the earlier values as the first component.

#### Scenario: Unsupported field
- **WHEN** a correction names any other field or is empty
- **THEN** it fails with 400 "Korrektur muss mindestens ein gültiges Ernährungsfeld enthalten."

#### Scenario: Meal that was not a composite
- **WHEN** a product is added to a single-source meal
- **THEN** the meal becomes a composite whose first component carries the earlier nutrients and basis, with kind `estimate` for Coach, photo or voice entries and `manual` otherwise, and the amount and unit of its basis or 1 `portion`.

#### Scenario: Limits
- **WHEN** the meal already has 20 components
- **THEN** the addition fails with 400; otherwise the description gains " + {product name}" and stays within 500 characters, cutting the older text first.

Evidence: `backend/nutrition/diary.py::NutritionDiaryService.correct_meal`, `backend/nutrition/diary.py::NutritionDiaryService._apply_meal_correction`, `backend/nutrition/diary.py::NutritionDiaryService.add_product_to_meal`, `backend/nutrition/meal_components.py::wrapped_component`, `backend/nutrition/diary.py::NutritionDiaryService._extended_description`; `tests/test_nutrition_favorites_and_meal_additions.py::test_an_estimated_meal_keeps_its_values_as_the_first_component`, `tests/test_nutrition_favorites_and_meal_additions.py::test_description_keeps_the_newest_product_within_the_limit`, `tests/test_nutrition_favorites_and_meal_additions.py::test_wrapped_meal_keeps_sugar_and_salt_totals`, `tests/test_nutrition.py::test_database_values_override_model_values_and_provenance_survives_corrections`.

### Requirement: Diary and history view states
The client SHALL show the nutrition area as Tagebuch, Verlauf, Körper and Produkte. The diary SHALL offer a date field that names its weekday ("Heute · Montag" for today) with previous, next and "Heute", show a cached copy of the requested view at once, and let the athlete delete a meal directly at once, with a "Rückgängig" receipt. The history SHALL switch between 4 weeks, 3 months and 6 months, draw one card layout with the period in every title row, and state the coverage of every figure.

#### Scenario: Empty day
- **WHEN** a day has no entries
- **THEN** no total tiles show, the status reads "Noch keine Mahlzeiten erfasst." and "Dem Coach sagen" writes "Ich habe heute gegessen: " into the composer without sending it

#### Scenario: Failed load
- **WHEN** a day cannot be loaded and no cached copy exists
- **THEN** the requested date stays in the field and one error with "Erneut laden" is shown; with a cached copy the status reads "Gespeicherter Stand. Aktualisierung fehlgeschlagen: …".

#### Scenario: Balance label
- **WHEN** the history is rendered
- **THEN** the balance tile is named "Kaloriendefizit", "Kalorienüberschuss" or "Ausgeglichene Bilanz", or "Erfasste Teilbilanz" when fewer days than the period are paired.

#### Scenario: Twelve weeks
- **WHEN** 84 days are shown
- **THEN** the chart has one row per calendar week (the first and the last partial) under the heads "Woche", "Aufnahme · Verbrauch" and "Bilanz", each reading "Woche ab dd.mm." (the week that holds today "Diese Woche") with weekly averages of intake and expenditure and "{n} von {days of that week} Tagen", and consecutive rows without data collapse into one "keine Daten" row, in the chart and in the day table (#3930).

Detailed contract: a meal card shows time and meal label (the label alone when the time is unknown), description, values, and a source line unless the entry is a locally stored manual entry; synced entries show "Synchronisiert". The expenditure section shows "Tagesverbrauch", "Aktivkalorien" and "Grundumsatz"; a current measurement has no status line and "Tagesverbrauch" carries a source chip with the provider's short label ("Garmin"); otherwise the status line names the source and the freshness ("Veraltet", "Verzögert", else "Aktualität unbekannt") and, for the running day, "Vorläufig, da der Tag noch läuft", or says that no Garmin data exists for the day; its date line reads "Sync: …" or else "Abgerufen: …". For today "Prognose Tagesverbrauch" carries an estimate chip and a tooltip naming the resting reference day.

Evidence: `public/nutrition.js::renderNutritionDay`, `public/nutrition.js::showNutritionDayLoadError`, `public/nutrition.js::loadNutrition`, `public/nutrition.js::nutritionCard`, `public/nutrition.js::deleteNutritionEntry`, `public/nutrition.js::renderNutritionExpenditure`, `public/nutrition.js::renderNutritionWeek`, `public/nutrition.js::nutritionChartDays`; `e2e/nutrition-diary-undo.spec.js::@responsive diary shows decimal commas and offers undo and delete for back-filled meals`, `e2e/nutrition.spec.js::@responsive a failed diary load keeps the requested date, shows one error and retries the day`, `e2e/nutrition-week.spec.js::@responsive nutrition history switches between 14 days and 12 weeks`, `e2e/nutrition-expenditure.spec.js::@responsive nutrition diary presents Garmin energy expenditure and unavailable states`.
