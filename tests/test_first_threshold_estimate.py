"""Athlete-level first threshold estimate, its adoption and its Coach visibility (#1867)."""

import json
import re
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock

from support import dispatch_route

from backend import change_history
from backend.activities.hr_cap import (
    SOURCE_ESTIMATED,
    SOURCE_MEASURED,
    SOURCES,
    cap_label,
    cap_percents,
    effective_caps,
    hr_cap,
    normalize_hr_cap,
)
from backend.activities.zone_settings import own_lt1_share
from backend.athlete.profile import (
    COACH_READ_ONLY_FIELDS,
    DEFAULT_PROFILE,
    ProfileService,
    audit_view,
    normalize_profile,
)
from backend.canonical.activity_detail import (
    ActivityDfaAlpha1,
    ActivityStreamSet,
    DfaWindow,
)
from backend.canonical.first_threshold import (
    FIRST_THRESHOLD_FIELD,
    FIRST_THRESHOLD_ORIGIN_FIELD,
    ORIGIN_ESTIMATED,
    ORIGIN_MEASURED,
    first_threshold_estimated,
)
from backend.canonical.records import Provenance
from backend.canonical.vocabulary import (
    ActivityDetailOrigin,
    DfaQuality,
    HeartRateSource,
    ProviderId,
    RecordingOrigin,
    SourceKind,
    Sport,
    StreamChannel,
    StreamResolution,
)
from backend.coach import tool_schemas
from backend.coach.context import CoachStructuredContextService
from backend.db import (
    DatabaseManager,
    activity_detail_store,
    canonical_store,
    row_factory,
)
from backend.db.activity_detail_store import (
    detail_sets_for,
    first_threshold_results,
    replace_detail_set,
    stored_dfa_alpha1,
)
from backend.db.repositories import KeyValueRepository, ProfileRepository
from backend.db.schema import initialize_schema
from backend.errors import AppError
from backend.http_api.athlete_get import AthleteGetRoutes
from backend.http_api.first_threshold_post import ADOPT_PATH, FirstThresholdPostRoutes
from backend.performance.current_metrics import coach_thresholds
from backend.performance.first_threshold_estimate import (
    MIN_SESSIONS,
    SessionThreshold,
    estimate_view,
    first_threshold_estimate,
)
from backend.performance.first_threshold_service import FirstThresholdService
from backend.planning import run_hr_cap
from tests.test_dfa_alpha1 import ramp
from tests.test_fit_rr_mapping import fit_with_rr, streams

TODAY = date(2026, 11, 9)
GOOD, LOW = DfaQuality.GOOD, DfaQuality.LOW
STAMP = "2026-11-01T08:00:00+00:00"


def session(days_ago, bpm, quality=GOOD):
    return SessionThreshold(TODAY - timedelta(days=days_ago), bpm, quality)


def lthr_of(value):
    return lambda _day: value


class AthleteEstimateTests(unittest.TestCase):
    def test_the_median_of_three_sessions_is_percent_of_the_lthr(self) -> None:
        # 150, 153 and 156 bpm at an LTHR of 170 are 88.2, 90.0 and 91.8 percent.
        estimate = first_threshold_estimate(
            [session(5, 150), session(10, 153), session(15, 156)],
            lthr_of(170.0),
            TODAY,
        )
        assert estimate is not None
        self.assertEqual(estimate.percent_lthr, 90.0)
        self.assertEqual(estimate.sessions, 3)
        self.assertEqual(estimate.spread_pct, 3.6)
        self.assertEqual(estimate.first_day, TODAY - timedelta(days=15))
        self.assertEqual(estimate.last_day, TODAY - timedelta(days=5))
        self.assertIs(estimate.quality, LOW)  # three sessions are the minimum only

    def test_fewer_than_three_sessions_give_no_estimate(self) -> None:
        self.assertEqual(MIN_SESSIONS, 3)
        sessions = [session(5, 150), session(10, 153)]
        self.assertIsNone(first_threshold_estimate(sessions, lthr_of(170.0), TODAY))
        self.assertIsNone(first_threshold_estimate([], lthr_of(170.0), TODAY))

    def test_each_session_uses_the_lthr_in_effect_on_its_own_day(self) -> None:
        sessions = [session(5, 162), session(10, 153), session(15, 144)]
        by_day = {
            TODAY - timedelta(days=5): 180.0,
            TODAY - timedelta(days=10): 170.0,
            TODAY - timedelta(days=15): 160.0,
        }
        estimate = first_threshold_estimate(sessions, by_day.get, TODAY)
        assert estimate is not None
        # 162/180, 153/170 and 144/160 are all 90 percent although the bpm differ.
        self.assertEqual((estimate.percent_lthr, estimate.spread_pct), (90.0, 0.0))

    def test_only_the_last_60_days_count(self) -> None:
        sessions = [
            session(5, 150),
            session(10, 153),
            session(59, 156),
            session(60, 120),
        ]
        estimate = first_threshold_estimate(sessions, lthr_of(170.0), TODAY)
        assert estimate is not None
        self.assertEqual(estimate.sessions, 3)  # day 59 counts, day 60 does not
        self.assertIsNone(
            first_threshold_estimate(
                [session(5, 150), session(10, 153), session(60, 156)],
                lthr_of(170.0),
                TODAY,
            )
        )

    def test_low_quality_and_implausible_sessions_do_not_qualify(self) -> None:
        sessions = [
            session(5, 150),
            session(10, 153),
            session(15, 156, LOW),
            session(20, 400),  # 235 percent of the threshold
        ]
        self.assertIsNone(first_threshold_estimate(sessions, lthr_of(170.0), TODAY))

    def test_a_session_without_a_known_threshold_does_not_qualify(self) -> None:
        sessions = [session(5, 150), session(10, 153), session(15, 156)]
        known = {TODAY - timedelta(days=10): 170.0, TODAY - timedelta(days=15): 170.0}
        self.assertIsNone(first_threshold_estimate(sessions, known.get, TODAY))
        known[TODAY - timedelta(days=5)] = 170.0
        self.assertIsNotNone(first_threshold_estimate(sessions, known.get, TODAY))

    def test_five_agreeing_sessions_make_a_good_estimate(self) -> None:
        agreeing = [
            session(day, bpm)
            for day, bpm in ((2, 150), (6, 152), (9, 153), (12, 154), (20, 155))
        ]
        estimate = first_threshold_estimate(agreeing, lthr_of(170.0), TODAY)
        assert estimate is not None
        self.assertIs(estimate.quality, GOOD)
        wide = [*agreeing[:4], session(30, 120)]
        low = first_threshold_estimate(wide, lthr_of(170.0), TODAY)
        assert low is not None
        self.assertIs(low.quality, LOW)  # spread above five points

    def test_the_view_resolves_bpm_with_the_current_threshold_and_names_the_source(
        self,
    ) -> None:
        estimate = first_threshold_estimate(
            [session(5, 153), session(10, 153), session(15, 153)],
            lthr_of(170.0),
            TODAY,
        )
        view = estimate_view(estimate, 172.0)
        assert view is not None
        self.assertEqual(view["percent_lthr"], 90.0)
        self.assertEqual(view["bpm"], 155)  # 90 percent of 172 is 154.8
        self.assertEqual(view["quality"], "low")
        self.assertEqual(view["sessions"], 3)
        self.assertIn("kein Laborwert", view["source"])
        without_threshold = estimate_view(estimate, None)
        assert without_threshold is not None
        self.assertIsNone(without_threshold["bpm"])
        self.assertIsNone(estimate_view(None, 170.0))


class CapSourceTests(unittest.TestCase):
    def test_an_adopted_estimate_gives_the_estimated_cap_source(self) -> None:
        percents, source = cap_percents(90.0, True)
        self.assertEqual(source, SOURCE_ESTIMATED)
        self.assertEqual(percents["easy"], cap_percents(90.0)[0]["easy"])
        self.assertEqual(cap_percents(90.0)[1], SOURCE_MEASURED)
        self.assertEqual(cap_percents(None, True)[1], "default")
        self.assertEqual(effective_caps(90.0, True)["easy"]["source"], "estimated")

    def test_the_third_source_is_accepted_labelled_and_checked(self) -> None:
        self.assertEqual(SOURCES, ("default", "measured", "estimated"))
        cap = hr_cap(87.0, SOURCE_ESTIMATED)
        self.assertEqual(normalize_hr_cap(cap)["source"], "estimated")
        self.assertTrue(cap_label(cap, 170.0).endswith("geschätzt"))
        unit = {"sport": "run", "session_focus": "easy", "hr_cap": cap}
        mismatch = run_hr_cap.cap_problem(unit, 90.0)
        assert mismatch is not None
        self.assertEqual(mismatch["reason"], run_hr_cap.SOURCE_MISMATCH)
        adopted = {**unit, "hr_cap": effective_caps(90.0, True)["easy"]}
        self.assertIsNone(run_hr_cap.cap_problem(adopted, 90.0, True))
        self.assertEqual(len(run_hr_cap.problems([adopted], 90.0, False)), 1)

    def test_the_profile_origin_decides_the_estimated_flag_and_the_coach_thresholds(
        self,
    ) -> None:
        stored = {
            FIRST_THRESHOLD_FIELD: "90",
            FIRST_THRESHOLD_ORIGIN_FIELD: "estimated",
        }
        self.assertTrue(first_threshold_estimated(stored))
        measured = {**stored, FIRST_THRESHOLD_ORIGIN_FIELD: "measured"}
        self.assertFalse(first_threshold_estimated(measured))
        self.assertFalse(
            first_threshold_estimated({FIRST_THRESHOLD_ORIGIN_FIELD: "estimated"})
        )
        metrics: dict[str, dict[str, Any]] = {
            "cycling_ftp_watts": {"value": None, "source": None},
            "max_hr_bpm": {"value": 190},
            "bike_threshold_hr_bpm": {"value": 170},
            "run_threshold_hr_bpm": {"value": 170},
        }
        thresholds = coach_thresholds(metrics, 90.0, True)
        self.assertEqual(thresholds["run_hr_caps"]["easy"]["source"], "estimated")


def stream_set(
    activity_id, estimate=None, *, provider=ProviderId.GARMIN, hr_source=None
):
    return ActivityStreamSet(
        provider_activity_id=activity_id,
        provenance=Provenance(
            provider=provider,
            source_kind=SourceKind.PROVIDER_REPORTED,
            source_record_id=None,
            recorded_by=RecordingOrigin.GARMIN,
            retrieved_at=datetime(2026, 11, 9, 8, tzinfo=UTC),
            label=None,
        ),
        origin=ActivityDetailOrigin.FETCH,
        resolution=StreamResolution.FULL,
        channels={
            StreamChannel.TIME_S: (0.0, 1.0, 2.0),
            StreamChannel.HEART_RATE_BPM: (120.0, 121.0, 122.0),
        },
        dfa_alpha1=estimate,
        hr_source=hr_source,
    )


def result(bpm, quality=GOOD, windows=40):
    return ActivityDfaAlpha1(
        estimate_bpm=bpm,
        windows=windows,
        artifact_share=0.01,
        quality=quality,
        method=1,
    )


def open_database(case: Any) -> None:
    """Give ``case`` a temporary encrypted-schema database in ``case.manager``."""
    temporary = tempfile.TemporaryDirectory()
    case.addCleanup(temporary.cleanup)
    case.manager = DatabaseManager(
        Path(temporary.name) / "first.db", sqlite3, row_factory=row_factory
    )
    case.addCleanup(case.manager.close)
    with case.manager.unit_of_work() as db:
        initialize_schema(db)
    case.counter = 0


def add_session(db, activity_id, provider, record_id, day):
    db.execute(
        "INSERT INTO activity_sessions(activity_id, primary_provider, "
        "primary_provider_activity_id, started_at, local_date, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            activity_id,
            provider,
            record_id,
            f"{day}T07:00:00+00:00",
            day,
            STAMP,
            STAMP,
        ),
    )


def add_record(db, activity_id, provider, record_id, day, sport):
    db.execute(
        "INSERT INTO activity_records(provider, provider_activity_id, activity_id, "
        "state, mapper_version, source_kind, recorded_by, sport, environment, "
        "started_at, local_date, load_model, fetched_at, created_at, updated_at) "
        "VALUES (?, ?, ?, 'active', 1, 'provider_reported', ?, ?, 'outdoor', ?, ?, "
        "'unknown', ?, ?, ?)",
        (
            provider,
            record_id,
            activity_id,
            provider,
            sport,
            f"{day}T07:00:00+00:00",
            day,
            STAMP,
            STAMP,
            STAMP,
        ),
    )


def add_run(
    case: Any,
    days_ago,
    estimate,
    *,
    sport="run",
    provider="garmin",
    session=None,
    hr_source=None,
):
    """A live session with one record and a stream set holding ``estimate``."""
    case.counter += 1
    day = (TODAY - timedelta(days=days_ago)).isoformat()
    activity_id = session or f"act_{case.counter}"
    record_id = f"r{case.counter}"
    with case.manager.unit_of_work() as db:
        if session is None:
            add_session(db, activity_id, provider, record_id, day)
        add_record(db, activity_id, provider, record_id, day, sport)
        replace_detail_set(
            db,
            stream_set(
                record_id, estimate, provider=ProviderId(provider), hr_source=hr_source
            ),
            mapper_version=6,
            summary_sha256=None,
            now=STAMP,
        )
    return activity_id


class StoredEstimateTests(unittest.TestCase):
    manager: DatabaseManager
    counter: int

    def setUp(self) -> None:
        open_database(self)

    def test_the_estimate_round_trips_with_its_stream_set(self) -> None:
        add_run(self, 3, result(152.5))
        with self.manager.reader() as db:
            stored = detail_sets_for(db, "garmin", "r1")
            text = db.execute("SELECT dfa_alpha1 FROM activity_detail_sets").fetchone()[
                "dfa_alpha1"
            ]
        detail = stored[0].detail
        assert isinstance(detail, ActivityStreamSet)
        self.assertEqual(detail.dfa_alpha1, result(152.5))
        self.assertEqual(
            json.loads(text),
            {
                "estimate_bpm": 152.5,
                "windows": 40,
                "artifact_share": 0.01,
                "quality": "good",
                "method": 1,
            },
        )

    def test_a_set_without_an_estimate_stores_null_and_bad_text_reads_as_none(
        self,
    ) -> None:
        add_run(self, 3, None)
        with self.manager.reader() as db:
            row = db.execute("SELECT dfa_alpha1 FROM activity_detail_sets").fetchone()
        self.assertIsNone(row["dfa_alpha1"])
        for text in ("{}", '{"estimate_bpm": 5000}', '{"quality": "x"}', "[]"):
            self.assertIsNone(stored_dfa_alpha1(text), text)

    def test_the_series_round_trips_and_a_bad_series_keeps_the_estimate(self) -> None:
        # #3273: a method-3 row keeps its windows; a malformed series reads as an
        # empty one (not evaluated) and never drops the 0.75 result.
        window = DfaWindow(
            start_s=30.0, heart_rate_bpm=150.2, alpha1=0.81, artifact_share=0.0
        )
        add_run(self, 3, replace(result(152.5), method=3, series=(window,)))
        with self.manager.reader() as db:
            text = db.execute("SELECT dfa_alpha1 FROM activity_detail_sets").fetchone()[
                "dfa_alpha1"
            ]
        read = stored_dfa_alpha1(text)
        assert read is not None
        self.assertEqual(read.series, (window,))
        self.assertTrue(read.series_evaluated)
        for series in ('"x"', "[[1, 2]]", "[[0, 500, 1, 0]]", "[[0, 150, 1, 2]]"):
            item = {**json.loads(text), "series": json.loads(series)}
            broken = stored_dfa_alpha1(json.dumps(item))
            assert broken is not None, series
            self.assertEqual(broken.estimate_bpm, 152.5)
            self.assertEqual(broken.series, ())
            self.assertFalse(broken.series_evaluated)

    def test_a_series_only_row_reads_without_an_estimate_and_older_builds_skip_it(
        self,
    ) -> None:
        # A recording without a 0.75 crossing stores null estimate and quality:
        # this build reads its series; a build before method 3 required a number
        # for estimate_bpm and so reads the row as no result at all (rollback).
        window = DfaWindow(
            start_s=0.0, heart_rate_bpm=171.0, alpha1=0.48, artifact_share=0.0
        )
        add_run(
            self,
            3,
            ActivityDfaAlpha1(
                estimate_bpm=None,
                windows=1,
                artifact_share=0.0,
                quality=None,
                method=3,
                series=(window,),
            ),
        )
        with self.manager.reader() as db:
            text = db.execute("SELECT dfa_alpha1 FROM activity_detail_sets").fetchone()[
                "dfa_alpha1"
            ]
            rows = first_threshold_results(
                db, since=TODAY - timedelta(days=59), until=TODAY, sport=Sport.RUN
            )
        item = json.loads(text)
        self.assertIsNone(item["estimate_bpm"])
        self.assertIsNone(item["quality"])
        read = stored_dfa_alpha1(text)
        assert read is not None
        self.assertEqual(read.series, (window,))
        self.assertEqual(rows, [])  # the LT1 reader skips it

    def test_a_method_2_row_is_not_evaluated_and_still_counts_for_lt1(self) -> None:
        # Rows stored before #3273 have no series: "not evaluated", never "no
        # crossing"; their 0.75 estimate stays an LT1 input.
        add_run(self, 3, replace(result(150.0), method=2))
        with self.manager.reader() as db:
            rows = first_threshold_results(
                db, since=TODAY - timedelta(days=59), until=TODAY, sport=Sport.RUN
            )
        self.assertEqual([row.result.estimate_bpm for row in rows], [150.0])
        self.assertFalse(rows[0].result.series_evaluated)

    def test_diagnostics_count_chest_strap_runs_by_dfa_method(self) -> None:
        # #3273: counts only, chest-strap runs of the window; a ride, an optical
        # run and an older run stay out.
        window = DfaWindow(
            start_s=0.0, heart_rate_bpm=150.0, alpha1=0.8, artifact_share=0.0
        )
        strap = HeartRateSource.CHEST_STRAP
        add_run(
            self, 2, replace(result(150.0), method=3, series=(window,)), hr_source=strap
        )
        add_run(self, 3, replace(result(151.0), method=2), hr_source=strap)
        add_run(self, 4, None, hr_source=strap)
        add_run(self, 5, None, sport="ride", hr_source=strap)
        add_run(
            self, 6, replace(result(150.0), method=2), hr_source=HeartRateSource.OPTICAL
        )
        add_run(self, 70, replace(result(150.0), method=2), hr_source=strap)
        with self.manager.reader() as db:
            counts = activity_detail_store.dfa_series_counts(
                db, since=TODAY - timedelta(days=59)
            )
        self.assertEqual(
            counts,
            {
                "runs": 3,
                "with_series": 1,
                "unreadable_series": 0,
                "before_series": 1,
                "without_result": 1,
            },
        )

    def test_the_chest_strap_max_hr_ignores_optical_and_older_recordings(self) -> None:
        # #3920 C6: the highest max HR of a chest-strap recording since the day,
        # any sport; an optical recording and one before the window never count.
        strap = HeartRateSource.CHEST_STRAP
        for days_ago, bpm, source, sport in (
            (10, 188.0, strap, "run"),
            (20, 191.0, strap, "ride"),
            (5, 199.0, HeartRateSource.OPTICAL, "run"),
            (400, 195.0, strap, "run"),
        ):
            activity_id = add_run(self, days_ago, None, sport=sport, hr_source=source)
            with self.manager.unit_of_work() as db:
                db.execute(
                    "UPDATE activity_records SET details = ? WHERE activity_id = ?",
                    (json.dumps({"max_hr": bpm}), activity_id),
                )

        def read(since: date):
            with self.manager.reader() as db:
                return canonical_store.chest_strap_max_hr_row(
                    db, since=since.isoformat(), low=20.0, high=250.0
                )

        row = read(TODAY - timedelta(days=365))
        assert row is not None
        self.assertEqual(row["max_hr"], 191.0)
        self.assertEqual(row["local_date"], (TODAY - timedelta(days=20)).isoformat())
        self.assertIsNone(read(TODAY - timedelta(days=2)))

    def test_quality_ranks_before_the_series_within_a_session(self) -> None:
        # Fable review of #3279: a method-3 `low` record must not beat a method-2
        # `good` one of the same session; among equal quality the series wins.
        window = DfaWindow(
            start_s=0.0, heart_rate_bpm=150.0, alpha1=0.8, artifact_share=0.0
        )
        for quality, expected in ((LOW, 151.0), (GOOD, 149.0)):
            with self.subTest(quality=quality):
                open_database(self)
                shared = add_run(self, 4, replace(result(151.0, GOOD, 60), method=2))
                add_run(
                    self,
                    4,
                    replace(result(149.0, quality, 20), method=3, series=(window,)),
                    provider="wahoo",
                    session=shared,
                )
                with self.manager.reader() as db:
                    rows = first_threshold_results(
                        db,
                        since=TODAY - timedelta(days=59),
                        until=TODAY,
                        sport=Sport.RUN,
                    )
                self.assertEqual([row.result.estimate_bpm for row in rows], [expected])

    def test_a_series_only_record_leaves_the_session_its_method_2_estimate(
        self,
    ) -> None:
        window = DfaWindow(
            start_s=0.0, heart_rate_bpm=171.0, alpha1=0.48, artifact_share=0.0
        )
        shared = add_run(self, 4, replace(result(150.0, GOOD, 40), method=2))
        add_run(
            self,
            4,
            ActivityDfaAlpha1(
                estimate_bpm=None,
                windows=1,
                artifact_share=0.0,
                quality=None,
                method=3,
                series=(window,),
            ),
            provider="wahoo",
            session=shared,
        )
        with self.manager.reader() as db:
            rows = first_threshold_results(
                db, since=TODAY - timedelta(days=59), until=TODAY, sport=Sport.RUN
            )
        self.assertEqual(
            [(row.result.estimate_bpm, row.result.method) for row in rows], [(150.0, 2)]
        )

    def test_reader_returns_one_estimate_per_live_run_session_in_the_range(
        self,
    ) -> None:
        add_run(self, 3, result(150.0))
        shared = add_run(self, 8, result(151.0, LOW, 12))
        # The better record of the same session wins.
        add_run(self, 8, result(152.0, GOOD, 30), provider="wahoo", session=shared)
        add_run(self, 12, result(160.0), sport="ride")
        add_run(self, 70, result(140.0))
        add_run(self, 20, None)
        with self.manager.reader() as db:
            rows = first_threshold_results(
                db, since=TODAY - timedelta(days=59), until=TODAY, sport=Sport.RUN
            )
        self.assertEqual(
            [(row.day, row.result.estimate_bpm) for row in rows],
            [
                (TODAY - timedelta(days=8), 152.0),
                (TODAY - timedelta(days=3), 150.0),
            ],
        )

    def test_a_merged_duplicate_session_counts_once(self) -> None:
        primary = add_run(self, 5, result(150.0, GOOD, 30))
        duplicate = add_run(self, 5, result(151.0, GOOD, 31), provider="wahoo")
        with self.manager.unit_of_work() as db:
            db.execute(
                "UPDATE activity_sessions SET state = 'duplicate', merged_into = ? "
                "WHERE activity_id = ?",
                (primary, duplicate),
            )
        with self.manager.reader() as db:
            rows = first_threshold_results(
                db, since=TODAY - timedelta(days=59), until=TODAY, sport=Sport.RUN
            )
        self.assertEqual(
            [(row.session_id, row.result.estimate_bpm) for row in rows],
            [(primary, 151.0)],
        )


def open_service(case: Any) -> None:
    """Give ``case`` a profile service and a first threshold service over its database."""
    case.kv = KeyValueRepository(lambda: STAMP)
    case.lthr = 170.0
    case.profile = ProfileService(
        case.manager,
        ProfileRepository(case.kv),
        case.kv,
        on_location_changed=lambda *args, **kwargs: None,
        mutation_coordinator=threading.RLock(),
        run_lthr_on=lambda _day: case.lthr,
    )
    case.service = FirstThresholdService(
        case.manager, case.profile, lambda: lambda _day: case.lthr, lambda: TODAY
    )


def seed(case: Any, bpms=(150.0, 153.0, 156.0, 153.0)):
    for index, bpm in enumerate(bpms):
        add_run(case, 5 * (index + 1), result(bpm))


def history(case: Any):
    with case.manager.reader() as db:
        return [
            json.loads(row["diff"])
            for row in db.execute(
                "SELECT diff FROM change_history WHERE entity_type = 'profile' "
                "ORDER BY rowid"
            )
        ]


class AdoptFlowTests(unittest.TestCase):
    manager: DatabaseManager
    counter: int
    kv: KeyValueRepository
    lthr: float
    profile: ProfileService
    service: FirstThresholdService

    def setUp(self) -> None:
        open_database(self)
        open_service(self)

    def test_the_profile_read_shows_the_estimate_and_changes_nothing(self) -> None:
        seed(self)
        view = self.service.estimate()
        assert view is not None
        self.assertEqual(
            (view["percent_lthr"], view["bpm"], view["sessions"], view["quality"]),
            (90.0, 153, 4, "low"),
        )
        self.assertEqual(self.profile.get()[FIRST_THRESHOLD_FIELD], "")
        self.assertEqual(history(self), [])

    def test_without_enough_sessions_there_is_nothing_to_show_or_adopt(self) -> None:
        seed(self, (150.0, 153.0))
        self.assertIsNone(self.service.estimate())
        with self.assertRaises(AppError) as raised:
            self.service.adopt({"percent_lthr": 90.0})
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.profile.get()[FIRST_THRESHOLD_FIELD], "")

    def test_adopting_stores_bpm_and_origin_with_a_change_receipt(self) -> None:
        # 90 % of today's threshold HR of 170 bpm is stored as 153 bpm (#3915).
        seed(self)
        outcome = self.service.adopt({"percent_lthr": 90.0})
        stored = self.profile.get()
        self.assertEqual(stored[FIRST_THRESHOLD_FIELD], "153")
        self.assertEqual(stored[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_ESTIMATED)
        self.assertEqual(outcome["profile"][FIRST_THRESHOLD_ORIGIN_FIELD], "estimated")
        (entry,) = history(self)
        self.assertEqual(
            set(entry["fields"]),
            {FIRST_THRESHOLD_FIELD, FIRST_THRESHOLD_ORIGIN_FIELD},
        )
        self.assertEqual(
            entry["fields"][FIRST_THRESHOLD_ORIGIN_FIELD],
            {"before": "", "after": "estimated"},
        )
        self.assertTrue(first_threshold_estimated(stored))

    def test_adopting_the_value_already_stored_writes_nothing(self) -> None:
        seed(self)
        first = self.service.adopt({"percent_lthr": 90.0})
        self.assertTrue(first["adopted"])
        entries = len(history(self))
        again = self.service.adopt({"percent_lthr": 90.0})
        self.assertFalse(again["adopted"])
        self.assertEqual(again["profile"][FIRST_THRESHOLD_FIELD], "153")
        self.assertEqual(again["profile"][FIRST_THRESHOLD_ORIGIN_FIELD], "estimated")
        self.assertEqual(again["first_threshold_estimate"]["percent_lthr"], 90.0)
        self.assertEqual(len(history(self)), entries)
        # The shown value is checked and stored under the profile lock (#3956).
        lock = MagicMock(wraps=self.profile.mutation_coordinator)
        self.profile.mutation_coordinator = lock
        self.service.adopt({"percent_lthr": 90.0})
        lock.__enter__.assert_called()

    def test_a_stale_or_missing_shown_value_is_refused(self) -> None:
        seed(self)
        for payload in ({"percent_lthr": 89.0}, {}, {"percent_lthr": "x"}):
            with self.subTest(payload=payload), self.assertRaises(AppError) as raised:
                self.service.adopt(payload)
            self.assertEqual(raised.exception.status, 409)
        with self.assertRaises(AppError):
            self.service.adopt([])
        self.assertEqual(history(self), [])

    def test_a_measured_value_is_replaced_only_when_the_request_says_so(self) -> None:
        seed(self)
        self.profile.patch({FIRST_THRESHOLD_FIELD: "145"})
        self.assertEqual(
            self.profile.get()[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_MEASURED
        )
        with self.assertRaises(AppError) as raised:
            self.service.adopt({"percent_lthr": 90.0})
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.profile.get()[FIRST_THRESHOLD_FIELD], "145")
        self.service.adopt({"percent_lthr": 90.0, "replace_measured": True})
        self.assertEqual(self.profile.get()[FIRST_THRESHOLD_FIELD], "153")
        # An adopted estimate needs no confirmation to be adopted again.
        add_run(self, 1, result(160.0))
        view = self.service.estimate()
        assert view is not None
        self.service.adopt({"percent_lthr": view["percent_lthr"]})
        stored = self.profile.get()
        self.assertEqual(stored[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_ESTIMATED)
        self.assertEqual(
            stored[FIRST_THRESHOLD_FIELD], f"{round(view['percent_lthr'] * 1.7)}"
        )

    def test_a_good_step_test_lt1_is_adopted_only_on_request(self) -> None:
        # #3833: the step test's 153 bpm is stored as bpm (#3915), as estimated.
        tests: dict[str, dict[str, Any]] = {
            "good": {"lt1": {"status": "ok", "heart_rate_bpm": 153, "quality": "good"}},
            "low": {"lt1": {"status": "ok", "heart_rate_bpm": 153, "quality": "low"}},
            "missing": {"lt1": {"status": "not_reached", "reason": "x"}},
        }
        self.assertEqual(self.profile.get()[FIRST_THRESHOLD_FIELD], "")
        for payload in (
            {"step_test": "low", "heart_rate_bpm": 153},
            {"step_test": "missing", "heart_rate_bpm": 153},
            {"step_test": "good", "heart_rate_bpm": 152},
            {"step_test": "unknown", "heart_rate_bpm": 153},
        ):
            with self.subTest(payload=payload), self.assertRaises(AppError) as raised:
                self.service.adopt(payload, tests.get(str(payload["step_test"])))
            self.assertEqual(raised.exception.status, 409)
        self.assertEqual(history(self), [])
        outcome = self.service.adopt(
            {"step_test": "good", "heart_rate_bpm": 153}, tests["good"]
        )
        self.assertTrue(outcome["adopted"])
        stored = self.profile.get()
        self.assertEqual(stored[FIRST_THRESHOLD_FIELD], "153")
        self.assertEqual(stored[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_ESTIMATED)
        self.assertEqual(len(history(self)), 1)

    def test_a_manual_entry_or_edit_resets_the_origin_to_measured(self) -> None:
        seed(self)
        self.service.adopt({"percent_lthr": 90.0})
        self.profile.patch({FIRST_THRESHOLD_FIELD: "144"})  # stored as entered
        stored = self.profile.get()
        self.assertEqual(stored[FIRST_THRESHOLD_FIELD], "144")
        self.assertEqual(stored[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_MEASURED)
        self.service.adopt({"percent_lthr": 90.0, "replace_measured": True})
        self.profile.patch({FIRST_THRESHOLD_FIELD: "150"})
        self.assertEqual(
            self.profile.get()[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_MEASURED
        )
        self.service.adopt({"percent_lthr": 90.0, "replace_measured": True})
        self.profile.patch({"name": "Ada"})  # other edits keep the origin
        self.assertEqual(
            self.profile.get()[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_ESTIMATED
        )
        self.profile.patch({FIRST_THRESHOLD_FIELD: ""})
        self.assertEqual(self.profile.get()[FIRST_THRESHOLD_ORIGIN_FIELD], "")

    def test_a_full_replacement_without_the_origin_keeps_it(self) -> None:
        seed(self)
        self.service.adopt({"percent_lthr": 90.0})
        replacement = {
            key: value
            for key, value in self.profile.get().items()
            if key != FIRST_THRESHOLD_ORIGIN_FIELD
        }
        self.profile.save({**replacement, "name": "Ada"})
        stored = self.profile.get()
        self.assertEqual(stored["name"], "Ada")
        self.assertEqual(stored[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_ESTIMATED)

    def test_a_profile_stored_before_the_origin_existed_reads_as_measured(self) -> None:
        with self.manager.unit_of_work() as db:
            self.kv.set(db, "profile", json.dumps({FIRST_THRESHOLD_FIELD: "150"}))
        stored = self.profile.get()
        self.assertEqual(stored[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_MEASURED)
        self.assertFalse(first_threshold_estimated(stored))

    def test_a_percent_stored_before_the_bpm_field_reads_in_bpm_until_a_save(
        self,
    ) -> None:
        # #3915: 90 % with the threshold HR of the day the percent was last
        # changed (160 bpm on 2026-10-01) is 144 bpm; the read writes nothing.
        profile = ProfileService(
            self.manager,
            ProfileRepository(self.kv),
            self.kv,
            on_location_changed=lambda *args, **kwargs: None,
            mutation_coordinator=threading.RLock(),
            run_lthr_on=lambda day: 160.0 if day == date(2026, 10, 1) else 170.0,
        )
        legacy = {
            "first_threshold_pct": "90",
            FIRST_THRESHOLD_ORIGIN_FIELD: "estimated",
        }
        with self.manager.unit_of_work() as db:
            self.kv.set(db, "profile", json.dumps(legacy))
        stored = profile.get()
        # Without a change-history entry the threshold HR of today applies.
        self.assertEqual(stored[FIRST_THRESHOLD_FIELD], "153")
        with self.manager.unit_of_work() as db:
            db.execute(
                "INSERT INTO change_history(id, entity_type, entity_id, action, "
                "source, created_at, before_hash, after_hash, diff) "
                "VALUES ('h1', 'profile', 'profile', 'update', 'local', "
                "'2026-10-01T08:00:00+00:00', 'a', 'b', ?)",
                (
                    json.dumps(
                        {
                            "fields": {
                                "first_threshold_pct": {"before": "", "after": "90"}
                            }
                        }
                    ),
                ),
            )
        stored = profile.get()
        self.assertEqual(stored[FIRST_THRESHOLD_FIELD], "144")
        self.assertEqual(stored[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_ESTIMATED)
        self.assertNotIn("first_threshold_pct", stored)
        with self.manager.reader() as db:
            raw = json.loads(self.kv.get(db, "profile") or "{}")
        self.assertEqual(raw, legacy)
        # The next save stores the bpm and drops the percent.
        profile.patch({"name": "Ada"})
        with self.manager.reader() as db:
            raw = json.loads(self.kv.get(db, "profile") or "{}")
        self.assertEqual(raw[FIRST_THRESHOLD_FIELD], "144")
        self.assertNotIn("first_threshold_pct", raw)
        self.assertEqual(raw[FIRST_THRESHOLD_ORIGIN_FIELD], ORIGIN_ESTIMATED)
        # Without any threshold HR the percent gives no value.
        blind = ProfileService(
            self.manager,
            ProfileRepository(self.kv),
            self.kv,
            on_location_changed=lambda *args, **kwargs: None,
            mutation_coordinator=threading.RLock(),
        )
        with self.manager.unit_of_work() as db:
            self.kv.set(db, "profile", json.dumps(legacy))
        self.assertEqual(blind.get()[FIRST_THRESHOLD_FIELD], "")


class LegacyPercentTests(unittest.TestCase):
    """A profile stored before #3915 keeps working until a bpm is written."""

    manager: DatabaseManager
    counter: int
    kv: KeyValueRepository
    lthr: float
    profile: ProfileService
    service: FirstThresholdService

    def setUp(self) -> None:
        open_database(self)
        open_service(self)

    def profile_service(self, run_lthr_on: Any) -> ProfileService:
        return ProfileService(
            self.manager,
            ProfileRepository(self.kv),
            self.kv,
            on_location_changed=lambda *args, **kwargs: None,
            mutation_coordinator=threading.RLock(),
            run_lthr_on=run_lthr_on,
        )

    def store(self, profile: dict[str, Any]) -> None:
        with self.manager.unit_of_work() as db:
            self.kv.set(db, "profile", json.dumps(profile))

    def raw(self) -> dict[str, Any]:
        with self.manager.reader() as db:
            return json.loads(self.kv.get(db, "profile") or "{}")

    def test_the_threshold_lookup_reading_the_profile_never_recurses(self) -> None:
        # The run threshold lookup reads the athlete's local date, which reads
        # the profile again (the real wiring); the inner read sees no LT1.
        inner: list[str] = []

        def run_lthr_on(_day: Any) -> float:
            inner.append(service.get()[FIRST_THRESHOLD_FIELD])
            return 170.0

        service = self.profile_service(run_lthr_on)
        self.store(
            {"first_threshold_pct": "90", FIRST_THRESHOLD_ORIGIN_FIELD: "measured"}
        )
        self.assertEqual("153", service.get()[FIRST_THRESHOLD_FIELD])
        self.assertEqual([""], inner)

    def test_without_a_threshold_hr_the_percent_survives_an_unrelated_save(
        self,
    ) -> None:
        service = self.profile_service(lambda _day: None)
        self.store(
            {"first_threshold_pct": "88", FIRST_THRESHOLD_ORIGIN_FIELD: "measured"}
        )
        self.assertEqual("", service.get()[FIRST_THRESHOLD_FIELD])
        service.patch({"name": "Ada"})
        raw = self.raw()
        self.assertEqual(
            ("Ada", "88", "measured"),
            (
                raw["name"],
                raw["first_threshold_pct"],
                raw[FIRST_THRESHOLD_ORIGIN_FIELD],
            ),
        )
        # Once a threshold HR is known the read gives the bpm and a save stores it.
        known = self.profile_service(lambda _day: 170.0)
        self.assertEqual("150", known.get()[FIRST_THRESHOLD_FIELD])
        known.patch({"name": "Ada B."})
        raw = self.raw()
        self.assertEqual("150", raw[FIRST_THRESHOLD_FIELD])
        self.assertNotIn("first_threshold_pct", raw)

    def test_a_save_that_keeps_the_percent_stays_undoable(self) -> None:
        service = self.profile_service(lambda _day: None)
        self.store(
            {"first_threshold_pct": "88", FIRST_THRESHOLD_ORIGIN_FIELD: "measured"}
        )
        service.patch({"name": "Ada"})
        with self.manager.unit_of_work() as db:
            row = db.execute(
                "SELECT after_hash FROM change_history WHERE entity_type='profile' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        current = change_history.audit_projection("profile", audit_view(self.raw()))
        self.assertIn(
            row["after_hash"], change_history.accepted_audit_hashes("profile", current)
        )

    def test_clearing_a_legacy_derived_bpm_drops_the_percent(self) -> None:
        service = self.profile_service(lambda _day: 170.0)
        self.store(
            {"first_threshold_pct": "88", FIRST_THRESHOLD_ORIGIN_FIELD: "measured"}
        )
        self.assertEqual("150", service.get()[FIRST_THRESHOLD_FIELD])
        service.patch({FIRST_THRESHOLD_FIELD: ""})
        self.assertNotIn("first_threshold_pct", self.raw())
        self.assertEqual("", service.get()[FIRST_THRESHOLD_FIELD])

    def test_rows_recorded_before_the_bpm_field_still_match_for_undo(self) -> None:
        stored = {
            "first_threshold_pct": "88",
            FIRST_THRESHOLD_ORIGIN_FIELD: "measured",
            "name": "Ada",
        }
        # How a pre-#3915 save projected this profile (no bpm field yet).
        old = normalize_profile({**stored, FIRST_THRESHOLD_FIELD: ""})
        old.pop(FIRST_THRESHOLD_FIELD)
        old["first_threshold_pct"] = "88"
        old[FIRST_THRESHOLD_ORIGIN_FIELD] = "measured"
        recorded = change_history.audit_hash(
            change_history.audit_projection("profile", old)
        )
        current = change_history.audit_projection("profile", audit_view(stored))
        self.assertIn(
            recorded, change_history.accepted_audit_hashes("profile", current)
        )

    def test_undoing_to_a_state_without_a_percent_clears_the_bpm(self) -> None:
        service = self.profile_service(lambda _day: 170.0)
        service.patch({FIRST_THRESHOLD_FIELD: "150"})
        with self.manager.unit_of_work() as db:
            restored = service.restore_in_transaction(
                db, service.get_from_db(db), {"first_threshold_pct": "", "name": "Ada"}
            )
        self.assertEqual("", restored[FIRST_THRESHOLD_FIELD])

    def test_an_implausible_share_keeps_the_stand_in(self) -> None:
        self.assertEqual(0.9, own_lt1_share(153.0, 170.0))
        for bpm, lthr in (
            (150.0, 148.0),
            (150.0, 150.0),
            (60.0, 170.0),
            (None, 170.0),
            (150.0, None),
        ):
            with self.subTest(bpm=bpm, lthr=lthr):
                self.assertIsNone(own_lt1_share(bpm, lthr))


class ProfileReadTests(unittest.TestCase):
    def read(self, service):
        handler = Mock()
        profile = Mock()
        profile.get.return_value = {"name": "Test Athlete"}
        competitions = Mock()
        competitions.list.return_value = []
        routes = AthleteGetRoutes(
            lambda: Mock(),
            lambda: profile,
            lambda: competitions,
            lambda: Mock(),
            lambda: Mock(),
            Mock(),
            first_threshold_service=service,
        )
        self.assertTrue(dispatch_route(routes, handler, "/api/profile"))
        return handler.send_json.call_args.args[1]

    def test_the_profile_read_carries_the_estimate_or_null(self) -> None:
        estimate = {"percent_lthr": 90.0, "sessions": 4, "quality": "low"}
        service = Mock()
        service.estimate.return_value = estimate
        self.assertEqual(
            self.read(lambda: service)["first_threshold_estimate"], estimate
        )
        service.estimate.return_value = None
        self.assertIsNone(self.read(lambda: service)["first_threshold_estimate"])

    def test_a_failing_estimate_never_fails_the_profile_read(self) -> None:
        service = Mock()
        service.estimate.side_effect = RuntimeError("synthetic failure")
        payload = self.read(lambda: service)
        self.assertIsNone(payload["first_threshold_estimate"])
        self.assertEqual(payload["profile"], {"name": "Test Athlete"})


class RouteTests(unittest.TestCase):
    def test_the_route_adopts_through_the_service_under_the_profile_lock(self) -> None:
        # The service takes the profile lock (#3956), so the route and the
        # Coach share it; the route reads a step test before calling it.
        service = Mock()
        service.adopt.return_value = {"profile": {}, "first_threshold_estimate": None}
        step_test = Mock()
        routes = FirstThresholdPostRoutes(lambda: service, step_test=step_test)
        handler = Mock()
        handler.read_json.return_value = {"percent_lthr": 90.0}
        self.assertTrue(dispatch_route(routes, handler, ADOPT_PATH, method="POST"))
        service.adopt.assert_called_once_with({"percent_lthr": 90.0}, None)
        step_test.assert_not_called()
        handler.send_json.assert_called_once_with(
            200, {"profile": {}, "first_threshold_estimate": None}
        )
        # A step test's detail read (#3833) runs before the profile lock is taken.
        events: list[str] = []
        def adopt(*_args: Any) -> dict[str, Any]:
            events.append("lock")
            return {}

        service.adopt.side_effect = adopt

        def read(activity_id: str) -> dict[str, str]:
            events.append("read")
            return {"read": activity_id}

        step_test.side_effect = read
        handler.read_json.return_value = {"step_test": "act_1", "heart_rate_bpm": 152}
        service.adopt.reset_mock()
        dispatch_route(routes, handler, ADOPT_PATH, method="POST")
        service.adopt.assert_called_once_with(
            {"step_test": "act_1", "heart_rate_bpm": 152}, {"read": "act_1"}
        )
        self.assertEqual(events, ["read", "lock"])
        self.assertFalse(dispatch_route(routes, handler, "/api/profile", method="POST"))


class CoachVisibilityTests(unittest.TestCase):
    def test_the_coach_sees_the_estimate_with_quality_count_and_source(self) -> None:
        estimate = {
            "percent_lthr": 90.0,
            "bpm": 153,
            "sessions": 4,
            "quality": "low",
            "from": "2026-10-10",
            "to": "2026-11-04",
            "spread_pct": 3.6,
            "source": "Schätzung aus Brustgurt-RR-Intervallen (DFA-α1), kein Laborwert",
        }
        performance = Mock()
        performance.current_performance.return_value = {"ftp": 260}

        def current(read):
            return CoachStructuredContextService(
                Mock(),
                Mock(),
                Mock(),
                Mock(),
                performance,
                first_threshold_estimate=read,
            )._current_performance(None)

        seen = current(lambda: estimate)["first_threshold_estimate"]
        self.assertEqual(seen, estimate)
        self.assertLessEqual({"quality", "sessions", "source"}, set(seen))
        self.assertNotIn("first_threshold_estimate", current(lambda: None))

        def failing():
            raise RuntimeError("synthetic failure")

        self.assertNotIn("first_threshold_estimate", current(failing))

    def test_no_coach_tool_writes_the_first_threshold_or_adopts_the_estimate(
        self,
    ) -> None:
        self.assertLessEqual(
            {FIRST_THRESHOLD_FIELD, FIRST_THRESHOLD_ORIGIN_FIELD},
            COACH_READ_ONLY_FIELDS,
        )
        serialized = json.dumps(
            tool_schemas.build_structured_tool_schemas(
                default_profile=DEFAULT_PROFILE,
                checkin_text_limits={},
                checkin_score_fields=(),
                training_change_limit=5,
                library_bulk_max_entries=5,
                training_plan_statuses=("active",),
            ),
            default=str,
        )
        for name in (
            FIRST_THRESHOLD_FIELD,
            FIRST_THRESHOLD_ORIGIN_FIELD,
            "first_threshold_hr",
            "adopt",
        ):
            self.assertNotIn(f'"{name}"', serialized, name)
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path(tool_schemas.__file__).parent.glob("*.py")
        )
        for forbidden in (
            "set_first_threshold",
            "FirstThresholdService",
            "first_threshold.adopt",
            "ORIGIN_ESTIMATED",
        ):
            self.assertNotIn(forbidden, sources, forbidden)


class RrNeverLeavesTheMapperTests(unittest.TestCase):
    manager: DatabaseManager
    counter: int

    def setUp(self) -> None:
        open_database(self)

    def test_the_rr_series_is_in_no_stored_set_export_or_coach_view(self) -> None:
        marker = 1234.0  # an interval no ramp beat has; found only if it leaked
        series = ramp(0)
        series[40] = marker
        detail = streams(fit_with_rr(series))
        found = detail.dfa_alpha1
        assert found is not None
        with self.manager.unit_of_work() as db:
            add_session(db, "act_1", "garmin", "a1", "2026-11-01")
            add_record(db, "act_1", "garmin", "a1", "2026-11-01", "run")
            replace_detail_set(
                db, detail, mapper_version=6, summary_sha256=None, now=STAMP
            )
        with self.manager.reader() as db:
            rows = [
                dict(row) for row in db.execute("SELECT * FROM activity_detail_sets")
            ]
            channels = [
                row["channel"]
                for row in db.execute("SELECT channel FROM activity_stream_channels")
            ]
            export = [dict(row) for row in activity_detail_store.stream_export_rows(db)]
        stored = json.loads(rows[0]["dfa_alpha1"])
        # Since method 3 (#3273) the window series is kept: per window only its
        # start, mean heart rate, alpha1 and artifact share, never a beat.
        self.assertEqual(
            set(stored),
            {
                "estimate_bpm",
                "windows",
                "artifact_share",
                "quality",
                "method",
                "series",
            },
        )
        self.assertEqual(len(stored["series"]), stored["windows"])
        self.assertTrue(all(len(window) == 4 for window in stored["series"]))
        self.assertEqual(sorted(channels), ["heart_rate_bpm", "time_s"])
        view = estimate_view(
            first_threshold_estimate(
                [SessionThreshold(date(2026, 11, 1), found.estimate_bpm, GOOD)] * 3,
                lthr_of(170.0),
                TODAY,
            ),
            170.0,
        )
        for text in (
            repr(detail),
            json.dumps(rows, default=str),
            json.dumps(export, default=str),
            json.dumps(view, default=str),
        ):
            self.assertIsNone(re.search(r"\b1234(\.0)?\b", text), text[:80])


if __name__ == "__main__":
    unittest.main()
