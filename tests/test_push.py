"""Web Push: RFC 8291/8292 transport, subscriptions, once-only delivery and checks (#3914)."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from support import FakeManager, dispatch_route

from backend.db.repositories import KeyValueRepository
from backend.errors import AppError
from backend.http_api.push import PushRoutes
from backend.provider_credentials import (
    CREDENTIAL_KV_KEYS,
    PUSH_SECRET_KEYS,
    PUSH_SUBSCRIPTIONS_KEY,
)
from backend.push import web_push
from backend.push.checks import PushChecks
from backend.push.events import coach_answer_event, morning_plan_event, unit_line
from backend.push.service import (
    TYPES,
    Notification,
    PushService,
    diagnostics,
)
from backend.weather.recommendations import day_warning

# RFC 8291 Appendix A: the worked example of one encrypted push message.
RFC_PLAINTEXT = b"When I grow up, I want to be a watermelon"
RFC_SENDER_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_RECEIVER_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_BODY = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)
ENDPOINT = "https://fcm.googleapis.com/fcm/send/fake-device"
NOT_A_POINT = web_push.b64url(bytes([4]) + bytes([1]) * 64)
SUBSCRIPTION = {
    "endpoint": ENDPOINT,
    "keys": {"p256dh": RFC_RECEIVER_PUBLIC, "auth": RFC_AUTH},
}
NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
# 80.6 % of a threshold HR of 160 is 129 bpm.
EASY_RUN = {
    "date": "2026-11-22",
    "name": "Lockerer Dauerlauf",
    "sport": "run",
    "duration_minutes": 45,
    "session_focus": "easy",
    "hr_cap": {"percent_lthr": 80.6, "low_percent_lthr": 65.0, "source": "measured"},
}
INTERVALS = {
    "name": "Schwelle",
    "sport": "run",
    "planned_duration_s": 3600,
    "session_focus": "threshold",
    "description": "Warmup\n- 15m Z1\n\nMain Set 5x\n- 4m Z4\n- 2m Z1\n\nCooldown\n- 10m Z1",
}


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "CREATE TABLE provider_refresh_history (id TEXT PRIMARY KEY, provider TEXT NOT NULL, "
        "area TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, "
        "error_code TEXT);"
        "CREATE TABLE competitions (id TEXT PRIMARY KEY, event_date TEXT NOT NULL);"
        "CREATE TABLE athlete_checkins (checkin_date TEXT PRIMARY KEY);"
        "CREATE TABLE planned_units (local_id TEXT PRIMARY KEY, payload TEXT NOT NULL);"
        "CREATE TABLE activity_sessions (activity_id TEXT PRIMARY KEY, "
        "primary_provider TEXT NOT NULL, state TEXT NOT NULL, "
        "started_at TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE observations (id INTEGER PRIMARY KEY, metric TEXT NOT NULL, "
        "observed_on TEXT NOT NULL, state TEXT NOT NULL);"
    )
    return connection


class Sender:
    """A mocked push service: records each delivery and answers ``status``."""

    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, subscription, message, private, *, ttl) -> int:
        self.sent.append((subscription["endpoint"], message))
        return self.status


def _service(case: Any, sender: Sender | None = None) -> PushService:
    case.connection = _database()
    case.key_values = KeyValueRepository(lambda: NOW.isoformat())
    case.sender = sender or Sender()
    case.clock = {"now": NOW}
    return PushService(
        lambda: FakeManager(case.connection),
        case.key_values,
        lambda: case.clock["now"],
        sender=case.sender,
    )


class WebPushTransportTests(unittest.TestCase):
    def test_payload_encryption_matches_the_rfc_8291_example(self):
        body = web_push.encrypt(
            RFC_PLAINTEXT,
            RFC_RECEIVER_PUBLIC,
            RFC_AUTH,
            sender_key=RFC_SENDER_PRIVATE,
            salt=web_push.unb64url(RFC_SALT),
        )
        self.assertEqual(web_push.b64url(body), RFC_BODY)

    def test_vapid_token_is_an_es256_jwt_for_the_endpoint_origin(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        private = web_push.generate_private_key()
        header = web_push.vapid_authorization(ENDPOINT, private, NOW.timestamp())
        token, key = header.removeprefix("vapid t=").split(", k=")
        self.assertEqual(key, web_push.public_key(private))
        head, claims, signature = token.split(".")
        self.assertEqual(
            json.loads(web_push.unb64url(head)), {"typ": "JWT", "alg": "ES256"}
        )
        self.assertEqual(
            json.loads(web_push.unb64url(claims)),
            {
                "aud": "https://fcm.googleapis.com",
                "exp": int(NOW.timestamp()) + 43200,
                "sub": "mailto:push@pacecraft.invalid",
            },
        )
        raw = web_push.unb64url(signature)
        public = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), web_push.unb64url(key)
        )
        public.verify(
            encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:])),
            f"{head}.{claims}".encode(),
            ec.ECDSA(hashes.SHA256()),
        )

    def test_only_chrome_and_firefox_push_services_are_accepted(self):
        cases = {
            ENDPOINT: True,
            "https://updates.push.services.mozilla.com/wpush/v2/fake": True,
            "http://fcm.googleapis.com/fcm/send/x": False,
            "https://fcm.googleapis.com:8443/fcm/send/x": False,
            "https://web.push.apple.com/fake": False,
            "https://wns2-par02p.notify.windows.com/w/?token=x": False,
            "https://fcm.googleapis.com.evil.example/x": False,
            None: False,
        }
        for endpoint, accepted in cases.items():
            with self.subTest(endpoint=endpoint):
                self.assertIs(web_push.supported_endpoint(endpoint), accepted)

    def test_send_posts_the_encrypted_body_with_vapid_and_reports_the_status(self):
        requests = []

        class Response:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def opener(request, timeout):
            requests.append(request)
            return Response()

        status = web_push.send(
            {"endpoint": ENDPOINT, "p256dh": RFC_RECEIVER_PUBLIC, "auth": RFC_AUTH},
            {"title": "t"},
            web_push.generate_private_key(),
            ttl=60,
            opener=opener,
        )
        self.assertEqual(status, 201)
        (request,) = requests
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_header("Content-encoding"), "aes128gcm")
        self.assertEqual(request.get_header("Ttl"), "60")
        self.assertTrue(request.get_header("Authorization").startswith("vapid t="))
        self.assertNotIn(b'"title"', request.data)


class PushServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = _service(self)

    def test_subscribe_stores_one_device_and_refuses_other_services(self):
        self.assertTrue(self.service.subscribe(SUBSCRIPTION)["subscribed"])
        self.service.subscribe(SUBSCRIPTION)
        stored = json.loads(
            self.key_values.get(self.connection, PUSH_SUBSCRIPTIONS_KEY)
        )
        self.assertEqual([item["endpoint"] for item in stored], [ENDPOINT])
        for payload, reason in (
            (
                {**SUBSCRIPTION, "endpoint": "https://web.push.apple.com/x"},
                "unsupported_push_endpoint",
            ),
            (
                {**SUBSCRIPTION, "keys": {"p256dh": "AAAA", "auth": RFC_AUTH}},
                "invalid_push_keys",
            ),
            (
                # 65 bytes, but no point on the curve.
                {**SUBSCRIPTION, "keys": {"p256dh": NOT_A_POINT, "auth": RFC_AUTH}},
                "invalid_push_keys",
            ),
            ([], "unsupported_push_endpoint"),
        ):
            with self.subTest(reason=reason), self.assertRaises(AppError) as raised:
                self.service.subscribe(payload)
            self.assertEqual(raised.exception.reason, reason)

    def test_a_repeated_subscription_keeps_when_the_device_first_subscribed(self):
        self.service.subscribe(SUBSCRIPTION)
        first = self.service.subscribed_since()
        self.clock["now"] = NOW + timedelta(days=3)
        self.service.subscribe(SUBSCRIPTION)
        self.assertEqual(self.service.subscribed_since(), first)

    def test_unsubscribe_removes_the_device(self):
        self.service.subscribe(SUBSCRIPTION)
        self.assertFalse(self.service.unsubscribe({"endpoint": ENDPOINT})["subscribed"])
        self.assertFalse(self.service.state()["subscribed"])

    def test_key_is_generated_once_and_kept_secret(self):
        first = self.service.prepare()["public_key"]
        self.assertEqual(self.service.prepare()["public_key"], first)
        self.assertEqual(len(web_push.unb64url(first)), 65)
        self.assertLessEqual(PUSH_SECRET_KEYS, CREDENTIAL_KV_KEYS)

    def test_each_event_is_sent_once_and_a_switched_off_type_never(self):
        event = Notification(
            "upcoming", "competition:c1:2026-11-25", "T", "B", "#plan/season"
        )
        self.assertFalse(self.service.notify(event))  # no device yet: not recorded
        self.service.subscribe(SUBSCRIPTION)
        self.assertTrue(self.service.notify(event))
        self.assertFalse(self.service.notify(event))
        self.assertEqual(
            self.sender.sent,
            [
                (
                    ENDPOINT,
                    {
                        "title": "T",
                        "body": "B",
                        "url": "#plan/season",
                        # One tag per event: two events never replace each other.
                        "tag": "competition:c1:2026-11-25",
                    },
                )
            ],
        )
        self.service.set_preferences({"types": {"upcoming": False}})
        off = Notification(
            "upcoming", "competition:c2:2026-11-25", "T", "B", "#plan/season"
        )
        self.assertFalse(self.service.notify(off))
        self.service.set_preferences({"types": {"upcoming": True}})
        self.assertFalse(self.service.notify(off))  # handled while off: no replay
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(
            diagnostics(self.key_values, self.connection),
            {"subscriptions": 1, "per_type": {"upcoming": {"sent": 1, "failed": 0}}},
        )

    def test_preferences_default_on_and_refuse_unknown_types(self):
        self.assertEqual(self.service.state()["types"], dict.fromkeys(TYPES, True))
        for payload in (
            {"types": {"unknown": True}},
            {"types": {"upcoming": 1}},
            {"types": {}},
        ):
            with self.subTest(payload=payload), self.assertRaises(AppError):
                self.service.set_preferences(payload)

    def test_a_gone_subscription_is_removed_and_the_log_names_no_endpoint(self):
        self.sender.status = 410
        self.service.subscribe(SUBSCRIPTION)
        with self.assertLogs("intervals_coach", "WARNING") as logs:
            delivered = self.service.notify(
                Notification("upcoming", "k", "T", "B", "#plan")
            )
        self.assertFalse(delivered)
        self.assertFalse(self.service.state()["subscribed"])
        self.assertNotIn(
            "fcm.googleapis", "".join(logs.output) + repr(logs.records[0].__dict__)
        )
        self.assertEqual(
            diagnostics(self.key_values, self.connection)["per_type"],
            {"upcoming": {"sent": 0, "failed": 1}},
        )

    def test_a_failed_delivery_is_tried_again_and_still_sent_once(self):
        event = Notification("upcoming", "competition:c1:2026-11-25", "T", "B", "#p")
        self.service.subscribe(SUBSCRIPTION)
        for status in (0, 503, 429):
            self.sender.status = status
            with (
                self.subTest(status=status),
                self.assertLogs("intervals_coach", "WARNING"),
            ):
                self.assertFalse(self.service.notify(event))
        self.sender.status = 201
        self.assertTrue(self.service.notify(event))
        self.assertFalse(self.service.notify(event))
        self.assertEqual(len(self.sender.sent), 4)

    def test_a_refusal_is_not_retried(self):
        event = Notification("upcoming", "competition:c1:2026-11-25", "T", "B", "#p")
        self.service.subscribe(SUBSCRIPTION)
        self.sender.status = 403
        with self.assertLogs("intervals_coach", "WARNING"):
            self.assertFalse(self.service.notify(event))
        self.assertFalse(self.service.notify(event))
        self.assertEqual(len(self.sender.sent), 1)
        self.assertTrue(self.service.state()["subscribed"])

    def test_an_unusable_subscription_is_dropped_without_stopping_delivery(self):
        def opener(request, timeout):
            raise AssertionError("an unusable subscription is never sent")

        def sender(subscription, message, private, *, ttl):
            return web_push.send(subscription, message, private, ttl=ttl, opener=opener)

        service = _service(self, sender=sender)
        service.subscribe(SUBSCRIPTION)
        stored = json.loads(
            self.key_values.get(self.connection, PUSH_SUBSCRIPTIONS_KEY)
        )
        stored[0]["p256dh"] = NOT_A_POINT
        self.key_values.set(self.connection, PUSH_SUBSCRIPTIONS_KEY, json.dumps(stored))
        event = Notification("upcoming", "k", "T", "B", "#plan")
        with self.assertLogs("intervals_coach", "WARNING"):
            self.assertFalse(service.notify(event))
        self.assertFalse(service.state()["subscribed"])

    def test_a_still_current_event_never_ages_out_of_the_record(self):
        event = Notification(
            "upcoming", "step_test:ftp:Ride:2026-09-27", "T", "B", "#p"
        )
        self.service.subscribe(SUBSCRIPTION)
        self.assertTrue(self.service.notify(event))
        for days in (30, 60, 90):
            self.clock["now"] = NOW + timedelta(days=days)
            self.assertFalse(self.service.notify(event))
        self.assertEqual(len(self.sender.sent), 1)


class PushChecksTests(unittest.TestCase):
    def setUp(self):
        self.service = _service(self)
        self.service.subscribe(SUBSCRIPTION)
        self.local = {"now": datetime(2026, 11, 22, 9, 0, tzinfo=UTC)}
        self.hint = {"value": None}
        self.warnings = {"value": None}
        # What the other owners report: equipment, records, forecast, server, RPE.
        self.state: dict[str, Any] = {
            "equipment": None,
            "records": None,
            "days": None,
            "server": None,
            "unrated": [],
            "offers": None,
        }
        self.checks = PushChecks(
            self.service,
            lambda: FakeManager(self.connection),
            lambda: self.local["now"],
            infection_hint=lambda: self.hint["value"],
            threshold_warnings=lambda: self.warnings["value"],
            equipment=lambda: self.state["equipment"],
            records=lambda: self.state["records"],
            threshold_offers=lambda: self.state["offers"],
            weather_days=lambda: self.state["days"],
            server_state=lambda: self.state["server"],
            unrated_sessions=lambda day: self.state["unrated"],
            lthr_for=lambda: lambda unit: 160.0,
        )

    def _refresh(
        self, ident, status, minutes, code=None, provider="garmin", area="data"
    ):
        at = (NOW + timedelta(minutes=minutes)).isoformat()
        self.connection.execute(
            "INSERT INTO provider_refresh_history VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ident, provider, area, at, at, status, code),
        )

    def _bodies(self):
        return [message["body"] for _endpoint, message in self.sender.sent]

    def test_a_repeated_failure_is_announced_once_per_streak(self):
        self._refresh("r1", "success", 1)
        self._refresh("r2", "error", 2, "timeout")
        self._refresh("r3", "skipped", 3)
        self._refresh("r4", "error", 4, "timeout")
        self.checks.check()
        self.assertEqual(self.sender.sent, [])  # two failures are no streak yet
        self._refresh("r5", "error", 5, "timeout")
        self._refresh("r6", "error", 6, "timeout")
        self.checks.check()
        self.checks.check()
        self.assertEqual(
            self._bodies(), ["Eine Synchronisierung schlägt wiederholt fehl."]
        )
        self.assertEqual(self.sender.sent[0][1]["url"], "#more/connections")

    def test_a_streak_longer_than_the_rows_read_is_announced_once(self):
        self._refresh("ok", "success", 1)
        for minute in range(2, 40):
            self._refresh(f"e{minute}", "error", minute, "timeout")
            self.checks.check()
        self.assertEqual(len(self.sender.sent), 1)

    def test_the_checks_hold_the_gate_only_around_database_steps(self):
        events = []
        gate = {"held": False}

        class Gate:
            def __enter__(self):
                gate["held"] = True
                events.append("enter")

            def __exit__(self, *_exc):
                gate["held"] = False
                events.append("exit")

        def sender(subscription, message, private, *, ttl):
            events.append(("send", gate["held"]))
            return 201

        service = _service(self, sender=sender)
        service.subscribe(SUBSCRIPTION)
        self.connection.execute("INSERT INTO competitions VALUES ('c1', '2026-11-23')")
        checks = PushChecks(
            service,
            lambda: FakeManager(self.connection),
            lambda: self.local["now"],
            operation=Gate,
        )
        self.assertEqual(checks.check(), 1)
        # Events read, then claim, send and settle per event.
        self.assertEqual(
            events,
            ["enter", "exit", "enter", "exit", ("send", False), "enter", "exit"],
        )

    def test_a_delivery_that_raises_is_retried_on_the_next_check(self):
        calls = {"n": 0}

        def sender(subscription, message, private, *, ttl):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("protocol error")
            return 201

        service = _service(self, sender=sender)
        service.subscribe(SUBSCRIPTION)
        self.connection.execute("INSERT INTO competitions VALUES ('c1', '2026-11-23')")
        checks = PushChecks(
            service, lambda: FakeManager(self.connection), lambda: self.local["now"]
        )
        with self.assertLogs("intervals_coach", "WARNING"):
            self.assertEqual(checks.check(), 0)
        self.assertEqual(checks.check(), 1)
        self.assertEqual(checks.check(), 0)

    def test_an_expired_sign_in_is_announced_at_once(self):
        self._refresh(
            "w1", "error", 2, "auth_required", provider="wahoo", area="activities"
        )
        self.checks.check()
        self.assertEqual(self._bodies(), ["Eine Anmeldung ist abgelaufen."])

    def test_a_failure_before_the_subscription_is_not_announced(self):
        self._refresh("old", "error", -60, "auth_required")
        self.checks.check()
        self.assertEqual(self.sender.sent, [])

    def test_a_competition_within_three_days_is_announced_once(self):
        self.connection.executemany(
            "INSERT INTO competitions VALUES (?, ?)",
            [("c1", "2026-11-25"), ("c2", "2026-11-26"), ("c0", "2026-11-21")],
        )
        self.checks.check()
        self.checks.check()
        self.assertEqual(self._bodies(), ["In 3 Tagen ist ein Wettkampf."])
        self.assertEqual(self.sender.sent[0][1]["url"], "#plan/season")

    def test_the_infection_hint_is_announced_once_a_day(self):
        self.hint["value"] = {"status": "hint"}
        self.checks.check()
        self.checks.check()
        self.assertEqual(self._bodies(), ["Bitte bestätige kurz, wie es dir geht."])

    def test_a_step_test_is_announced_once_when_a_threshold_newly_turns_stale(self):
        # The subscription is on 2026-11-22; a value is stale from day 57 on (8 weeks).
        def item(metric, dated_on, reason="stale"):
            return {
                "metric": metric,
                "sport": "Ride",
                "dated_on": dated_on,
                "reason": reason,
            }

        self.warnings["value"] = {
            "items": [
                item("ftp", "2026-09-26"),  # stale since 2026-11-22: not new
                item("threshold_hr", "2026-09-27", "sdk_failed"),  # no staleness
            ]
        }
        self.checks.check()
        self.assertEqual(self.sender.sent, [])
        self.warnings["value"]["items"].append(item("threshold_hr", "2026-09-27"))
        self.checks.check()  # stale from 2026-11-23 on: newly stale
        self.checks.check()
        self.assertEqual(
            self._bodies(), ["Ein Schwellenwert ist älter als acht Wochen."]
        )
        self.assertEqual(self.sender.sent[0][1]["url"], "#more/profile")
        # Two values were announced together; updating one never re-announces the other.
        self.warnings["value"]["items"].append(item("ftp", "2026-09-28"))
        self.checks.check()
        self.assertEqual(len(self.sender.sent), 2)
        self.warnings["value"]["items"].pop()
        self.checks.check()
        self.assertEqual(len(self.sender.sent), 2)

    def test_nothing_is_sent_at_night_and_it_waits_for_the_morning(self):
        self.connection.execute("INSERT INTO competitions VALUES ('c1', '2026-11-23')")
        for hour, expected in ((23, 0), (6, 0), (7, 1)):
            with self.subTest(hour=hour):
                self.local["now"] = datetime(2026, 11, 22, hour, 0, tzinfo=UTC)
                self.checks.check()
                self.assertEqual(len(self.sender.sent), expected)

    def test_without_a_device_nothing_is_checked(self):
        self.service.unsubscribe({})
        self.connection.execute("INSERT INTO competitions VALUES ('c1', '2026-11-23')")
        self.assertEqual(self.checks.check(), 0)

    def _at(self, hour, day=22):
        self.local["now"] = datetime(2026, 11, day, hour, 0, tzinfo=UTC)

    def _titled(self, title):
        return [m for _endpoint, m in self.sender.sent if m["title"] == title]

    def _announced(self, title, body, url):
        """Two checks send the title exactly once; its body is fixed and digit-free."""
        self.checks.check()
        self.checks.check()
        (message,) = self._titled(title)
        self.assertEqual((message["body"], message["url"]), (body, url))
        self.assertFalse(any(char.isdigit() for char in message["body"]))
        return message

    def _unit(self, local_id, **payload):
        self.connection.execute(
            "INSERT INTO planned_units VALUES (?, ?)", (local_id, json.dumps(payload))
        )

    def _session(self, ident, created_minutes, started="2026-11-22T08:00:00+00:00"):
        created = (NOW + timedelta(minutes=created_minutes)).isoformat()
        self.connection.execute(
            "INSERT INTO activity_sessions VALUES (?, 'garmin', 'active', ?, ?)",
            (ident, started, created),
        )

    def test_the_morning_plan_names_todays_units_once_a_day(self):
        self._unit("u0", date="2026-11-23", name="Morgen", sport="Swim")
        self._unit("u1", date="2026-11-22", name="Archiviert", archived=True)
        self._unit("u_rest", date="2026-11-22", name="Ruhetag", duration_minutes=0)
        self.checks.check()
        self.assertEqual(self.sender.sent, [])  # tomorrow's, archived and rest units
        self._unit("u2", date="2026-11-22", name="Schwimmen", sport="Swim")
        self._unit("u3", **EASY_RUN)
        self._at(6)
        self.checks.check()
        self.assertEqual(self.sender.sent, [])  # not before 07:00
        self._at(7)
        self.checks.check()
        self.checks.check()
        (message,) = self._titled("Heute")
        self.assertEqual(
            (message["body"], message["url"]),
            ("Schwimmen\nLockerer Dauerlauf · 45 min · Puls bis 129", "#plan/overview"),
        )
        self._at(8, day=23)
        self.checks.check()
        self.assertEqual(len(self._titled("Heute")), 2)  # a new day, a new key

    def test_each_unit_line_names_its_duration_and_key_target(self):
        cases = {
            "an easy unit with a cap": (
                EASY_RUN,
                160.0,
                "Lockerer Dauerlauf · 45 min · Puls bis 129",
            ),
            "a cap without a threshold HR": (
                EASY_RUN,
                None,
                "Lockerer Dauerlauf · 45 min",
            ),
            "an interval unit named by its structure": (
                {**INTERVALS, "name": "Intervalle 5×4 min"},
                None,
                "Intervalle 5×4 min · 60 min",
            ),
            "an interval unit": (INTERVALS, None, "Schwelle · 60 min · 5×4 min"),
            "strength": (
                {
                    "name": "Kraft Oberkörper",
                    "sport": "strength",
                    "duration_minutes": 30,
                },
                None,
                "Kraft Oberkörper · 30 min",
            ),
            "a missing duration": ({"name": "Lauf", "sport": "run"}, None, "Lauf"),
        }
        for label, (unit, lthr, line) in cases.items():
            with self.subTest(label):
                self.assertEqual(unit_line(unit, lthr), line)
        event = morning_plan_event(
            [
                ("a", {**EASY_RUN, "date": "2026-11-22"}),
                ("b", {**INTERVALS, "date": "2026-11-22"}),
            ],
            NOW.date(),
            lambda unit: 160.0,
        )
        self.assertEqual(
            event.body,
            "Lockerer Dauerlauf · 45 min · Puls bis 129\nSchwelle · 60 min · 5×4 min",
        )
        self.assertEqual(event.key, "morning_plan:2026-11-22")

    def test_new_sessions_are_announced_as_one_and_only_when_recent(self):
        self._session("act_old", -30)  # stored before the subscription
        self._session("act_backfill", 40, started="2026-11-01T08:00:00+00:00")
        self.connection.execute(
            "INSERT INTO activity_sessions VALUES ('act_i', 'intervals', 'active', "
            "'2026-11-22T08:00:00+00:00', ?)",
            ((NOW + timedelta(minutes=50)).isoformat(),),
        )
        self._at(11)
        self.checks.check()
        self.assertEqual(self.sender.sent, [])
        self._session("act_1", 30)
        self._session("act_2", 45)
        self._announced(
            "Neue Einheit", "Eine neue Einheit ist synchronisiert.", "#plan/overview"
        )
        self._session("act_3", 90)
        self.checks.check()
        self.assertEqual(len(self._titled("Neue Einheit")), 2)

    def test_the_night_values_are_announced_once_when_they_are_dated_today(self):
        self.connection.executemany(
            "INSERT INTO observations (metric, observed_on, state) VALUES (?, ?, ?)",
            [("hrv_rmssd", "2026-11-21", "active"), ("sleep_score", "2026-11-22", "x")],
        )
        self.checks.check()
        self.assertEqual(self.sender.sent, [])  # yesterday's, and not an active one
        self.connection.execute(
            "INSERT INTO observations (metric, observed_on, state) "
            "VALUES ('sleep_duration', '2026-11-22', 'active')"
        )
        self._announced(
            "Morgenwerte", "Die Werte der Nacht sind da.", "#analysis/recovery"
        )

    def test_the_check_in_reminder_follows_tonights_sleep_and_never_a_stored_check_in(
        self,
    ):
        # #3953: sleep dated today and no check-in of today: once, opening the form.
        insert = "INSERT INTO observations (metric, observed_on, state) VALUES (?, ?, 'active')"
        self.connection.execute(insert, ("sleep_score", "2026-11-21"))
        self.connection.execute(insert, ("hrv_rmssd", "2026-11-22"))
        self.checks.check()
        self.assertEqual(self._titled("Check-in"), [])  # yesterday's sleep, today's HRV
        self.connection.execute(insert, ("sleep_duration", "2026-11-22"))
        self._announced("Check-in", "Wie fühlst du dich heute?", "#plan/checkin")
        # The next day has sleep and a check-in already: no reminder.
        self.local["now"] = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
        self.connection.execute(insert, ("sleep_score", "2026-11-23"))
        self.connection.execute("INSERT INTO athlete_checkins VALUES ('2026-11-23')")
        self.checks.check()
        self.assertEqual(len(self._titled("Check-in")), 1)

    def test_equipment_at_its_interval_is_announced_once_per_maintenance(self):
        def item(ident, due, *done, status="active"):
            return {
                "id": ident,
                "status": status,
                "usage": {"maintenance_due": due},
                "maintenance": [{"date": day} for day in done],
            }

        self.state["equipment"] = {
            "items": [
                item("e0", False),
                item("e1", None),
                item("e2", True, status="archived"),
                item("e3", True, "2026-10-01"),
            ]
        }
        self._announced(
            "Wartung fällig",
            "Ein Ausrüstungsteil hat sein Wartungsintervall erreicht.",
            "#more/equipment",
        )
        # Maintained, then due again: the last maintenance makes a new key.
        self.state["equipment"]["items"][3] = item(
            "e3", True, "2026-10-01", "2026-11-10"
        )
        self.checks.check()
        self.assertEqual(len(self._titled("Wartung fällig")), 2)

    def test_a_record_after_the_subscription_is_announced_once(self):
        def record(activity_id, day, status="ok", **fields):
            return {
                "sport": "ride",
                "environment": "outdoor",
                "duration_seconds": 300,
                "activity_id": activity_id,
                "date": day,
                "status": status,
                **fields,
            }

        window = {
            "power": [
                record("a0", "2026-11-20", duration_seconds=60),  # before
                record("a1", "2026-11-22"),
                record(None, None, "unknown", duration_seconds=5),
            ],
            "running": {"speed": [], "distance": []},
            "activity_records": [],
        }
        self.state["records"] = {"all_time": window}
        self._at(11)
        self.checks.check()
        self.assertEqual(self.sender.sent, [])  # no session arrived: not read
        self._session("act_1", 30)
        self._announced(
            "Neue Bestleistung", "Du hast eine neue Bestleistung.", "#analysis/records"
        )
        window["activity_records"].append(
            {
                "sport": "run",
                "field": "distance_m",
                "status": "ok",
                "activity_id": "a2",
                "date": "2026-11-22",
            }
        )
        self.checks.check()
        self.assertEqual(len(self._titled("Neue Bestleistung")), 2)
        # A record first seen long after its day is no news (its key could have aged out).
        window["power"].append(record("a3", "2026-11-22", duration_seconds=1200))
        self._at(11, day=30)
        self._session("act_2", 60 * 24 * 8, started="2026-11-30T08:00:00+00:00")
        self.checks.check()
        self.assertEqual(len(self._titled("Neue Bestleistung")), 2)

    def test_a_new_threshold_is_announced_once_per_sport_and_value(self):
        def offer(sport, source, value, newest="2026-11-23"):
            return {
                "key": f"lt2:{sport}:power:{source}:{value}",
                "sport": sport,
                "source": source,
                "value": value,
                "newest": newest,
            }

        offers = [
            offer("run", "dfa_alpha1", 171, "2026-11-22"),  # open before: not news
            offer("run", "garmin", 168),
            offer("ride", "garmin", 280, None),  # undated: no news (Gitar, #4135)
        ]
        self.state["offers"] = []
        self._at(11)
        self.checks.check()  # the first read only seeds
        self.state["offers"] = offers
        self._announced(
            "Neue Schwelle für Laufen",
            "Von Garmin gibt es einen neuen Wert. Tippen zum Ansehen.",
            "#more/profile/run",
        )
        self.assertEqual(self._titled("Neue Schwelle für Radfahren"), [])
        # Two run offers and a ride offer at once: one notification per sport.
        offers += [
            offer("run", "critical_speed", 301),
            offer("run", "dfa_alpha1", 172),
            offer("ride", "critical_power", 250),
        ]
        self.checks.check()
        self.checks.check()
        run, ride = (
            self._titled("Neue Schwelle für Laufen")[1],
            self._titled("Neue Schwelle für Radfahren")[0],
        )
        self.assertEqual(
            run["body"],
            "Aus deinen letzten Läufen gibt es einen neuen Wert. Tippen zum Ansehen.",
        )
        self.assertEqual(
            (ride["body"], ride["url"]),
            (
                "Aus deinen letzten Fahrten gibt es einen neuen Wert. Tippen zum Ansehen.",
                "#more/profile/ride",
            ),
        )
        self.assertEqual(len(self.sender.sent), 3)
        for message in (run, ride):
            self.assertFalse(
                any(char.isdigit() for char in message["title"] + message["body"])
            )
        # A dismissed offer is no longer offered; the same values never repeat.
        offers.pop()
        self.checks.check()
        self.assertEqual(len(self.sender.sent), 3)
        offers.append(offer("run", "garmin", 169))  # a new value is a new notification
        self.checks.check()
        self.assertEqual(len(self.sender.sent), 4)

    def test_a_switched_off_new_threshold_is_marked_sent_without_delivery(self):
        self.service.set_preferences({"types": {"new_threshold": False}})
        offers = [
            {
                "key": "lt1:run:heart_rate:dfa_alpha1:140",
                "sport": "run",
                "source": "dfa_alpha1",
                "newest": "2026-11-23",
            }
        ]
        self._at(11)
        self.state["offers"] = []
        self.checks.check()  # seeds
        self.state["offers"] = offers
        self.checks.check()
        self.service.set_preferences({"types": {"new_threshold": True}})
        self.checks.check()
        self.assertEqual(self.sender.sent, [])

    def _offer(self, source, value, newest="2026-11-23"):
        return {
            "key": f"lt2:run:pace:{source}:{value}",
            "sport": "run",
            "source": source,
            "newest": newest,
        }

    def test_offers_open_before_the_first_read_are_seeded_not_announced(self):
        # The device subscribed on 2026-11-22; these offers have newer data but were
        # open when the feature began.
        self.state["offers"] = [self._offer("dfa_alpha1", 300)]
        self._at(11)
        self.checks.check()
        self.checks.check()
        self.assertEqual(self.sender.sent, [])
        self.state["offers"].append(self._offer("dfa_alpha1", 299))
        self.checks.check()
        self.assertEqual(len(self._titled("Neue Schwelle für Laufen")), 1)

    def test_a_group_names_the_origin_of_the_keys_it_announces(self):
        self.state["offers"] = []
        self._at(11)
        self.checks.check()
        local = self._offer("dfa_alpha1", 300)
        self.state["offers"] = [local]
        self.checks.check()
        self.state["offers"].append(self._offer("garmin", 290))
        self.checks.check()
        self.assertEqual(
            self._bodies()[-2:],
            [
                "Aus deinen letzten Läufen gibt es einen neuen Wert. Tippen zum Ansehen.",
                "Von Garmin gibt es einen neuen Wert. Tippen zum Ansehen.",
            ],
        )

    def test_an_offer_source_that_fails_skips_only_itself_and_a_failed_send_retries(
        self,
    ):
        self.state["offers"] = []
        self._at(11)
        self.checks.check()
        self.state["offers"] = [self._offer("garmin", 290)]
        self.sender.status = 503
        self.checks.check()  # no device received it: kept for the next check
        self.sender.status = 201
        self.checks.check()
        self.assertEqual(len(self._titled("Neue Schwelle für Laufen")), 2)
        self.assertEqual(len(self.sender.sent), 2)
        self.checks.check()
        self.assertEqual(len(self.sender.sent), 2)
        self.state["offers"] = None
        self.hint["value"] = {"status": "hint"}
        self.checks._threshold_offers = lambda: 1 / 0
        self.checks.check()
        self.assertEqual(len(self.sender.sent), 3)  # the infection hint still came

    def test_missing_feedback_is_announced_once_a_day_from_the_evening(self):
        self.state["unrated"] = ["act_1"]
        self._at(19)
        self.checks.check()
        self.assertEqual(self.sender.sent, [])
        self._at(20)
        self._announced(
            "Feedback",
            "Für eine Einheit von heute fehlt noch dein Feedback.",
            "#plan/overview",
        )
        self.state["unrated"] = []
        self._at(21, day=23)
        self.checks.check()
        self.assertEqual(len(self._titled("Feedback")), 1)
        self.state["unrated"] = ["act_2"]
        self.checks.check()
        self.assertEqual(len(self._titled("Feedback")), 2)  # tomorrow's own key

    def test_server_problems_are_announced_once_per_problem_and_day(self):
        healthy = {
            "automatic": {
                "enabled": True,
                # Last night's copy, just before tonight's is written: not stale.
                "newest_age_hours": 30.0,
                "last_failure_age_hours": 40.0,  # older than the newest copy
            },
            "free_bytes": 50 * 2**30,
        }
        self.state["server"] = healthy
        self.checks.check()
        self.assertEqual(self.sender.sent, [])
        self.state["server"] = {**healthy, "automatic": {"enabled": False}}
        self.state["server"]["free_bytes"] = 2**20
        self.checks.check()  # a disabled backup is no problem; the disk is low
        self.assertEqual(len(self._titled("Server")), 1)
        self.state["server"] = {
            "automatic": {
                "enabled": True,
                "newest_age_hours": 40.0,
                "last_failure_age_hours": 2.0,
            },
            "free_bytes": 2**20,
        }
        self.checks.check()
        self.checks.check()
        self.assertEqual(len(self._titled("Server")), 2)  # a failed and a stale copy
        self._at(10, day=23)
        self.checks.check()
        self.assertEqual(len(self._titled("Server")), 3)
        message = self._titled("Server")[0]
        self.assertEqual(
            (message["body"], message["url"]),
            ("Backup oder Speicher braucht Aufmerksamkeit.", "#more/operations"),
        )

    def test_a_weather_warning_for_an_outdoor_unit_is_announced_in_its_window(self):
        self._unit("u1", date="2026-11-22", name="Waldlauf", sport="Run")
        self._unit("u2", date="2026-11-23", name="Ausfahrt", sport="Ride")
        self._unit(
            "u3", date="2026-11-22", name="Rolle", sport="Ride", environment="indoor"
        )
        self.state["days"] = [
            {"date": "2026-11-22", "apparent_temperature_max": 31.0},
            {"date": "2026-11-23", "weather_code": 95},
        ]
        self.checks.check()
        self.checks.check()
        self.assertEqual(len(self._titled("Wetter")), 1)  # today's run only
        self._at(17)
        self.checks.check()
        self.assertEqual(len(self._titled("Wetter")), 1)  # tomorrow from 18:00
        self._at(18)
        self.checks.check()
        self.checks.check()
        messages = self._titled("Wetter")
        self.assertEqual(len(messages), 2)
        self.assertEqual(
            (messages[0]["body"], messages[0]["url"]),
            (
                "Für eine geplante Einheit draußen gibt es einen Wetterhinweis.",
                "#plan/overview",
            ),
        )
        self.assertNotIn("Waldlauf", json.dumps(messages))
        self.state["days"] = []
        self._at(9, day=24)
        self._unit("u4", date="2026-11-24", name="Lauf", sport="Run")
        self.checks.check()
        self.assertEqual(len(self._titled("Wetter")), 2)  # a calm day

    def test_a_weather_warning_needs_heat_poor_air_high_pollen_or_a_storm(self):
        cases = {
            "calm": ({"apparent_temperature_max": 24.9, "weather_code": 3}, False),
            "heat": ({"apparent_temperature_max": 25.0}, True),
            # 60 is "mäßig" in the app; "schlecht" starts above it.
            "air": ({"air_quality": {"european_aqi_max": 61.0}}, True),
            "moderate air": ({"air_quality": {"european_aqi_max": 60.0}}, False),
            # DWD level 3 is high; the in-between "2-3" is not.
            "pollen": ({"pollen": {"levels": {"Birke": "3"}}}, True),
            "medium pollen": ({"pollen": {"levels": {"Birke": "2-3"}}}, False),
            "light pollen": ({"pollen": {"levels": {"Birke": "1-2"}}}, False),
            "storm": ({"weather_code": 95}, True),
            "no forecast": (None, False),
        }
        for name, (day, expected) in cases.items():
            with self.subTest(name):
                self.assertIs(day_warning(day), expected)

    def test_a_coach_answer_is_sent_at_once_and_at_any_hour(self):
        self._at(23)
        self.assertTrue(self.checks.notify(coach_answer_event("turn-1")))
        self.assertFalse(self.checks.notify(coach_answer_event("turn-1")))
        (message,) = self._titled("Coach")
        self.assertEqual(
            (message["body"], message["url"], message["tag"]),
            ("Deine Antwort ist da.", "#coach", "coach:turn-1"),
        )
        self.assertTrue(self.checks.notify(coach_answer_event("turn-2")))

    def test_a_source_that_fails_skips_only_itself(self):
        def broken():
            raise RuntimeError("source down")

        checks = PushChecks(
            self.service,
            lambda: FakeManager(self.connection),
            lambda: self.local["now"],
            equipment=broken,
            server_state=lambda: {"free_bytes": 1},
            lthr_for=lambda: lambda unit: broken(),
        )
        self._unit("u1", **EASY_RUN)
        with self.assertLogs("intervals_coach", "WARNING") as logs:
            self.assertEqual(checks.check(), 2)
        self.assertNotIn("source down", "".join(logs.output))
        self.assertEqual(len(self._titled("Server")), 1)
        # A failing threshold HR lookup leaves only the cap out.
        (today,) = self._titled("Heute")
        self.assertEqual(today["body"], "Lockerer Dauerlauf · 45 min")


class PushRouteTests(unittest.TestCase):
    def test_routes_reach_the_service(self):
        service = _service(self)

        class Handler:
            def __init__(self, body=None):
                self.body = body
                self.sent = None

            def read_json(self, _limit):
                return self.body

            def send_json(self, status, payload):
                self.sent = (status, payload)

        routes = PushRoutes(lambda: service)
        key = Handler()
        dispatch_route(routes, key, "/api/push/key", method="POST")
        self.assertEqual(len(web_push.unb64url(key.sent[1]["public_key"])), 65)
        subscribe = Handler(SUBSCRIPTION)
        dispatch_route(routes, subscribe, "/api/push/subscribe", method="POST")
        self.assertTrue(subscribe.sent[1]["subscribed"])
        state = Handler()
        dispatch_route(routes, state, f"/api/push?endpoint={ENDPOINT}")
        self.assertEqual(
            state.sent, (200, {"subscribed": True, "types": dict.fromkeys(TYPES, True)})
        )
        preferences = Handler({"types": {"weather": False}})
        dispatch_route(routes, preferences, "/api/push/preferences", method="POST")
        self.assertFalse(preferences.sent[1]["types"]["weather"])
        gone = Handler({"endpoint": ENDPOINT})
        dispatch_route(routes, gone, "/api/push/unsubscribe", method="POST")
        self.assertFalse(gone.sent[1]["subscribed"])


if __name__ == "__main__":
    unittest.main()
