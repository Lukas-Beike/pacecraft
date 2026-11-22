# Backend ownership

Domain owners: `coach/` (Coach), `planning/` (plans/seasons), `sync/` (sync/jobs),
`providers/` (transport), `db/` (persistence), `http_api/` (HTTP/route assembly),
`backup/` (backup/export), `athlete/` (profile/check-ins/local time/body
records: current weight and body fat, `body_records.py`),
`activities/` (feedback/reads/workout text), `calendar/` (external calendars),
`diagnostics/` (safe diagnostics), `history/` (changes/undo), `nutrition/`,
`performance/` (derived/readiness), `runtime/` (lifecycle/maintenance),
`weather/` (weather/cache), `push/` (Web Push notifications), `app/`
(application context and composition),
and `canonical/` (stdlib-only vocabulary/records).
Cross-cutting owners: `config.py`, `privacy.py`, `settings.py`,
`change_history.py`, `observability.py`, `errors.py`, `pagination.py`,
`provider_credentials.py`. Extend a cohesive owner first.

Domain services own use-case orchestration and
transaction boundaries; provider adapters own external transport, not local
authorization or persistence policy. Assembly modules wire explicit dependencies.
Never import or indirectly reach `server.py` globals.

For Coach changes trace schema, authorization, dispatcher, durable effect, receipt,
and HTTP/UI refresh. Preserve local authority, source/freshness labels, explicit
remote writes, adaptive preview/apply, session/CSRF, and maintenance boundaries.
Use temporary storage and mocked providers; never weaken SQLCipher startup.

Validate focused tests, architecture guards, syntax, and CI-equivalent Ruff/mypy
checks for changed backend files. Follow the root secret-scan protocol.

If Sonar reports `DataclassInstance` for a frozen dataclass returned by
`dataclasses.replace`, use an explicit `typing.cast` to the concrete dataclass
type and check it with mypy; keep Sonar's type rules enabled.
