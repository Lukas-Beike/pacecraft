# notifications Specification

## Purpose

Describe browser-local PWA notifications as currently implemented. This baseline inspects notification state and the service-worker click handler. It does not promise a server push service, subscription endpoint or closed-app scheduled delivery. Coach turn sending and safe shell interaction are in `web-pwa-and-accessibility`; session/cache invalidation is in `view-refresh-and-client-state`. Named browser tests are inspected evidence, not an executed permission/device matrix.

## Requirements

### Requirement: Notification permission is opt-in and capability-aware
The client SHALL show whether browser notifications are granted, denied, unsupported, or not enabled, and SHALL request permission only after the athlete explicitly activates the enable control.

#### Scenario: Athlete enables notifications
- **WHEN** the athlete selects the enable action in a supporting browser
- **THEN** the browser permission prompt is requested and the UI updates to the returned permission state.

#### Scenario: Unsupported or denied notifications
- **WHEN** notifications are unsupported or blocked by the browser
- **THEN** the UI shows that status and no delivery attempt is made without granted permission; the rest of the app remains usable.

#### Scenario: Ordinary state render
- **WHEN** state is rendered without an enable action
- **THEN** no permission prompt is raised automatically.

#### Scenario: Status line
- **WHEN** the notification status is shown
- **THEN** it reads "Aktiv", "Im Browser blockiert", "Von diesem Browser nicht unterstützt" or "Noch nicht aktiviert"; the button "Benachrichtigungen aktivieren" is hidden when permission is granted and disabled when unsupported

Evidence: `public/notifications.js::notificationPermission`, `public/notifications.js::renderNotificationStatus`, `public/notifications.js::enableNotifications`, `public/notifications.js::showPwaNotification`; `tests/notifications-retry.test.cjs::a successful notification is marked delivered and is not sent again` covers granted delivery. Permission prompting/status projection has source evidence only; a dedicated denied/unsupported permission test is an explicit evidence gap.

### Requirement: State notifications contain minimal private information
The client SHALL derive notifications only from freshly loaded eligible application state with granted permission, use generic competition countdown/sync-attention bodies, and avoid competition names and provider error text in lock-screen notification content.

#### Scenario: Upcoming competition
- **WHEN** fresh state contains a competition zero through three days away
- **THEN** a generic countdown notification identifies today, tomorrow, or days remaining without exposing the competition name: the title is "Wettkampf steht bevor", the body "Der Wettkampf ist heute.", "Der Wettkampf ist morgen." or "Der Wettkampf ist in N Tagen." and the tag `competition:<id>`

#### Scenario: Sync failure
- **WHEN** eligible state indicates a sync error
- **THEN** a generic attention notification points to details in the app without copying raw provider error text: the title is "Pacecraft benötigt Aufmerksamkeit", the body "Eine Synchronisierung ist fehlgeschlagen. Details stehen in der App." and the tag `sync-error`

#### Scenario: Cached device snapshot
- **WHEN** previously cached state is shown during startup
- **THEN** snapshot display alone does not produce new notifications; fresh loaded state controls notification eligibility.

Evidence: `public/notifications.js::notifyState`, `public/notifications.js::competitionCountdownText`, `public/state-loader.js::render`; `tests/frontend-write-safety.test.cjs::notification bodies omit competition names and provider error text`, `tests/state-loader-startup.test.cjs::loaded state renders as real data, so it may notify`. There is no server push scheduling or guaranteed closed-app delivery in this inspected flow.

### Requirement: Notification delivery retries are bounded per client lifetime
The client SHALL suppress duplicate successfully delivered notification keys, allow retry after a failed attempt only after thirty seconds, and stop after three attempts for a persistently failing key. Clicking a notification SHALL focus an existing app window or open the app root.

#### Scenario: Successful delivery
- **WHEN** the service-worker registration displays a notification successfully
- **THEN** repeated renders with the same key do not display it again in the current client state lifetime.

#### Scenario: Transient or persistent platform failure
- **WHEN** notification display fails
- **THEN** the key can retry after the delay, a later success ends retries, and persistent failure receives at most three attempts without an unhandled application failure.

#### Scenario: Notification clicked
- **WHEN** the athlete clicks a displayed notification
- **THEN** it closes and the worker focuses an existing window when available, otherwise opens `/`.

Evidence: `public/notifications.js::reserveNotificationAttempt`, `public/notifications.js::showPwaNotification`, `public/service-worker.js` (notificationclick listener); `tests/notifications-retry.test.cjs::a persistently rejecting notification is retried at most three times`, `tests/notifications-retry.test.cjs::renders inside the retry delay do not resend a rejected notification`, `tests/notifications-retry.test.cjs::a transient failure followed by success is delivered once`. Click handling is inspected source only; duplicate suppression is in-memory and does not promise cross-tab/restart delivery deduplication.
