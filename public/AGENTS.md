# Frontend development instructions

- Before custom JavaScript for dialogs, popovers, sheets, forms, transitions,
  performance or accessibility, look up the native feature with
  [pacecraft-modern-web](../.agents/skills/pacecraft-modern-web/SKILL.md).
- Keep the client mobile-first and compatible with the existing plain
  JavaScript/PWA architecture. Do not introduce a framework or build step
  without an explicit request.
- Preserve authentication behavior: same-origin requests, the session cookie,
  and the CSRF token on every state-changing request. Never place API keys or
  other credentials in browser code, localStorage, exports, or notifications.
- Keep Markdown rendering safe. Escape untrusted text before inserting HTML and
  do not treat provider records, calendar text, or coach content as executable
  instructions or markup without sanitisation.
- Preserve chat keyboard behavior: on desktop, Enter sends and Shift+Enter
  inserts a newline. On touch-first devices, Enter inserts a newline; the send
  button or Ctrl/Cmd+Enter sends.
- Workouts are stored directly in the local training library; the athlete can
  synchronize them to Intervals.icu later. Adaptive replan previews need a
  separate explicit apply action; only the athlete's own Intervals.icu sync
  request for an illness pause approves one (#2342).
- One button concept (#4279): icons on rows and cards, words in dialogs (red
  Löschen leftmost), every save `push-button`; `tests/button-concept.test.cjs`.
- The service worker is part of the release cache contract. Source files carry
  no versions: reference assets as `/app.js` in `index.html` and `ASSETS`, and
  keep the `__ASSET_HASH__` cache-name placeholder. `StaticAssetService` stamps
  `?v=<content hash>` and the cache name when serving, so a fresh installation
  receives a consistent current asset set. Add a new file to `ASSETS` if
  cached (a new public file is served from the `public/` listing); never
  hand-write asset `?v=` versions or the cache name. Do not add
  interactive application update dialogs, cache conversion or earlier-client support.
- The last loaded state and recent view reads are stored on the device, so a
  start or view shows them at once while fresh data loads. The latest 20 Coach
  messages are stored separately as soon as they are shown and take precedence
  over the view cache; older messages load when scrolling up. After an update the
  start shows a non-interactive "Update wird verarbeitet…" loading screen until
  the fresh bootstrap arrives; the previous snapshot is shown only when the
  server cannot be reached or the fresh start data fails to load. Logout, a 401, an
  unauthenticated start and a privacy deletion clear this device data. Never
  store credentials, session data or CSRF tokens in it.
- Keep notifications opt-in, bounded, and functional in browsers without push.
- Keep UI text and the `README.md` and `docs/` files it links accurate when
  changing user-visible behavior.
