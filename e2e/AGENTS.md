# Browser fixture safety

Use only the disposable SQLCipher fixture runtime, fake credentials, mocked
providers, and a fresh browser profile. Never point tests at the connected app,
real environment, data volume, token store, or an authenticated athlete session.

Follow `.agents/skills/pacecraft-pwa-e2e/references/browser-validation.md` and
the current CI runtime setup. Keep the configured single worker and restore
fixture state where scenarios deliberately mutate it. Test all affected viewports,
safe Markdown, accessibility, reduced motion, cache/offline behavior, and the
changed journey. Never publish authenticated storage state or private artifacts.

A spec that aborts or fails an `/api/` request blocks the service worker with
`test.use({ serviceWorkers: "block" })`: routes miss requests that pass through
the worker's fetch listener (#3842). A spec that tests the worker itself is
listed in `tests/test_server_frontend.py` with its reason.
