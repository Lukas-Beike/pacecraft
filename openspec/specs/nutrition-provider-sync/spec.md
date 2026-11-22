# nutrition-provider-sync Specification

## Purpose

Describe the rule that nutrition is never sent to a provider and the scoped Garmin nutrition refreshes, re-validated against application revision `d1beefd77`. Requirements document inspected implementation and tests, not a completed live-provider conformance run. Local diary and totals are specified by `nutrition-diary-and-targets`; client request races by `view-refresh-and-client-state`.

## Requirements

### Requirement: Nutrition is never sent to a provider
The system SHALL NOT send nutrition to Intervals.icu or any other provider: no Coach tool, no HTTP route, no sync job type and no scheduler queues a nutrition export, and the Coach answers a request to send nutrition by saying that nutrition stays local (athlete decision, #4031). Diary reads, edits, product lookup and view refresh stay local, and entries already written to a provider earlier are left as they are.

#### Scenario: No nutrition job
- **WHEN** a client posts a `nutrition_sync` job for any provider to `/api/sync/jobs`, or a job of that type is queued directly
- **THEN** it is an unsupported job type and no provider call is made; a stored job of that type fails without a provider call and cannot be retried (409 `job_type_retired`).

#### Scenario: No Coach tool
- **WHEN** the athlete asks the Coach to transfer nutrition to Intervals.icu
- **THEN** the Coach has no tool for it and says that nutrition is not sent.

#### Scenario: Stored state is kept
- **WHEN** the application starts on a database that holds `nutrition_sync_dates` rows or `nutrition_logs.sync_state` values
- **THEN** they stay as stored and nothing reads or writes them for an export; the table is left for a later schema change.

Evidence: `backend/sync/jobs.py::JOB_TYPES`, `backend/coach/tool_specs.py`, `backend/coach/prompt.py`; `tests/test_sync_jobs.py`, `tests/test_http_sync_commands.py`, `tests/test_coach_tool_coverage.py`.

### Requirement: Nutrition refresh reads only its selected source and dates
The system SHALL accept explicit refreshes for diary, week, body, or products and restrict each to its owned data. Week/body refreshes SHALL accept only fourteen or eighty-four days and reject future end dates and unsupported payload fields.

#### Scenario: Diary energy refresh
- **WHEN** the athlete refreshes a selected diary date
- **THEN** local meals are reread and only Garmin daily energy for that date is requested, with local meal status distinguished from provider status.

#### Scenario: Week or body refresh
- **WHEN** a valid period is refreshed
- **THEN** week requests only daily stats for the closed-day range, while body requests only weight for its bounded range ending on the athlete-local day by default.

#### Scenario: Local product refresh or invalid request
- **WHEN** products are refreshed or a period/area/payload is invalid
- **THEN** products return local status without provider I/O; invalid requests fail before provider access.

#### Scenario: Defaults
- **WHEN** `days` and `date` are omitted
- **THEN** week reads 28 days ending yesterday and body 84 days ending today.

#### Scenario: Partial diary refresh
- **WHEN** the energy read of a diary refresh is not `ok`
- **THEN** the returned day's `energy_expenditure.freshness` is `partial` and the response status follows the energy read.

#### Scenario: Garmin busy
- **WHEN** another Garmin synchronization holds the coordination lock
- **THEN** the request fails with 503 `provider_busy` ("Garmin wird bereits aktualisiert.") instead of a partial result.

Detailed contract: request and response shapes are in `docs/api-routes.md` (`POST /api/nutrition/refresh`). The partial reasons are `not_configured`, `not_supported`, `library_unavailable` and `provider_unavailable`, and a weight read without records is never `ok`.

Evidence: `backend/http_api/nutrition.py::NutritionPostRoutes._refresh`, `backend/http_api/nutrition.py::NutritionPostRoutes._refresh_provider`; `tests/test_nutrition_api_and_sync.py::NutritionHttpApiTests.test_nutrition_refresh_week_uses_requested_range`, `tests/test_nutrition_api_and_sync.py::NutritionHttpApiTests.test_nutrition_refresh_body_uses_bounded_weight_range`, `tests/test_nutrition_api_and_sync.py::NutritionHttpApiTests.test_nutrition_refresh_local_products_never_calls_provider`, `tests/test_nutrition_api_and_sync.py::NutritionHttpApiTests.test_nutrition_refresh_rejects_invalid_range`.

### Requirement: Failed or empty Garmin reads retain honest freshness
The system SHALL preserve previously stored nutrition-related measurements on a failed refresh, return explicit partial/unconfigured/unsupported status, and scope successful freshness to the selected source and dates rather than claiming a complete provider sync.

#### Scenario: Provider unavailable
- **WHEN** a narrow provider refresh fails or is unconfigured
- **THEN** the response is partial with an actionable reason, local meals remain available, and retained data is not persisted as freshly fetched.

#### Scenario: Empty body refresh
- **WHEN** the selected weight read returns no usable measurement
- **THEN** prior measurements remain as stale context and the result is partial.

#### Scenario: Multiple same-day scale observations
- **WHEN** a bounded weight refresh includes distinct readings or timestamped corrections
- **THEN** stored history retains distinct samples and replaces identified corrections while preserving measurements outside the refreshed window.

Evidence: `backend/sync/garmin_service.py::GarminTargetedRefreshService.refresh_nutrition`, `backend/http_api/nutrition.py::NutritionPostRoutes._refresh_provider`; `tests/test_nutrition_garmin_refresh.py::GarminNutritionRefreshTests.test_provider_failure_returns_partial_without_persisting_stale_as_fresh`, `tests/test_nutrition_garmin_refresh.py::GarminNutritionRefreshTests.test_empty_body_refresh_is_partial_and_retains_prior_measurement_as_stale`, `tests/test_nutrition_garmin_refresh.py::GarminNutritionRefreshTests.test_weight_snapshot_merge_replaces_timestamped_corrections_and_keeps_metadata`.

