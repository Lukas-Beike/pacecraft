# Architecture

`server.py` is the composition root: it reads the configuration, builds the one application context (`backend/app/context.py`, typed, with live `config` and `database` holders that a credential save or a restore replaces) and runs the lifecycle (startup, workers, shutdown). Each domain's builder (`backend/<domain>/wiring.py`) constructs that domain's services from the context; the HTTP server class is `backend/http_api/server.py`. Business logic lives in `backend/`. `http_api` owns HTTP transport and route assembly, `coach` owns Coach workflows, `sync` owns synchronization and scheduling, `providers` owns external adapters, and `db` owns persistence; `http_api` receives the application context as an untyped parameter.

## Import rules

The AST layer tests in `tests/test_backend_layer_contracts.py` enforce the permitted imports of each layer below (generated from their layer map by `python tests/test_backend_layer_contracts.py --write`), an empty package-cycle baseline, and two rules on top: `athlete` imports no other domain package, and `canonical` imports nothing outside the standard library and itself. No backend module imports or reaches `server`.

<!-- BEGIN GENERATED LAYER RULES -->
| Layer | Packages | May import |
| --- | --- | --- |
| providers | `backend.providers` | `backend.canonical`, `backend.config`, `backend.errors`, `backend.observability`, `backend.providers`, `backend.runtime.clock`, `backend.runtime.ports`, `backend.runtime.socket_deadline` |
| canonical | `backend.canonical` | `backend.canonical` |
| http_api | `backend.http_api` | domain packages, shared modules, `backend.http_api`, `backend.runtime` |
| domain | `backend.activities`, `backend.athlete`, `backend.backup`, `backend.calendar`, `backend.coach`, `backend.diagnostics`, `backend.history`, `backend.nutrition`, `backend.performance`, `backend.planning`, `backend.sync`, `backend.weather` | domain packages, shared modules, `backend.app`, `backend.db`, `backend.providers`, `backend.runtime` |
| db | `backend.db` | shared modules, `backend.db`, `backend.runtime` |
| runtime | `backend.runtime` | shared modules, `backend.runtime` |
| shared | every other `backend` package or module, among them `backend.app`, `backend.change_history`, `backend.config`, `backend.errors`, `backend.observability`, `backend.pagination`, `backend.privacy`, `backend.provider_credentials`, `backend.settings` | domain packages, shared modules, `backend.db`, `backend.providers`, `backend.runtime` |

Shared modules: `backend.canonical`, `backend.change_history`, `backend.config`, `backend.errors`, `backend.observability`, `backend.pagination`, `backend.privacy`, `backend.provider_credentials`, `backend.settings`. Domain packages: the packages of the domain layer.
<!-- END GENERATED LAYER RULES -->

```text
               +-------------------------------------------------------------+
               |                       Trusted Athlete                       |
               |                (Mobile PWA / Desktop Browser)               |
               +------------------------------+------------------------------+
                                              | HTTPS / SSE
                                              v
               +-------------------------------------------------------------+
               |                  Reverse Proxy (Caddy / Nginx)              |
               |               Terminates TLS, Sets Secure Headers           |
               +------------------------------+------------------------------+
                                              | HTTP (Port 8090)
                                              v
+-------------------------------------------------------------------------------------------+
| Pacecraft Container (/app)                                                          |
|                                                                                           |
|  +-------------------------------------------------------------------------------------+  |
|  | server.py (Composition Root: configuration, application context, lifecycle)         |  |
|  +-------------------------------------------+-----------------------------------------+  |
|                                              |                                            |
|                                              v                                            |
|  +-------------------------------------------------------------------------------------+  |
|  | backend/ Domain Layer                                                               |  |
|  |   - backend.app       : Shared application context (domain builders read it)        |  |
|  |   - backend.http_api  : HTTP server, routing and request/response transport         |  |
|  |   - backend.coach     : Coach conversations, context, tools, jobs and SSE           |  |
|  |   - backend.sync      : Provider synchronization, scheduling and durable jobs       |  |
|  |   - backend.providers : External provider adapters and response handling            |  |
|  |   - backend.athlete   : Profile, check-ins and athlete-local time                  |  |
|  |   - backend.activities: Activity reads, matching, feedback and workout projections  |  |
|  |   - backend.performance: Derived/readiness context and chart history                |  |
|  |   - backend.planning  : Plans, library, workouts, conflicts and season preparation  |  |
|  |   - backend.weather   : Weather projections and caching                              |  |
|  |   - backend.calendar  : Public/external calendar data                                |  |
|  |   - backend.nutrition : Nutrition entries, templates and product workflows          |  |
|  |   - backend.backup    : Export, validation and restore workflows                    |  |
|  |   - backend.diagnostics: Safe diagnostics and logging views                         |  |
|  |   - backend.history   : Change history and undo                                      |  |
|  |   - backend.runtime   : Lifecycle and maintenance state                              |  |
|  |   - backend.db        : Repositories, schema and SQLCipher persistence              |  |
|  |   - backend.canonical : Standard-library vocabulary and canonical records           |  |
|  +-------------------------------------------+-----------------------------------------+  |
|                                              |                                            |
|                                              v                                            |
|  +-------------------------------------------------------------------------------------+  |
|  | Persistent Storage Mount (/data)                                                    |  |
|  |   - intervals-coach.db (SQLCipher encrypted database)                               |  |
|  |   - garmin_tokens (legacy plaintext token file, imported once, left in place)       |  |
|  |   - intervals-coach.db.pre-restore-<timestamp>-<id> (restore safety copies)          |  |
|  +-------------------------------------------------------------------------------------+  |
+-------------------------------------------------------------------------------------------+
       |                                |                             |
       | HTTPS REST (writes only)       | HTTPS (Garmin, Wahoo)       | HTTPS REST
       v                                v                             v
+------------------+      +-----------------------+      +--------------------+
|  Intervals.icu   |      |  Garmin / Wahoo       |      |  Open-Meteo & ICS  |
|  (output only)   |      |  - Activities, FIT    |      |  - 14-Day Forecast |
|  - Workouts      |      |  - Sleep, HRV, Health |      |  - Air & Pollen    |
|  - Competitions  |      |  - Thresholds         |      |  - Life Calendars  |
|  - Nutrition     |      |  - Strength workouts  |      |                    |
+------------------+      +-----------------------+      +--------------------+
```

The browser client is a plain JavaScript PWA. `app.js` bootstraps the client;
`auth.js` owns login, session, confirmation, and app-shell loading; `shared.js`
owns generic helpers and cross-view UI state; `sync-status.js` owns sync,
connectivity, provider freshness, and progress state; and `sync-actions.js`
owns refresh actions. `notifications.js` owns notification permission,
notifications, and service-worker registration. `performance-view.js` owns
performance, Garmin, and editable metrics; `diagnostics.js` owns change
history, logs, and diagnostic capture; and `settings.js` owns profile,
check-ins, model and calendar settings, backups, and privacy actions. The
remaining modules own their named domains: `api.js`, `navigation.js`,
`state.js`, `state-loader.js`, `views.js`, `plan-views.js`, `coach.js`,
`analysis.js`, `activity-details.js`, `activity-analysis.js`, `nutrition.js`,
`components.js`, `connection-status.js` (provider status chips),
`status-strip.js` (the one status strip), `format.js` (number, date and
duration formats), `view-refresh.js` (the refresh control of a view),
`provider-connections.js` (credential forms), `provider-checks.js` (explicit
connection checks), `wahoo-connection.js` (the Wahoo connection card), and
`appearance.js`. The service worker owns the offline asset cache.
