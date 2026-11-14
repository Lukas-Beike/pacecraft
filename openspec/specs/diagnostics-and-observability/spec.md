# diagnostics-and-observability Specification

## Purpose

Expose useful local technical evidence while redacting secrets, bounding captures and keeping diagnostics distinct from athlete dialogue.

Owns logs, technical capture and local reports. Sanitized Coach request/response bodies can be retained by diagnostics; this is not a blanket body-free logging promise. Credentials, tokens, sessions and raw secret-bearing error values remain excluded/redacted by the implemented sanitizer.

## Requirements

### Requirement: Structured secret redaction

Structured log formatting and diagnostic projections SHALL redact configured secrets, common credential forms, nested/encoded variants and sensitive URL/query values before exposed logging or diagnostic output.


#### Scenario: Nested credential in an error context
- **WHEN** a log/diagnostic value contains a configured secret or recognized credential field
- **THEN** the exposed projection masks it, including nested and supported encoded representations

#### Scenario: Sensitive request query
- **WHEN** an access-log path includes free-text query values
- **THEN** logging preserves safe path context while masking the sensitive query values

#### Scenario: Credential configuration changes
- **WHEN** a configured credential is replaced
- **THEN** redaction refreshes its prepared patterns for current credential values

#### Scenario: Stored provider tokens
- **WHEN** a Garmin or Wahoo token is stored, or a Wahoo login state is pending
- **THEN** the redactor masks it in log output through the provider secret registry

#### Scenario: Known secrets
- **WHEN** a value holds a configured secret (the values of `config_secret_values`: API keys, account names and passwords, token store and fixture path, APP_PASSWORD, the Wahoo client secret) or a runtime secret (stored Garmin and Wahoo tokens, a pending Wahoo login state, nutrition provider secrets)
- **THEN** it becomes `[REDACTED]`, matched case-insensitively in its raw form and after up to two rounds of URL encoding and decoding, when it has at least 4 characters; in structured values the base64 form and the JSON-escaped form of the configured keys, accounts, passwords, APP_PASSWORD, Wahoo client secret and calendar URL are replaced too

#### Scenario: URLs in text
- **WHEN** text holds an `http` or `https` URL
- **THEN** its user info is removed, a path segment of 32 or more characters (or 16 or more with two character classes and eight distinct characters) becomes `[REDACTED_PATH]`, the value of a query key named `access_token`, `api_key`, `apikey`, `auth`, `authorization`, `client_secret`, `code`, `credential`, `key`, `password`, `refresh_token`, `secret`, `signature`, `sig`, `state` or `token` becomes `[REDACTED]` and the fragment is dropped; the configured calendar URL becomes `<scheme>://<host>/redacted`

#### Scenario: Key patterns in text
- **WHEN** text holds `sk-` plus 8 or more key characters, a three-part `eyJ` token, `AIza` plus 20 or more characters, or `authorization: basic|bearer <value>`
- **THEN** they become `[REDACTED_OPENAI_KEY]`, `[REDACTED_TOKEN]` and `[REDACTED_GEMINI_KEY]`, and the label of the last is kept with `[REDACTED]`

#### Scenario: Structured keys and values
- **WHEN** a structured value is cleaned (log lines, captured details, diagnostic responses)
- **THEN** a value under the key `key`, `credentials`, `session`, `sessionid`, `sessionhash`, `sessionkey`, `sessionkeyhash`, `signature`, `oauth1`, `oauth2`, `csrftoken` or `csrf`, or under a key ending in `password`, `passwd`, `passphrase`, `passwordhash`, `secret`, `token`, `apikey`, `privatekey`, `databasekey`, `encryptionkey`, `credential`, `authorization`, `cookie`, `csrfhash`, `sessionid` or `sessionhash` (lower-cased, non-alphanumerics removed), becomes `[REDACTED]`; `inlinedata`, `filedata`, `audio` and `inputaudio` become `[OMITTED_BINARY]`, bytes `{"omitted_binary_bytes": n}`; a string that parses as a JSON object or array is cleaned as structure and nesting deeper than 40 becomes `{"truncated": true, "reason": "depth_limit"}`

#### Scenario: Structured text
- **WHEN** a string in a structured value holds `bearer` or `basic` plus a value, a PEM private key block, a `data:` URL, or a label `password`, `passwd`, `secret`, `api_key`, `token`, `authorization` or `cookie` followed by `:` or `=`
- **THEN** they become `[REDACTED_AUTHORIZATION]`, `[REDACTED_PRIVATE_KEY]`, `[OMITTED_BINARY]` and `<label>=[REDACTED]` (the rest up to the next comma, semicolon or line end)

#### Scenario: Request targets in the access log
- **WHEN** a request target carries query values
- **THEN** a value is kept only for the keys `client_turn_id`, `cursor`, `date`, `days`, `end`, `limit`, `local`, `since`, `start` and `v` and only when it matches `[A-Za-z0-9._:-]{0,128}`; every other value and every credential-named key (an OAuth callback's `code` and `state`) is masked as `[REDACTED]`

#### Scenario: Provider transport metadata
- **WHEN** a provider response or path is captured
- **THEN** only the headers `content-type`, `content-length`, `date`, `retry-after`, `server` and `x-ratelimit-*` are kept, redacted and cut to 160 characters, and a provider path keeps only the segments `api`, `v<digits>` and lower-case words of at most 32 characters, with the segment after `athlete`, `activities`, `activity`, `event`, `events`, `profile`, `user`, `workout` or `workouts` replaced by `[REDACTED_PATH]`

Evidence: `backend/observability.py::Redactor`; `backend/sync/wahoo_connection.py::WahooSecretRegistry`; `backend/observability.py::JsonLogFormatter`; `backend/observability.py::redact_request_query`. Tests: `tests/test_observability.py::test_redacts_nested_values_and_secret_variants`; `tests/test_observability.py::test_redacts_url_credentials_query_and_unguessable_path`; `tests/test_observability.py::test_formatter_redacts_traceback_and_context`; `tests/test_observability.py::test_access_log_masks_free_text_query_on_success_and_error_paths`; `tests/test_secret_exposure.py::DiagnosticsAndLogSecretTests::test_wahoo_tokens_are_redacted_from_logs`; `tests/test_wahoo_connection.py::WahooConnectionServiceTests::test_the_redactor_registry_follows_tokens_and_login_state`; `tests/test_observability.py::test_redacts_provider_key_patterns_and_authorization`; `tests/test_observability.py::test_log_values_get_the_diagnostic_key_label_and_auth_rules`; `tests/test_observability.py::test_registered_runtime_tokens_are_redacted_from_log_lines`; `tests/test_observability.py::test_safe_response_headers_keeps_only_allowlisted_transport_headers`; `tests/test_observability.py::test_safe_provider_path_redacts_resource_ids`; `tests/test_secret_exposure.py::test_every_secret_looking_configuration_field_is_redacted`. Code: `backend/observability.py::config_secret_values`, `_encoded_diagnostic_secrets`, `_clean_diagnostic_value`, `safe_response_headers`, `safe_provider_path`.

### Requirement: Bounded durable technical capture

Diagnostic capture SHALL sanitize captured details, bound entry count and stored byte sizes, preserve useful classified error evidence and persist dirty entries through its configured flush lifecycle.


#### Scenario: Oversized technical detail
- **WHEN** a capture exceeds configured entry or storage bounds
- **THEN** details are compacted/trimmed while classified error markers remain where supported

#### Scenario: Burst of technical events
- **WHEN** multiple captures arrive before a delayed flush
- **THEN** the dirty burst can be merged into a bounded background write

#### Scenario: Shutdown or explicit flush
- **WHEN** capture is closed or flushed
- **THEN** pending sanitized entries are persisted without leaving an active delayed write

#### Scenario: Limits and entry shape
- **WHEN** the capture is active (it always is)
- **THEN** it keeps at most 10,000 entries of the last 7 days within 2 MiB in total and 512 KiB per entry, as one JSON list under the key `diagnostic_capture_entries`; an entry is `{"timestamp": UTC ISO, "event": name cut to 80 characters, "details": object}`, redacted before it enters memory; the oldest entries go first when the count, total size or age is exceeded, stored entries older than 7 days are dropped on load, and an entry whose timestamp has no readable time zone never expires

#### Scenario: Entry above the size limit
- **WHEN** an entry exceeds 512 KiB
- **THEN** its largest detail fields, except `error`, `provider_error`, `last_error` and `diagnostic_error`, are replaced one by one by `{"truncated": true, "original_bytes": n, "preview": <the first 1/16 of the limit>}`; if that is not enough the entry becomes `{"timestamp", "event", "details": {"truncated": true, "reason": "entry_size_limit", <the protected errors reduced to code, type, param, message, reason and status>}}`

#### Scenario: Shapes instead of bodies
- **WHEN** a provider call is captured
- **THEN** every response and the request bodies of services other than OpenAI are stored as a shape only (`type`, `field_count`, up to 50 field names with a non-standard name as `[nonstandard]`, one sample level, array item count, string length), while request bodies sent to OpenAI are stored sanitized

#### Scenario: Write timing and failure
- **WHEN** entries are captured
- **THEN** the list is rewritten at most 30 seconds after a capture as background work that yields to foreground writes, and at shutdown; a failed write keeps the entries pending for the next write, and at most the last 30 seconds are lost when the process is killed

Evidence: `backend/observability.py::DiagnosticCapture.capture`; `backend/observability.py::diagnostic_response_shape`; `backend/providers/http.py::_captured_request`; `server.py` (`DIAGNOSTIC_CAPTURE_FLUSH_DELAY_SECONDS`); `backend/observability.py::DiagnosticCapture._bounded_entry`; `backend/observability.py::DiagnosticCapture.flush`; `backend/observability.py::DiagnosticCapture.close`. Tests: `tests/test_observability.py::test_detail_capture_retains_evidence_and_removes_nested_and_encoded_credentials`; `tests/test_observability.py::test_capture_limits_bytes_preserves_error_and_persists_failure_before_restart`; `tests/test_observability.py::test_diagnostic_capture_is_bounded_and_keeps_recording`; `tests/test_observability.py::test_capture_drops_entries_older_than_seven_days`; `tests/test_observability.py::test_stored_entries_older_than_seven_days_are_removed_on_load`; `tests/test_observability.py::test_capture_flush_stays_within_the_two_mebibyte_default`; `tests/test_observability.py::test_capture_retains_error_markers_when_error_itself_exceeds_entry_limit`; `tests/test_observability.py::test_diagnostic_shapes_are_bounded_and_keep_only_structure`; `tests/test_observability.py::test_diagnostic_capture_delay_merges_a_burst_into_one_write_off_thread`; `tests/test_observability.py::test_diagnostic_capture_retries_failed_flush_without_losing_dirty_entries`.

### Requirement: Local diagnostic report without provider refresh

The diagnostic report SHALL compose local configuration flags, provider/job state, source freshness, counts and technical runtime information from consistent local reads without returning raw credentials or initiating a provider refresh.


#### Scenario: Report requested
- **WHEN** the authenticated athlete opens/downloads diagnostics
- **THEN** the report returns local counts/state and configuration booleans with redacted errors

#### Scenario: Composed database evidence
- **WHEN** the report reads multiple counts and KV entries
- **THEN** those reads share one read snapshot rather than mixing committed states

#### Scenario: Unsupported process counters
- **WHEN** the platform lacks readable Linux process counter files
- **THEN** available diagnostic information is still returned without treating missing counters as athlete values

#### Scenario: Plan-sync outcomes
- **WHEN** the report is generated
- **THEN** it lists the recent plan-push jobs of every provider with their unsettled items and the unsynced planned units (upcoming first, with their total count), with error classes only and no provider error text or workout content

#### Scenario: Canonical session conflicts
- **WHEN** the report is generated
- **THEN** `canonical_session_conflicts` lists the newest canonical sessions (with their total count) that hold more than one active record of one provider and recording origin, by session id, local date, provider, origin and record count only (at most 50 sessions), and `link_kinds` counts the active sessions per link kind

#### Scenario: Section list and guard
- **WHEN** the report is built
- **THEN** it is one JSON object whose top-level keys are those of `DiagnosticReportService.report` (listed in `docs/api-routes.md` under `GET /api/diagnostics`), and a section that fails is `{"status": "unavailable"}`, logged as `diagnostics_section_unavailable` with the section name and the error type only, while the rest is still returned

#### Scenario: Read order
- **WHEN** the report is built
- **THEN** the diagnostic capture and the log files are read before the database read snapshot is opened, so slow file reads do not hold the snapshot, and all database sections share that one snapshot

#### Scenario: Configuration and database sections
- **WHEN** the report is generated
- **THEN** `configuration` holds booleans and names only (`openai_configured`, `openai_base_url_custom` and `anthropic_base_url_custom`, true only when the endpoint differs from the default and never the URL, `transcription_endpoint_custom`, `anthropic_configured`, `intervals_configured`, `garmin_library_available`, `garmin_configured`, `garmin_fixture_configured`, the selected model, provider and thinking level, the task settings and the available model ids), and `database` holds `schema_version`, the row counts of messages, workout library, competitions, check-ins, activity feedback and change history, `stored_payload_bytes`, `workout_library_state` and `external_calendar_events`

#### Scenario: Provider sync times
- **WHEN** the report is generated
- **THEN** `sync.providers` holds for `garmin`, `wahoo` and `intervals` the fields `last_sync_at` (the last successful or partial refresh; for the output target Intervals.icu the last completed push job) and `last_plan_sync_at` (the last completed plan push)

#### Scenario: Logs and capture
- **WHEN** the report is generated
- **THEN** `logs` holds the latest 200 lines that are not INFO and the latest 100 INFO lines over all ten log files in log order, so more than 100 INFO lines after a warning do not push it out; `debug_capture` holds `active`, `entries`, `maximum_entries`, `bytes`, `maximum_bytes`, `maximum_entry_bytes` and `maximum_age_days`; `note` is a fixed German explanation of what the capture keeps

#### Scenario: Canonical provider composition
- **WHEN** the report is generated
- **THEN** `canonical_provider_composition` counts the surviving canonical sessions by the set of providers of their active records, counts the stored observation kinds outside the vocabulary, and counts active sleep period, zone configuration and power curve rows per provider
- **AND** it carries only provider identifiers, table names and counts; other provider text is reported as `other` and observation kinds outside the vocabulary only as a count, never as ids, names, dates or values
- **AND** a failing count reports the section as `unavailable` without details instead of failing the report

Evidence: `backend/diagnostics/report.py::DiagnosticReportService.report`; `backend/diagnostics/plan_sync.py::summarize_plan_sync`; `tests/test_diagnostic_plan_sync.py`; `backend/diagnostics/session_conflicts.py::summarize_session_conflicts`; `tests/test_diagnostic_session_conflicts.py`; `backend/diagnostics/provider_composition.py::summarize_provider_composition`; `tests/test_diagnostic_provider_composition.py`; `backend/diagnostics/provider_sync.py::summarize_provider_sync`; `tests/test_diagnostic_provider_sync.py`; `backend/diagnostics/canonical_coverage.py`; `backend/runtime/memory.py::process_memory`. Tests: `tests/test_diagnostic_report.py::test_report_shape_counts_redaction_and_privacy`; `tests/test_diagnostic_report.py::test_database_counts_and_kv_reads_share_one_read_snapshot`; `tests/test_diagnostic_report.py::test_payload_sizes_count_utf8_bytes_without_fetching_the_payload`; `tests/test_diagnostic_report.py::test_a_failing_section_is_unavailable_and_the_report_still_builds`; `tests/test_diagnostic_report.py::test_diagnostic_capture_is_snapshotted_before_the_read_snapshot`; `tests/test_diagnostic_report.py::test_log_files_are_read_before_the_read_snapshot`; `tests/test_diagnostic_report.py::test_schema_version_wahoo_state_and_base_url_flag_carry_no_secrets`; `tests/test_diagnostic_report.py::test_note_states_what_the_capture_keeps`; `tests/test_diagnostic_logs.py::test_report_keeps_overnight_warnings_beyond_routine_info`; `tests/test_diagnostic_provider_sync.py` (sync times).

### Requirement: Content-free migration and method state in the report

The diagnostic report SHALL include the state of the local method passes as counts and state words only, and a failing read of a section SHALL report it as `unavailable` without failing the report.

#### Scenario: Local method passes
- **WHEN** the report is generated
- **THEN** `local_load`, `session_metrics` and `session_analysis` each carry the status, method version and counts of their last pass, never ids, dates, names or values

#### Scenario: LT1 ride coverage
- **WHEN** the report is generated
- **THEN** `lt1_ride_coverage` counts the rides of the last 60 days with a power profile, outdoor and indoor, and per power variation limit 0.15, 0.20 and 0.25 how many have a 10 bpm band with at least 10 steady minutes within it; counts only, never dates, values or names (#2535)

#### Scenario: Storage and runtime health
- **WHEN** the report is generated
- **THEN** `runtime` carries the uptime in seconds and the allocator arena limit, `database` carries the database and write-ahead log sizes and the free space of the data directory, and `recovery_copies` carries the count, newest age in hours and total size of the recovery copies beside the database, measured by stat only, never a name, path or content (#2848)

#### Scenario: Garmin strength push counts
- **WHEN** the report is generated
- **THEN** `garmin_strength_push` counts the active strength units dated today or later: with a Garmin copy, those changed since their push, and of the others their exercise structure (none, one, several) and whether a weight is sent; and the units the app sent only to Intervals.icu, with and without a structure; imports from Intervals.icu are left out; counts only, never names, ids or dates (#2850)

#### Scenario: All-time curve build
- **WHEN** the report is generated
- **THEN** `curve_build` carries the state word of the background all-time session-curve build (`pending`, `building`, `ready`, `failed`), the finished builds and failures, and in seconds the last build's duration, the running build's time so far and the age of a failure (#2852)

#### Scenario: FIT file inventory
- **WHEN** a Garmin or Wahoo FIT file is read by the detail fetch or the stream backfill
- **THEN** `fit_inventory` counts it once per recording origin and sport (`files`) with the files holding each unmapped message number, unread field of a mapped message and declared developer field, as numbers only; an unreadable file counts nothing, and no value, name, id, position or file hash is shown

#### Scenario: Failing section
- **WHEN** reading the local load state fails
- **THEN** that section reports `{"status": "unavailable"}` and the rest of the report is still returned

Evidence: `backend/diagnostics/report.py::DiagnosticReportService.report`; `backend/sync/local_load.py::summarize_local_load`; `backend/sync/session_metrics.py::summarize_session_metrics`; `backend/sync/session_analysis_stage.py::summarize_session_analysis`. Tests: `tests/test_diagnostic_report.py::test_report_shape_counts_redaction_and_privacy`.

### Requirement: Diagnostics and log routes

The log preview, log download and diagnostic report SHALL be authenticated GET routes and the log and diagnostics deletion authenticated, CSRF-protected POST routes; their request, response and error contracts are in `docs/api-routes.md`.

#### Scenario: Preview limit
- **WHEN** the log preview is requested
- **THEN** it returns the last `limit` lines of the active log file only (1 to 500, default 200, a non-integer value gives 200; the app asks for 250), so it is nearly empty right after a rotation

#### Scenario: Download
- **WHEN** the log download is requested
- **THEN** the answer is `application/x-ndjson; charset=utf-8` named `pacecraft-server-logs.jsonl` with the nine rotations oldest first followed by the active file; the browser saves the logs as `pacecraft-server-logs-<YYYY-MM-DD>.jsonl` and the report as `pacecraft-diagnostics-<YYYY-MM-DD>.json`

#### Scenario: Deletion failure
- **WHEN** a log file cannot be removed or emptied, or the capture cannot be cleared
- **THEN** the answers are 500 "Logs konnten nicht gelöscht werden." and 500 "Diagnose konnte nicht gelöscht werden."; a failed clear keeps the in-memory capture

#### Scenario: Unauthenticated request
- **WHEN** a GET route is called without a session
- **THEN** authentication fails before any log file or capture is read

Evidence: `backend/http_api/diagnostics_get.py::DiagnosticsGetRoutes`; `backend/http_api/diagnostics_delete_post.py::DiagnosticsDeletePostRoutes`; `backend/diagnostics/logs.py::RecentLogEntriesService`; `public/diagnostics.js`. Tests: `tests/test_http_diagnostics_get_routes.py::test_log_limit_defaults_clamps_and_uses_first_query_value`; `tests/test_http_diagnostics_get_routes.py::test_logs_download_sends_attachment`; `tests/test_http_diagnostics_delete_routes.py::test_logs_delete_failure_raises_500_app_error`; `tests/test_http_diagnostics_delete_routes.py::test_diagnostics_delete_failure_raises_500_app_error`; `tests/test_observability.py::test_diagnostic_capture_clear_failure_preserves_cache_and_raises`; `e2e/downloads.spec.js`.

### Requirement: Log line format, files and access log

Every log record SHALL be written as one sanitized JSON object per line to a rotating file in the data directory and to standard output, and logging calls SHALL pass structured data only as the `event` and `context` extras.

#### Scenario: Fields
- **WHEN** a record is formatted
- **THEN** the object holds `timestamp` (UTC ISO), `level`, `event` (default `log`), `message`, `context` when given and `traceback` when an exception is attached, and passes the structured redaction before it is written

#### Scenario: Sinks
- **WHEN** logging is configured
- **THEN** records of level INFO and above go to `intervals-coach.log` in the data directory, rotated at 5,000,000 bytes with nine rotations (`.1` newest to `.9`), and to standard output, under the logger `intervals_coach`, which does not propagate; configuring twice changes nothing

#### Scenario: Access log and request id
- **WHEN** a request is answered
- **THEN** the event `http_access` carries the context `method`, `path` without query and `request_id` (12 hexadecimal characters, new for each request, also in `http_app_error` and in error bodies), and the message is the request line with masked query values plus status and size

#### Scenario: New extra key
- **WHEN** code logs with an extra key other than `event` or `context`
- **THEN** the log contract guard test fails

Evidence: `backend/observability.py::JsonLogFormatter.format`; `backend/observability.py::configure_logging`; `backend/http_api/handler.py::RequestHandler._write_access_log`; `server.py` (log path). Tests: `tests/test_log_contract.py::test_extra_dicts_only_use_event_and_context`; `tests/test_log_contract.py::test_every_module_logger_is_inside_the_configured_hierarchy`; `tests/test_observability.py::test_configure_logging_is_idempotent_and_writes_file_and_stream`; `tests/test_observability.py::test_formatter_redacts_traceback_and_context`; `tests/test_observability.py::test_request_handler_access_log_uses_policy_level_without_secrets`.

### Requirement: Sanitized log preview and download

Log access SHALL provide sanitized recent previews and rotated/current JSONL download projections, handle missing/unparseable log lines safely, and clear only the owned active/rotated log files through the explicit clear operation.


#### Scenario: Missing log file
- **WHEN** the owned log file does not exist
- **THEN** the preview returns an empty list

#### Scenario: Malformed line
- **WHEN** a log line cannot be parsed as JSON
- **THEN** it is represented as an unparsed-log entry and sanitized before return

#### Scenario: Explicit log clear
- **WHEN** the clear operation succeeds
- **THEN** owned rotated files are removed and the active log is truncated

Evidence: `backend/diagnostics/logs.py::RecentLogEntriesService.list`; `backend/diagnostics/logs.py::RecentLogEntriesService.download`; `backend/diagnostics/logs.py::RecentLogEntriesService.clear`; `backend/http_api/diagnostics_get.py`. Tests: `tests/test_diagnostic_logs.py::test_missing_file_returns_empty_list`; `tests/test_diagnostic_logs.py::test_list_tails_lines_falls_back_for_invalid_json_and_sanitizes_values`; `tests/test_diagnostic_logs.py::test_download_includes_rotated_logs_in_order_and_redacts_secrets`; `tests/test_diagnostic_logs.py::test_clear_removes_rotated_logs_and_truncates_active_log`.

### Requirement: Privacy-safe Coach failure history

Coach diagnostic history SHALL project bounded command/step/error metadata using known status/tool values and safe classifications rather than return session credentials or arbitrary raw provider exception text.


#### Scenario: Failed or rejected tool
- **WHEN** a Coach turn records a classified tool failure or incomplete response
- **THEN** diagnostics retain the safe classification and useful bounded step metadata

#### Scenario: Unknown stored metadata
- **WHEN** stored diagnostic tool/status/error data contains unsupported values
- **THEN** the projection uses its safe fallback/allowlist instead of exposing arbitrary values; a malformed stored receipt yields `unknown` or empty fields, never an error

#### Scenario: Bounds and identity
- **WHEN** the `coach_commands` section is built
- **THEN** it lists the 20 newest Coach turns newest first (a deterministic order for equal times), each with at most 40 steps, identified by the first 12 hexadecimal characters of the SHA-256 of the turn id instead of the id itself

#### Scenario: Error fields
- **WHEN** a stored turn or step error is projected
- **THEN** it keeps only `type`, `reason` and `validation_reason` (letters and underscores, at most 80), `provider_error_code` from the fixed list of known OpenAI response codes (anything else is omitted), an integer `status` from 100 to 599 and the last 8 code locations (`server.py` or `backend/…py`, function name, line number)

#### Scenario: Classified exceptions and request context
- **WHEN** an exception or request context is captured for diagnostics
- **THEN** the exception keeps its type, an integer status, the codes `reason`, `category` and `validation_reason` (`[a-z_]{1,80}`), a known `provider_error_code` and the three typed provider fields (`[a-z0-9_.\[\]-]{1,160}`), and the context keeps only `window_start`, `window_end`, `date`, `latest`, `range_supported`, `email_configured` and `tokenstore_exists`, text cut to 40 characters

Evidence: `backend/diagnostics/history.py::CoachDiagnosticHistoryService`; `backend/observability.py::safe_diagnostic_error`; `backend/observability.py::safe_diagnostic_context`. Tests: `tests/test_diagnostic_history_service.py`; `tests/test_diagnostic_history_service.py::test_limits_to_20_rows_40_steps_and_8_frames`; `tests/test_diagnostic_history_service.py::test_order_is_deterministic_for_equal_timestamps`; `tests/test_diagnostic_history_service.py::test_malformed_receipt_fields_fail_closed`; `tests/test_diagnostic_followups.py::test_failed_tool_is_diagnosable_without_session_credentials`; `tests/test_diagnostic_followups.py::test_rejected_tool_and_incomplete_reply_keep_classified_diagnostics`; `tests/test_observability.py::test_safe_diagnostic_context_and_error_never_retain_values_or_text`.

### Requirement: Clear and restore do not resurrect captures

Diagnostic clear, privacy erasure and restore SHALL coordinate pending flushes and caches so successfully removed or replaced diagnostic entries cannot later be written back from the previous in-memory state.


#### Scenario: Successful clear or state replacement
- **WHEN** diagnostic state is successfully cleared, erased or replaced during suspended flushing
- **THEN** old capture caches are discarded and cannot resurrect prior entries

#### Scenario: Mutation fails
- **WHEN** a suspended mutation fails before committing replacement diagnostic state
- **THEN** the existing cache is retained rather than falsely treating the failed clear as complete

Evidence: `backend/observability.py::DiagnosticCapture.clear`; `backend/observability.py::DiagnosticCapture.suspended`; `backend/observability.py::DiagnosticCapture.discard_cache`; `backend/backup/restore.py::DatabaseRestoreService.restore`; `backend/privacy.py::PrivacyDeleteService.delete`. Tests: `tests/test_observability.py::test_restore_swap_shows_only_restored_entries_after_discard`; `tests/test_observability.py::test_flush_during_suspension_cannot_write_cleared_entries_back`; `tests/test_observability.py::test_delayed_write_cannot_write_cleared_entries_back_after_suspension`; `tests/test_observability.py::test_suspension_keeps_the_cache_when_the_mutation_fails`.

### Requirement: Useful safe correlation and access-log levels

The application SHALL retain safe request/operation correlation for concurrent work, log successful routine GET polls at the configured reduced level, and retain higher-visibility error/other-request evidence without sensitive query text.


#### Scenario: Concurrent operations
- **WHEN** two unrelated operations log technical events concurrently
- **THEN** their safe correlation identifiers remain distinct

#### Scenario: Routine successful poll
- **WHEN** a GET with a 2xx status is answered on `/api/chat/status`, `/api/state/events`, `/api/sync/status`, `/api/health`, `/api/auth/status` or `/api/sync/jobs/<id>`
- **THEN** the access log uses DEBUG, so the INFO file sink does not write it

#### Scenario: Error response
- **WHEN** a request fails, answers with another status, is not one of those polls, or is an inherited server error message
- **THEN** the access log retains INFO-level visibility

Evidence: `backend/observability.py::http_access_log_level`; `backend/observability.py::JsonLogFormatter`; `backend/http_api/handler.py::RequestHandler`. Tests: `tests/test_sync_observation.py::test_context_is_isolated_between_threads`; `tests/test_observability.py::test_successful_routine_get_polls_log_at_debug`; `tests/test_observability.py::test_errors_and_other_requests_keep_info`.
