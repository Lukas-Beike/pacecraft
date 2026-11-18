"""The newest entered weight mirrored to the Wahoo profile weight (#3559).

Wahoo is a mocked client: no request leaves the test.
"""

import threading
import unittest
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs

from support import temp_database

from backend.athlete.body_entry import BODY_FAT_ENTRY, BodyEntryService
from backend.db.repositories import KeyValueRepository
from backend.sync.executor import WahooJobAdapter
from backend.sync.wahoo_weight_push import (
    STATE_KEY,
    WahooWeightPushService,
    summarize_weight_push,
)
from tests.test_wahoo_client import ClientCase

DAY = date(2026, 11, 15)
EARLIER = date(2026, 11, 10)
FULL = "user_read user_write workouts_read"
ONLINE = {"connected": True, "scope": FULL, "connected_at": "2026-11-01T08:00:00Z"}
SECRET = "fake-wahoo-error-text-123"


class FakeClient:
    def __init__(self) -> None:
        self.weights: list[float] = []
        self.fail: Exception | None = None

    def update_user_weight(self, weight_kg: float) -> None:
        if self.fail:
            raise self.fail
        self.weights.append(weight_kg)


class WahooWeightPushTests(unittest.TestCase):
    def setUp(self):
        self.manager = temp_database(self, schema=True)
        self.client = FakeClient()
        self.status = dict(ONLINE)
        self.push = WahooWeightPushService(
            self.manager,
            lambda: self.client,
            lambda: self.status,
            lambda: "2026-11-16T10:00:00+00:00",
        )
        clock = {"now": lambda: "2026-11-16T10:00:00+00:00", "today": lambda: DAY}
        self.outcomes: list[str] = []

        def changed(day):
            self.outcomes.append(self.push.push())
            return "unchanged"

        self.weights = BodyEntryService(self.manager, **clock, on_change=changed)
        self.fats = BodyEntryService(
            self.manager, **clock, entry=BODY_FAT_ENTRY, on_change=changed
        )

    def counts(self):
        with self.manager.reader() as db:
            return summarize_weight_push(db)

    def refresh(self):
        WahooJobAdapter(
            sync=SimpleNamespace(sync=lambda days, reason, operation_id: {}),
            weight_push=self.push,
        ).execute({"id": "job"}, {}, "manual", job_type="refresh")

    def test_save_change_and_remove_write_the_newest_entered_weight(self):
        self.weights.save({"date": EARLIER.isoformat(), "kg": "81,2"})
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        # An older day's change leaves the newest weight, so nothing is sent.
        self.weights.save({"date": EARLIER.isoformat(), "kg": 81.0})
        self.weights.save({"date": DAY.isoformat(), "kg": 80.0})
        # Removing the newest writes the then-newest remaining one.
        self.weights.remove({"date": DAY.isoformat(), "confirmed": True})
        self.assertEqual([81.2, 80.4, 80.0, 81.0], self.client.weights)
        self.assertEqual(["sent", "sent", "unchanged", "sent", "sent"], self.outcomes)
        self.assertEqual(
            {"sent": 4, "failed": 0, "retried": 0, "pending": 0}, self.counts()
        )

    def test_no_write_without_an_entered_weight(self):
        # Removing every entry writes nothing; a body fat never reaches Wahoo.
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.weights.remove({"date": DAY.isoformat(), "confirmed": True})
        self.fats.save({"date": DAY.isoformat(), "pct": 15})
        self.assertEqual([80.4], self.client.weights)
        self.assertEqual(["sent", "unchanged", "unchanged"], self.outcomes)
        self.refresh()
        self.assertEqual([80.4], self.client.weights)

    def test_a_failure_keeps_the_save_and_the_next_refresh_retries_once(self):
        self.client.fail = RuntimeError(SECRET)
        with self.assertLogs("intervals_coach", "WARNING") as logs:
            self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertEqual(["pending"], self.outcomes)
        self.assertEqual([{"date": DAY.isoformat(), "kg": 80.4}],
                         self.weights.read()["entries"])  # fmt: skip
        self.client.fail = None
        self.refresh()
        self.assertEqual([80.4], self.client.weights)
        self.assertEqual(
            {"sent": 1, "failed": 1, "retried": 1, "pending": 0}, self.counts()
        )
        # Nothing pending: a later refresh writes nothing.
        self.refresh()
        self.assertEqual([80.4], self.client.weights)
        with self.manager.reader() as db:
            raw = KeyValueRepository(lambda: "").get(db, STATE_KEY)
        self.assertNotIn(SECRET, raw)

    def test_no_write_until_the_grant_has_user_write(self):
        self.status = {"connected": True, "scope": "user_read workouts_read"}
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.refresh()
        self.assertEqual([], self.client.weights)
        self.assertEqual(1, self.counts()["pending"])
        # After the reconnect with user_write the refresh sends the newest weight.
        self.status = dict(ONLINE)
        self.refresh()
        self.assertEqual([80.4], self.client.weights)
        self.assertEqual({"sent": 1, "failed": 0, "retried": 1, "pending": 0},
                         self.counts())  # fmt: skip

    def test_a_new_connection_is_written_anew(self):
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.status = dict(ONLINE, connected_at="2026-11-17T08:00:00Z")
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.assertEqual([80.4, 80.4], self.client.weights)

    def test_an_unanswered_write_is_never_taken_as_the_older_value(self):
        self.weights.save({"date": DAY.isoformat(), "kg": 80.0})
        # Wahoo applies 81.0 but the answer is lost; the athlete goes back to 80.0.
        self.client.fail = TimeoutError()
        with self.assertLogs("intervals_coach", "WARNING"):
            self.weights.save({"date": DAY.isoformat(), "kg": 81.0})
        self.client.fail = None
        self.weights.save({"date": DAY.isoformat(), "kg": 80.0})
        self.assertEqual([80.0, 80.0], self.client.weights)
        self.assertEqual(["sent", "pending", "sent"], self.outcomes)

    def test_a_push_stopped_mid_request_stays_pending(self):
        def stop(weight_kg):
            with self.manager.reader() as db:
                self.seen = summarize_weight_push(db)["pending"]
            raise SystemExit  # the process ends during the request

        self.client.update_user_weight = stop
        self.weights = BodyEntryService(
            self.manager, now=lambda: "x", today=lambda: DAY
        )
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        with self.assertRaises(SystemExit):
            self.push.push()
        self.assertEqual(1, self.seen)
        self.assertEqual(1, self.counts()["pending"])

    def test_a_disconnected_wahoo_is_not_written_or_remembered(self):
        self.status = {"connected": False, "scope": None}
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.status = dict(ONLINE)
        self.refresh()
        self.assertEqual([], self.client.weights)
        self.assertEqual(["off"], self.outcomes)

    def test_start_pushes_in_the_background(self):
        self.status = {"connected": False, "scope": None}
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.status = dict(ONLINE)
        self.push.start()
        for thread in threading.enumerate():
            if thread.name == "wahoo-weight-push":
                thread.join(5)
        self.assertEqual([80.4], self.client.weights)


class UpdateUserWeightTests(ClientCase):
    def test_puts_the_weight_form_to_the_user(self) -> None:
        self.request.return_value = {"id": 1}
        self.client.update_user_weight(80.44)
        call = self.request.call_args
        self.assertEqual("PUT", call.args[0])
        self.assertTrue(call.args[1].endswith("/v1/user"))
        self.assertEqual(
            {"user[weight]": ["80.4"]},
            parse_qs(call.kwargs["raw_body"].decode("ascii")),
        )


if __name__ == "__main__":
    unittest.main()
