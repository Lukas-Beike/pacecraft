"""Exactly-once answers for the offline outbox's four routes (#3761)."""

from __future__ import annotations

import http.client
import json
import threading
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from server_test_support import ServerTestCase, create_test_session, server

from backend.http_api import server as http_server_module
from backend.http_api.idempotency import KEY_PREFIX, valid_key
from backend.http_api.route_table import Access, Route
from backend.providers import http as provider_http

DAY = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()


class IdempotencyHttpTests(ServerTestCase):
    """The real routes on temporary storage; no provider is reached."""

    def setUp(self) -> None:
        super().setUp()
        for owner, name in (
            (server.PROVIDER_TRANSPORT.json_http_client(), "request"),
            (provider_http, "urlopen"),
        ):
            guard = patch.object(
                owner, name, side_effect=AssertionError("Unexpected network request")
            )
            guard.start()
            self.addCleanup(guard.stop)
        patcher = patch.object(
            server.app_config, "security_configuration_error", return_value=None
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        handler_class = server.HTTP_API.request_handler_class()
        handler_class.protocol_version = "HTTP/1.1"
        self.httpd = http_server_module.CoachHTTPServer(("127.0.0.1", 0), handler_class)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.session = create_test_session(server)

    def send(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        key: str | None = None,
        *,
        csrf: str | None = "csrf",
        session: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if session:
            headers["Cookie"] = f"ic_session={self.session}"
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if key is not None:
            headers["Idempotency-Key"] = key
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_port, timeout=30
        )
        connection.request(method, path, json.dumps(body), headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data)

    def weight(self, kg: float, key: str | None = None, **options: Any) -> Any:
        return self.send(
            "PUT", "/api/body/weight", {"date": DAY, "kg": kg}, key, **options
        )

    def stored_keys(self) -> list[str]:
        with server.database_manager().reader() as db:
            rows = db.execute(
                "SELECT key FROM kv WHERE substr(key, 1, ?) = ?",
                (len(KEY_PREFIX), KEY_PREFIX),
            ).fetchall()
        return [row["key"] for row in rows]

    def test_the_first_request_stores_its_result_under_the_key(self) -> None:
        key = str(uuid.uuid4())
        status, payload = self.weight(71.5, key)
        self.assertEqual(status, 200)
        self.assertEqual(self.stored_keys(), [KEY_PREFIX + key])
        with server.database_manager().reader() as db:
            stored = json.loads(
                str(server.KEY_VALUE_REPOSITORY.get(db, KEY_PREFIX + key))
            )
        self.assertEqual(stored["path"], "/api/body/weight")
        self.assertEqual(stored["status"], 200)
        self.assertEqual(stored["payload"], payload)

    def test_a_repeat_answers_the_stored_result_and_writes_nothing(self) -> None:
        key = str(uuid.uuid4())
        first = self.weight(71.5, key)
        # A different body under the same key must not overwrite the entry.
        again = self.weight(99.0, key)
        self.assertEqual(again, first)
        self.assertEqual(
            [item["kg"] for item in again[1]["entries"] if item["date"] == DAY],
            [71.5],
        )
        self.assertEqual(len(self.stored_keys()), 1)

    def test_the_same_key_on_another_path_is_refused(self) -> None:
        key = str(uuid.uuid4())
        self.weight(71.5, key)
        status, payload = self.send(
            "PUT", "/api/body/body-fat", {"date": DAY, "pct": 18}, key
        )
        self.assertEqual(status, 409)
        self.assertIn("Idempotenzschlüssel", payload["error"])

    def test_an_invalid_or_absent_key_behaves_as_before(self) -> None:
        for key in (None, "not-a-uuid", "", "1" * 36):
            with self.subTest(key=key):
                self.assertEqual(self.weight(72.0, key)[0], 200)
        self.assertEqual(self.stored_keys(), [])
        self.assertEqual(self.weight(73.0, "x")[0], 200)
        self.assertEqual(self.stored_weight_value(), 73.0)

    def stored_weight_value(self) -> float:
        _status, payload = self.send("GET", "/api/body/weight", {})
        return next(item["kg"] for item in payload["entries"] if item["date"] == DAY)

    def test_a_route_that_is_not_replayable_ignores_the_header(self) -> None:
        self.weight(71.5)
        key = str(uuid.uuid4())
        removal = {"date": DAY, "confirmed": True}
        self.assertEqual(
            self.send("PUT", "/api/body/weight/remove", removal, key)[0], 200
        )
        # A replay would answer 200 again; the route ran and found nothing.
        self.assertEqual(
            self.send("PUT", "/api/body/weight/remove", removal, key)[0], 404
        )
        self.assertEqual(self.stored_keys(), [])

    def test_a_refused_request_is_not_stored(self) -> None:
        key = str(uuid.uuid4())
        status, _payload = self.weight(5.0, key)
        self.assertEqual(status, 400)
        self.assertEqual(self.stored_keys(), [])
        self.assertEqual(self.weight(71.5, key)[0], 200)

    def test_entries_older_than_thirty_days_are_dropped_when_storing(self) -> None:
        old, recent = str(uuid.uuid4()), str(uuid.uuid4())
        self.weight(71.5, old)
        with server.database_manager().unit_of_work() as db:
            db.execute(
                "UPDATE kv SET updated_at = ? WHERE key = ?",
                (
                    (datetime.now(UTC) - timedelta(days=31)).isoformat(),
                    KEY_PREFIX + old,
                ),
            )
        self.weight(72.5, recent)
        self.assertEqual(self.stored_keys(), [KEY_PREFIX + recent])

    def test_session_and_csrf_are_still_checked(self) -> None:
        key = str(uuid.uuid4())
        self.weight(71.5, key)
        self.assertEqual(self.weight(99.0, key, csrf=None)[0], 403)
        self.assertEqual(self.weight(99.0, key, csrf="wrong")[0], 403)
        self.assertEqual(self.weight(99.0, key, session=False)[0], 401)
        self.assertEqual(self.stored_weight_value(), 71.5)

    def test_the_other_replayable_routes_answer_a_repeat_once(self) -> None:
        for path, body in (
            ("/api/nutrition/entry", {"description": "Apfel", "kcal": 80}),
            ("/api/body/body-fat", {"date": DAY, "pct": 18.5}),
        ):
            with self.subTest(path=path):
                key = str(uuid.uuid4())
                method = "POST" if "nutrition" in path else "PUT"
                first = self.send(method, path, body, key)
                self.assertEqual(self.send(method, path, body, key), first)
        with server.database_manager().reader() as db:
            count = db.execute(
                "SELECT COUNT(*) AS n FROM nutrition_logs WHERE description = ?",
                ("Apfel",),
            ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_a_meal_and_its_key_commit_together_or_not_at_all(self) -> None:
        # Orchestrator on #3816: exactly once. When keeping the key fails inside
        # the write's transaction, the meal is rolled back too; the retry with the
        # same key then logs it once.
        from backend.http_api.idempotency import IdempotencyStore

        key = str(uuid.uuid4())
        body = {"description": "Birne", "kcal": 90}
        with patch.object(
            IdempotencyStore, "store", side_effect=RuntimeError("crash before commit")
        ):
            status, _ = self.send("POST", "/api/nutrition/entry", body, key)
        self.assertEqual(status, 500)
        self.assertEqual(self.meals("Birne"), 0)
        self.assertEqual(self.stored_keys(), [])
        first = self.send("POST", "/api/nutrition/entry", body, key)
        self.assertEqual(first[0], 200)
        self.assertEqual(self.send("POST", "/api/nutrition/entry", body, key), first)
        self.assertEqual(self.meals("Birne"), 1)

    def meals(self, description: str) -> int:
        with server.database_manager().reader() as db:
            return db.execute(
                "SELECT COUNT(*) AS n FROM nutrition_logs WHERE description = ?",
                (description,),
            ).fetchone()["n"]

    def test_only_weight_and_body_fat_keep_their_answer_after_the_commit(self) -> None:
        # Their save is followed by the Garmin push in the same request; one value
        # per day, so a repeat after a lost answer sets the same value again.
        after_commit = {
            (route.method, route.path)
            for route in server.HTTP_API.route_table.routes
            if route.replayable and not route.replay_with_write
        }
        self.assertEqual(
            after_commit, {("PUT", "/api/body/weight"), ("PUT", "/api/body/body-fat")}
        )


class ValidKeyTests(unittest.TestCase):
    def test_only_a_uuid_is_a_key(self) -> None:
        key = str(uuid.uuid4())
        self.assertEqual(valid_key(key.upper()), key)
        for value in (None, "", "abc", key + "0", "{" + key + "}", "urn:uuid:" + key):
            self.assertIsNone(valid_key(value), value)

    def test_replayable_is_decided_by_the_reviewed_list(self) -> None:
        def handle(_handler: Any, _request: Any) -> None:
            return None

        with self.assertRaises(ValueError):
            Route("POST", "/api/other", Access.SESSION_CSRF, handle, replayable=True)
        with self.assertRaises(ValueError):
            Route("PUT", "/api/body/weight", Access.SESSION_CSRF, handle)
        Route("PUT", "/api/body/weight", Access.SESSION_CSRF, handle, replayable=True)


if __name__ == "__main__":
    unittest.main()
