# encrypted-storage-and-migrations Specification

## Purpose

Preserve durable athlete state through encrypted creation, bounded transactions, supported schema upgrades and restarts.

Owns database lifecycle and upgrade contracts. The only supported baseline is v26 (release 2.0.5, frozen fixture); the released v27 to v33 (production's since 2.1.51) are accepted by their digests; the current schema is v34; canonical/domain records are described in their capability specs. SQLCipher conformance requires encrypted fixtures, separate from SQLite test doubles.

## Requirements

### Requirement: Encrypted application database

A configured application database SHALL be opened/created through SQLCipher with APP_PASSWORD and cipher compatibility 4; encrypted startup SHALL not fall back to an unencrypted database when SQLCipher is unavailable.

#### Scenario: New encrypted database
- **WHEN** a valid secure configuration opens new temporary application storage
- **THEN** the database is created with the configured SQLCipher key

#### Scenario: Same-key restart
- **WHEN** a current encrypted database is reopened with the same application key
- **THEN** the existing records and schema remain available

#### Scenario: Missing encryption backend
- **WHEN** a secure database is requested while SQLCipher is unavailable
- **THEN** startup/database access fails without creating a plaintext replacement

#### Scenario: Connection settings
- **WHEN** a connection is opened
- **THEN** these statements run in this order: `PRAGMA key` with the password (single quotes doubled), `cipher_compatibility = 4`, `cipher_memory_security = OFF` (memory locking would split the process into too many mappings; the file stays encrypted), `foreign_keys = ON`, `cache_size = -16384` (16 MiB) and `journal_size_limit = 67108864` (64 MiB); the journal mode is WAL, set when the schema is created (`docs/schema-reference.md`)

#### Scenario: Pool and memory budget
- **WHEN** the database manager is composed
- **THEN** it holds one persistent writer and at most four readers, each waiting at most 20 seconds on a database lock, so the page caches take at most 80 MiB in all; connections are kept open only when a password is configured

#### Scenario: File unreadable with the configured key
- **WHEN** the existing file cannot be read with APP_PASSWORD
- **THEN** the first read fails with the driver's database error, startup ends there, and no database is created, replaced or modified; no handler or dedicated log event exists for this case

Evidence: `backend/db/schema.py::configure_cipher`; `backend/db/manager.py::DatabaseManager`, `backend/db/manager.py::DatabaseManager._connect`; `backend/db/manager.py::DatabaseHolder`; `server.py::database_manager`; `backend/config.py::security_configuration_error`. Tests: `tests/test_server_database.py::test_unavailable_sqlcipher_closes_manager_for_changed_secure_configuration`; `tests/test_db_migrations.py::test_encrypted_current_database_reopens_with_same_key`; `tests/test_db_manager.py::test_page_cache_budget_applies_to_writer_and_each_reader`; `tests/test_db_manager.py::test_keyed_connections_disable_memory_locking`; `tests/test_database_space_reclaim.py::test_every_connection_limits_the_wal_size`; `tests/test_db_encrypted_upgrade_matrix.py::test_the_baseline_restarts_unchanged_and_needs_its_key` (the wrong-key read fails; the startup abort itself is derived from the code path and not run).

### Requirement: Baseline schema upgrades

The database SHALL recognize the version-and-shape pair of the baseline (v26, release 2.0.5, frozen as one fixture and one digest), of the released later schemas by their frozen digests, and of the current schema, executing the versioned steps from the baseline within one rollback-capable migration transaction. A schema that never reached production SHALL get no frozen shape of its own.

#### Scenario: Baseline database
- **WHEN** a populated baseline database is opened by this build
- **THEN** the steps from the baseline run to the exact current schema while preserving existing rows.

#### Scenario: One step from the previous shape
- **WHEN** a previous shape with one registered step is opened
- **THEN** the step runs in one transaction to the exact current schema and keeps every row.

#### Scenario: Baseline food mirror becomes canonical products
- **WHEN** a v26 database with own products and mirrored foods is upgraded
- **THEN** every own product keeps its row as origin `own`, every mirrored food becomes a product of its database's origin with its fetch time and all fields, an own product with the EAN or id of mirrored foods wins and only its empty fields are filled, and the mirror table is gone. The step uses its own frozen copy of the v27 table and row move, so changes to the live product DDL or repositories never change its result.

#### Scenario: Failing baseline upgrade
- **WHEN** the baseline upgrade fails after moving some rows
- **THEN** the database, its rows and its version stay exactly as before, and a later attempt succeeds.

#### Scenario: Normative schema reference
- **WHEN** a reader needs the exact current schema or the acceptance rule
- **THEN** `docs/schema-reference.md` states the signature and digest rule, how a new database is created and the startup state, and carries the current version, shape digest and DDL generated from `backend/db/schema.py`; the baseline shape is the frozen fixture `tests/fixtures/schema_v26.sql.txt`, and a test fails when the generated reference is out of date

#### Scenario: Normative key catalogue
- **WHEN** a reader needs the keys of the `kv` table
- **THEN** `docs/kv-catalogue.md` lists every key or key pattern with its writers, deleters, export, startup and purge treatment, generated from the SQL writes of `backend/`, and a test fails when it is out of date or a write has a key the scan cannot read

Evidence: `backend/db/migrations.py`, `backend/db/schema_history.py`; `tests/test_db_migrations.py::test_populated_baseline_reaches_the_live_schema_with_its_rows`, `tests/test_db_migrations.py::test_schema_reference_is_current`, `tests/test_db_migrations.py::test_baseline_mirror_rows_become_canonical_products_without_loss`, `tests/test_db_migrations.py::test_v27_product_step_is_frozen_against_live_repository_changes`, `tests/test_db_migrations.py::test_failing_baseline_upgrade_rolls_back_and_a_retry_succeeds`, `tests/test_db_migrations.py::test_upgraded_database_restarts_with_the_same_build_unchanged`, `tests/test_db_migrations.py::test_frozen_baseline_fixture_is_the_recorded_production_shape`, `tests/test_db_migrations.py::test_a_previous_shape_upgrades_in_one_step_with_its_rows`, `tests/test_db_encrypted_upgrade_matrix.py`, `tests/test_key_value_service.py::KeyValueCatalogueTests::test_generated_catalogue_is_current`.

### Requirement: Unknown schema preservation

The database SHALL reject unsupported versions, unknown shapes and mismatched version/shape pairs before modifying the database; a current exact schema SHALL be restart-idempotent.

#### Scenario: Newer or unsupported schema
- **WHEN** a database version is outside the supported released set
- **THEN** opening fails and no reset, deletion or partial upgrade is performed

#### Scenario: Shape does not match version
- **WHEN** the stored version is supported but tables/indexes/shape do not match an accepted release
- **THEN** migration refuses the database unchanged

#### Scenario: Current restart
- **WHEN** the exact current schema is initialized repeatedly
- **THEN** the current version and durable rows remain unchanged by migration

#### Scenario: Check order and messages
- **WHEN** a database is opened
- **THEN** before any write the stored version is checked against the oldest supported one (the retired-schema message below), then against the accepted version set ("Die Datenbankversion wird von diesem Release nicht unterstützt."; this also rejects version 0 on a database that already holds schema objects), then the shape against the shapes accepted for that version ("Die vorhandene Datenbank entspricht keinem unterstützten Schema. Der Datenbestand bleibt erhalten; bitte ein kompatibles Release verwenden."); each failure leaves the file unchanged

#### Scenario: Upgrade validation
- **WHEN** a supported older shape is opened
- **THEN** its steps run in order inside one savepoint of one immediate transaction, the stored version is set to the current one inside it, and a result that is not the exact current schema fails with "Die Datenbankmigration konnte nicht validiert werden." after the savepoint is rolled back

Evidence: `backend/db/migrations.py::migrate_schema`; `backend/db/bootstrap.py::initialize_application_database`; `backend/db/schema.py::database_schema_is_current`. No test pins the two German texts above. Tests: `tests/test_db_bootstrap.py::test_tableless_nonfresh_database_is_rejected_without_mutation`; `tests/test_db_migrations.py::test_older_or_newer_schema_version_is_rejected_without_modification`; `tests/test_db_migrations.py::test_unknown_schema_shape_is_rejected_without_modification`; `tests/test_db_migrations.py::test_version_and_shape_must_belong_together`; `tests/test_db_migrations.py::test_current_schema_is_accepted_repeatedly_without_changes`.

### Requirement: Rollback on failed upgrades

A failing migration SHALL roll back its schema and data changes, preserve the original upgrade error even when a savepoint was lost, and leave the database available for a later valid attempt.

#### Scenario: Upgrade step fails
- **WHEN** a schema/data upgrade raises before current-schema validation succeeds
- **THEN** the enclosing migration changes are rolled back rather than committed partially

#### Scenario: Storage engine loses savepoint
- **WHEN** a storage failure rolls back the outer transaction and removes the savepoint
- **THEN** cleanup does not replace the original migration exception with a savepoint error

Evidence: `backend/db/migrations.py::migrate_schema`; `backend/db/migrations.py::_abandon_migration_savepoint`; `backend/db/schema.py::execute_ddl`. Tests: `tests/test_db_encrypted_upgrade_matrix.py::test_a_failing_upgrade_leaves_the_encrypted_database_unchanged`; `tests/test_db_migrations.py::test_lost_savepoint_does_not_mask_the_original_upgrade_error`.

### Requirement: Owned atomic transactions and consistent reads

The database manager SHALL serialize writers, commit/roll back an owned unit of work atomically, bound reader leases, and offer query-only read snapshots whose reads share one committed WAL state.

#### Scenario: Write fails inside a unit of work
- **WHEN** a domain operation raises before leaving its owned transaction
- **THEN** its partial writes are rolled back before the connection is reused

#### Scenario: Read snapshot composition
- **WHEN** a response composes several queries in one read_snapshot block
- **THEN** the queries observe one committed state and writes through that snapshot are rejected

#### Scenario: Resource contention
- **WHEN** the writer is not free within the wait of the call (20 seconds by default; `0` tries once) or no reader slot is free within 20 seconds
- **THEN** the manager raises the retryable `DatabaseBusyError` naming `writer` or `reader` instead of waiting indefinitely

#### Scenario: Foreground before background
- **WHEN** a foreground and a background unit of work (marked with `background_writer`) wait for the writer
- **THEN** the foreground one is served first, and after eight foreground grants the waiting background one is served, so background work is delayed but never starved; the writer is re-entrant per thread and a held writer is never preempted

#### Scenario: Nested access
- **WHEN** a reader or read snapshot is requested inside a unit of work
- **THEN** it uses the writer connection and sees uncommitted rows; a unit of work requested inside a read snapshot gets the read-only connection, so a write fails

#### Scenario: Write left open on a reader
- **WHEN** a pooled reader connection is returned with an open transaction
- **THEN** the transaction is rolled back before reuse, or the connection is closed

#### Scenario: After-commit callback
- **WHEN** a callback is registered inside a unit of work
- **THEN** it runs once after the outermost commit, is dropped on rollback, and runs at once outside a unit of work; a failing callback is logged without undoing the commit

#### Scenario: Changed key or path
- **WHEN** the database path, APP_PASSWORD or SQLCipher availability changes
- **THEN** `DatabaseHolder` closes the manager after its leased work ends and opens a new one; a password without SQLCipher fails with "SQLCipher ist für eine verschlüsselte Datenbank erforderlich."

Evidence: `backend/db/manager.py::DatabaseManager.unit_of_work`; `backend/db/manager.py::DatabaseManager.after_commit`; `backend/db/manager.py::DatabaseManager.read_snapshot`; `backend/db/manager.py::WriterGate`; `backend/db/manager.py::DatabaseHolder`. Tests: `tests/test_db_manager.py::test_unit_of_work_rolls_back_and_reader_pool_reuses_connections`; `tests/test_db_manager.py::test_unit_of_work_rolls_back_on_base_exceptions_before_reuse`; `tests/test_db_manager.py::test_writer_wait_is_bounded`; `tests/test_db_manager.py::test_unit_of_work_accepts_a_shorter_writer_wait`; `tests/test_db_manager.py::test_foreground_beats_a_waiting_background_writer`; `tests/test_db_manager.py::test_background_writer_is_not_starved_by_foreground_writers`; `tests/test_db_manager.py::test_reader_nested_in_unit_of_work_reuses_writer_and_sees_uncommitted_row`; `tests/test_db_manager.py::test_read_snapshot_is_read_only_and_restores_the_pooled_connection`; `tests/test_db_manager.py::test_reader_rolls_back_a_write_left_open_on_its_connection`; `tests/test_db_manager.py::test_after_commit_runs_only_after_the_outermost_commit`; `tests/test_db_manager.py::test_holder_replacement_waits_for_leased_unit_of_work`; `tests/test_diagnostic_report.py::test_database_counts_and_kv_reads_share_one_read_snapshot`.

### Requirement: Log truncation and secure space reclaim

The write-ahead log SHALL be truncated periodically without waiting on readers. A deletion that asks for it (the local data purge, the GPS track deletion) SHALL overwrite the deleted content and then remove it from the database file and the log.

#### Scenario: Idle checkpoint
- **WHEN** the daily sync loop runs its cycle (every 300 seconds)
- **THEN** a truncating checkpoint runs under the writer with a busy timeout of 0; a failure is logged as `wal_checkpoint_failed` with the error class only and the loop continues, and a refusal by maintenance is not a failure

#### Scenario: Secure deletion
- **WHEN** such a deletion runs
- **THEN** `secure_delete` is on for that unit of work only and its previous value is restored afterwards, also after a failure

#### Scenario: Reclaim
- **WHEN** such a deletion has committed
- **THEN** a checkpoint, a `VACUUM` and a second checkpoint run under the writer outside any transaction; a failure is logged (`privacy_space_reclaim_failed`, `privacy_space_reclaim_incomplete`) and neither undoes the committed deletion nor fails the request, and a reclaim requested inside a transaction raises

Evidence: `backend/db/manager.py::DatabaseManager.checkpoint_wal`; `backend/db/manager.py::secure_deletion`; `backend/db/manager.py::DatabaseManager.reclaim_space`; `backend/sync/scheduler.py::DailySyncLoop`; `backend/privacy.py::PrivacyDeleteService._reclaim_after_commit`. Tests: `tests/test_database_space_reclaim.py::test_checkpoint_truncates_the_wal`; `tests/test_database_space_reclaim.py::test_checkpoint_does_not_wait_on_an_open_reader`; `tests/test_database_space_reclaim.py::test_secure_delete_is_scoped_to_the_unit_of_work`; `tests/test_database_space_reclaim.py::test_secure_delete_is_restored_after_a_failed_unit_of_work`; `tests/test_database_space_reclaim.py::test_reclaim_inside_a_transaction_is_refused`; `tests/test_database_space_reclaim.py::test_purge_removes_the_content_from_the_file_and_the_wal`; `tests/test_database_space_reclaim.py::test_a_failing_vacuum_leaves_the_data_intact_and_the_purge_succeeds`; `tests/test_database_space_reclaim.py::test_an_incomplete_checkpoint_is_logged_without_failing_the_purge`; `tests/test_database_space_reclaim.py::test_a_failing_checkpoint_is_logged_and_never_stops_the_loop`; `tests/test_database_space_reclaim.py::test_maintenance_is_not_a_checkpoint_failure`.

### Requirement: Restore drains active connections

The database manager SHALL block new leases, wait for active operations and close existing connections while a restore swaps the database; later leases SHALL use the resulting active database.

#### Scenario: Restore begins with leased work
- **WHEN** a validated restore requests a drain while database work remains active
- **THEN** the swap waits for active leases and no new lease enters the drained manager

#### Scenario: Restore leaves the drain
- **WHEN** the swap completes or fails and the drain context exits
- **THEN** lease admission resumes without retaining a connection to the replaced file

Evidence: `backend/db/manager.py::DatabaseManager.restore_drain`; `backend/db/manager.py::DatabaseHolder`. Tests: `tests/test_db_manager.py`; `tests/test_database_restore_service.py::test_restores_after_checkpoint_and_drain_then_resumes_both_job_owners`.

### Requirement: Canonical extension upgrade preservation
The extended schema SHALL preserve all athlete records, feedback, snapshots, tombstones, plans, sessions, settings, conversations, usage and sync state. Supported populated upgrades, injected-failure rollback, same-build restart and encrypted backup/restore SHALL be verified from the baseline (the frozen v26 fixture, release 2.0.5) and from a database at the released production schema.

#### Scenario: Populated baseline
- **WHEN** the frozen baseline schema is populated and opened
- **THEN** existing identities, values, relationships and sync markers survive and new canonical structures satisfy their validation rules.

#### Scenario: Upgrade fails partway
- **WHEN** an injected failure interrupts a supported migration
- **THEN** schema and rows return to the unchanged prior state and a later valid attempt remains possible.

#### Scenario: Encrypted restart and backup
- **WHEN** an opened temporary SQLCipher baseline database is restarted and restored through the supported backup workflow
- **THEN** encryption stays enabled and old plus new canonical records and relationships remain available.

#### Scenario: Unknown or newer shape
- **WHEN** a database has an unsupported version, unrecognized shape or mismatched version/shape pair
- **THEN** initialization rejects it without deleting, resetting or partially modifying it.

Evidence: `backend/db/migrations.py`; `tests/test_db_encrypted_upgrade_matrix.py::test_the_encrypted_baseline_reaches_the_live_schema_like_the_plain_one`, `tests/test_db_encrypted_upgrade_matrix.py::test_a_failing_upgrade_leaves_the_encrypted_database_unchanged`, `tests/test_db_encrypted_upgrade_matrix.py::test_the_baseline_restarts_unchanged_and_needs_its_key`, `tests/test_db_encrypted_upgrade_matrix.py::test_an_encrypted_backup_at_the_baseline_restores_the_same_rows`, `tests/test_database_restore_validation.py::test_validate_accepts_a_populated_baseline_backup`.

### Requirement: Retired schema rejection

The database SHALL reject schema versions older than the baseline v26 (releases before 2.0.5) before changing their data or schema, with a message that names release 2.0.5 for the upgrade. Change-history entries written before v16 under the former planning field names are not translated and cannot be undone.

#### Scenario: Opening an older database
- **WHEN** a database from a release with schema v25 or older is opened
- **THEN** startup rejects it with the release 2.0.5 hint and leaves its rows and version unchanged.

Evidence: `backend/db/migrations.py::migrate_schema`, `backend/db/schema_history.py::OLDEST_SUPPORTED_SCHEMA_VERSION`; `tests/test_db_migrations.py::test_older_or_newer_database_file_is_refused_at_startup_byte_identical`, `tests/test_db_migrations.py::test_older_or_newer_schema_version_is_rejected_without_modification`, `tests/test_change_history.py::test_planning_field_names_before_schema_v16_are_not_translated`.

### Requirement: Closed value columns of the current schema

The current schema SHALL check each closed value column to its vocabulary or JSON shape (each nullable), SHALL require the parent detail set of a strength set row, and SHALL serve the backfill-continuation lookup from the partial index `idx_sync_jobs_backfill_end`. Release 2.0.5 upgrades older schemas to these columns with NULL values.

#### Scenario: Value outside the vocabulary
- **WHEN** `activity_feedback.purpose` is written outside training, race, commute, warmup and cooldown, `activity_detail_sets.hr_source` outside chest_strap, optical and unknown, `power_source` outside power_meter, trainer, estimated and unknown, `sensors` as anything but a JSON array, `dfa_alpha1` as anything but a JSON object, `workout_steps` as anything but a JSON array of a laps set, `device_zones` as anything but a JSON object of a streams set, or `activity_laps.workout_step_index` outside 0 to 999 or `lap_trigger` outside 1 to 32 characters
- **THEN** the database rejects the write.

#### Scenario: Orphan strength set and continuation lookup
- **WHEN** a strength set row names no detail set, or the backfill-continuation lookup runs
- **THEN** the insert is rejected, and the lookup's query plan reads `idx_sync_jobs_backfill_end`.

Evidence: `backend/db/schema.py`; `tests/test_db_provider_schema.py::test_closed_value_columns_accept_only_their_values`, `tests/test_strength_sets.py::test_the_schema_requires_the_set_source_and_a_parent_set`, `tests/test_sync_jobs.py::test_backfill_continuation_lookup_reads_its_index_on_the_live_schema`.

### Requirement: Released schemas enter the chain by their digests
A database of a released later schema, v27 (since 2.1.2), v28 (since 2.1.10), v29 (since 2.1.17) or v30 (since 2.1.20), SHALL be recognized by its frozen shape digest and upgraded through the remaining steps to the current schema inside the one migration transaction. The v29, v30 and v31 (#2220) steps SHALL run from frozen copies of their DDL.

#### Scenario: Populated v28 database
- **WHEN** a v28 database with competitions and calendar decisions is opened by this build
- **THEN** it reaches the exact current schema, every original column value of every table is unchanged, and each competition has an empty `competitions.location` (at most 200 characters, never NULL).

#### Scenario: Failing v29 step
- **WHEN** the v29 step fails after its DDL ran
- **THEN** the database keeps version 28, its shape and every row, and a later start upgrades it.

#### Scenario: Restart and backups after the upgrade
- **WHEN** an upgraded database restarts with the same build, or a v28 backup is restored
- **THEN** the restart changes nothing, and the restored backup is upgraded to the current schema with its competitions and an empty location.

#### Scenario: Populated v29 database
- **WHEN** a v29 database with sessions and located competitions is opened by this build
- **THEN** it reaches the exact current schema, every original row is unchanged, and `session_curves` is empty and accepts only a 64-character fingerprint, a JSON object payload and a stored session.

#### Scenario: Failing v30 step
- **WHEN** the v30 step fails after its DDL ran
- **THEN** the database keeps version 29, its shape and every row, and a later start upgrades it.

#### Scenario: Populated v30 database
- **WHEN** a v30 database with sessions, detail sets, laps and a stored curve is opened by this build
- **THEN** it reaches the exact current schema, every original row is unchanged, the new lap columns (`workout_step_index`, `lap_trigger`, `avg_respiration_rate_brpm`, `max_respiration_rate_brpm`, `avg_grade_adjusted_speed_mps`) and detail set columns (`workout_steps`, `device_zones`) are NULL, and each detail set column accepts only its own set kind and JSON type.

#### Scenario: Failing v31 step
- **WHEN** the v31 step fails after its DDL ran
- **THEN** the database keeps version 30, its shape and every row, and a later start upgrades it.

Evidence:
- `backend/db/migrations.py:_competition_location`
- `backend/db/migrations.py:_session_curves`
- `backend/db/migrations.py:_device_workout`
- `backend/db/schema_history.py`
- `tests/test_database_restore_validation.py::test_validate_accepts_a_populated_baseline_backup`
- `tests/test_db_migrations.py::test_encrypted_released_database_upgrades_and_restarts_with_its_rows`
- `tests/test_db_migrations.py::test_failing_step_rolls_back_the_released_database_and_a_retry_succeeds`
- `tests/test_db_migrations.py::test_frozen_v28_shape_is_v27_after_its_decision_step`
- `tests/test_db_migrations.py::test_populated_v28_database_gains_an_empty_location_with_every_row`
- `tests/test_db_migrations.py::test_frozen_v29_shape_is_v28_after_its_location_step`
- `tests/test_db_migrations.py::test_populated_v29_database_gains_an_empty_curve_table_with_every_row`
- `tests/test_db_migrations.py::test_frozen_v30_shape_is_v29_after_its_curve_step`
- `tests/test_db_migrations.py::test_populated_v30_database_gains_empty_watch_workout_columns_with_every_row`
- `tests/test_db_migrations.py::test_upgraded_database_restarts_with_the_same_build_unchanged`
