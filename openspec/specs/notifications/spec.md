# notifications Specification

## Purpose

Describe the Web Push notifications to the athlete's phone (#3914): subscription, server-side once-only delivery of lock-screen safe messages, and the service worker that shows them while the app is closed. Coach turn sending and safe shell interaction are in `web-pwa-and-accessibility`; session/cache invalidation is in `view-refresh-and-client-state`. Real-device delivery is verified on the athlete's phone after release, not in tests.

## Requirements

### Requirement: Push is opt-in and limited to supported browsers
The client SHALL subscribe to Web Push only after the athlete activates the enable control and grants permission, and SHALL show whether push is active, blocked, unsupported or not enabled. The server SHALL accept only Chrome (FCM) and Firefox (Mozilla autopush) push endpoints over https.

#### Scenario: Athlete enables notifications
- **WHEN** the athlete selects "Benachrichtigungen aktivieren" and grants permission
- **THEN** the browser subscribes with the server's public VAPID key, the server stores the subscription, and the status reads "Aktiv" with the button "Benachrichtigungen ausschalten"

#### Scenario: Athlete turns notifications off
- **WHEN** the athlete selects "Benachrichtigungen ausschalten"
- **THEN** the server removes that device's subscription and the browser unsubscribes

#### Scenario: Unsupported or refused push service
- **WHEN** the browser has no Push API, or the server refuses its push service
- **THEN** the status reads "Von diesem Browser nicht unterstützt" or the refusal is shown, the browser keeps no subscription, and the rest of the app remains usable

#### Scenario: App start
- **WHEN** the app starts with a browser subscription and granted permission
- **THEN** the subscription is sent to the server again (keeping when the device first subscribed), so a server that lost it receives it

#### Scenario: Ordinary state render
- **WHEN** state is rendered without an enable action
- **THEN** no permission prompt is raised automatically

Evidence: `public/notifications.js::enablePush`, `public/notifications.js::disablePush`, `public/notifications.js::renderNotificationStatus`, `backend/push/web_push.py::supported_endpoint`, `backend/http_api/push.py:PushRoutes`; `tests/service-worker-update.test.cjs::enabling push subscribes with the server key and stores the subscription`, `tests/service-worker-update.test.cjs::a subscription the server refuses is removed from the browser`, `tests/service-worker-update.test.cjs::each app start sends the browser's subscription to the server again`, `tests/test_push.py::test_only_chrome_and_firefox_push_services_are_accepted`.

### Requirement: Keys and subscriptions stay server-side
The server SHALL generate the VAPID key pair once, keep the private key and the subscriptions in the encrypted database, exclude both from export and diagnostics, encrypt every payload for the subscribing browser (RFC 8291) and sign each request with VAPID (RFC 8292). Logs and diagnostics SHALL hold counts and statuses only, never keys or endpoints.

#### Scenario: Diagnostics
- **WHEN** diagnostics are read
- **THEN** `push_notifications` holds the number of subscribed devices and sent/failed counts per type

#### Scenario: Expired subscription
- **WHEN** the push service answers 404 or 410
- **THEN** the subscription is removed and the failure is counted without logging the endpoint

Evidence: `backend/push/service.py:PushService`, `backend/push/service.py::diagnostics`, `backend/provider_credentials.py:CREDENTIAL_KV_KEYS`; `tests/test_push.py::test_payload_encryption_matches_the_rfc_8291_example`, `tests/test_push.py::test_vapid_token_is_an_es256_jwt_for_the_endpoint_origin`, `tests/test_push.py::test_a_gone_subscription_is_removed_and_the_log_names_no_endpoint`.

### Requirement: Each event is sent once, lock-screen safe, per-type switchable
The server SHALL send each event key at most once, only to subscribed devices, only for types switched on (all on by default), and only between 07:00 and 22:00 athlete-local time; an event found at night waits for the morning. An event of a switched-off type SHALL be recorded as handled, so switching it on does not replay it; an event that stays current is never announced again. Bodies SHALL carry no names, values, places, provider error text or Coach content.

#### Scenario: Delivery
- **WHEN** a claimed event reaches no device (no answer, 5xx or 429)
- **THEN** it is tried again on the next check; each notification has its own tag, and the network calls run outside the maintenance gate

#### Scenario: Provider problem
- **WHEN** a Garmin, Wahoo or Intervals.icu sign-in expires, or an area fails three refreshes in a row, after the subscription
- **THEN** one "Anbindung braucht Aufmerksamkeit" notification per provider and streak opens `#more/connections`

#### Scenario: Upcoming competition
- **WHEN** a competition is zero to three days away
- **THEN** one "Wettkampf steht bevor" notification per competition and date opens `#plan/season`

#### Scenario: Step test due
- **WHEN** a threshold turns stale (older than eight weeks, the existing staleness hint) after the subscription
- **THEN** one "Stufentest fällig" notification per set of newly stale values opens `#more/profile`; a value already stale at subscription is not announced

#### Scenario: Infection hint
- **WHEN** today's infection hint shows and is unanswered
- **THEN** one notification asks the athlete to confirm how they feel and opens `#plan/overview`

Evidence: `backend/push/service.py:PushService.notify`, `backend/push/checks.py:PushChecks`; `tests/test_push.py::test_each_event_is_sent_once_and_a_switched_off_type_never`, `tests/test_push.py::test_a_repeated_failure_is_announced_once_per_streak`, `tests/test_push.py::test_a_competition_within_three_days_is_announced_once`, `tests/test_push.py::test_a_step_test_is_announced_once_when_a_threshold_newly_turns_stale`, `tests/test_push.py::test_nothing_is_sent_at_night_and_it_waits_for_the_morning`, `tests/test_push.py::test_a_failed_delivery_is_tried_again_and_still_sent_once`, `tests/test_push.py::test_a_streak_longer_than_the_rows_read_is_announced_once`, `tests/test_push.py::test_the_checks_hold_the_gate_only_around_database_steps`.

### Requirement: The service worker shows push messages and opens their screen
The service worker SHALL show each push message with its title, body and tag, accept only an in-app `#route` as its target, and on a tap focus an open app window on that route or open the app there.

#### Scenario: Notification clicked
- **WHEN** the athlete taps a displayed notification
- **THEN** it closes and the app opens on the notification's screen

Evidence: `public/service-worker.js` (push and notificationclick listeners); `tests/service-worker-update.test.cjs::a push message shows its lock-screen text and keeps only an app route`, `tests/service-worker-update.test.cjs::tapping a notification opens its screen in the open app or a new window`.
