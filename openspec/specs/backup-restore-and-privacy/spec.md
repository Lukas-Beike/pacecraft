# backup-restore-and-privacy Specification

## Purpose

Provide consistent athlete-controlled data exports, validated encrypted restore and explicit local erasure with truthful outcomes.

Owns export/restore/erasure workflows, not a fresh-database upgrade policy. Production backup contents and credential stores were not inspected. The specifications refer to synthetic existing regression fixtures.

## Requirements

### Requirement: Privacy and backup routes

The privacy export, database backup, deletion preview and GPS state SHALL be authenticated GET routes; the local deletion, GPS setting, GPS track deletion and restore SHALL be authenticated, CSRF-protected POST routes. Their request, response and error contracts are in `docs/api-routes.md`.

#### Scenario: Download files
- **WHEN** the export or backup is requested
- **THEN** the answer is an attachment with `Content-Length` and `Cache-Control: no-store`: `application/zip` named `pacecraft-export.zip`, or `application/octet-stream` named `pacecraft-database.backup`; the browser saves them as `pacecraft-export-<YYYY-MM-DD>.zip` and `pacecraft-database-<YYYY-MM-DD>.backup`

#### Scenario: Typed confirmations and the GPS setting
- **WHEN** the deletion or GPS track deletion lacks its exact phrase (`LOKALE DATEN LÖSCHEN`, `GPS-SPUREN LÖSCHEN`), or the GPS setting body is not a boolean `enabled`
- **THEN** the answer is 400 "Zum Löschen muss <phrase> bestätigt werden." or "enabled muss true oder false sein."

#### Scenario: Unknown privacy path
- **WHEN** another path under `/api/privacy/` is requested
- **THEN** the privacy route modules decline it without resolving a session or a privacy service

Evidence: `backend/http_api/privacy_get.py::PrivacyGetRoutes`; `backend/http_api/privacy_delete_post.py::PrivacyDeletePostRoutes`; `backend/http_api/privacy_restore_post.py::PrivacyRestorePostRoutes`; `backend/http_api/export_streams.py::ExportStreamTransport`; `backend/privacy.py::PrivacyDeleteService.set_gps_tracks`; `public/settings.js` (`downloadDatabaseBackup`, export download). Tests: `tests/test_http_privacy_get_routes.py::test_export_and_backup_use_existing_stream_transport`; `tests/test_http_privacy_get_routes.py::test_unknown_path_does_not_resolve_privacy_services`; `tests/test_http_privacy_delete_post_routes.py::test_valid_request_reads_body_once_and_sends_service_result`; `tests/test_gps_position_privacy.py::test_delete_needs_the_exact_confirmation`; `e2e/downloads.spec.js`.

### Requirement: Consistent encrypted database backup

The database backup service SHALL create a consistent snapshot containing committed WAL data, preserve configured encryption, stream without holding the database writer for the download, and remove temporary snapshots afterward.

#### Scenario: Committed WAL data
- **WHEN** the athlete requests a database backup after a committed change remains in WAL
- **THEN** the snapshot includes the committed change

#### Scenario: Encrypted source
- **WHEN** a password-configured SQLCipher database is backed up
- **THEN** the target snapshot is configured with the same encryption before copying

#### Scenario: Download finishes or fails
- **WHEN** the snapshot streaming context exits
- **THEN** the temporary backup file is removed

Evidence: `backend/backup/database.py::DatabaseBackupService._create_snapshot`; `backend/backup/database.py::DatabaseBackupService._connect_target`; `backend/backup/database.py::DatabaseBackupService.stream_file`. Tests: `tests/test_database_backup.py::test_read_bytes_contains_wal_changes`; `tests/test_database_backup.py::test_stream_uses_complete_snapshot_without_holding_the_writer`; `tests/test_database_backup.py::test_sqlcipher_snapshot_keeps_encrypted_data_and_mapping_rows`.

### Requirement: Bounded export resource use

Database and privacy-archive exports SHALL enforce fixed size, free-space and time bounds (values: `runtime-and-configuration`, Composition limits) and SHALL reject missing database storage rather than create an empty successful backup.

#### Scenario: Insufficient capacity
- **WHEN** the configured export limit or required free-space reserve cannot be met
- **THEN** the export returns a bounded size/storage error and discards temporary files

#### Scenario: Missing source database
- **WHEN** a backup is requested without its source database file
- **THEN** the service returns unavailable storage rather than an empty backup

#### Scenario: Interrupted file transfer
- **WHEN** the client disconnects or the stream deadline is exceeded
- **THEN** streaming stops and its temporary-file context is released

#### Scenario: Backup bounds and answers
- **WHEN** a backup is prepared
- **THEN** a source size (`page_count` times `page_size`) or finished copy above the limit gives 413 "Das Datenbank-Backup überschreitet das Größenlimit."; free space below the copy size plus the reserve gives 507 "Für den Backup-Download ist nicht ausreichend freier Speicher verfügbar."; a missing database file gives 503 "Der Backup-Speicher ist nicht verfügbar."; any other failure (the time limit, a failed integrity check, a foreign-key violation in the copy) gives 503 "Die Datenbank konnte nicht konsistent als Backup vorbereitet werden." and the log event `database_backup_snapshot_failed` with the error class only

#### Scenario: Export bounds and answers
- **WHEN** the privacy archive is prepared
- **THEN** free space below the larger of the reserve and the smaller of the size limit and twice the database file gives 507 "Für den Export ist nicht ausreichend freier Speicher verfügbar."; unreadable storage gives 503 "Der Export-Speicher ist nicht verfügbar."; the time limit gives 408 "Der Export überschreitet das Zeitlimit."; the size limit, checked against the archive written so far after every row and at the end, gives 413 "Der Export überschreitet das Größenlimit."; the unencrypted archive is removed on every failure and interrupt

#### Scenario: Preparation versus sending
- **WHEN** a file is prepared and sent
- **THEN** the 120-second bound covers the preparation only; sending uses 64 KiB chunks and stops after 30 seconds without progress

#### Scenario: Backup under continuous writes
- **WHEN** a writer commits continuously while the backup is copied
- **THEN** the copy completes from its pinned read snapshot and passes the integrity and foreign-key checks

#### Scenario: Database above the limit
- **WHEN** the database file exceeds the backup limit
- **THEN** it can be neither backed up nor restored through the app, and `docs/operations.md` names the fallback (stop the container and copy `/data`)

Evidence: `backend/backup/database.py::DatabaseBackupService._snapshot`; `backend/backup/export.py::write_jsonl_rows`; `backend/http_api/response_transport.py` (`STREAM_CHUNK_BYTES`, `STREAM_IDLE_TIMEOUT_SECONDS`); `backend/backup/database.py::DatabaseBackupService._check_source_capacity`; `backend/backup/database.py::DatabaseBackupService._copy_snapshot`; `backend/backup/export.py::PrivacyArchiveExportService.create_file`; `backend/http_api/response_transport.py::HttpResponseTransport`. Tests: `tests/test_database_backup.py::test_size_and_free_space_limits_reject_backup`; `tests/test_database_backup.py::test_missing_database_does_not_create_an_empty_successful_backup`; `tests/test_server_database.py::test_privacy_archive_export_enforces_free_space_size_timeout_and_cleanup`; `tests/test_server_database.py::test_file_stream_uses_bounded_chunks_and_cleans_up_after_disconnect`; `tests/test_database_backup.py::test_backup_completes_while_a_writer_commits_continuously`; `tests/test_database_backup.py::test_backup_with_a_foreign_key_orphan_fails_and_is_logged`; `tests/test_server_database.py::test_interrupted_privacy_export_leaves_no_plaintext_archive`. The message texts are not all pinned by a test.

### Requirement: Complete secret-filtered privacy exports

The authenticated privacy export SHALL be a bounded ZIP of JSON and JSON Lines members that holds every durable table except login sessions and the planning-state revision counter; approval proposals are exported without their session and action-token hashes, and every key/value row except the credential, token and OAuth-state keys and the transient running/status flags.

#### Scenario: Privacy archive requested
- **WHEN** the authenticated athlete requests the archive export
- **THEN** the archive includes its complete declared manifest and streams the durable collections

#### Scenario: Data kinds added since release 1.13.40
- **WHEN** the archive is exported
- **THEN** it holds the derived session-load and session-metric observations, the Garmin RPE, feel and minimum-heart-rate ratings, the Garmin and FIT detail sets with the executed strength sets and their source, the heart-rate histogram and zone times of each session analysis, the remote links of every provider and entity kind, the field names of nutrition-entry changes, the backfill cursors, the athlete's session purpose marking, the profile sex and the consent, purge and stage state

#### Scenario: Credential-bearing runtime state
- **WHEN** stored application state contains sessions, provider credentials, the Personal API key or Garmin or Wahoo tokens
- **THEN** those values are excluded from the archive, and the non-secret Personal API key expiry date appears only in `provider_settings.json`

#### Scenario: Provider settings and format version
- **WHEN** the archive is exported
- **THEN** the stored non-secret provider fields (Personal API key expiry date, Intervals.icu athlete id, Wahoo client id, OpenAI base URL) are in `provider_settings.json`, or the file is `{}` without stored credentials, and the manifest names `PRIVACY_EXPORT_FORMAT_VERSION`, which follows the schema version

#### Scenario: Members and manifest
- **WHEN** the archive is exported
- **THEN** it is a deflate-compressed ZIP of compact UTF-8 JSON documents (`profile.json`, `application_state.json`, `provider_settings.json`, `planning.json` for the athlete's local date, `garmin_snapshot.json`, `weather_cache.json`) and one JSON Lines file per table of `PRIVACY_EXPORT_JSONL_SOURCES`, all read from one transaction without blocking the writer; an empty table gives an empty file and a JSON column whose stored text is not valid JSON is exported as `{}`

#### Scenario: Manifest content
- **WHEN** the archive is complete
- **THEN** `manifest.json` is written last and holds `format` "intervals-coach-privacy-export", `format_version`, `exported_at` (UTC), `status` "complete", `categories` (the sorted member names without extension, manifest excluded) and `jsonl_files` (the sorted JSON Lines names)

#### Scenario: Key/value state in the archive
- **WHEN** `application_state.json` is built
- **THEN** it holds every `kv` row except `profile`, the Garmin state and snapshot keys and the weather cache (exported as their own members), `CREDENTIAL_KV_KEYS` and every key ending in `_running` or `_status`; a value that is valid JSON is exported decoded and any other value as its text (key-by-key table: `docs/kv-catalogue.md`)

#### Scenario: Capture and conversation id in the archive
- **WHEN** the archive is exported
- **THEN** `application_state.json` also holds `diagnostic_capture_entries` (the redacted technical capture, up to 2 MiB, which can include sanitized Coach request bodies) and `openai_conversation_id`, because neither is an excluded key

#### Scenario: Unauthenticated download
- **WHEN** a privacy download is requested without a valid session
- **THEN** authentication fails before export streaming starts

Evidence: `backend/backup/export.py::PrivacyArchiveExportService`; `backend/backup/export.py::PRIVACY_EXPORT_JSONL_SOURCES`; `backend/backup/export.py::application_state`; `backend/backup/export.py::manifest`; `docs/kv-catalogue.md`; `backend/backup/export.py::PRIVACY_EXPORT_JSONL_FILES`; `backend/http_api/privacy_get.py`. Only the archive is served. `backend/provider_credentials.py::stored_visible_settings` selects the exported provider fields. Tests: `tests/test_data_safety_coverage.py::DataKindExportTests`; `tests/test_privacy_provider_data.py::test_stored_non_secret_provider_fields_are_exported_without_secrets`; `tests/test_server_database.py::test_privacy_export_zip_streams_collections_and_contains_complete_manifest`; `tests/test_privacy_provider_data.py::test_provider_settings_are_empty_without_stored_credentials`; `tests/test_privacy_provider_data.py::test_empty_provider_tables_export_empty_files`; `tests/test_data_safety_coverage.py::test_every_export_member_is_decided_and_present_in_a_real_archive`; `tests/test_data_safety_coverage.py::test_the_listed_kv_families_are_exported_or_excluded_with_a_reason`; `tests/test_data_safety_coverage.py::test_export_format_version_follows_the_schema_version`; `tests/test_server_database.py::test_privacy_export_compression_does_not_block_a_consistent_snapshot_writer`; `tests/test_server_database.py::test_privacy_download_routes_stream_through_the_transport`. No test pins the capture and conversation id in the archive or the `{}` of an invalid stored payload.

### Requirement: Provider payloads are sanitized on export

Stored provider payloads and detail documents SHALL be exported decoded and sanitized: every object key that looks like a credential is removed at any depth and every signed download URL is replaced by the marker `[redacted]`.

#### Scenario: Credential-like key
- **WHEN** a stored provider payload holds an object key whose lower-cased name contains `token`, `secret`, `password`, `credential`, `authorization`, `cookie`, `api_key` or `apikey`, at any depth
- **THEN** the exported row omits that key with its value and keeps the other keys

#### Scenario: Signed download URL
- **WHEN** a stored string contains `http` and one of `signature=`, `x-amz-signature` or `token=` (case-insensitive)
- **THEN** the exported value is `[redacted]`; plain text, numbers and unsigned URLs are exported unchanged

#### Scenario: Columns the rule covers
- **WHEN** the archive is exported
- **THEN** the rule applies to `provider_records.payload` and to the `details` column of the activity record, observation, sleep period, categorical observation, gear item, power curve point, power curve fetch and zone configuration files

Evidence: `backend/observability.py::sanitized_provider_payload`; `backend/backup/export.py::sanitized_provider_row`, `backend/backup/export.py::PRIVACY_EXPORT_JSONL_SOURCES`; the canonical Garmin ingest applies the same function (`backend/db/garmin_store.py`). Tests: `tests/test_privacy_provider_data.py::test_credential_like_keys_are_removed_at_every_depth`; `tests/test_privacy_provider_data.py::test_signed_download_urls_are_redacted`; `tests/test_privacy_provider_data.py::test_plain_text_and_unsigned_urls_pass_through`; `tests/test_privacy_provider_data.py::test_provider_rows_are_exported_with_sanitized_payloads`; `tests/test_privacy_provider_data.py::test_domain_rows_are_exported_with_decoded_details_and_limits`.

### Requirement: Coverage guards for stored data kinds

Every table in the current schema SHALL have a recorded export decision and a place in the erasure scope, every explicitly selected export column SHALL be exported or listed with its reason, and a schema change SHALL come with a decision on the export format version, so a new data kind cannot reach the database without a backup, export and erasure decision.

#### Scenario: New table without a decision
- **WHEN** a table is added to the schema without an export member, an exclusion reason or an erasure scope entry
- **THEN** the coverage tests fail

#### Scenario: New column of an exported table
- **WHEN** a column is added to a table whose export lists its columns
- **THEN** the coverage tests fail until the column is exported or listed as excluded

Evidence: `backend/backup/export.py::PRIVACY_EXPORT_FORMAT_VERSION`; `backend/privacy.py::PRIVACY_DELETE_SCOPE`. Tests: `tests/test_data_safety_coverage.py::CoverageGuardTests`; `tests/test_server_database.py::test_privacy_delete_preview_covers_every_durable_table_and_reports_counts`.

### Requirement: GPS positions stay in storage

Stored GPS position channels SHALL be part of the database backup, the privacy export and the local data purge, and SHALL NOT enter any read model, Coach context, diagnostics report, log line or structured log field. The athlete SHALL be able to turn off the storage of new tracks (default on); turning it off SHALL NOT delete stored tracks, which only a confirmed request with the exact phrase `GPS-SPUREN LÖSCHEN` removes.

#### Scenario: Positions in a stored stream set
- **WHEN** a stream set with latitude and longitude channels is merged, served as activity detail or summarized as coverage
- **THEN** no position channel and no coordinate value appears in the result

#### Scenario: Storage turned off
- **WHEN** the athlete turned "GPS-Spuren speichern" off and a file with positions is fetched
- **THEN** the stream set is stored without position channels and its content hash equals that of the stripped set

#### Scenario: Confirmed track deletion
- **WHEN** the athlete confirms `GPS-SPUREN LÖSCHEN`
- **THEN** every position channel is deleted, the other channels stay, the route index and its names are deleted and any other phrase is rejected

#### Scenario: Storage turned off with routes
- **WHEN** the athlete turns "GPS-Spuren speichern" off
- **THEN** stored tracks, the route index and the route names stay until the confirmed deletion

#### Scenario: Route read models
- **WHEN** a route is read for the activity view, the Coach, diagnostics or logs
- **THEN** only a label, a repeat count and an opaque route id appear

Evidence: `backend/canonical/vocabulary.py::POSITION_CHANNELS`; `backend/db/activity_detail_store.py::replace_detail_set`; `backend/privacy.py::PrivacyDeleteService`, `backend/activities/route_store.py::clear_routes`. Tests: `tests/test_gps_position_privacy.py`, `tests/test_activity_routes.py::PrivacyDeleteTests`, `tests/test_activity_routes.py::PrivacySweepTests`.

### Requirement: Secrets stay out of exports, reports and logs

The privacy export, the diagnostics report and the log output SHALL NOT contain the Personal API key, provider API keys, Garmin or Wahoo tokens, session hashes or APP_PASSWORD, and the database backup SHALL keep stored credentials only inside the encrypted database.

#### Scenario: Sentinel secrets in storage
- **WHEN** distinctive secrets are placed in temporary configuration and credential storage
- **THEN** none appears in the archive, the JSON projection, the diagnostics report, its log projection or a log line, and the only key/value rows holding one are the credential keys

#### Scenario: Encrypted backup
- **WHEN** the database is backed up with SQLCipher
- **THEN** the backup bytes show neither a plain SQLite header nor a stored secret

Evidence: `backend/provider_credentials.py::CREDENTIAL_KV_KEYS`; `backend/observability.py::Redactor`; `backend/diagnostics/report.py::DiagnosticReportService`. Tests: `tests/test_secret_exposure.py`; `tests/test_diagnostic_report.py::DiagnosticReportServiceTests::test_report_shape_counts_redaction_and_privacy`.

### Requirement: Stage and validate restore before promotion

Restore SHALL stage bounded input to a unique temporary file, migrate supported backup schemas, verify exact schema/integrity/foreign keys with the configured encryption, and leave the active database untouched on staging or validation failure.

#### Scenario: Supported older encrypted backup
- **WHEN** a staged backup has a supported released schema and decrypts under the configured key
- **THEN** it is migrated and validated in staging before promotion

#### Scenario: Malformed or invalid backup
- **WHEN** schema, integrity, foreign keys, encryption or body bounds fail validation
- **THEN** restore fails and the active database is not replaced

#### Scenario: Slow upload
- **WHEN** backup chunks are still arriving
- **THEN** staging proceeds before taking the maintenance gate

#### Scenario: Staging
- **WHEN** an upload is staged
- **THEN** it is written chunk by chunk to a new `.intervals-coach-restore-<32 hex>.db` file in the data directory; an empty upload or one above 1,000,000,000 bytes gives 413 "Das Datenbank-Backup ist leer oder zu groß."; a full disk gives 507 (reason `restore_storage_full`) and another storage failure 500 (reason `restore_storage_failed`), without OS text or paths

#### Scenario: Validation order
- **WHEN** the staged file is validated under the maintenance gate
- **THEN** the checks run in this order and any failure rolls the staged changes back: SQLCipher available (else 503 "SQLCipher ist für die Wiederherstellung nicht verfügbar."), key applied, foreign keys on, schema upgrade (an older schema than the baseline gives 400 with the release hint, any other unsupported one 400 "Das Backup entspricht keinem unterstützten Datenbankschema."), exact current schema (400 "Das Backup entspricht nicht exakt dem aktuellen Datenbankschema."), foreign-key check (400 "Das Backup enthält ungültige Fremdschlüssel."), integrity result (400 "Die Integritätsprüfung des Backups ist fehlgeschlagen."), startup state, deletion of every session, commit; any other error gives 400 "Das Datenbank-Backup konnte nicht validiert werden: " followed by the redacted error text cut to 300 characters

#### Scenario: Restored database lacks startup rows
- **WHEN** a valid backup is restored
- **THEN** before it becomes the live database it holds the planning state row, a profile and reset provider resync flags (the startup state of `docs/schema-reference.md`)

Evidence: `backend/backup/restore_validation.py::DatabaseRestoreValidationService.stage`; `backend/backup/restore_validation.py::DatabaseRestoreValidationService.validate`; `backend/backup/restore.py::DatabaseRestoreService.restore`. Tests: `tests/test_data_safety_coverage.py::BackupRestoreRoundTripTests`; `tests/test_database_restore_validation.py::test_stage_streams_chunks_and_enforces_the_limit_while_writing`; `tests/test_database_restore_validation.py::test_validate_accepts_a_populated_baseline_backup`; `tests/test_database_restore_validation.py::test_validate_rejects_foreign_key_violations`; `tests/test_database_restore_service.py::test_invalid_backup_never_changes_live_database_and_cleans_stage`; `tests/test_database_restore_service.py::test_full_disk_during_staging_reports_507_not_an_invalid_backup`; `tests/test_database_restore_service.py::test_unexpected_validation_error_is_redacted_without_mutation`; `tests/test_database_restore_validation.py::test_validate_applies_startup_invariants_to_the_restored_database`; `tests/test_database_restore_validation.py::test_validate_requires_sqlcipher_for_encrypted_restore`; `tests/test_database_restore_validation.py::test_validate_rejects_failed_integrity_check_before_scrubbing`.

### Requirement: Authenticated session-scrubbing restore

The restore endpoint SHALL require authentication and CSRF, delete sessions from a validated staged backup, preserve a unique pre-restore recovery copy when an active file exists, swap under maintenance/drain, and clear the response cookies on success.

#### Scenario: Successful restore
- **WHEN** an authorized backup passes validation and promotion succeeds
- **THEN** backup sessions are absent, the previous active file has a distinct recovery copy, and the athlete must authenticate again

#### Scenario: Repeated restores
- **WHEN** two valid backups are restored in quick succession
- **THEN** their pre-restore recovery-copy names are distinct, and after the second only its copy remains (see "Recovery copies and purge markers")

#### Scenario: Gate refuses promotion
- **WHEN** another maintenance restore already owns the gate
- **THEN** the staged file is cleaned up without promoting it

#### Scenario: Gate before validation
- **WHEN** the restore takes the maintenance gate
- **THEN** a running Coach turn is cancelled first, another running restore gives 409 "Eine Datenbankwiederherstellung läuft bereits.", and work still running after 20 seconds gives 409 `maintenance_drain_timeout` with nothing changed (`runtime-and-configuration`)

#### Scenario: Swap
- **WHEN** a validated file replaces the live one
- **THEN** a truncating checkpoint of the live database runs (a failure is logged as `database_restore_checkpoint_failed` and does not stop the restore), connections drain, free space is checked for the recovery copy (507, reason `restore_storage_full`), the live file is copied to `<database name>.pre-restore-<UTC YYYYmmddHHMMSS>-<32 hex>` (a partial copy is deleted), the live `-wal` and `-shm` files are renamed aside, the staged file replaces the live one atomically and the parked files are deleted; a failed replace renames them back, and another storage failure gives 500 (reason `restore_storage_failed`)

#### Scenario: No recovery copy without a live file
- **WHEN** no live database file exists
- **THEN** the restore installs the backup and answers `{"status": "ok", "restored": true, "previous_database_backup": null}`; otherwise the field names the recovery copy

#### Scenario: Old WAL frames
- **WHEN** the live database has `-wal` or `-shm` files at the swap
- **THEN** they never stay beside the restored file, and a `.restore-aside` file left by a kill during the swap is inert and not removed by the startup sweep

Evidence: `backend/http_api/privacy_restore_post.py::PrivacyRestorePostRoutes.routes`; `backend/backup/restore_validation.py::DatabaseRestoreValidationService.validate`; `backend/backup/restore.py::DatabaseRestoreService._replace`. Tests: `tests/test_http_privacy_restore_post.py::test_success_reads_body_restores_and_clears_cookies`; `tests/test_database_restore_validation.py::test_validate_commits_session_scrubbing_after_schema_integrity_and_fk_checks`; `tests/test_database_restore_service.py::test_two_restores_keep_only_the_newest_recovery_copy`; `tests/test_database_restore_service.py::test_restore_returns_409_within_the_bound_and_keeps_the_database`; `tests/test_database_restore_service.py::test_restore_cancels_a_running_coach_turn_before_draining`; `tests/test_database_restore_service.py::test_restore_continues_when_damaged_database_checkpoint_fails`; `tests/test_database_restore_service.py::test_full_disk_during_recovery_copy_reports_507_without_partial_copy`; `tests/test_database_restore_service.py::test_refused_swap_puts_the_live_sidecars_back`; `tests/test_database_restore_service.py::test_swap_failure_preserves_previous_database_and_releases_gate`. No test pins the `.restore-aside` leftover or the no-live-file result.

### Requirement: Recovery copies and purge markers

Restore SHALL keep the live database as a recovery copy beside the new one, and after a complete restore whose new copy is verified SHALL delete the older restore copies so that only the newest remains (athlete, 2026-11-14, #2914). Purge copies and marker files of release 2.0.0 stay until the confirmed local erasure removes them.

#### Scenario: Names
- **WHEN** the data directory is scanned for recovery copies
- **THEN** regular files named `<database name>.pre-restore-<14 digits>-<32 hex>` or `<database name>.pre-intervals-purge-v<N>-<14 digits>-<32 hex>`, and marker files named `intervals-purge-v<N>.done`, match; symbolic links and other names never match and are never followed or removed

#### Scenario: Many restores
- **WHEN** a restore completes and its new recovery copy has the size of the database it copied, opens read-only with the key and has the current schema
- **THEN** the `pre-restore` copies whose name carries an earlier time are deleted, so the newest remains; a copy of the same second or later (an overlapping restore), `pre-intervals-purge` copies, marker files and any other file are never touched, and a deletion that fails is logged without failing the finished restore

#### Scenario: Restore or copy not confirmed
- **WHEN** the restore fails at any step, including the resume after the swap, its new copy cannot be verified, or the live database had a non-empty `-wal` file when it was copied (the copy may miss the last commits)
- **THEN** every existing recovery copy stays

#### Scenario: Erasure and report
- **WHEN** the local erasure runs or the diagnostic report is built
- **THEN** the erasure counts and removes the matching files and reports the failures, and the report shows only their count, newest age and total size

Evidence: `backend/backup/recovery_copies.py::RecoveryCopies`, `backend/backup/recovery_copies.py::RecoveryCopies.prune_restore_copies`; `backend/backup/restore.py::DatabaseRestoreService._copy_previous`, `backend/backup/restore.py::DatabaseRestoreService._prune_older_copies`; `backend/backup/restore_validation.py::DatabaseRestoreValidationService.verify_copy`. Tests: `tests/test_database_restore_service.py::test_two_restores_keep_only_the_newest_recovery_copy`; `tests/test_database_restore_service.py::test_an_unverified_or_failed_restore_keeps_every_recovery_copy`; `tests/test_database_restore_service.py::test_a_copy_taken_beside_an_unflushed_wal_keeps_the_older_copies`; `tests/test_recovery_copies.py::test_pruning_keeps_the_newest_restore_copy_and_every_other_file`; `tests/test_database_restore_validation.py::test_verify_copy_needs_the_whole_file_and_the_current_schema`; `tests/test_db_encrypted_upgrade_matrix.py::test_an_encrypted_backup_at_the_baseline_restores_the_same_rows`; `tests/test_privacy_provider_data.py::test_the_preview_counts_recovery_copies_and_the_delete_erases_them`; `tests/test_privacy_provider_data.py::test_a_recovery_copy_that_cannot_be_removed_is_reported_not_raised`; `tests/test_diagnostic_report.py::test_storage_and_recovery_copies_are_sizes_and_ages_only`.

### Requirement: Truthful post-restore recovery

After replacing the database, restore SHALL refresh credential-derived configuration, resume interrupted sync and Coach jobs, restart canonical backfill and wake workers; resume failures SHALL be reported as restored_resume_failed rather than as an untouched invalid backup.

#### Scenario: Recovered durable work
- **WHEN** promotion completes and recovery dependencies succeed
- **THEN** restored jobs resume against restored provider configuration and both workers are signaled

#### Scenario: Resume fails after swap
- **WHEN** worker recovery raises after the active database has been replaced
- **THEN** the API reports restored_resume_failed and advises restart while worker wake signals still run

Evidence: `backend/backup/restore.py::DatabaseRestoreService._resume`; `backend/backup/restore.py::DatabaseRestoreService.restore`. Tests: `tests/test_database_restore_service.py::test_restore_restarts_the_canonical_backfill_worker_once_after_the_swap`; `tests/test_database_restore_service.py::test_resume_failure_after_replace_reports_restored_and_still_wakes_workers`; `tests/test_database_restore_service.py::test_coach_resume_failure_after_replace_still_wakes_both_workers`.

### Requirement: Explicit scoped local erasure

Local privacy deletion SHALL require the exact backend confirmation text before dependency access, expose a count preview covering durable deletion categories, and delete the declared local scope in one maintenance-gated transaction.

#### Scenario: Wrong deletion confirmation
- **WHEN** the request does not contain the exact required confirmation text
- **THEN** the service fails before touching deletion dependencies

#### Scenario: Confirmed erasure
- **WHEN** the exact confirmation is supplied
- **THEN** the declared durable tables and KV state, including the Personal API key, its expiry date, the consent and purge state and the profile sex, are cleared transactionally and the default profile/reset state is restored; a switched-off "GPS-Spuren speichern" setting stays off

#### Scenario: Remote links in the preview
- **WHEN** the preview counts the deletion scope
- **THEN** remote links are counted under the group owning their entity kind (library, competitions, or provider data for metric-export observations), so the counts add up to every row the deletion removes

#### Scenario: Files beside the database
- **WHEN** local erasure completes
- **THEN** the legacy Garmin token file and the `.env` file stay, and recovery copies and purge marker files are deleted (next scenario)

#### Scenario: Delete fails midway
- **WHEN** a table deletion fails within the local transaction
- **THEN** earlier table deletes roll back rather than reporting successful erasure

#### Scenario: Recovery copies beside the database
- **WHEN** restores or the Intervals.icu purge of release 2.0.0 saved database recovery copies and a purge marker file in the data directory
- **THEN** the preview counts them, the confirmed erasure deletes them after the transaction commits, and a copy that cannot be removed is reported without failing the erasure

#### Scenario: Server log files
- **WHEN** the server log files hold entries
- **THEN** the preview counts them as their own category, the confirmed erasure deletes the rotated files and empties the active one as its last step, and a file that cannot be removed is reported (`log_files_failed`) without failing the erasure

#### Scenario: Preview payload
- **WHEN** the preview is requested
- **THEN** it holds `status` "preview", `confirmation_text`, `categories` (`id`, `label`, `records`: the categories of `PRIVACY_DELETE_SCOPE` in order, then `recovery_copies`, then `server_logs`), `remote_untouched` (German sentences on Intervals.icu, Garmin, Wahoo and the external calendar) and `openai_conversation` (German text)

#### Scenario: Order of the steps
- **WHEN** the exact confirmation is supplied and the maintenance gate is taken, which cancels a running Coach turn first
- **THEN** the steps run in this order: ask OpenAI to delete the stored conversation, cancel a pending Garmin login, disconnect Wahoo and revoke its grant, one transaction with secure deletion that deletes every row of every table of the scope and the whole key/value store and then writes `garmin_tokenstore_state`, the default profile, the GPS setting `0` only when it was already switched off (absent otherwise, which means on) and the planning reset mark, then after the commit the space reclaim, the recovery copies and markers, the credential refresh and last the log files

#### Scenario: Local transaction fails after the remote steps
- **WHEN** the local transaction fails after the OpenAI deletion and the Wahoo revoke ran
- **THEN** the remote steps are not undone, the request ends with an error, local data (including the stored conversation id and tokens) is unchanged, and the athlete repeats the erasure (athlete decision 2026-11-10)

#### Scenario: Result payload
- **WHEN** the erasure completes
- **THEN** it answers `status` "ok", `local_data_deleted` true, `deleted_categories` (count per category, recovery copies and server logs included), `recovery_copies_failed`, `log_files_failed`, `remote_delete_attempted`, `remote_conversation_deleted`, `wahoo_revoke_attempted`, `wahoo_revoked` and `remote_untouched`

#### Scenario: Login sessions
- **WHEN** the erasure commits
- **THEN** the stored login sessions are deleted with the `sessions` category

Evidence: `backend/privacy.py::PrivacyDeleteService.preview`; `backend/privacy.py::PrivacyDeleteService.delete`; `backend/privacy.py::PRIVACY_DELETE_SCOPE`; `backend/backup/recovery_copies.py::RecoveryCopies`; `backend/diagnostics/logs.py::RecentLogEntriesService.clear`. Tests: `tests/test_data_safety_coverage.py::DataKindDeleteTests`; `tests/test_privacy_provider_data.py::test_preview_counts_remote_links_by_owning_group`; `tests/test_privacy_provider_data.py::test_the_preview_counts_recovery_copies_and_the_delete_erases_them`; `tests/test_privacy_provider_data.py::test_delete_clears_every_garmin_capability_pause`; `tests/test_privacy_delete_confirmation.py::test_missing_or_wrong_confirmation_fails_before_any_dependency_access`; `tests/test_server_database.py::test_privacy_delete_preview_covers_every_durable_table_and_reports_counts`; `tests/test_server_database.py::test_privacy_delete_rolls_back_earlier_table_deletes_on_sql_failure`.

### Requirement: Remote-erasure and credential outcomes remain separate

Privacy deletion SHALL report local erasure separately from its best-effort OpenAI conversation deletion, preserve the declared untouched remote-provider scope, cancel pending Garmin login and mark legacy Garmin token state disconnected to prevent silent re-import.

#### Scenario: Remote conversation deletion fails
- **WHEN** an existing OpenAI conversation cannot be deleted remotely
- **THEN** local erasure continues and the result reports the remote attempt and failure separately

#### Scenario: Pending Garmin login exists
- **WHEN** local erasure runs while a Garmin login may still complete
- **THEN** the pending login is cancelled and the legacy token state is marked disconnected

#### Scenario: Diagnostic cache after erasure
- **WHEN** the erasure transaction clears diagnostic state
- **THEN** pre-erasure capture caches cannot repopulate the deleted entries

#### Scenario: Wahoo grant
- **WHEN** a Wahoo grant is stored
- **THEN** it is revoked at Wahoo before the tokens are wiped; a failing or raising revoke leaves the erasure complete and is reported as `wahoo_revoke_attempted` true and `wahoo_revoked` false; without a grant no revoke is attempted

Evidence: `backend/privacy.py::PrivacyDeleteService.delete`; `backend/privacy.py::PRIVACY_REMOTE_SCOPE`; `backend/provider_credentials.py::GARMIN_TOKENSTORE_DISCONNECTED`. Tests: `tests/test_server_database.py::test_privacy_delete_reports_remote_attempt_and_failure`; `tests/test_privacy_delete_confirmation.py::test_deleted_diagnostic_entries_do_not_return_or_resurrect_after_delete`; `tests/test_privacy_provider_data.py::test_delete_revokes_the_wahoo_grant_before_wiping_the_tokens`; `tests/test_privacy_provider_data.py::test_a_failed_wahoo_revoke_is_reported_and_the_purge_completes`; `tests/test_privacy_provider_data.py::test_a_crashing_wahoo_disconnect_does_not_block_the_purge`; `tests/test_privacy_provider_data.py::test_a_purge_without_a_wahoo_grant_attempts_no_revoke`. No test pins the failure after the remote steps or the order of the steps.

### Requirement: Retired backup schema rejection

Restore SHALL reject backups with schema versions older than the baseline v26 (releases before 2.0.5) without changing the backup or the active database, with a message that names release 2.0.5 for the upgrade.

#### Scenario: Older backup is selected
- **WHEN** a backup with schema v25 or older is staged for restore
- **THEN** validation fails with the release hint, the staged backup keeps its rows and version, and the active database remains unchanged.

Evidence: `backend/backup/restore_validation.py`; `tests/test_database_restore_validation.py::test_validate_rejects_a_pre_baseline_backup_by_release_without_modifying_it`.

