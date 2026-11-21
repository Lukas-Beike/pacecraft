# Test development instructions

- Run tests from the repository root with:

  ```powershell
  python tests/run_tests.py --changed
  python -m compileall -q server.py backend tests
  node --test tests/*.cjs
  ```

- The one tracked live model run is `tests/coach_live_eval.py` (#2221): started
  by hand with a dedicated evaluation key, the dialogue catalogue once per run,
  at most 300 model calls and 5 USD, tool calls only on temporary storage with
  unconfigured providers; never part of the suite, CI or pre-push. Normal tests
  stay mocked.

- Format only the files you changed: `python scripts/check_changed.py
  --fix-format` formats the changed Python files and no other file, then runs
  the change's checks (affected tests, compile, quality), which can take
  minutes; stderr names each step as it starts (#3865). Never run
  `ruff format tests` (or any whole folder); it rewrites unchanged, baselined
  files (#3382).
- A test file named after a guarded script (such as the review-thread resolver's
  test module) is blocked in any shell command that names it, by design (#3103,
  #3347). Read and search it with the Read and Grep tools, run it through
  `python tests/run_tests.py --changed`, and format it through
  `--fix-format`; never disguise its name.

- `tests/changed_tests.py` selects modules by imports and by string literals
  naming changed paths or modules. A test that depends on a file another way
  (computed paths, dynamic imports) must name it as a literal, or the selector
  misses it locally.

- Helpers shared by several `TestCase` subclasses are module-level functions
  taking the case or its fixtures as arguments, not methods of a base `TestCase`
  (Sonar python:S5899 flags those as unused).
- A regression test extends an existing table or case of the owning test
  module; it adds no new test file, fake class, or helper when one exists.
  Tests live with their owner: no test files or test names organised by review
  round (such as `test_audit_remediation.py` or `task8_...`).
- Every test must use temporary `DATA_DIR`/database state or isolated patches.
  Never read or modify the real `.env`, `/data`, production database, Garmin
  token store, or runtime logs.
- Mock all OpenAI, Anthropic, Garmin, Wahoo, Intervals.icu (output only), iCalendar, and
  other network calls.
  Do not use real credentials, external accounts, or live network access.
- Do not print secrets or full athlete payloads in failures, fixtures, or test
  diagnostics. Use clearly fake values and sanitized/minimal fixtures.
- Preserve coverage for the security and approval boundaries: APP_PASSWORD,
  session/CSRF authentication, SQLCipher startup, path traversal,
  public-calendar SSRF validation, redacted logs/diagnostics, bounded request
  bodies, backup validation/restore, local-only planned units, and explicit remote
  workout approval.
- Take expected values from an independent source (a worked example, a known
  literal, the spec), never recomputed the way the code computes them; such a
  tautological test cannot fail. This matters most for training-science formulas.
  A stored input's fixture takes its scale from the producer, never an assumed
  one: build it through the producer or assert the `MetricKind` unit (#3230).
- When changing sync or context behavior, test source precedence and provenance
  for Wahoo, Garmin, local feedback, derived metrics, and AI estimates.
- When changing the frontend, backend API contracts, PWA cache behavior, or
  deployment, add or update focused regression tests where the current
  standard-library test setup permits it.
