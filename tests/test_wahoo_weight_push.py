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
from backend.errors import AppError
from backend.sync.executor import WahooJobAdapter
from backend.sync.wahoo_weight_push import (
    STATE_KEY,
    WahooWeightPushService,
    push_state,
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
            {"sent": 4, "failed": 0, "retried": 0, "pending": 0,
             "last_outcome": "sent"},
            self.counts(),
        )  # fmt: skip

    def test_an_undo_leaves_the_restored_weight_for_the_next_refresh(self):
        # #3640: a Coach turn undo writes to no provider; it marks the weight
        # pending, and the next Wahoo refresh sends the then-newest entry.
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.assertEqual([80.4], self.client.weights)
        with self.manager.unit_of_work():
            # The undo put an earlier value back inside its own transaction.
            self.push.mark_restored()
        self.assertEqual([80.4], self.client.weights)
        self.assertEqual("sent", self.push.retry_pending())
        self.assertEqual([80.4, 80.4], self.client.weights)
        self.assertIsNone(self.push.retry_pending())

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
            {"sent": 1, "failed": 1, "retried": 1, "pending": 0,
             "last_outcome": "sent"},
            self.counts(),
        )  # fmt: skip
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
        # #3677: prod showed exactly this, pending 1 with nothing sent, failed or
        # retried; the outcome now names the missing scope.
        self.assertEqual(
            {"sent": 0, "failed": 0, "retried": 0, "pending": 1,
             "last_outcome": "scope_missing"},
            self.counts(),
        )  # fmt: skip
        # After the reconnect with user_write the refresh sends the newest weight.
        self.status = dict(ONLINE)
        self.refresh()
        self.assertEqual([80.4], self.client.weights)
        self.assertEqual({"sent": 1, "failed": 0, "retried": 1, "pending": 0,
                          "last_outcome": "sent"}, self.counts())  # fmt: skip

    def test_a_refused_write_ends_as_failed_and_a_server_error_waits(self):
        # #3677: Wahoo refusing the write (4xx) is no outage; retrying it on
        # every refresh would never end, so it ends as failed with its class.
        self.client.fail = AppError(502, SECRET, upstream_status=403)
        with self.assertLogs("intervals_coach", "WARNING"):
            self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.assertEqual({"sent": 0, "failed": 1, "retried": 0, "pending": 0,
                          "last_outcome": "http_4xx"}, self.counts())  # fmt: skip
        self.refresh()
        self.assertEqual(1, self.counts()["failed"])
        # A timeout or rate limit (408, 429) is transient and waits (Gitar).
        for status in (408, 429):
            self.client.fail = AppError(502, SECRET, upstream_status=status)
            with self.assertLogs("intervals_coach", "WARNING"):
                self.weights.save({"date": DAY.isoformat(), "kg": 80.2})
            self.assertEqual((1, "throttled"), (
                self.counts()["pending"], self.counts()["last_outcome"]))  # fmt: skip
        # A server error or a lost answer waits for the next refresh.
        self.client.fail = AppError(502, SECRET, upstream_status=503)
        with self.assertLogs("intervals_coach", "WARNING"):
            self.weights.save({"date": DAY.isoformat(), "kg": 80.0})
        self.assertEqual((1, "http_5xx"), (
            self.counts()["pending"], self.counts()["last_outcome"]))  # fmt: skip
        self.client.fail = TimeoutError()
        with self.assertLogs("intervals_coach", "WARNING"):
            self.refresh()
        self.assertEqual((1, "error"), (
            self.counts()["pending"], self.counts()["last_outcome"]))  # fmt: skip
        with self.manager.reader() as db:
            raw = KeyValueRepository(lambda: "").get(db, STATE_KEY)
        self.assertNotIn(SECRET, raw)

    def test_a_retry_stopped_after_an_undo_is_never_taken_as_sent(self):
        # #3677: with the push already pending (an undo), the cleared sent value
        # was not written before the request, so after a stop mid-request a
        # save of the same weight read the old value as sent and dropped it.
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.push.mark_restored()
        # The process ends during the retry's request.
        self.client.fail = SystemExit()
        with self.assertRaises(SystemExit):
            self.push.retry_pending()
        self.client.fail = None
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.assertEqual([80.4, 80.4], self.client.weights)
        self.assertEqual(0, self.counts()["pending"])

    def test_the_outcome_names_a_local_stop_and_an_absent_connection(self):
        self.status = {"connected": False, "scope": None}
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.assertEqual("off", self.counts()["last_outcome"])

        def broken():
            raise RuntimeError(SECRET)

        self.push = WahooWeightPushService(
            self.manager, lambda: self.client, broken, lambda: "x"
        )
        with self.assertLogs("intervals_coach", "WARNING"):
            self.assertEqual("pending", self.push.push())
        self.assertEqual("stopped", self.counts()["last_outcome"])

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
        # #4267: the save answers "running" at once; the sync status follows the
        # push until it is done, and a failed one reads as pending.
        self.status = {"connected": False, "scope": None}
        self.weights.save({"date": DAY.isoformat(), "kg": 80.4})
        self.assertEqual("off", self.push.start())
        # Without a connection the off outcome is still recorded (review on #4267).
        with self.manager.reader() as db:
            self.assertEqual(
                {"running": False, "pending": False, "outcome": "off"}, push_state(db)
            )
        self.status = dict(ONLINE)
        release = threading.Event()
        update = self.client.update_user_weight
        self.client.update_user_weight = lambda kg: (release.wait(5), update(kg))
        self.assertEqual("running", self.push.start())
        with self.manager.reader() as db:
            self.assertTrue(push_state(db)["running"])
        release.set()
        for thread in threading.enumerate():
            if thread.name == "wahoo-weight-push":
                thread.join(5)
        self.assertEqual([80.4], self.client.weights)
        with self.manager.reader() as db:
            self.assertEqual(
                {"running": False, "pending": False, "outcome": "sent"}, push_state(db)
            )
        self.client.fail = TimeoutError()
        self.weights.save({"date": DAY.isoformat(), "kg": 80.6})
        with self.assertLogs("intervals_coach"):
            self.assertEqual("running", self.push.start())
            for thread in threading.enumerate():
                if thread.name == "wahoo-weight-push":
                    thread.join(5)
        with self.manager.reader() as db:
            self.assertEqual(
                {"running": False, "pending": True, "outcome": "error"}, push_state(db)
            )


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
