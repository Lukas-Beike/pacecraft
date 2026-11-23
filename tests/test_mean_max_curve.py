"""Local mean-maximal curve from canonical full-resolution streams (#1430)."""

import dataclasses
import hashlib
import itertools
import json
import sqlite3
import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.canonical.activity_detail import (
    ActivityLap,
    ActivityLapSet,
    ActivityStreamSet,
)
from backend.canonical.records import Provenance
from backend.canonical.vocabulary import (
    ActivityDetailOrigin,
    LapKind,
    LapSource,
    ProviderId,
    RecordingOrigin,
    SourceKind,
    StreamChannel,
    StreamResolution,
)
from backend.db import activity_detail_store, row_factory
from backend.db.activity_detail_store import (
    detail_sets_for,
    full_stream_keys,
    lap_set_keys,
    lap_sets_for_session,
    replace_detail_set,
)
from backend.db.canonical_store import canonical_json
from backend.db.schema import initialize_schema
from backend.performance import mean_max_curve, report_service
from backend.performance.canonical_detail import CanonicalDetailReader
from backend.performance.mean_max_curve import SessionCurveReader
from backend.performance.power_profile import running_profile
from backend.performance.report_service import TrainingProfileSelectionService
from backend.providers.canonical_plans import ACTIVITY_RECORD_KIND
from backend.sync.canonical_table_reader import CanonicalTableReader
from tests.test_power_profile import steady_run

STAMP = "2026-10-18T08:00:00+00:00"
RETRIEVED = datetime(2026, 10, 18, 8, 0, tzinfo=UTC)
TIME = tuple(float(second) for second in range(901))
INTERVALS = ProviderId.INTERVALS
GARMIN = ProviderId.GARMIN
WAHOO = ProviderId.WAHOO


def ride_power(peak: float) -> tuple[float, ...]:
    """200 W with a 3-min block at ``peak`` from second 300."""
    return tuple(peak if 300 <= second < 480 else 200.0 for second in range(len(TIME)))


def add_session(db, activity_id, state="active", merged_into=None):
    db.execute(
        "INSERT INTO activity_sessions(activity_id, primary_provider, "
        "primary_provider_activity_id, state, merged_into, started_at, "
        "created_at, updated_at) VALUES (?, 'garmin', ?, ?, ?, ?, ?, ?)",
        (activity_id, activity_id, state, merged_into, STAMP, STAMP, STAMP),
    )


def add_record(
    db, provider, provider_activity_id, activity_id, sport="ride", started_at=STAMP
):
    db.execute(
        "INSERT INTO activity_records(provider, provider_activity_id, activity_id, "
        "state, mapper_version, source_kind, recorded_by, sport, environment, "
        "started_at, load_model, fetched_at, created_at, updated_at) "
        "VALUES (?, ?, ?, 'active', 1, 'provider_reported', 'garmin', ?, 'outdoor', "
        "?, 'unknown', ?, ?, ?)",
        (
            provider.value,
            provider_activity_id,
            activity_id,
            sport,
            started_at,
            STAMP,
            STAMP,
            STAMP,
        ),
    )


def store_summary(db, provider_activity_id, payload_hash):
    """Store the Garmin raw summary whose hash decides whether a set is current."""
    db.execute(
        "INSERT INTO provider_records(provider, record_kind, source_record_id, "
        "payload_hash, fetched_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(provider, record_kind, source_record_id) "
        "DO UPDATE SET payload_hash=excluded.payload_hash",
        (
            GARMIN.value,
            ACTIVITY_RECORD_KIND,
            provider_activity_id,
            payload_hash,
            STAMP,
            STAMP,
            STAMP,
        ),
    )


def store_streams(
    db,
    provider,
    provider_activity_id,
    watts,
    resolution,
    summary_sha256=None,
    retrieved_at=RETRIEVED,
    heart_rate=None,
):
    channels = {StreamChannel.TIME_S: TIME, StreamChannel.POWER_W: watts}
    if heart_rate:
        channels[StreamChannel.HEART_RATE_BPM] = heart_rate
    detail = ActivityStreamSet(
        provider_activity_id=provider_activity_id,
        provenance=Provenance(
            provider=provider,
            source_kind=SourceKind.PROVIDER_REPORTED,
            source_record_id=None,
            recorded_by=RecordingOrigin.GARMIN,
            retrieved_at=retrieved_at,
            label=provider.value.title(),
        ),
        origin=ActivityDetailOrigin.FETCH,
        resolution=resolution,
        channels=channels,
    )
    replace_detail_set(
        db, detail, mapper_version=1, summary_sha256=summary_sha256, now=STAMP
    )


def store_laps(db, provider, provider_activity_id, windows, summary_sha256=None):
    """Lap set of (kind, start, end) windows on the stream clock."""
    detail = ActivityLapSet(
        provider_activity_id=provider_activity_id,
        provenance=Provenance(
            provider=provider,
            source_kind=SourceKind.PROVIDER_REPORTED,
            source_record_id=None,
            recorded_by=RecordingOrigin.GARMIN,
            retrieved_at=RETRIEVED,
            label=provider.value.title(),
        ),
        origin=ActivityDetailOrigin.FETCH,
        lap_source=LapSource.DEVICE_LAPS,
        laps=tuple(
            ActivityLap(
                index=index,
                kind=LapKind(kind),
                start_offset_s=start,
                end_offset_s=end,
            )
            for index, (kind, start, end) in enumerate(windows)
        ),
    )
    replace_detail_set(
        db, detail, mapper_version=1, summary_sha256=summary_sha256, now=STAMP
    )


INTERVAL_LAPS = (
    ("warmup", 0.0, 300.0),
    ("work", 300.0, 480.0),
    ("recovery", 480.0, 600.0),
    ("work", 600.0, 780.0),
    ("cooldown", 780.0, 900.0),
)


def interval_power() -> tuple[float, ...]:
    return tuple(
        300.0 if 300 <= second < 480 or 600 <= second < 780 else 150.0
        for second in range(len(TIME))
    )


def store_run(db, provider_activity_id):
    """A 15-min run with speed, distance, heart rate and running dynamics."""
    speeds = tuple(3.0 + (second % 60) / 60 for second in range(len(TIME)))
    distance = [0.0]
    for speed in speeds[:-1]:
        distance.append(distance[-1] + speed)
    detail = ActivityStreamSet(
        provider_activity_id=provider_activity_id,
        provenance=Provenance(
            provider=GARMIN,
            source_kind=SourceKind.PROVIDER_REPORTED,
            source_record_id=None,
            recorded_by=RecordingOrigin.GARMIN,
            retrieved_at=RETRIEVED,
            label="Garmin",
        ),
        origin=ActivityDetailOrigin.FETCH,
        resolution=StreamResolution.FULL,
        channels={
            StreamChannel.TIME_S: TIME,
            StreamChannel.SPEED_MPS: speeds,
            StreamChannel.DISTANCE_M: tuple(distance),
            StreamChannel.ALTITUDE_M: tuple(100.0 for _ in TIME),
            StreamChannel.HEART_RATE_BPM: tuple(
                140.0 + s % 20 for s in range(len(TIME))
            ),
            StreamChannel.CADENCE_RPM: tuple(85.0 for _ in TIME),
            StreamChannel.GROUND_CONTACT_TIME_MS: tuple(240.0 for _ in TIME),
            StreamChannel.VERTICAL_OSCILLATION_CM: tuple(8.5 for _ in TIME),
            StreamChannel.STEP_LENGTH_M: tuple(1.1 for _ in TIME),
        },
    )
    replace_detail_set(db, detail, mapper_version=1, summary_sha256=None, now=STAMP)


# Digest of the golden sessions' stored curves with the format version it was pinned
# under (#2821): an output change needs a CURVE_FORMAT_VERSION bump, then a new pin.
GOLDEN_CURVE = (4, "875875b2bef398b77b57614cbe02a2e0d3f8977ab014a41908897cdc132f524a")


def watts_at(curve, duration):
    return next(
        point["watts"]
        for point in curve.power["duration_curve"]
        if point["duration_seconds"] == duration
    )


def _without_all_time(payload):
    return {key: value for key, value in payload.items() if key != "all_time"}


class SessionCurveReaderTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = row_factory
        self.addCleanup(self.db.close)
        initialize_schema(self.db)
        self.reader = SessionCurveReader(CanonicalTableReader().session_activities)

    def test_only_full_resolution_streams_enter_the_curve(self):
        add_session(self.db, "act_1")
        add_record(self.db, WAHOO, "w-1", "act_1")
        store_streams(self.db, WAHOO, "w-1", ride_power(350.0), StreamResolution.FULL)
        add_record(self.db, GARMIN, "g-1", "act_1")
        store_streams(
            self.db, GARMIN, "g-1", ride_power(999.0), StreamResolution.DOWNSAMPLED
        )
        add_session(self.db, "act_2")
        add_record(self.db, GARMIN, "g-2", "act_2")
        store_streams(
            self.db, GARMIN, "g-2", ride_power(999.0), StreamResolution.DOWNSAMPLED
        )

        curves = self.reader.curves(self.db, ["act_1", "act_2"])

        self.assertEqual({"act_1"}, set(curves))
        self.assertEqual(350.0, watts_at(curves["act_1"], 180))
        self.assertEqual("Wahoo original watts stream", curves["act_1"].power["source"])
        # The power-at-heart-rate bins (#2427) come from the same streams; this
        # ride has no heart rate, so it gives none.
        self.assertEqual(
            ("power-at-heart-rate-v2", "insufficient_data"),
            (
                curves["act_1"].power_at_hr["method"],
                curves["act_1"].power_at_hr["status"],
            ),
        )

    def test_merged_duplicate_counts_once_under_its_session(self):
        add_session(self.db, "act_1")
        add_record(self.db, GARMIN, "g-1", "act_1")
        store_streams(self.db, GARMIN, "g-1", ride_power(350.0), StreamResolution.FULL)
        add_session(self.db, "act_2", "duplicate", merged_into="act_1")
        add_record(self.db, WAHOO, "w-1", "act_2")
        store_streams(self.db, WAHOO, "w-1", ride_power(350.0), StreamResolution.FULL)

        curves = self.reader.curves(self.db, ["act_1", "act_2"])

        self.assertEqual({"act_1"}, set(curves))
        self.assertEqual(350.0, watts_at(curves["act_1"], 180))

    def test_curves_are_recomputed_only_when_the_stream_content_changes(self):
        add_session(self.db, "act_1")
        add_record(self.db, GARMIN, "g-1", "act_1")
        store_streams(self.db, GARMIN, "g-1", ride_power(350.0), StreamResolution.FULL)

        with patch.object(
            mean_max_curve, "power_profile", wraps=mean_max_curve.power_profile
        ) as computed:
            first = self.reader.curves(self.db, ["act_1"])["act_1"]
            again = self.reader.curves(self.db, ["act_1"])["act_1"]
            store_streams(
                self.db, GARMIN, "g-1", ride_power(380.0), StreamResolution.FULL
            )
            changed = self.reader.curves(self.db, ["act_1"])["act_1"]

        self.assertEqual(2, computed.call_count)
        self.assertIs(first, again)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        self.assertEqual(380.0, watts_at(changed, 180))

    def test_a_refetch_that_changes_the_chosen_set_recomputes_the_curve(self):
        add_session(self.db, "act_1")
        add_record(self.db, WAHOO, "w-1", "act_1")
        store_streams(self.db, WAHOO, "w-1", ride_power(350.0), StreamResolution.FULL)
        add_session(self.db, "act_2", "duplicate", merged_into="act_1")
        add_record(self.db, WAHOO, "w-2", "act_2")
        store_streams(self.db, WAHOO, "w-2", ride_power(450.0), StreamResolution.FULL)
        before = self.reader.curves(self.db, ["act_1"])["act_1"]

        # Same content fetched later: the merge now prefers this set.
        later = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
        for provider_activity_id, peak in (("w-1", 350.0), ("w-2", 450.0)):
            if watts_at(before, 180) != peak:
                store_streams(
                    self.db,
                    WAHOO,
                    provider_activity_id,
                    ride_power(peak),
                    StreamResolution.FULL,
                    retrieved_at=later,
                )
        cached = self.reader.curves(self.db, ["act_1"])["act_1"]
        fresh = SessionCurveReader(CanonicalTableReader().session_activities).curves(
            self.db, ["act_1"]
        )["act_1"]

        self.assertNotEqual(watts_at(before, 180), watts_at(fresh, 180))
        self.assertEqual(watts_at(fresh, 180), watts_at(cached, 180))

    def test_a_stored_intervals_copy_is_ignored_but_kept(self):
        # #2148: an Intervals.icu stream set counts nowhere, yet stays stored.
        add_session(self.db, "act_1")
        add_record(self.db, INTERVALS, "i-1", "act_1")
        store_streams(
            self.db, INTERVALS, "i-1", ride_power(350.0), StreamResolution.FULL
        )

        self.assertEqual({}, self.reader.curves(self.db, ["act_1"]))
        # Since #2650 the detail store serves it as no key, yet keeps it.
        self.assertEqual({}, full_stream_keys(self.db, ["act_1"]))
        stored = detail_sets_for(self.db, INTERVALS.value, "i-1")
        self.assertEqual(["i-1"], [item.provider_activity_id for item in stored])

    def test_a_set_fetched_for_an_older_summary_is_left_out(self):
        add_session(self.db, "act_1")
        add_record(self.db, GARMIN, "g-1", "act_1")
        store_streams(
            self.db,
            GARMIN,
            "g-1",
            ride_power(350.0),
            StreamResolution.FULL,
            summary_sha256="a" * 64,
        )

        store_summary(self.db, "g-1", "a" * 64)
        current = self.reader.curves(self.db, ["act_1"])
        store_summary(self.db, "g-1", "b" * 64)
        changed = self.reader.curves(self.db, ["act_1"])

        self.assertEqual(350.0, watts_at(current["act_1"], 180))
        self.assertEqual({}, changed)

    def golden_run(self):
        add_session(self.db, "act_run")
        add_record(self.db, GARMIN, "g-run", "act_run", sport="run")
        store_run(self.db, "g-run")
        return self.reader.curves(self.db, ["act_run"])["act_run"]

    def golden_ride(self):
        # Wahoo and Garmin power merged, Garmin heart rate and interval laps.
        add_session(self.db, "act_ride")
        add_record(self.db, GARMIN, "g-ride", "act_ride")
        add_record(self.db, WAHOO, "w-ride", "act_ride")
        heart_rate = tuple(130.0 + second % 30 for second in range(len(TIME)))
        store_streams(
            self.db,
            GARMIN,
            "g-ride",
            interval_power(),
            StreamResolution.FULL,
            heart_rate=heart_rate,
        )
        store_streams(
            self.db, WAHOO, "w-ride", ride_power(320.0), StreamResolution.FULL
        )
        store_laps(self.db, GARMIN, "g-ride", INTERVAL_LAPS)
        return self.reader.curves(self.db, ["act_ride"])["act_ride"]

    def test_a_golden_curve_pins_its_format_version(self):
        payload = {
            "run": mean_max_curve.curve_payload(self.golden_run()),
            "ride": mean_max_curve.curve_payload(self.golden_ride()),
        }
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

        self.assertEqual(
            GOLDEN_CURVE,
            (mean_max_curve.CURVE_FORMAT_VERSION, digest),
            "the stored curves changed: bump mean_max_curve.CURVE_FORMAT_VERSION, "
            "then pin the new digest",
        )

    def test_every_curve_field_survives_the_stored_round_trip(self):
        run, ride = self.golden_run(), self.golden_ride()
        self.assertIsNotNone(run.running_dynamics)
        self.assertTrue(ride.power and ride.power_at_hr and ride.execution_summary)
        for name, curve in (("run", run), ("ride", ride)):
            stored = json.loads(canonical_json(mean_max_curve.curve_payload(curve)))
            loaded = mean_max_curve.curve_from_payload(curve.fingerprint, stored)
            for field in dataclasses.fields(curve):
                with self.subTest(curve=name, field=field.name):
                    self.assertEqual(
                        getattr(curve, field.name), getattr(loaded, field.name)
                    )
            self.assertEqual(curve, loaded)

    def test_the_recording_origin_is_part_of_the_fingerprint(self):
        before = self.golden_run().fingerprint
        self.db.execute("UPDATE activity_records SET recorded_by = 'wahoo'")
        after = SessionCurveReader(CanonicalTableReader().session_activities).curves(
            self.db, ["act_run"]
        )["act_run"]
        self.assertNotEqual(before, after.fingerprint)

    def test_detail_view_labels_a_profile_from_downsampled_streams(self):
        add_session(self.db, "act_1")
        add_record(self.db, GARMIN, "g-1", "act_1")
        store_streams(
            self.db, GARMIN, "g-1", ride_power(350.0), StreamResolution.DOWNSAMPLED
        )
        add_session(self.db, "act_2")
        add_record(self.db, WAHOO, "w-2", "act_2")
        store_streams(self.db, WAHOO, "w-2", ride_power(350.0), StreamResolution.FULL)
        reader = CanonicalDetailReader(
            CanonicalTableReader().session_activities,
            refreshable=lambda: frozenset({INTERVALS}),
        )

        labels = {
            activity_id: reader.read(self.db, activity_id)["analysis"]["power_profile"][
                "stream_resolution"
            ]
            for activity_id in ("act_1", "act_2")
        }

        self.assertEqual({"act_1": "downsampled", "act_2": "full"}, labels)


class SessionExecutionCurveTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = row_factory
        self.addCleanup(self.db.close)
        initialize_schema(self.db)
        self.reader = SessionCurveReader(CanonicalTableReader().session_activities)
        add_session(self.db, "act_1")
        add_record(self.db, GARMIN, "g-1", "act_1")
        store_streams(self.db, GARMIN, "g-1", interval_power(), StreamResolution.FULL)

    def test_a_curve_missed_by_two_threads_is_computed_once(self):
        # Parallel view loads after a start computed the same cold curve in
        # every request (#2783); a second miss now waits for the first.
        shared = sqlite3.connect(":memory:", check_same_thread=False)
        shared.row_factory = row_factory
        self.addCleanup(shared.close)
        self.db.commit()
        self.db.backup(shared)
        entered, release = threading.Event(), threading.Event()
        compute = mean_max_curve._session_curve

        def gated(*args):
            entered.set()
            release.wait(5)
            return compute(*args)

        results = {}
        with patch.object(mean_max_curve, "_session_curve", side_effect=gated) as spy:
            first = threading.Thread(
                target=lambda: results.update(a=self.reader.curves(shared, ["act_1"]))
            )
            first.start()
            self.assertTrue(entered.wait(5))
            # The first computation's marker: note when the second miss waits on it.
            (marker,) = self.reader._computing.values()
            waiting, wait = threading.Event(), marker.wait
            marker.wait = lambda *args: (waiting.set(), wait(*args))[1]
            second = threading.Thread(
                target=lambda: results.update(b=self.reader.curves(shared, ["act_1"]))
            )
            second.start()
            self.assertTrue(waiting.wait(5), "the second miss waits")
            release.set()
            first.join(5)
            second.join(5)
        self.assertEqual(1, spy.call_count)
        self.assertIs(results["a"]["act_1"], results["b"]["act_1"])

    def test_lap_sets_are_read_by_key_and_alone(self):
        store_laps(self.db, GARMIN, "g-1", INTERVAL_LAPS)
        keys = lap_set_keys(self.db, ["act_1"])
        self.assertEqual(["g-1"], [key.provider_activity_id for key in keys["act_1"]])
        sets = lap_sets_for_session(self.db, "act_1")
        self.assertEqual(1, len(sets))
        self.assertIsInstance(sets[0].detail, ActivityLapSet)

    def test_the_curve_holds_the_execution_summary_of_its_laps(self):
        store_laps(self.db, GARMIN, "g-1", INTERVAL_LAPS)
        curve = self.reader.curves(self.db, ["act_1"])["act_1"]
        self.assertEqual("interval", curve.execution_summary["structure"])
        self.assertEqual(2, curve.execution_summary["rep_count"])
        self.assertEqual(300, curve.execution_summary["rep_output_mean"])

    def test_a_lap_change_recomputes_and_a_stale_lap_set_stays_out(self):
        store_laps(self.db, GARMIN, "g-1", INTERVAL_LAPS)
        first = self.reader.curves(self.db, ["act_1"])["act_1"]
        store_laps(self.db, GARMIN, "g-1", INTERVAL_LAPS, summary_sha256="a" * 64)
        store_summary(self.db, "g-1", "a" * 64)
        current = self.reader.curves(self.db, ["act_1"])["act_1"]
        store_summary(self.db, "g-1", "b" * 64)
        stale = self.reader.curves(self.db, ["act_1"])["act_1"]
        assert current.execution_summary is not None
        assert stale.execution_summary is not None
        self.assertNotEqual(first.fingerprint, stale.fingerprint)
        # The same laps count while current and stay out once stale.
        self.assertEqual("interval", current.execution_summary["structure"])
        self.assertEqual("continuous", stale.execution_summary["structure"])


class ProfileSelectionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = row_factory
        self.addCleanup(self.db.close)
        initialize_schema(self.db)
        self.session_records = Mock(wraps=CanonicalTableReader().session_activities)
        self.day = date(2026, 10, 18)
        self.snapshots_open = 0
        self.writes = []

    def service(self, summaries, spawn=None, capacity=2048, **pacing):
        @contextmanager
        def read_snapshot():
            self.snapshots_open += 1
            try:
                yield self.db
            finally:
                self.snapshots_open -= 1

        canonical = SimpleNamespace(
            activity_summary_reader=lambda: SimpleNamespace(
                activity_summaries=lambda _db, **_bounds: summaries,
            ),
            database_manager=lambda: SimpleNamespace(
                read_snapshot=read_snapshot, unit_of_work=self.unit_of_work
            ),
        )
        return TrainingProfileSelectionService(
            lambda: self.day,
            canonical=canonical,
            curves=SessionCurveReader(
                self.session_records, spawn=spawn, capacity=capacity
            ),
            **pacing,
        )

    @contextmanager
    def unit_of_work(self):
        # A write outside any read snapshot (#2821).
        self.writes.append(self.snapshots_open)
        yield self.db

    def stored(self):
        return {
            row["activity_id"]: row["fingerprint"]
            for row in self.db.execute(
                "SELECT activity_id, fingerprint FROM session_curves"
            )
        }

    def built(self, summaries, **options):
        builds = []
        service = self.service(
            summaries, spawn=builds.append, pause=lambda _seconds: None, **options
        )
        service.power_profiles(all_time=True)
        builds.pop()()
        return service

    def test_a_restart_loads_the_stored_curves_without_computing(self):
        summaries = []
        self.add_ride(summaries, "act_recent", day="2026-10-10")
        self.add_ride(summaries, "act_old", day="2025-03-14")
        first = self.built(summaries)
        stored = self.stored()
        self.assertEqual({"act_recent", "act_old"}, set(stored))
        # Every write, the two curves and the cleanup, ran outside any snapshot.
        self.assertEqual([0, 0, 0], self.writes)

        with patch.object(
            mean_max_curve, "_session_curve", wraps=mean_max_curve._session_curve
        ) as computed:
            restarted = self.built(summaries)

        self.assertEqual(0, computed.call_count)
        state = restarted._curves.background_state()
        self.assertEqual(
            (2, 0, 0),
            (state["curves_loaded"], state["curves_computed"], state["curves_stored"]),
        )
        self.assertEqual(
            first.power_profiles(all_time=True)["all_time"]["power"],
            restarted.power_profiles(all_time=True)["all_time"]["power"],
        )

    def test_a_changed_or_request_computed_curve_is_stored_by_the_build(self):
        summaries = []
        self.add_ride(summaries, "act_recent", day="2026-10-10")
        self.add_ride(summaries, "act_old", day="2025-03-14")
        service = self.service(summaries, pause=lambda _seconds: None)
        # A request computes the recent curve but never writes it.
        service.curve_records(90)
        self.assertEqual({}, self.stored())
        current = self.built(summaries).curve_records(90)[0]["fingerprint"]
        self.db.execute(
            "UPDATE session_curves SET fingerprint = ? WHERE activity_id = 'act_recent'",
            ("0" * 64,),
        )

        with patch.object(
            mean_max_curve, "_session_curve", wraps=mean_max_curve._session_curve
        ) as computed:
            self.built(summaries)

        # The outdated row is computed once more and written back.
        self.assertEqual(1, computed.call_count)
        self.assertEqual(current, self.stored()["act_recent"])

    def test_a_failed_curve_write_leaves_the_others_and_the_build(self):
        summaries = []
        self.add_ride(summaries, "act_recent", day="2026-10-10")
        self.add_ride(summaries, "act_old", day="2025-03-14")
        save = activity_detail_store.save_session_curve
        calls = []

        def flaky(db, activity_id, **values):
            calls.append(activity_id)
            if len(calls) == 1:
                raise RuntimeError("synthetic write failure")
            return save(db, activity_id, **values)

        with patch.object(activity_detail_store, "save_session_curve", flaky):
            service = self.built(summaries)

        state = service._curves.background_state()
        self.assertEqual(
            (1, 1, 1),
            (len(self.stored()), state["curves_stored"], state["curve_store_failures"]),
        )
        self.assertTrue(service.power_profiles(all_time=True)["all_time"]["power"])

    def test_the_build_drops_the_curves_of_sessions_no_longer_live(self):
        summaries = []
        self.add_ride(summaries, "act_recent", day="2026-10-10")
        self.add_ride(summaries, "act_old", day="2025-03-14")
        self.built(summaries)
        self.db.execute(
            "UPDATE activity_sessions SET state = 'duplicate', merged_into = 'act_recent' "
            "WHERE activity_id = 'act_old'"
        )
        summaries.pop()

        self.built(summaries)

        self.assertEqual({"act_recent"}, set(self.stored()))

    def add_ride(self, summaries, activity_id, day="2026-10-10", peak=350.0):
        add_session(self.db, activity_id)
        add_record(self.db, GARMIN, f"g-{activity_id}", activity_id)
        store_streams(
            self.db, GARMIN, f"g-{activity_id}", ride_power(peak), StreamResolution.FULL
        )
        summaries.append(
            {
                "id": activity_id,
                "sport": "ride",
                "environment": "outdoor",
                "started_at": f"{day}T08:00:00+00:00",
                "started_at_local": f"{day}T10:00:00",
                "record_ids": {"garmin": f"g-{activity_id}"},
                "source": "Garmin",
            }
        )

    def test_a_record_names_the_activity_weather_temperature_never_the_watch(self):
        # #3036: the watch's temperature sensor reads body-warmed values.
        summaries = []
        for activity_id in ("act_1", "act_2"):
            self.add_ride(summaries, activity_id)
            summaries[-1].update(avg_temp_c=31.0, min_temp_c=29.0, max_temp_c=33.0)
        weather = {"act_1": 12.5}
        service = self.service(summaries)

        def temperatures():
            with patch.object(
                report_service,
                "session_temperatures",
                side_effect=lambda _db, ids: {
                    k: weather[k] for k in ids if k in weather
                },
            ):
                rows = service.curve_records(60, shared=True)
            return {row["activity_id"]: row["weather_temp_c"] for row in rows}

        # Without stored weather the temperature is unknown.
        self.assertEqual({"act_1": 12.5, "act_2": None}, temperatures())
        # Weather stored later reaches the shared records.
        weather["act_2"] = 20.0
        self.assertEqual({"act_1": 12.5, "act_2": 20.0}, temperatures())
        for row in service.curve_records(60):
            self.assertFalse({"avg_temp_c", "min_temp_c", "max_temp_c"} & set(row))

    def test_window_reads_canonical_sessions_and_counts_missing_streams(self):
        summaries = []
        for activity_id, day, peak, resolution in (
            ("act_1", "2026-10-10", 350.0, StreamResolution.FULL),
            ("act_2", "2026-10-12", 999.0, StreamResolution.DOWNSAMPLED),
            ("act_3", "2026-07-01", 999.0, StreamResolution.FULL),
            ("act_4", "2026-10-19", 999.0, StreamResolution.FULL),
        ):
            add_session(self.db, activity_id)
            add_record(self.db, GARMIN, f"g-{activity_id}", activity_id)
            store_streams(
                self.db, GARMIN, f"g-{activity_id}", ride_power(peak), resolution
            )
            summaries.append(
                {
                    "id": activity_id,
                    "sport": "ride",
                    "environment": "outdoor",
                    "started_at": f"{day}T08:00:00+00:00",
                    "started_at_local": f"{day}T10:00:00",
                    "record_ids": {"garmin": f"g-{activity_id}"},
                    "source": "Intervals.icu",
                }
            )

        result = self.service(summaries).power_profiles()

        window = result["windows"]["90"]
        self.assertEqual(
            {"records": 2, "usable_records": 1, "unknown_records": 1},
            {
                key: window["cache_coverage"][key]
                for key in ("records", "usable_records", "unknown_records")
            },
        )
        best = next(
            point
            for point in window["power"]
            if point["duration_seconds"] == 180 and point["environment"] == "outdoor"
        )
        self.assertEqual("Garmin Connect original watts stream", best["source"])
        self.assertEqual(
            (350.0, "act_1", 300.0),
            (best["watts"], best["activity_id"], best["start_seconds"]),
        )
        self.assertEqual(
            ("mean-max-curve", 3), (result["method"], result["method_version"])
        )
        self.assertRegex(window["input_fingerprint"], r"^[0-9a-f]{64}$")

    def test_a_shared_read_queries_no_session_while_the_inputs_are_unchanged(self):
        summaries = []
        for activity_id in ("act_1", "act_2", "act_3"):
            self.add_ride(summaries, activity_id)
        service = self.service(summaries)

        with patch.object(
            mean_max_curve.activity_detail_store,
            "full_stream_keys",
            wraps=full_stream_keys,
        ) as stream_keys:
            first = service.curve_records(60, shared=True)
            self.assertEqual((1, 3), (stream_keys.call_count, len(first)))
            self.assertEqual(3, self.session_records.call_count)
            again = service.curve_records(60, shared=True)

        # The second read of the day touches no stream key and no session.
        self.assertEqual(1, stream_keys.call_count)
        self.assertEqual(3, self.session_records.call_count)
        self.assertIs(first, again)
        self.assertEqual(list(first), service.curve_records(60))
        # One tuple of read-only records: a caller cannot change what others read.
        self.assertIsInstance(first, tuple)
        with self.assertRaises(TypeError):
            first[0]["date"] = "2000-01-01"

    def test_a_shared_read_is_built_again_on_a_new_day_or_window(self):
        summaries = []
        self.add_ride(summaries, "act_1")
        service = self.service(summaries)
        service.curve_records(60, shared=True)

        for day, days in ((date(2026, 10, 19), 60), (date(2026, 10, 19), 30)):
            self.day = day
            with self.subTest(day=day, days=days):
                before = self.session_records.call_count
                service.curve_records(days, shared=True)
                self.assertEqual(before + 1, self.session_records.call_count)
                service.curve_records(days, shared=True)
                self.assertEqual(before + 1, self.session_records.call_count)

    def test_a_shared_read_follows_a_changed_session_stream_or_summary(self):
        summaries = []
        self.add_ride(summaries, "act_1")
        service = self.service(summaries)
        store_summary(self.db, "g-act_1", "a" * 64)
        store_streams(
            self.db,
            GARMIN,
            "g-act_1",
            ride_power(350.0),
            StreamResolution.FULL,
            summary_sha256="a" * 64,
        )

        def peaks():
            return [
                row["power_profile"].get("duration_curve")
                and next(
                    point["watts"]
                    for point in row["power_profile"]["duration_curve"]
                    if point["duration_seconds"] == 180
                )
                for row in service.curve_records(60, shared=True)
            ]

        self.assertEqual([350.0], peaks())
        store_streams(
            self.db,
            GARMIN,
            "g-act_1",
            ride_power(380.0),
            StreamResolution.FULL,
            summary_sha256="a" * 64,
        )
        self.assertEqual([380.0], peaks())
        self.add_ride(summaries, "act_2", peak=300.0)
        self.assertEqual([380.0, 300.0], peaks())
        # A newer provider summary makes the set stale: the profile is empty.
        store_summary(self.db, "g-act_1", "b" * 64)
        self.assertEqual([None, 300.0], peaks())
        # A change of the session's own summary (the athlete marks it a
        # commute) leaves the records and sets alone and still counts.
        summaries[1]["purpose"] = "commute"
        self.assertEqual([None], peaks())

    def test_a_shared_read_follows_a_linked_record_with_a_wrong_device_clock(self):
        summaries = []
        self.add_ride(summaries, "act_1")
        # The session's streams come from a linked record that starts months
        # before the window: only the session link, not its date, ties it in.
        add_record(
            self.db,
            WAHOO,
            "w-act_1",
            "act_1",
            started_at="2026-01-01T08:00:00+00:00",
        )
        store_streams(
            self.db, WAHOO, "w-act_1", ride_power(330.0), StreamResolution.FULL
        )
        self.db.execute("DELETE FROM activity_detail_sets WHERE provider = 'garmin'")
        service = self.service(summaries)

        def peak():
            (row,) = service.curve_records(60, shared=True)
            return next(
                point["watts"]
                for point in row["power_profile"]["duration_curve"]
                if point["duration_seconds"] == 180
            )

        self.assertEqual(330.0, peak())
        store_streams(
            self.db, WAHOO, "w-act_1", ride_power(360.0), StreamResolution.FULL
        )
        self.assertEqual(360.0, peak())

    def test_a_shared_read_ignores_changes_the_records_do_not_show(self):
        summaries = []
        self.add_ride(summaries, "act_1")
        service = self.service(summaries)
        service.curve_records(60, shared=True)
        calls = self.session_records.call_count

        summaries[0].update(load=99.0, zone_times={"z2": 600}, notes="x")
        service.curve_records(60, shared=True)
        self.assertEqual(calls, self.session_records.call_count)
        # A field the records show is read again.
        summaries[0]["name"] = "Morning ride"
        (row,) = service.curve_records(60, shared=True)
        self.assertEqual(
            (calls + 1, "Morning ride"), (self.session_records.call_count, row["name"])
        )

    def test_a_shared_read_follows_a_merge_a_lap_set_and_a_record_update(self):
        summaries = []
        self.add_ride(summaries, "act_1")
        add_session(self.db, "act_2")
        add_record(self.db, WAHOO, "w-act_2", "act_2")
        store_streams(
            self.db, WAHOO, "w-act_2", ride_power(400.0), StreamResolution.FULL
        )
        service = self.service(summaries)
        service.curve_records(60, shared=True)
        changes = {
            "merge": lambda: self.db.execute(
                "UPDATE activity_sessions SET state = 'duplicate', "
                "merged_into = 'act_1' WHERE activity_id = 'act_2'"
            ),
            "lap set": lambda: store_laps(self.db, GARMIN, "g-act_1", INTERVAL_LAPS),
            "record update": lambda: self.db.execute(
                "UPDATE activity_records SET updated_at = '2026-10-17T00:00:00+00:00' "
                "WHERE provider_activity_id = 'g-act_1'"
            ),
        }
        for name, change in changes.items():
            with self.subTest(name):
                calls = self.session_records.call_count
                change()
                service.curve_records(60, shared=True)
                self.assertEqual(calls + 1, self.session_records.call_count)

    def test_a_concurrent_build_finishing_later_keeps_the_first_result(self):
        self.add_ride([], "act_1")
        reader = SessionCurveReader(self.session_records)
        builds = []

        def build(name):
            builds.append(name)
            return [{"build": name}]

        def older():
            # A newer read finishes while this build is still running.
            reader.shared_records(self.db, ["act_1"], "newer", lambda: build("newer"))
            return build("older")

        reader.shared_records(self.db, ["act_1"], "older", older)
        self.assertEqual(["newer", "older"], builds)
        (kept,) = reader.shared_records(self.db, ["act_1"], "newer", lambda: build("x"))
        self.assertEqual("newer", kept["build"])
        self.assertEqual(["newer", "older"], builds)

    def test_a_new_weather_row_starts_no_all_time_rebuild(self):
        # No all-time reader names a temperature, so the activity weather is
        # not read for them and a new weather row builds nothing (#3036).
        summaries = []
        self.add_ride(summaries, "act_1", day="2026-10-10")
        builds = []
        service = self.service(
            summaries, spawn=builds.append, pause=lambda _seconds: None
        )
        service.power_profiles(all_time=True)
        builds.pop()()
        # The 90-day comparisons still read it; the all-time records do not.
        with patch.object(
            report_service, "session_temperatures", return_value={"act_1": 21.0}
        ):
            service.power_profiles(all_time=True)
        self.assertEqual([], builds)

    def test_the_all_time_build_keeps_every_curve_cached(self):
        # #2822: with more sessions than the memo held, the build evicted the
        # recent windows' curves and every rebuild recomputed the whole history.
        # It now reserves room for every session.
        summaries = []
        for index, day in enumerate(("2026-10-10", "2026-09-01")):
            self.add_ride(summaries, f"act_recent{index}", day=day)
        for index in range(4):
            self.add_ride(summaries, f"act_old{index}", day=f"2025-0{index + 1}-14")
        builds = []
        service = self.service(
            summaries, spawn=builds.append, capacity=4, pause=lambda _seconds: None
        )
        service.power_profiles(all_time=True)
        builds.pop()()
        self.add_ride(summaries, "act_new", day="2026-10-17")

        with patch.object(
            mean_max_curve, "_session_curve", wraps=mean_max_curve._session_curve
        ) as computed:
            recent = service.curve_records(90)
            service.power_profiles(all_time=True)
            builds.pop()()

        self.assertEqual(3, len(recent))
        # The new session is computed by the 90-day read and evicts the oldest
        # curve, which the rebuild loads from the store (#2821); no recent curve
        # is recomputed.
        self.assertEqual(
            ["act_new"], [call.args[1] for call in computed.call_args_list]
        )

    def test_all_time_bests_keep_old_efforts_while_the_90_days_and_coach_do_not(self):
        summaries = []
        self.add_ride(summaries, "act_old", day="2025-03-14", peak=450.0)
        self.add_ride(summaries, "act_recent", day="2026-10-10", peak=350.0)
        summaries[0].update(distance_m=80000.0, sources={"distance_m": "Garmin"})
        summaries[1].update(distance_m=40000.0, sources={"distance_m": "Garmin"})
        builds = []
        pauses = []
        ticks = itertools.count(0.0, 0.25)
        service = self.service(
            summaries,
            spawn=builds.append,
            pause=lambda seconds: pauses.append((seconds, self.snapshots_open)),
            clock=lambda: next(ticks),
        )

        def best(window):
            return next(
                (point["watts"], point["activity_id"], point["date"])
                for point in window["power"]
                if point["duration_seconds"] == 180
                and point["environment"] == "outdoor"
            )

        # The request never reads the whole history cold: it starts one
        # background build and answers pending until that build is done.
        self.assertEqual(
            {"status": "pending"}, service.power_profiles(all_time=True)["all_time"]
        )
        self.assertEqual(
            {"status": "pending"}, service.power_profiles(all_time=True)["all_time"]
        )
        self.assertEqual(1, len(builds))
        read = {call.args[1] for call in self.session_records.call_args_list}
        self.assertNotIn("act_old", read)
        builds.pop()()
        # One session per read snapshot, then a pause as long as that session
        # took, outside the snapshot, so request threads get the interpreter
        # (#2757); the records equal those of one read over the same days.
        self.assertEqual([(0.25, 0), (0.25, 0)], pauses)
        every, current = service.all_time_records()
        self.assertTrue(current)
        one_read = service.curve_records((self.day - date(2025, 3, 14)).days + 1)
        # The all-time records add only each session's totals (#3826).
        self.assertCountEqual(
            one_read,
            [{k: v for k, v in record.items() if k != "totals"} for record in every],
        )
        cards = service.power_profiles(all_time=True)
        self.assertEqual([], builds)
        self.assertEqual((450.0, "act_old", "2025-03-14"), best(cards["all_time"]))
        self.assertEqual("2025-03-14", cards["all_time"]["date_start"])
        self.assertFalse(cards["all_time"]["stale"])
        self.assertEqual(2, cards["all_time"]["cache_coverage"]["power_records"])
        # The single-activity records read the same all-time sessions (#3826).
        longest = cards["all_time"]["activity_records"][0]
        self.assertEqual(
            ("ride", "distance_m", 80000.0, "act_old", "Garmin"),
            tuple(
                longest[k] for k in ("sport", "field", "value", "activity_id", "source")
            ),
        )
        # The 90-day window, the CP/CS input and the Coach's copy stay recent.
        self.assertEqual(
            (350.0, "act_recent", "2026-10-10"), best(cards["windows"]["90"])
        )
        self.assertEqual(
            ["act_recent"], [row["activity_id"] for row in cards["activities"]]
        )
        self.assertNotIn("act_old", str(cards["critical_power"]))
        coach = service.power_profiles()
        self.assertNotIn("all_time", coach)
        self.assertEqual(_without_all_time(cards), coach)

    def test_a_background_build_keeps_the_last_records_and_pauses_after_a_failure(self):
        add_session(self.db, "act_1")
        builds = []
        reader = SessionCurveReader(self.session_records, spawn=builds.append)

        # A fixed clock: each build takes a known time; the diagnostics state
        # reports it (#2852).
        now = [100.0]
        clock = SimpleNamespace(monotonic=lambda: now[0])
        self.enterContext(patch.object(mean_max_curve, "time", clock))
        seen = []

        def timed(seconds, records=None):
            def build():
                now[0] += seconds
                seen.append(reader.background_state()["building_s"])
                if records is None:
                    raise ValueError("unreadable stream")
                return records

            return build

        broken = timed(5.0)

        def state(*names):
            current = reader.background_state()
            return tuple(current[name] for name in ("status", *names))

        self.assertEqual(state("builds", "failures"), ("pending", 0, 0))
        self.assertIsNone(
            reader.background_records(self.db, ["act_1"], "a", timed(40.0, [{"n": 1}]))
        )
        self.assertEqual(state("builds"), ("building", 0))
        builds.pop()()
        self.assertEqual(seen, [40.0])  # a running build shows its time so far
        self.assertEqual(state("builds", "last_build_s"), ("ready", 1, 40.0))
        (first,), current = reader.background_records(self.db, ["act_1"], "a", list)
        self.assertTrue(current)
        # Changed inputs build again in the background; the last records show
        # meanwhile, marked as not current.
        self.assertEqual(
            ((first,), False),
            reader.background_records(self.db, ["act_1"], "b", broken),
        )
        self.assertEqual(state("last_build_s"), ("building", 40.0))
        with self.assertLogs("intervals_coach", "ERROR"):
            builds.pop()()
        now[0] += 30.0
        self.assertEqual(
            state("builds", "failures", "last_build_s", "failed_age_s"),
            ("failed", 1, 1, 5.0, 30.0),
        )
        now[0] -= 30.0  # back inside the retry pause
        with self.assertRaises(RuntimeError):
            reader.background_records(self.db, ["act_1"], "b", broken)
        self.assertEqual([], builds)
        later = SimpleNamespace(monotonic=lambda: float("inf"))
        with patch.object(mean_max_curve, "time", later):
            reader.background_records(self.db, ["act_1"], "b", broken)
        self.assertEqual(1, len(builds))
        # A thread that cannot start leaves no build marked as running.
        spawn = Mock(side_effect=RuntimeError("can't start new thread"))
        failing = SessionCurveReader(self.session_records, spawn=spawn)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                failing.background_records(self.db, ["act_1"], "a", list)
        self.assertEqual(2, spawn.call_count)

    def test_e_bike_sessions_stay_out_of_the_bests_and_fits(self):
        summaries = []
        for activity_id, peak, assist in (
            ("act_1", 300.0, None),
            ("act_2", 900.0, "electric"),
        ):
            add_session(self.db, activity_id)
            add_record(self.db, GARMIN, f"g-{activity_id}", activity_id)
            store_streams(
                self.db,
                GARMIN,
                f"g-{activity_id}",
                ride_power(peak),
                StreamResolution.FULL,
            )
            summary = {
                "id": activity_id,
                "sport": "ride",
                "environment": "outdoor",
                "started_at": "2026-10-10T08:00:00+00:00",
                "started_at_local": "2026-10-10T10:00:00",
                "record_ids": {"garmin": f"g-{activity_id}"},
                "source": "Intervals.icu",
            }
            if assist:
                summary["assist"] = assist
            summaries.append(summary)

        result = self.service(summaries).power_profiles()

        self.assertEqual(
            ["act_1"], [row["activity_id"] for row in result["activities"]]
        )
        bests = {
            point["watts"]
            for point in result["windows"]["90"]["power"]
            if point["watts"] is not None
        }
        self.assertTrue(bests)
        self.assertLessEqual(max(bests), 300.0)

    def test_commutes_stay_out_and_races_are_flagged(self):
        summaries = []
        for activity_id, peak, purpose in (
            ("act_1", 300.0, "race"),
            ("act_2", 900.0, "commute"),
            ("act_3", 950.0, "warmup"),
        ):
            add_session(self.db, activity_id)
            add_record(self.db, GARMIN, f"g-{activity_id}", activity_id)
            store_streams(
                self.db,
                GARMIN,
                f"g-{activity_id}",
                ride_power(peak),
                StreamResolution.FULL,
            )
            summaries.append(
                {
                    "id": activity_id,
                    "sport": "ride",
                    "environment": "outdoor",
                    "started_at": "2026-10-10T08:00:00+00:00",
                    "started_at_local": "2026-10-10T10:00:00",
                    "record_ids": {"garmin": f"g-{activity_id}"},
                    "source": "Intervals.icu",
                    "purpose": purpose,
                }
            )

        records = self.service(summaries).curve_records()

        self.assertEqual(
            [("act_1", "race")],
            [(row["activity_id"], row["effort_context"]) for row in records],
        )

    def test_overlapping_bests_of_one_effort_share_an_effort_group(self):
        def ride(activity_id, curve):
            return {
                "activity_id": activity_id,
                "date": "2026-10-10",
                "sport": "ride",
                "environment": "outdoor",
                "fingerprint": activity_id,
                "power_profile": {
                    "status": "ok",
                    "points": [],
                    "duration_curve": [
                        {"duration_seconds": d, "watts": w, "start_seconds": s}
                        for d, w, s in curve
                    ],
                },
            }

        records = [
            # 3 and 7 min overlap within a; its 12 min starts after both end.
            ride("a", [(180, 400.0, 100.0), (420, 330.0, 0.0), (720, 250.0, 1000.0)]),
            ride("b", [(180, 300.0, 0.0), (420, 300.0, 0.0), (720, 300.0, 0.0)]),
        ]
        service = TrainingProfileSelectionService(lambda: date(2026, 10, 18))

        window = service.power_profiles(records)["windows"]["90"]

        groups = {
            point["duration_seconds"]: (point["activity_id"], point["effort_group"])
            for point in window["power"]
            if point["environment"] == "outdoor" and point["status"] == "ok"
        }
        self.assertEqual(groups[180][1], groups[420][1])
        self.assertEqual("b", groups[720][0])
        self.assertNotEqual(groups[180][1], groups[720][1])
        reordered = service.power_profiles(records[::-1])["windows"]["90"]
        self.assertEqual(window["input_fingerprint"], reordered["input_fingerprint"])

    def test_running_distance_bests_run_up_to_the_marathon(self):
        records = [
            {
                **activity,
                "activity_id": activity_id,
                "fingerprint": activity_id,
                "running_profile": running_profile(activity),
            }
            for activity_id, activity in (
                ("long", steady_run(43, speed=3.0, day="2026-09-01")),
                ("fast", steady_run(12, speed=4.0, day="2026-10-10")),
            )
        ]
        service = TrainingProfileSelectionService(lambda: date(2026, 10, 18))

        window = service.power_profiles(records)["windows"]["90"]

        self.assertEqual(
            [
                (1000, "fast"),
                (5000, "fast"),
                (10000, "fast"),
                (21097.5, "long"),
                (42195, "long"),
            ],
            [
                (point["distance_meters"], point["activity_id"])
                for point in window["running"]["distance"]
            ],
        )


if __name__ == "__main__":
    unittest.main()
