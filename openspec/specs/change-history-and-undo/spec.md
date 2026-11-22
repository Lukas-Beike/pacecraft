# change-history-and-undo Specification

## Purpose

Describe bounded local audit history and optimistic undo for profile, workout-library, planned-unit, competition, training-plan, zone-configuration, activity-feedback, activity-session, nutrition-entry, nutrition-template, nutrition-product, stored-food, check-in, equipment, equipment-assignment and equipment-maintenance records, the listed-but-never-undone metric export entries, together with receipt visibility and the narrower complete-turn undo surface.

## Requirements

### Requirement: HU1 Bounded local audit projections
The system SHALL record supported local creates, updates, deletes and undos using allowlisted bounded fields, stable before/after hashes and private diffs, skipping unchanged ordinary mutations.

#### Scenario: Supported changed record
- **WHEN** a supported local record changes within its owning transaction
- **THEN** history stores its entity/action/source/time, before/after hashes and restorable changed-field values without copying arbitrary raw provider or credential fields.

#### Scenario: Unchanged record
- **WHEN** normalized before and after projections hash identically for an ordinary create/update/delete
- **THEN** no redundant change row is created; an undo action remains recorded under its distinct policy.

#### Scenario: Unknown or oversized fields
- **WHEN** input contains nonallowlisted or oversized field values
- **THEN** those fields are excluded from the audit projection rather than widening the persisted history surface.

Evidence: `backend/change_history.py:audit_projection`, `backend/change_history.py:audit_hash`, `backend/change_history.py:audit_diff`, `backend/change_history.py:record_change`; `tests/test_change_history.py:test_projection_keeps_allowlisted_bounded_fields_only`, `tests/test_change_history.py:test_record_change_persists_hashes_and_private_diff`, `tests/test_change_history.py:test_unchanged_record_is_skipped_but_undo_is_retained`, `tests/test_change_history.py:test_every_persisted_profile_field_is_audited`.

### Requirement: HU1a Audit projection, hash and diff format
A change entry SHALL store the hashes and diff of the audit projection of its entity type, and the projection, hash function and diff layout SHALL stay compatible across releases, so that entries written earlier still match the current state.

#### Scenario: Projection
- **WHEN** a record is projected
- **THEN** only the allowlisted fields of its entity type that are present are kept, in the constants of `backend/change_history.py` (`PROFILE_FIELDS`, `LIBRARY_FIELDS`, `PLANNED_UNIT_FIELDS`, `COMPETITION_FIELDS`, `PLAN_FIELDS`, `ZONE_CONFIGURATION_FIELDS`, `NUTRITION_*_FIELDS`, `FOOD_DATABASE_ENTRY_FIELDS`, `CHECKIN_FIELDS`, `EQUIPMENT_*_FIELDS`, `METRIC_EXPORT_FIELDS`); feedback is the one field `rows` (all stored rows of a session group, at most 32) and a session correction the one field `sessions`; library and planned-unit rows are read from their stored document with `sync_status` taken from the row's sync state

#### Scenario: Field size limit
- **WHEN** the compact JSON text of one field is longer than its limit
- **THEN** that field is left out of projection, hash and diff and the others are kept; the limit is 4,000 characters, 400,000 for `activity_feedback`, 100,000 for `nutrition_entry` and `nutrition_template`, and 20,000 for `activity_session`, `checkin` and `equipment_maintenance`

#### Scenario: Hash and diff
- **WHEN** an entry is recorded
- **THEN** each hash is the SHA-256 hex of the projection (`{}` when there is none, so an absent and an empty record hash alike) as JSON with sorted keys, no ASCII escaping, separators `,` and `:` and non-JSON values as text; the private diff is `{"fields": {name: {"before", "after"}}, "before_present", "after_present"}` over the changed fields only

#### Scenario: Entry fields
- **WHEN** an entry is stored
- **THEN** it holds a UUID4 `id`, the `entity_type`, the `entity_id` as text, an `action` of `create`, `update`, `delete` or `undo`, a `source` cut to 40 characters (default `local`) and the UTC ISO `created_at`; an unsupported type or action raises `ValueError`

#### Scenario: Deliberately untracked state
- **WHEN** only sync bookkeeping of a nutrition entry, the update time of a template or product, a product's favorite flag, or an equipment item's revision, update time or Garmin-owned mileage changes
- **THEN** no entry is written and an earlier change of that record stays undoable

#### Scenario: Age of a value
- **WHEN** a service asks when a value was last written and no matching entry is left
- **THEN** the time of the oldest kept entry is returned as a lower bound, or nothing for an empty history

Evidence: `backend/change_history.py::audit_projection`, `backend/change_history.py::audit_hash`, `backend/change_history.py::audit_diff`, `backend/change_history.py::record_change`, `backend/change_history.py::value_recorded_at`, the `*_FIELDS` constants and `_FIELD_LIMITS`. Tests: `tests/test_change_history.py::test_projection_keeps_allowlisted_bounded_fields_only`; `tests/test_change_history.py::test_record_change_persists_hashes_and_private_diff`; `tests/test_change_history.py::test_every_persisted_profile_field_is_audited`; `tests/test_change_history.py::test_each_profile_field_alone_records_a_restorable_change`; `tests/test_change_history.py::test_planned_unit_time_only_edit_is_recorded`; `tests/test_nutrition_product_history.py::test_create_update_and_archive_each_record_one_change` (the favorite flag). No test pins the size limits or `value_recorded_at`.

### Requirement: HU2 Retention and private public history
The system SHALL expose bounded newest-first history for the 180-day retention window, cap retained rows at one thousand and keep actual before/after values out of the public history representation.

#### Scenario: Public list
- **WHEN** authenticated history is read
- **THEN** entries expose identity, type/action/source/time, hashes, field-changed markers and sync scope (`local_only`, or `remote` for metric exports), without retained athlete values.

#### Scenario: Old or excessive rows
- **WHEN** history contains expired or excessive entries
- **THEN** reads hide entries older than the retention cutoff and clamp limits; normal history recording performs retention cleanup and makes room for its own row first, so at most one thousand rows remain after it.

#### Scenario: Where the list is read
- **WHEN** the history list is needed
- **THEN** the Coach tool `list_change_history` reads it (limit clamped to 1 through 500, default 100; a non-integer limit is rejected with 400, reason `invalid_list_request`) without record values, and the athlete's view reads `GET /api/change-history`: newest first, 30 entries per page (at most 100) with a `next_cursor`, each entry with `line` (the readable change, e.g. "Check-in, Fr 20.11.: Muskelkater 3 → 5") and `can_undo` (a create, update or delete that is still the newest entry of its record); `source` is `coach` for a change a Coach tool round records (a default-source write inside it), `local` for the app's own, `undo` for an undo and another producer's name for the rest, and Coach changes recorded before this field was set read `local`; the line names the record from its current state when the stored diff holds only the changed fields; Mehr › Betrieb & Diagnose shows the list in "Verlauf der Änderungen", read when the section opens, and "Rückgängig" asks once before it posts `POST /api/change-history/undo` with the entry's `id` and `after_hash`

#### Scenario: Atomic operation exceeds capacity
- **WHEN** a replacement reserves more than one thousand audit rows
- **THEN** it is rejected instead of silently pruning part of its own audit evidence or claiming complete undo coverage.

Evidence: `backend/history/service.py:ChangeHistoryService.list`; `backend/change_history.py:public_view`, `backend/change_history.py:cleanup`, `backend/change_history.py:reserve_capacity`; `tests/test_history_service.py:test_list_clamps_limits_orders_rows_and_returns_private_projection`, `tests/test_history_service.py:test_list_hides_expired_and_caps_rows_without_deleting`; `tests/test_change_history.py:test_public_view_never_exposes_diff_values`, `tests/test_change_history.py:test_cleanup_and_capacity_preserve_bounds`; `backend/coach/read_tools.py` (`list_change_history`); `backend/history/service.py:ChangeHistoryService.page`; `backend/http_api/history_get.py:HistoryGetRoutes`; `public/diagnostics.js:loadChangeHistory`; `tests/test_history_service.py:test_page_reads_lines_and_marks_only_the_newest_entry_undoable`; `e2e/history-undo.spec.js` ("the change history loads when opened and undoes its newest entry").

### Requirement: HU3 Read-only undo previews bound to current state
The system SHALL preview undo only for an existing supported create/update/delete whose recorded after-state still matches the current local projection, returning a safe proposal with the expected current hash.

#### Scenario: Eligible undo preview
- **WHEN** a valid history ID still describes the record's latest supported local state
- **THEN** preview returns public change metadata, target hash and a local undo proposal without restoring the record.

#### Scenario: Invalid, missing or changed target
- **WHEN** the history ID is malformed/missing, the current projection differs, or the history row cannot reconstruct a supported target
- **THEN** preview fails with the applicable validation/not-found/conflict response rather than proposing an overwrite of newer state.

#### Scenario: Already an undo action
- **WHEN** the selected history row is itself an undo
- **THEN** it cannot be undone recursively through this surface.

Evidence: `backend/history/undo_service.py:HistoryUndoService.preview`, `backend/history/undo_service.py:HistoryUndoService._load_history`; `backend/history/service.py:ChangeHistoryService.target`; `tests/test_history_undo_service.py:test_preview_returns_safe_proposal_and_opens_one_uow`, `tests/test_history_undo_service.py:test_preview_rejects_changed_current_hash`, `tests/test_history_undo_service.py:test_invalid_missing_and_repeated_change_ids_are_rejected`; `tests/test_history_service.py:test_target_preserves_action_diff_and_error_semantics`.

### Requirement: HU3a Supported entities and never-undone entries
The system SHALL audit and restore the entity types profile, workout_library, planned_unit, competition, training_plan, zone_configuration, activity_feedback, activity_session, nutrition_entry, nutrition_template, nutrition_product, food_database_entry, checkin, equipment, equipment_assignment and equipment_maintenance through their owning services, and SHALL keep `metric_export` entries of confirmed metric exports and entries whose diff carries `undoable: false` listed but never undo them.

#### Scenario: Metric export entry
- **WHEN** a confirmed or automatic metric export writes values to the provider
- **THEN** an update entry for `<provider>:<remote record>` with the allowlisted field names is recorded, shows `remote_sync` remote and `undoable` false, and a preview or apply for it fails with HTTP 400

#### Scenario: Entry marked not undoable
- **WHEN** an entry's diff carries `undoable: false`, as set by the Intervals.icu purge of release 2.0.0 for the feedback rows it re-keyed (no current code sets the marker)
- **THEN** the entry stays listed but preview and apply fail with HTTP 400 instead of restoring a record the purge moved

Evidence: `backend/change_history.py:ENTITY_TYPES`, `backend/change_history.py:NOT_UNDOABLE_ENTITY_TYPES`, `backend/change_history.py:undoable` (the unused `mark_not_undoable` is the only writer of the marker), `backend/history/undo_service.py:HistoryUndoService._restore`, `backend/history/undo_service.py:HistoryUndoService._load_history`, `backend/sync/metric_export.py`; `tests/test_metric_export.py:test_writes_are_in_the_change_history_without_undo`.

### Requirement: HU4 Atomic hash-checked domain restore
The system SHALL revalidate both recorded after-hash and supplied expected current hash during apply, restore through the owning domain transaction, audit the undo and leave remote state untouched.

#### Scenario: Apply eligible undo
- **WHEN** a valid undo request carries the previewed current hash
- **THEN** the prior local state is restored for the supported entity, planned-unit/plan changes advance planning revision, one undo row is written and the result reports remote_untouched.

#### Scenario: Concurrent edit
- **WHEN** current local state changed after preview or the supplied expected hash is absent/wrong
- **THEN** apply aborts before restore instead of overwriting that newer edit.

#### Scenario: Failure during restore/revision/history
- **WHEN** domain restoration, revision update or undo audit persistence fails
- **THEN** the complete transaction rolls back and no partial restored state remains.

Evidence: `backend/history/undo_service.py:HistoryUndoService.apply`, `backend/history/undo_service.py:HistoryUndoService._apply_one`, `backend/history/undo_service.py:HistoryUndoService._restore`; `tests/test_history_undo_service.py:test_apply_dispatches_all_entity_restores_and_records_once`, `tests/test_history_undo_service.py:test_apply_requires_preview_hash_even_when_current_is_unchanged`, `tests/test_history_undo_service.py:test_apply_rejects_stale_hash_before_domain_restore`, `tests/test_history_undo_service.py:test_domain_restore_failure_rolls_back_effects`, `tests/test_history_undo_service.py:test_revision_failure_rolls_back_restore_and_revision`, `tests/test_history_undo_service.py:test_history_failure_rolls_back_restore_and_revision`.

### Requirement: HU5 Data-preserving domain undo semantics
The system SHALL preserve supported current metadata while restoring audited local fields, support legacy audit-hash generations, and recheck visibility/date conflicts when restoring planned records.

#### Scenario: Legacy profile history
- **WHEN** an older history row predates added profile audit fields but still describes the same supported local state
- **THEN** its accepted legacy hash remains matchable, also when a field added later holds another value, while an edit of a field the row already knew still causes a conflict.

#### Scenario: Recreate or remove local object
- **WHEN** undo targets a deleted record or reverses a creation
- **THEN** the owning restore path recreates or removes the local record with its supported identity/link policy rather than deleting a corresponding remote record. A planned unit that already has an Intervals.icu event is kept hidden with its links, so the event is listed as a leftover and removed only by the athlete's next week sync.

#### Scenario: Planning conflicts or location changes
- **WHEN** planned restoration would collide with newer calendar state, or profile restoration changes location
- **THEN** the planning restore rejects the collision and location restoration uses the existing cache invalidation hook.

Evidence: `backend/change_history.py:accepted_audit_hashes`; `backend/athlete/profile.py:ProfileService.restore_in_transaction`; `backend/planning/planned_unit_service.py:PlannedUnitService.restore_in_transaction`; `tests/test_history_undo_service.py:test_legacy_profile_row_stays_undoable_and_later_edits_still_conflict`, `tests/test_history_undo_service.py:test_legacy_row_accepts_batch_after_hash_and_rejects_wrong_expected_hash`; `tests/test_server_planning.py:test_planned_unit_undo_rejects_a_new_date_conflict`, `tests/test_server_planning.py:test_deleted_local_library_entry_can_be_undone_without_provider_write`; `tests/test_athlete_profile_service.py:test_restore_of_location_change_invokes_location_hook_and_clears_weather`.

### Requirement: HU5a Accepted hash generations
An entry SHALL stay undoable when its after-hash equals the hash of the current projection with the fields of later audit generations removed, or with a retired field restored at its empty value.

#### Scenario: Entry older than a new field
- **WHEN** an entry was hashed before a projection field existed (for the profile, for example `first_threshold_origin`) and the record is otherwise unchanged
- **THEN** its undo is accepted

#### Scenario: Generations and retired fields
- **WHEN** the current projection is compared with an entry
- **THEN** the generations of `_FIELDS_ADDED_TO_AUDIT` (profile, planned unit, workout library, stored food) are stripped newest first and cumulatively, and each stripped form is also accepted with the retired field of `_FIELDS_RETIRED_FROM_AUDIT` (profile `weekly_plan`, empty) added back

#### Scenario: New audit field in a later release
- **WHEN** a release adds a field to an audit projection
- **THEN** it appends a generation, so entries written earlier stay matchable

Evidence: `backend/change_history.py::accepted_audit_hashes`, `backend/change_history.py::_FIELDS_ADDED_TO_AUDIT`, `backend/change_history.py::_FIELDS_RETIRED_FROM_AUDIT`. Tests: `tests/test_change_history.py::test_each_released_profile_generation_stays_matchable`; `tests/test_change_history.py::test_legacy_hash_matches_projection_without_added_fields`; `tests/test_change_history.py::test_a_stored_food_change_recorded_before_its_origin_marks_stays_undoable`; `tests/test_feedback_history.py::test_changes_recorded_before_schema_v18_keep_their_undo`.

### Requirement: HU5b Entity-specific undo guards
Undo of an equipment item, equipment assignment, maintenance entry, saved product, stored food or activity feedback SHALL be refused when the restored state would no longer be valid, and SHALL keep data that other records still use.

#### Scenario: Created equipment already used
- **WHEN** the creation of an equipment item is undone after the item was used
- **THEN** the undo is refused with 409 "Die Ausrüstung wird bereits verwendet und bleibt erhalten."

#### Scenario: Equipment edit and parent
- **WHEN** an equipment edit is undone
- **THEN** the restored item is validated like a save (a parent that no longer fits refuses the undo), its revision advances and the Garmin-owned mileage keeps its current value

#### Scenario: Assignment or maintenance target gone
- **WHEN** an undo would reassign gear that is archived, incomplete or of another sport, or restore a maintenance entry of a vanished item
- **THEN** the undo is refused with 409

#### Scenario: Product barcode taken
- **WHEN** undoing a product change would restore a barcode another product now has
- **THEN** the undo is refused with 409 (reason `nutrition_product_barcode_conflict`); diary entries and saved meals that used the product keep their own values

#### Scenario: Stored food gap fill
- **WHEN** a gap fill of a stored food is undone
- **THEN** exactly the fields that change wrote are emptied, an earlier fill and other values stay, and the undo is refused with 409 once the food changed or is gone

#### Scenario: Feedback after a merge
- **WHEN** the session of a feedback change was merged afterwards
- **THEN** the undo follows the surviving session and restores hidden originals and full-length notes; a restored row whose activity id another group now holds refuses with 409

Evidence: `backend/athlete/equipment_history.py`; `backend/nutrition/catalog.py::ProductCatalogService.restore_in_transaction`; `backend/nutrition/food_mirror.py::FoodDatabaseMirror.restore_in_transaction`; `backend/activities/feedback.py::ActivityFeedbackService.restore_in_transaction`; dispatch in `backend/history/undo_service.py::HistoryUndoService._restore`. Tests: `tests/test_equipment_history.py::test_undo_of_a_created_item_is_refused_once_it_is_used`; `tests/test_equipment_history.py::test_undo_is_refused_when_the_parent_no_longer_fits`; `tests/test_equipment_history.py::test_undo_keeps_garmin_owned_mileage`; `tests/test_equipment_history.py::test_undo_of_a_reassignment_is_refused_when_the_old_gear_is_archived`; `tests/test_nutrition_product_history.py::test_undo_is_refused_when_the_barcode_was_taken`; `tests/test_nutrition_product_history.py::test_undo_keeps_diary_entries_that_used_the_product`; `tests/test_nutrition_product_history.py::test_undo_of_a_gap_fill_empties_exactly_the_filled_fields`; `tests/test_nutrition_product_history.py::test_undo_of_a_gap_fill_is_refused_after_the_food_changed`; `tests/test_feedback_history.py::test_undo_follows_a_session_merged_after_the_edit`; `tests/test_feedback_history.py::test_undoing_a_clear_restores_hidden_originals_and_long_notes`.

### Requirement: HU6 Scoped Coach undo and explicit proposal path
The system SHALL require matching operation/change scope for the Coach undo tool, support direct local apply of an update or deletion when explicitly requested by that tool path, and return a session-bound proposal for previews and creation undo requiring explicit approval.

#### Scenario: Coach preview or creation undo
- **WHEN** `undo_training_change` requests preview for an authorized change ID, or requests apply for a change whose action is create
- **THEN** the current-state preview is converted to a session-bound local proposal without applying the restore; creation undo returns confirmation_required and deletes the created record only after the athlete confirms the card.

#### Scenario: Direct authorized apply
- **WHEN** the Coach requests apply to undo an update or deletion for that scoped change under the current athlete-started turn
- **THEN** the server creates a fresh preview hash and uses the same conflict-checked local undo service; no remote reversal is implied.

#### Scenario: Proposal confirmation or replay
- **WHEN** the owning session confirms and executes an eligible undo proposal
- **THEN** the token/hash/session checks apply and only one execution consumes it; foreign, expired or used approvals cannot dispatch another restore.

Evidence: `backend/coach/planning_action_tools.py:CoachPlanningActionToolService.execute`; `backend/http_api/history_undo_post.py:HistoryUndoPostRoutes.routes`; `backend/coach/proposal_execution.py:CoachProposalExecutionService.execute`; `tests/test_coach_tool_coverage.py:test_undo_is_a_bound_preview_until_explicit_confirmation`; `tests/test_coach_planning_action_tools.py` (preview/apply/scoped undo contracts); `tests/test_coach_planning_action_tools.py:test_create_undo_with_apply_is_only_offered_as_a_card`, `tests/test_coach_planning_action_tools.py:test_create_undo_deletes_only_after_the_athlete_confirms_the_card`, `tests/test_coach_planning_action_tools.py:test_update_undo_with_apply_still_runs_directly`; `tests/test_coach_proposal_execution.py:test_valid_undo_consumes_token_and_replay_is_rejected`, `tests/test_coach_proposal_execution.py:test_foreign_session_wrong_hash_and_expiry_cannot_consume_or_dispatch`.

### Requirement: HU7 Complete-effect receipt undo eligibility
The system SHALL summarize applied Coach record changes on receipts/answers and expose whole-turn undo only when every applied effect is fully tracked with history references.

#### Scenario: Fully tracked turn
- **WHEN** all applied changes in a turn come from fully tracked tools (profile, competition save, training-template management, activity feedback and sessions, nutrition entries, templates and products, check-ins and equipment), each with history references
- **THEN** the receipt/assistant message exposes distinct change labels, an undoable flag and no undone timestamp before undo.

#### Scenario: Mixed untracked effects
- **WHEN** any applied write lacks history or also changes untracked state such as plan constraints, sync flags or other records
- **THEN** its changes can be listed but whole-turn undo is false, preventing a misleading partial rollback.

#### Scenario: Applied single-change undo in a turn
- **WHEN** `undo_training_change` applied an undo in the turn (its result status is `undone`)
- **THEN** the receipt lists "Lokale Änderung zurückgenommen" and whole-turn undo is false.

#### Scenario: Plan-check notes
- **WHEN** an applied Coach plan write has plan-check notes
- **THEN** its receipt includes safe structured notes, and the answer shows their labels, dates, evidence strength and severity alongside the change summary.

#### Scenario: Read, failed or pending action
- **WHEN** a turn only reads, fails before applying, stages/previews or waits for approval
- **THEN** those steps do not appear as applied record changes, and model input does not receive private undo references.

Evidence: `backend/coach/change_receipts.py:change_summary`, `backend/coach/change_receipts.py:_FULLY_TRACKED_TOOLS`, `backend/coach/change_receipts.py:turn_changes`; `backend/coach/final_receipt.py:CoachFinalReceiptService.persist`; `public/coach.js:appendMessageChanges`; `tests/test_coach_change_receipts.py:test_applied_tracked_writes_are_listed_once_and_undoable`, `tests/test_coach_change_receipts.py:test_a_write_with_untracked_effects_is_listed_without_turn_undo`, `tests/test_coach_change_receipts.py:test_an_applied_history_undo_is_listed_and_blocks_turn_undo`, `tests/test_coach_planning_action_tools.py:test_undo_applies_directly_when_apply_is_true`, `tests/test_coach_change_receipts.py:test_reads_failures_proposals_and_previews_are_not_changes`, `tests/test_coach_change_receipts.py:test_the_model_never_receives_undo_references`; `tests/test_server_coach_turn_undo.py:test_turn_changes_show_on_the_answer_and_undo_together`.

### Requirement: HU8 All-or-none whole-turn undo
The system SHALL undo an eligible completed turn's collected local changes newest-first in one transaction and update its durable receipt once, refusing repeated or stale/ineligible turn undo.

#### Scenario: Whole turn restored
- **WHEN** an authenticated explicit undo-turn request selects an eligible finished turn
- **THEN** changes restore in reverse recording order, the receipt gains an undone timestamp, the UI updates the change summary and remote state remains untouched.

#### Scenario: One record changed later
- **WHEN** any collected change no longer matches current state
- **THEN** the whole undo rolls back rather than restoring only the unaffected records.

#### Scenario: Invalid or repeated turn
- **WHEN** the ID is invalid/missing, the turn is not completed, it has untracked/no changes or it was already undone
- **THEN** the request fails explicitly and cannot apply the turn twice.

Evidence: `backend/coach/turn_undo.py:CoachTurnUndoService.undo`; `backend/history/undo_service.py:HistoryUndoService.apply_many`; `public/coach.js:undoCoachTurn`; `tests/test_server_coach_turn_undo.py:test_turn_changes_show_on_the_answer_and_undo_together`, `tests/test_server_coach_turn_undo.py:test_a_record_changed_after_the_turn_aborts_the_whole_undo`, `tests/test_server_coach_turn_undo.py:test_a_turn_with_untracked_writes_cannot_be_undone`, `tests/test_server_coach_turn_undo.py:test_unknown_and_invalid_turns_are_rejected`; `tests/coach-turn-undo.test.cjs` (UI receipt refresh contracts). Whole-turn undo is an authenticated single-athlete API action; per-session ownership is not asserted beyond the implemented route.

### Requirement: HU8a Undo routes
The API SHALL apply an undo with an authenticated, CSRF-protected `POST /api/change-history/undo` and a whole-turn undo with `POST /api/change-history/undo-turn`; request, response and error texts are in `docs/api-routes.md`. The undo preview is served to the Coach tool only; the history view sends the entry's `after_hash` as `expected_current_hash`, which is accepted for the newest entry of a record.

#### Scenario: Undo request
- **WHEN** an undo request has an id that is not 36 characters of `0-9`, `a-f` and `-`, an unknown id, an undo entry, a metric export, or an entry marked not undoable
- **THEN** the answers are 400 "Ungültige Änderungshistorie-ID.", 404 "Änderung nicht gefunden.", 409 "Eine Undo-Aktion kann nicht erneut zurückgenommen werden.", 400 "Ein Export an einen Anbieter lässt sich nicht zurücknehmen." and 400 "Diese Änderung lässt sich nicht mehr zurücknehmen."

#### Scenario: Changed state or missing hash
- **WHEN** the current state differs, or `expected_current_hash` is neither the current hash nor the entry's after-hash (a missing one counts as empty)
- **THEN** the answer is 409 "Die lokale Änderung wurde inzwischen weiter geändert; Undo wurde abgebrochen." even when the record is unchanged; an unreadable diff gives 409 "Die Änderungshistorie ist beschädigt."; an unsupported entity gives 400 "Unbekannte lokale Änderung."

#### Scenario: Success
- **WHEN** an undo succeeds
- **THEN** the answer is `{"status": "undone", "change_id", "entity_type", "entity_id", "remote_untouched": true}`, with `session_rpe_load` added for an activity feedback entry whose `activity_id` is given

#### Scenario: Whole-turn request
- **WHEN** a turn undo has an empty id or one above 120 characters, an unknown or not completed turn, a turn without a change summary, an already undone turn, or one that is not collectively undoable
- **THEN** the answers are 400 "Ungültiger Coach-Auftrag.", 404 "Coach-Antwort nicht gefunden.", 409 "Diese Coach-Antwort hat nichts geändert.", 409 "Diese Coach-Änderungen wurden bereits zurückgenommen." and 409 "Diese Coach-Änderungen können nicht gesammelt zurückgenommen werden."; a history row that is gone answers 404 and rolls the whole undo back

Evidence: `backend/http_api/history_undo_post.py::HistoryUndoPostRoutes`; `backend/history/undo_service.py::HistoryUndoService`; `backend/coach/turn_undo.py::CoachTurnUndoService.undo`. Tests: `tests/test_history_undo_service.py::test_apply_requires_preview_hash_even_when_current_is_unchanged`; `tests/test_history_undo_service.py::test_invalid_missing_and_repeated_change_ids_are_rejected`; `tests/test_server_coach_turn_undo.py::test_unknown_and_invalid_turns_are_rejected`; `tests/test_server_coach_turn_undo.py::test_a_turn_with_untracked_writes_cannot_be_undone`.
