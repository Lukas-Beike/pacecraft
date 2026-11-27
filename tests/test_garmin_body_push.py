"""The entered weight and body fat mirrored to Garmin Connect on save (#3403).

Garmin is a mocked SDK: no request leaves the test.
"""

import contextlib
import json
import logging
import threading
import time
import unittest
import unittest.mock
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from support import temp_database

from backend.athlete.body_entry import BODY_FAT_ENTRY, BodyEntryService
from backend.canonical.records import MetricValue, Observation, Provenance
from backend.canonical.vocabulary import (
    MetricKind,
    ProviderId,
    RecordingOrigin,
    RemoteEntityKind,
    RemoteLinkState,
    SourceKind,
)
from backend.db import canonical_store
from backend.db.remote_links import RemoteLinkRepository
from backend.providers.canonical_plans import current_observation_mapper_versions
from backend.sync import garmin_body_push
from backend.sync.executor import GarminJobAdapter
from backend.sync.garmin_body_push import (
    UPLOAD_TIME,
    GarminBodyPushService,
    push_state,
    summarize_body_push,
)

DAY = date(2026, 11, 15)
NOW = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
SECRET = "fake-session-token-123"


STAMP = "2026-11-15T12:00:17"


def _noon_ms(day: date) -> int:
    # Garmin's day view dates a weigh-in in local time, read as if it were UTC.
    return int(datetime.combine(day, UPLOAD_TIME, tzinfo=UTC).timestamp() * 1000)


class _HttpError(Exception):
    def __init__(self, status: int, text: str = "") -> None:
        super().__init__(text)
        self.response = SimpleNamespace(status_code=status)


class FakeSdk:
    """Garmin's weigh-ins per day, as the SDK reads and writes them."""

    def __init__(self) -> None:
        self.days: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self.fail: Exception | None = None
        # Garmin keeps the upload but the answer is lost (a timeout).
        self.fail_after_upload: Exception | None = None
        self.hide_uploads = False
        self._next = 100

    def add_body_composition(self, timestamp, weight, percent_fat=None):
        self.calls.append(("upload", timestamp, weight, percent_fat))
        if self.fail:
            raise self.fail
        moment = datetime.fromisoformat(timestamp)
        self._next += 1
        if not self.hide_uploads:
            self.days.setdefault(moment.date().isoformat(), []).append(
                {
                    "samplePk": self._next,
                    "date": _noon_ms(moment.date()),
                    "weight": weight * 1000,
                    "bodyFat": percent_fat,
                    "sourceType": "INDEX_SCALE",
                }
            )
        if self.fail_after_upload:
            raise self.fail_after_upload
        return {"detailedImportResult": {"successes": [{"id": 1}], "failures": []}}

    def get_daily_weigh_ins(self, cdate):
        self.calls.append(("read", cdate))
        return {"dateWeightList": [dict(item) for item in self.days.get(cdate, [])]}

    def delete_weigh_in(self, weight_pk, cdate):
        self.calls.append(("delete", str(weight_pk), cdate))
        if self.fail:
            raise self.fail
        items = self.days.get(cdate, [])
        kept = [item for item in items if str(item["samplePk"]) != str(weight_pk)]
        if len(kept) == len(items):
            raise _HttpError(404)
        self.days[cdate] = kept

    def writes(self):
        return [call for call in self.calls if call[0] != "read"]


class FakeSyncState:
    """The Garmin sync state's rate-limit pause, as garmin.py keeps it."""

    def __init__(self) -> None:
        self.limited_at: datetime | None = None
        self.failures: list[str] = []

    def last_rate_limit(self):
        return self.limited_at

    def capability_allowed(self, source):
        return True

    def record_capability_success(self, source):
        self.failures.clear()

    def record_capability_failure(self, source, error):
        self.failures.append(source)
        if getattr(getattr(error, "response", None), "status_code", None) == 429:
            self.limited_at = NOW


class GarminBodyPushTests(unittest.TestCase):
    def setUp(self):
        self.manager = temp_database(
            self,
            schema=True,
        )
        self.sdk = FakeSdk()
        self.state = FakeSyncState()
        self.now = NOW
        self.ready = True

        @contextlib.contextmanager
        def session():
            yield self.sdk

        # A save's push runs in place here, so its outcome is the receipt's.
        self.push = GarminBodyPushService(
            self.manager,
            session,
            self.state,
            lambda db, day: None,
            lambda: self.ready,
            lambda: self.now,
            start=lambda run: run(),
        )
        clock = {"now": lambda: "2026-11-16T10:00:00+00:00", "today": lambda: DAY}
        self.weights = BodyEntryService(self.manager, **clock, on_change=self.push.push)
        self.fats = BodyEntryService(
            self.manager, **clock, entry=BODY_FAT_ENTRY, on_change=self.push.push
        )

    def link(self, day=DAY):
        with self.manager.reader() as db:
            return RemoteLinkRepository().get(
                db, RemoteEntityKind.BODY_COMPOSITION, day.isoformat(), "garmin"
            )

    def counts(self):
        with self.manager.reader() as db:
            return summarize_body_push(db)

    def push_state(self):
        with self.manager.reader() as db:
            return push_state(db, DAY)

    def wait_for(self, done):
        for _ in range(250):
            if done():
                return
            time.sleep(0.02)
        self.fail("the push did not finish")

    def store_garmin_weight(self, kg, day=DAY):
        with self.manager.unit_of_work() as db:
            canonical_store.upsert_observation(
                db,
                Observation(
                    kind=MetricKind.WEIGHT,
                    value=MetricValue(kg, "kg"),
                    observed_on=day,
                    observed_at=None,
                    provenance=Provenance(
                        provider=ProviderId.GARMIN,
                        source_kind=SourceKind.DEVICE_MEASURED,
                        source_record_id=day.isoformat(),
                        recorded_by=RecordingOrigin.GARMIN,
                        retrieved_at=None,
                        label=None,
                    ),
                ),
                mapper_version=current_observation_mapper_versions()["garmin"],
                now="2026-11-16T10:00:00+00:00",
            )

    def test_save_writes_one_entry_and_links_it(self):
        read = self.weights.save({"date": "2026-11-15", "kg": 80})
        self.assertEqual("sent", read["remote"])
        self.assertEqual(
            "sent", self.fats.save({"date": "2026-11-15", "pct": 15})["remote"]
        )
        # The body fat goes with the weight of its day; the first entry is replaced.
        self.assertEqual(
            [
                ("upload", STAMP, 80.0, None),
                ("delete", "101", "2026-11-15"),
                ("upload", STAMP, 80.0, 15.0),
            ],
            self.sdk.writes(),
        )
        link = self.link()
        self.assertEqual(
            (RemoteLinkState.SYNCED, "102", "80.0|15.0"),
            (link.sync_state, link.remote_id, link.baseline_hash),
        )
        # Saving the same values again writes nothing.
        self.assertEqual(
            "unchanged", self.fats.save({"date": "2026-11-15", "pct": 15})["remote"]
        )
        self.assertEqual(3, len(self.sdk.writes()))

    def test_edit_updates_and_delete_removes_only_the_linked_entry(self):
        # The athlete's own Garmin weigh-in at the same time, other values, stays.
        own = {"samplePk": 7, "date": _noon_ms(DAY), "weight": 79500.0, "bodyFat": None}
        self.sdk.days["2026-11-15"] = [dict(own)]
        self.weights.save({"date": "2026-11-15", "kg": 80})
        self.assertEqual("101", self.link().remote_id)
        self.assertEqual(
            "sent", self.weights.save({"date": "2026-11-15", "kg": 81})["remote"]
        )
        self.assertEqual("102", self.link().remote_id)
        read = self.weights.remove({"date": "2026-11-15", "confirmed": True})
        self.assertEqual("removed", read["remote"])
        self.assertIsNone(self.link())
        self.assertEqual([own], self.sdk.days["2026-11-15"])
        self.assertEqual(
            {"sent": 2, "removed": 1, "failed": 0, "retried": 0, "linked_days": 0,
             "pending_days": 0, "unlinked_uploads": 0},
            self.counts(),
        )  # fmt: skip

    def test_an_undo_leaves_the_day_pending_without_counting_a_failure(self):
        # #3640 (Gitar): an undo writes nothing to Garmin; it leaves the day for
        # the next refresh, and no failed push is counted for it.
        self.weights.save({"date": "2026-11-15", "kg": 80.4})
        before = self.counts()
        with self.manager.unit_of_work():
            self.push.mark_restored(DAY)
        self.assertEqual(RemoteLinkState.PENDING, self.link().sync_state)
        after = self.counts()
        self.assertEqual(
            (before["failed"], before["pending_days"] + 1),
            (after["failed"], after["pending_days"]),
        )
        self.assertEqual(1, self.push.retry_pending())
        self.assertEqual(RemoteLinkState.SYNCED, self.link().sync_state)

    def test_a_rate_limit_keeps_the_local_save_and_a_paced_retry_follows(self):
        self.sdk.fail = _HttpError(429, f"Too many requests {SECRET}")
        with self.assertLogs("intervals_coach", logging.WARNING) as logs:
            read = self.weights.save({"date": "2026-11-15", "kg": 80.37})
        self.assertEqual(("pending", [{"date": "2026-11-15", "kg": 80.4}]),
                         (read["remote"], read["entries"]))  # fmt: skip
        self.assertEqual(RemoteLinkState.PENDING, self.link().sync_state)
        # Garmin's text, the credentials and the payload stay out of the logs:
        # the messages and each record's context, not its timing fields (#3487).
        standard = set(vars(logging.makeLogRecord({})))
        logged = "\n".join(logs.output) + json.dumps(
            [
                {
                    key: value
                    for key, value in vars(record).items()
                    if key not in standard
                }
                for record in logs.records
            ],
            default=str,
        )
        for leaked in (SECRET, "Too many", "80.37", "80.4"):
            self.assertNotIn(leaked, logged)
        # While Garmin's pause holds, neither a save nor a refresh calls Garmin.
        self.sdk.fail = None
        calls = len(self.sdk.calls)
        self.assertEqual(
            "pending", self.weights.save({"date": "2026-11-15", "kg": 80.5})["remote"]
        )
        self.assertEqual(0, self.push.retry_pending())
        self.assertEqual(calls, len(self.sdk.calls))
        # After the pause the Garmin refresh's retry sends the current value once.
        self.now = NOW + timedelta(days=1, minutes=1)
        GarminJobAdapter(
            jobs=SimpleNamespace(execute=lambda job, payload, reason: {"status": "ok"}),
            body_push=self.push,
        ).execute({"id": "job"}, {}, "manual", job_type="refresh")
        self.assertEqual(("upload", STAMP, 80.5, None), self.sdk.writes()[-1])
        self.assertEqual(RemoteLinkState.SYNCED, self.link().sync_state)
        counts = self.counts()
        self.assertEqual((1, 2, 1, 0), (counts["sent"], counts["failed"],
                                        counts["retried"], counts["pending_days"]))  # fmt: skip

    def test_no_write_for_provider_values_or_without_a_garmin_write_login(self):
        self.store_garmin_weight(79.0)
        self.assertEqual("unchanged", self.push.push(DAY))
        self.assertEqual([], self.sdk.calls)
        self.ready = False
        self.assertEqual(
            "off", self.weights.save({"date": "2026-11-15", "kg": 80})["remote"]
        )
        self.assertEqual([], self.sdk.calls)

    def test_a_body_fat_goes_with_the_days_garmin_weight_else_waits(self):
        self.assertEqual(
            "needs_weight", self.fats.save({"date": "2026-11-15", "pct": 15})["remote"]
        )
        self.assertEqual([], self.sdk.calls)
        self.store_garmin_weight(79.0)
        self.assertEqual("sent", self.push.push(DAY))
        self.assertEqual([("upload", STAMP, 79.0, 15.0)], self.sdk.writes())
        with unittest.mock.patch.object(
            garmin_body_push, "FAT_NEEDS_ENTERED_WEIGHT", True
        ):
            self.assertEqual("needs_weight", self.push.push(DAY))
        # Option B removed the entry that went with Garmin's weight.
        self.assertEqual(("delete", "101", "2026-11-15"), self.sdk.writes()[-1])

    def test_an_upload_that_cannot_be_read_back_is_searched_never_sent_twice(self):
        self.sdk.hide_uploads = True
        self.assertEqual(
            "pending", self.weights.save({"date": "2026-11-15", "kg": 80})["remote"]
        )
        self.assertEqual(1, self.counts()["unlinked_uploads"])
        self.sdk.hide_uploads = False
        # Garmin shows it later: the retry links it without a second upload.
        self.sdk.days["2026-11-15"] = [
            {"samplePk": 55, "date": _noon_ms(DAY), "weight": 80000.0, "bodyFat": None}
        ]
        self.assertEqual(1, self.push.retry_pending())
        self.assertEqual(1, len([c for c in self.sdk.calls if c[0] == "upload"]))
        self.assertEqual(("55", RemoteLinkState.SYNCED),
                         (self.link().remote_id, self.link().sync_state))  # fmt: skip

    def test_an_accepted_upload_that_never_shows_is_not_sent_again(self):
        # Final review: a read-back that keeps failing must not add one entry per refresh.
        self.sdk.hide_uploads = True
        self.assertEqual(
            "pending", self.weights.save({"date": "2026-11-15", "kg": 80})["remote"]
        )
        for _ in range(3):
            self.push.retry_pending()
        self.assertEqual(1, len([c for c in self.sdk.calls if c[0] == "upload"]))
        self.assertEqual(1, self.counts()["unlinked_uploads"])

    def test_a_change_while_garmin_cannot_be_written_waits_for_it(self):
        self.weights.save({"date": "2026-11-15", "kg": 80})
        self.ready = False
        read = self.weights.remove({"date": "2026-11-15", "confirmed": True})
        self.assertEqual(("pending", RemoteLinkState.PENDING),
                         (read["remote"], self.link().sync_state))  # fmt: skip
        # A day Garmin never held stays off: no backfill.
        self.assertEqual(
            "off", self.weights.save({"date": "2026-11-14", "kg": 79})["remote"]
        )
        self.ready = True
        self.assertEqual(1, self.push.retry_pending())
        self.assertEqual([], self.sdk.days["2026-11-15"])
        self.assertIsNone(self.link())

    def hold_garmin(self):
        """A Garmin that answers only after the returned event is set, on a real thread."""
        release = threading.Event()

        @contextlib.contextmanager
        def busy():
            release.wait(5)
            yield self.sdk

        self.push._sdk_session = busy
        self.push._start = lambda run: threading.Thread(target=run, daemon=True).start()
        return release

    def test_a_save_answers_at_once_while_garmin_is_slow(self):
        # #4267 (option B): the save returns while the push blocks; the sync
        # status shows the day running, then neither running nor pending.
        release = self.hold_garmin()
        started = time.monotonic()
        read = self.weights.save({"date": "2026-11-15", "kg": 80})
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual("running", read["remote"])
        self.assertEqual({"date": "2026-11-15", "kg": 80.0}, read["entries"][0])
        self.assertEqual(["2026-11-15"], self.push_state()["running"])
        release.set()
        self.wait_for(lambda: not self.push_state()["running"])
        self.assertEqual({"running": [], "pending": []}, self.push_state())
        link = self.link()
        self.assertEqual(
            ("101", RemoteLinkState.SYNCED), (link.remote_id, link.sync_state)
        )

    def test_a_slow_push_that_fails_reads_as_pending_afterwards(self):
        release = self.hold_garmin()
        self.sdk.fail = _HttpError(500)
        with self.assertLogs("intervals_coach", logging.WARNING):
            self.assertEqual(
                "running", self.weights.save({"date": "2026-11-15", "kg": 80})["remote"]
            )
            release.set()
            self.wait_for(lambda: not self.push_state()["running"])
        self.assertEqual({"running": [], "pending": ["2026-11-15"]}, self.push_state())

    def test_a_restart_during_a_push_reads_as_pending(self):
        # The upload went out but its answer never came: the link is pending with
        # what was sent. After a restart no push runs, so the day reads pending.
        self.sdk.fail_after_upload = TimeoutError()
        with self.assertLogs("intervals_coach", logging.WARNING):
            self.weights.save({"date": "2026-11-15", "kg": 80})
        self.assertEqual({"running": [], "pending": ["2026-11-15"]}, self.push_state())
        # Pending days older than the retry window are not reported.
        with self.manager.reader() as db:
            self.assertEqual([], push_state(db, DAY + timedelta(days=31))["pending"])

    def test_two_matching_entries_link_the_newest_and_leave_the_other(self):
        twin = {
            "samplePk": 9,
            "date": _noon_ms(DAY),
            "weight": 80000.0,
            "bodyFat": None,
        }
        self.sdk.days["2026-11-15"] = [dict(twin)]
        self.assertEqual(
            "sent", self.weights.save({"date": "2026-11-15", "kg": 80})["remote"]
        )
        self.assertEqual("101", self.link().remote_id)
        self.weights.remove({"date": "2026-11-15", "confirmed": True})
        self.assertEqual([twin], self.sdk.days["2026-11-15"])

    def test_an_upload_with_a_lost_answer_is_never_sent_twice(self):
        # Review of the write path: Garmin keeps the entry, the answer times out.
        self.sdk.fail_after_upload = _HttpError(504)
        self.assertEqual(
            "pending", self.weights.save({"date": "2026-11-15", "kg": 80})["remote"]
        )
        self.sdk.fail_after_upload = None
        self.assertEqual(1, self.push.retry_pending())
        self.assertEqual(1, len([c for c in self.sdk.calls if c[0] == "upload"]))
        self.assertEqual(("101", RemoteLinkState.SYNCED),
                         (self.link().remote_id, self.link().sync_state))  # fmt: skip

    def test_a_change_or_removal_cleans_up_an_upload_not_read_back(self):
        for change in ({"kg": 81}, None):
            with self.subTest(change=change):
                self.sdk.days.clear()
                if self.link():
                    self.weights.remove({"date": "2026-11-15", "confirmed": True})
                self.sdk.hide_uploads = True
                self.weights.save({"date": "2026-11-15", "kg": 80})
                self.sdk.hide_uploads = False
                # The first upload shows up in Garmin later.
                self.sdk.days["2026-11-15"] = [
                    {
                        "samplePk": 55,
                        "date": _noon_ms(DAY),
                        "weight": 80000.0,
                        "bodyFat": None,
                    }
                ]
                if change:
                    self.weights.save({"date": "2026-11-15", **change})
                    weights = [item["weight"] for item in self.sdk.days["2026-11-15"]]
                    self.assertEqual([81000.0], weights)
                else:
                    self.weights.remove({"date": "2026-11-15", "confirmed": True})
                    self.assertEqual([], self.sdk.days["2026-11-15"])
                    self.assertIsNone(self.link())

    def test_a_removed_weight_is_not_sent_back_from_garmins_echo(self):
        self.weights.save({"date": "2026-11-15", "kg": 80})
        self.fats.save({"date": "2026-11-15", "pct": 15})
        # The next refresh reads the app's own upload back as a Garmin weight.
        self.store_garmin_weight(80.0)
        read = self.weights.remove({"date": "2026-11-15", "confirmed": True})
        self.assertEqual("needs_weight", read["remote"])
        self.assertEqual([], self.sdk.days["2026-11-15"])
        self.assertIsNone(self.link())

    def test_a_local_fault_never_fails_the_save_or_the_refresh(self):
        self.weights.save({"date": "2026-11-15", "kg": 80})
        broken = GarminBodyPushService(
            SimpleNamespace(reader=lambda: (_ for _ in ()).throw(OSError("db down"))),
            None, self.state, lambda db, day: None, lambda: True, lambda: self.now,
        )  # fmt: skip
        with self.assertLogs("intervals_coach", logging.WARNING):
            self.assertEqual("pending", broken.push(DAY))
            self.assertEqual(0, broken.retry_pending())


if __name__ == "__main__":
    unittest.main()
