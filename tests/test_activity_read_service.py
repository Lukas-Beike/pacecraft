import copy
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from typing import Any, cast
from unittest.mock import patch

from activity_row_support import (
    FixedActivityRows,
    SnapshotActivityRows,
    canonical_source,
    summaries_of_rows,
)
from canonical_detail_support import (
    NOW,
    ZONE,
    add_session_record,
    open_manager,
    store_garmin_streams,
)
from test_activity_routes import loop, store_track

from backend.activities.activity_reference import ActivityReferences
from backend.canonical.activity_summary import stored_activity_key
from backend.canonical.vocabulary import ProviderId
from backend.db.repositories import KeyValueRepository
from backend.errors import AppError
from backend.performance.activity_read_service import (
    ActivityAnalysisReadService,
    _provider_training_effect,
)
from backend.performance.canonical_inputs import CanonicalPerformanceSource
from backend.performance.freshness import NO_GARMIN_SYNC_STATUS
from backend.sync.activity_routes import ActivityRoutesStage
from backend.sync.activity_weather import ActivityWeatherStage
from backend.sync.canonical_table_reader import CanonicalTableReader

LEGACY_KEYS = {
    "activity",
    "session_analysis",
    "activity_id",
    "local_activity_id",
    "snapshot_synced_at",
}


class _Manager:
    @contextmanager
    def unit_of_work(self):
        yield object()

    def reader(self):
        return self.unit_of_work()

    read_snapshot = reader


class _Snapshots:
    def __init__(self, snapshot):
        self.payload = json.dumps(snapshot)

    def latest_payload(self, _db):
        return self.payload

    def latest_snapshot(self, _db):
        return json.loads(self.payload)


class _Feedback:
    def __init__(self):
        self.rows = [{"activity_id": "act_new", "notes": "good"}]

    def list(self, _limit=100):
        return self.rows

    def for_activity_ids(self, activity_ids, **_options):
        return [
            row
            for key in activity_ids
            for row in self.rows
            if row["activity_id"] == key
        ]

    def attach_to_activities(self, activities):
        by_id = {row["activity_id"]: row for row in self.rows}
        return [
            {
                **item,
                **(
                    {"activity_feedback": by_id[stored_activity_key(item)]}
                    if stored_activity_key(item) in by_id
                    else {}
                ),
            }
            for item in activities
        ]


class _NoObservations:
    def observations(self, _db, _kind, *, start=None, end=None, sport=None):
        return ()


class _DatedRows(FixedActivityRows):
    """Raw activity rows that honour the start bound and record each read."""

    def __init__(self, rows):
        self.rows = rows
        self.starts = []
        self.units = []

    def raw_rows(self, db, *, start=None, end=None, limit=None):
        return tuple(self.rows)

    def activity_summaries(self, db, *, start=None, end=None, limit=None):
        self.starts.append(start)
        self.units.append(db)
        return summaries_of_rows(
            row
            for row in self.rows
            if start is None or row["start_date_local"][:10] >= start.isoformat()
        )


class ActivityReadServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = open_manager(directory.name)
        self.addCleanup(self.manager.close)
        add_session_record(
            self.manager,
            ProviderId.GARMIN,
            "new",
            "act_new",
            sport="run",
            name="Morning run",
        )
        add_session_record(
            self.manager,
            ProviderId.GARMIN,
            "old",
            "act_old",
            sport="run",
            name="Winter run",
        )

    def detail_service(self, snapshot, **kwargs):
        """Serve details from the canonical sessions stored in ``setUp``."""
        rows = SnapshotActivityRows(_Snapshots(snapshot))
        kwargs.setdefault("canonical", canonical_source(rows, lambda: self.manager))
        return ActivityAnalysisReadService(
            self.manager,
            rows,
            _Feedback(),
            references=ActivityReferences(self.manager),
            session_records=CanonicalTableReader().session_activities,
            **kwargs,
        )

    def service(self, fetched_at=None):
        snapshot = {
            "synced_at": "2026-09-20T08:00:00+00:00",
            "athlete": {},
            "recent_wellness": [],
            "recent_activities": [
                {"id": "old", "start_date_local": "2026-09-18T08:00:00", "type": "Run"},
                {"id": "new", "start_date_local": "2026-09-20T08:00:00", "type": "Run"},
                "invalid",
            ],
            "raw_provider_data": {
                "activities": [
                    {
                        "id": "old",
                        "type": "Run",
                        "start_date_local": "2026-09-18T08:00:00",
                    },
                    {
                        "id": "new",
                        "name": "Morning run",
                        "type": "Run",
                        "start_date_local": "2026-09-20T08:00:00",
                    },
                ]
            },
        }
        # The fixture snapshot the rows below are served from.
        self.snapshot = snapshot
        rows = SnapshotActivityRows(_Snapshots(snapshot))
        return ActivityAnalysisReadService(
            _Manager(),
            rows,
            _Feedback(),
            canonical=canonical_source(rows, _Manager, fetched_at=fetched_at),
        )

    def test_page_filters_orders_attaches_feedback_and_uses_cursor(self):
        service = self.service()
        page = service.page(limit=1, days=3, today=date(2026, 9, 20))
        self.assertEqual(["act_new"], [item["id"] for item in page["activities"]])
        self.assertEqual("good", page["activities"][0]["activity_feedback"]["notes"])
        second = service.page(page["next_cursor"], 1, 3, today=date(2026, 9, 20))
        self.assertEqual(["act_old"], [item["id"] for item in second["activities"]])
        self.assertIsNone(second["next_cursor"])

    def test_page_clamps_oversized_days_instead_of_overflowing(self):
        service = self.service()
        for days in ("999999999", 10**30, "99999999999999999999"):
            page = service.page(days=days, today=date(2026, 9, 20))
            self.assertEqual(3650, page["days"])
            self.assertEqual(
                ["act_new", "act_old"], [item["id"] for item in page["activities"]]
            )
        self.assertEqual(1, service.page(days=0, today=date(2026, 9, 20))["days"])

    def test_recent_filters_by_explicit_today_without_mutating_snapshot(self):
        service = self.service()
        before = copy.deepcopy(self.snapshot)
        result = service.recent(days=1, today=date(2026, 9, 20))
        self.assertEqual(["act_new"], [item["id"] for item in result["activities"]])
        self.assertEqual(before, self.snapshot)

    def test_detail_is_bounded_and_includes_feedback(self):
        self.service()
        snapshot = self.snapshot
        result = self.detail_service(snapshot).detail(
            "new",
            garmin_status=NO_GARMIN_SYNC_STATUS,
            profile={},
            today=date(2026, 9, 20),
        )
        self.assertTrue(result["ok"])
        self.assertEqual("activity_detail.v1", result["contract"])
        self.assertEqual("act_new", result["session"]["activity_id"])
        self.assertEqual("Morning run", result["session"]["name"])
        self.assertEqual("good", result["activity_feedback"]["notes"])
        self.assertEqual(
            "bounded sanitized activity_detail.v1 document of exactly one local "
            "session; providers: Garmin Connect; recorded by: garmin",
            result["data_scope"],
        )
        self.assertFalse(LEGACY_KEYS & set(result))
        # #2831: a session without provider training-effect values has no section.
        self.assertNotIn("training_effect", result)
        # #3038: pace zone times come only from a summary that has them.
        self.assertNotIn("pace_zone_times", result)
        summary = {
            "id": "act_new",
            "pace_zone_times": [{"id": "Z2", "secs": 1200}],
            "sources": {"pace_zone_times": "Lokale Zonen"},
        }
        with patch(
            "backend.performance.activity_read_service._find_summary",
            return_value=summary,
        ):
            paced = self.detail_service(snapshot).detail(
                "new",
                garmin_status=NO_GARMIN_SYNC_STATUS,
                profile={},
                today=date(2026, 9, 20),
            )
        self.assertEqual(
            {"zones": [{"id": "Z2", "secs": 1200}], "source": "Lokale Zonen"},
            paced["pace_zone_times"],
        )
        self.assertEqual(
            result,
            self.detail_service(snapshot).detail(
                "act_new",
                garmin_status=NO_GARMIN_SYNC_STATUS,
                profile={},
                today=date(2026, 9, 20),
            ),
        )

    def test_the_detail_never_changes_the_kept_summaries(self):
        # The kept summaries are shared by every reader (#3043): a detail that
        # takes its summary from them leaves them as they were.
        self.service()
        shared = (
            {
                "id": "act_new",
                "record_ids": {"garmin": "new"},
                "purpose": "race",
                "aerobic_training_effect": 3.7,
                "training_effect_label": "VO2MAX",
                "pace_zone_times": [{"id": "Z2", "secs": 1200}],
                "sources": {
                    "aerobic_training_effect": "Garmin Connect",
                    "pace_zone_times": "Lokale Zonen",
                },
            },
        )
        before = copy.deepcopy(shared)
        result = self.detail_service(
            self.snapshot, session_summaries=lambda: shared
        ).detail(
            "new",
            garmin_status=NO_GARMIN_SYNC_STATUS,
            profile={},
            today=date(2026, 9, 20),
            validation=False,
        )
        self.assertEqual(3.7, result["training_effect"]["aerobic_training_effect"])
        self.assertEqual(before, shared)

    def test_detail_carries_the_providers_own_training_effect(self):
        # #2831: one record's values with its label, an effect of 0.0 included.
        summary = {
            "id": "act_1",
            "aerobic_training_effect": 3.7,
            "anaerobic_training_effect": 0.0,
            "training_effect_label": "VO2MAX",
            "training_effect_load": 120.0,
            "sources": {"aerobic_training_effect": "Garmin Connect"},
        }
        self.assertEqual(
            {
                "aerobic_training_effect": 3.7,
                "anaerobic_training_effect": 0.0,
                "training_effect_label": "VO2MAX",
                "training_effect_load": 120.0,
                "source": "Garmin Connect",
            },
            _provider_training_effect(cast(Any, summary)),
        )
        self.assertIsNone(_provider_training_effect(None))
        self.assertIsNone(
            _provider_training_effect(cast(Any, {"id": "act_1", "sources": {}}))
        )

    def test_detail_reads_an_activity_older_than_the_performance_window(self):
        snapshot = {"synced_at": "2026-09-20T08:00:00+00:00", "athlete": {}}
        rows = _DatedRows(
            [
                {"id": "new", "type": "Run", "start_date_local": "2026-09-20T08:00:00"},
                {
                    "id": "old",
                    "name": "Winter run",
                    "type": "Run",
                    "start_date_local": "2024-01-10T08:00:00",
                    "average_speed": 3.1,
                },
            ]
        )
        service = self.detail_service(
            snapshot,
            canonical=CanonicalPerformanceSource(
                database_manager=_Manager,
                observation_reader=_NoObservations,
                activity_summaries=lambda: rows,
            ),
        )
        today = date(2026, 9, 20)
        recent = service.detail(
            "new", garmin_status=NO_GARMIN_SYNC_STATUS, profile={}, today=today
        )
        self.assertEqual("act_new", recent["session"]["activity_id"])
        self.assertEqual([date(2025, 9, 21)], rows.starts)
        old = service.detail(
            "old", garmin_status=NO_GARMIN_SYNC_STATUS, profile={}, today=today
        )
        # The Intervals.icu summary row is never read for scalars (#1685).
        self.assertNotIn("avg_speed_mps", old["metrics"])
        self.assertTrue(old["activity_validation"]["available"])
        self.assertEqual([date(2025, 9, 21), date(2025, 9, 21), None], rows.starts)
        # The full-history lookup shares the read snapshot of the windowed read.
        self.assertIs(rows.units[1], rows.units[2])
        with self.assertRaises(AppError) as missing:
            service.detail(
                "missing", garmin_status=NO_GARMIN_SYNC_STATUS, profile={}, today=today
            )
        self.assertEqual("activity_details_not_found", missing.exception.reason)
        # An unknown session is rejected before any summary row is read.
        self.assertEqual(3, len(rows.starts))

    def test_detail_rejects_invalid_or_missing_activity(self):
        self.service()
        service = self.detail_service(self.snapshot)
        with self.assertRaises(AppError) as invalid:
            service.detail(
                "",
                garmin_status=NO_GARMIN_SYNC_STATUS,
                profile={},
                today=date(2026, 9, 20),
            )
        self.assertEqual("invalid_activity_request", invalid.exception.reason)
        self.assertEqual(400, invalid.exception.status)
        for missing_id in ("missing", "act_missing"):
            with self.assertRaises(AppError) as missing:
                service.detail(
                    missing_id,
                    garmin_status=NO_GARMIN_SYNC_STATUS,
                    profile={},
                    today=date(2026, 9, 20),
                )
            self.assertEqual("activity_details_not_found", missing.exception.reason)
            self.assertEqual(404, missing.exception.status)
            self.assertEqual(
                "Diese Aktivität ist lokal nicht vorhanden.", str(missing.exception)
            )

    def test_activity_sync_time_comes_from_canonical_coverage(self):
        # The snapshot carries other times; only the stored coverage is consulted.
        fetched_at = "2026-10-19T12:00:00+00:00"
        service = self.service(fetched_at=fetched_at)

        page = service.page(today=date(2026, 9, 20))
        recent = service.recent(today=date(2026, 9, 20))

        self.assertEqual(fetched_at, page["snapshot_synced_at"])
        self.assertEqual(fetched_at, recent["snapshot_synced_at"])
        self.assertNotEqual("2026-09-20T08:00:00+00:00", page["snapshot_synced_at"])

    def test_activity_sync_time_is_unknown_without_stored_coverage(self):
        service = self.service()

        self.assertIsNone(service.page(today=date(2026, 9, 20))["snapshot_synced_at"])
        self.assertIsNone(service.recent(today=date(2026, 9, 20))["snapshot_synced_at"])

    def test_detail_reads_no_provider_summary_row_without_snapshot_activities(self):
        snapshot = {"synced_at": "2026-09-20T08:00:00+00:00", "athlete": {}}
        rows = _DatedRows(
            [
                {
                    "id": "new",
                    "type": "Run",
                    "start_date_local": "2026-09-20T08:00:00",
                    "average_speed": 3.3,
                }
            ]
        )
        service = self.detail_service(
            snapshot, canonical=canonical_source(rows, _Manager)
        )

        result = service.detail(
            "new",
            garmin_status=NO_GARMIN_SYNC_STATUS,
            profile={},
            today=date(2026, 9, 20),
        )

        self.assertEqual("act_new", result["session"]["activity_id"])
        self.assertNotIn("avg_speed_mps", result["metrics"])

    def test_detail_carries_the_stored_weather_block_with_its_heat_note(self):
        snapshot = {"synced_at": "2026-09-20T08:00:00+00:00", "athlete": {}}
        stage = ActivityWeatherStage(
            key_values=KeyValueRepository(lambda: NOW),
            zone_provider=lambda: ZONE,
            clock=lambda: NOW,
            fetch=lambda *_args: {
                "hourly": {
                    "time": ["2026-09-10T05:00", "2026-09-10T06:00"],
                    "temperature_2m": [24.0, 24.0],
                    "apparent_temperature": [25.0, 25.0],
                }
            },
            home_location=lambda _db: (52.52, 13.41),
        )
        stage.prefetch(lambda: self.manager)
        with self.manager.unit_of_work() as db:
            stage.run_batch(db)
        service = self.detail_service(snapshot)

        result = service.detail(
            "new",
            garmin_status=NO_GARMIN_SYNC_STATUS,
            profile={},
            today=date(2026, 9, 20),
        )

        weather = result["weather"]
        self.assertEqual(25.0, weather["apparent_temperature_c"])
        self.assertEqual("home", weather["location_kind"])
        self.assertIn("Hitze", weather["heat_note"])
        self.assertNotIn("52.5", json.dumps(weather))

    def test_a_session_shows_only_its_weather_temperature_never_the_watch(self):
        # #3036: the watch's temperature sensor reads body-warmed values.
        snapshot = {"synced_at": "2026-09-20T08:00:00+00:00", "athlete": {}}
        store_garmin_streams(
            self.manager,
            "new",
            {
                "time": [0, 1, 2, 3],
                "heartrate": [120, 121, 122, 123],
                "temperature": [31.0, 32.0, 33.0, 34.0],
            },
        )
        with self.manager.unit_of_work() as db:
            db.execute(
                "UPDATE activity_records SET details = ? WHERE provider_activity_id = 'new'",
                (
                    json.dumps(
                        {"avg_temp_c": 32.0, "min_temp_c": 31.0, "max_temp_c": 34.0}
                    ),
                ),
            )
        service = self.detail_service(snapshot)

        def detail():
            result = service.detail(
                "new",
                garmin_status=NO_GARMIN_SYNC_STATUS,
                profile={},
                today=date(2026, 9, 20),
            )
            self.assertIn("heart_rate_bpm", result["streams"]["channels"])
            self.assertNotIn("temperature_c", result["streams"]["channels"])
            self.assertFalse(
                {"avg_temp_c", "min_temp_c", "max_temp_c"} & set(result["metrics"])
            )
            for provider in result["detail_data"]["providers"]:
                self.assertNotIn("temperature_c", provider.get("coverage") or {})
            return result

        # Without weather the temperature is unknown.
        self.assertIsNone(detail().get("weather"))
        stage = ActivityWeatherStage(
            key_values=KeyValueRepository(lambda: NOW),
            zone_provider=lambda: ZONE,
            clock=lambda: NOW,
            fetch=lambda *_args: {
                "hourly": {
                    "time": ["2026-09-10T05:00", "2026-09-10T06:00"],
                    "temperature_2m": [14.0, 14.0],
                    "apparent_temperature": [13.0, 13.0],
                }
            },
            home_location=lambda _db: (52.52, 13.41),
        )
        stage.prefetch(lambda: self.manager)
        with self.manager.unit_of_work() as db:
            stage.run_batch(db)
        weather = detail()["weather"]
        self.assertEqual(
            (14.0, "Open-Meteo Modell, Wohnort"),
            (weather["temperature_c"], weather["source"]),
        )

    def test_detail_carries_only_the_route_label_and_repeat_count(self):
        snapshot = {"synced_at": "2026-09-20T08:00:00+00:00", "athlete": {}}
        add_session_record(
            self.manager,
            ProviderId.GARMIN,
            "gn",
            "act_new",
            sport="run",
            distance_m=5026.5,
        )
        start = (51.98765, 7.65432)
        store_track(self.manager, "gn", loop(start, seed=1))
        stage = ActivityRoutesStage(
            key_values=KeyValueRepository(lambda: NOW), zone_provider=lambda: ZONE
        )
        with self.manager.unit_of_work() as db:
            stage.run_batch(db)

        result = self.detail_service(snapshot).detail(
            "new",
            garmin_status=NO_GARMIN_SYNC_STATUS,
            profile={},
            today=date(2026, 9, 20),
        )

        self.assertEqual(
            {
                "label": "Strecke 5,0 km",
                "repeat_index": 1,
                "repeat_count": 1,
                "route_id": "r1",
            },
            result["route"],
        )
        text = json.dumps(result)
        for needle in ("51.98", "7.65", "latitude", "longitude", "fingerprint"):
            self.assertNotIn(needle, text)
        # With the session summaries the block lists the visits (#3763).
        summary = {
            "id": "act_new",
            "started_at_local": "2026-09-20T08:00:00",
            "sport": "run",
            "moving_duration_s": 1500.0,
            "avg_hr": 148.0,
        }
        listed = self.detail_service(
            snapshot, session_summaries=lambda: [summary]
        ).detail(
            "new",
            garmin_status=NO_GARMIN_SYNC_STATUS,
            profile={},
            today=date(2026, 9, 20),
            validation=False,
        )
        visits = listed["route"]["visits"]
        self.assertEqual(
            [("act_new", "2026-09-20", 1500.0, 148.0, None)],
            [
                (
                    v["activity_id"],
                    v["date"],
                    v["moving_duration_s"],
                    v["avg_hr"],
                    v["temperature_c"],
                )
                for v in visits
            ],
        )
        for needle in ("51.98", "7.65", "latitude", "longitude", "fingerprint"):
            self.assertNotIn(needle, json.dumps(listed))

    def test_detail_service_requires_a_canonical_source(self):
        with self.assertRaises(TypeError):
            ActivityAnalysisReadService(  # type: ignore[call-arg]
                _Manager(),
                SnapshotActivityRows(_Snapshots({})),
                _Feedback(),
            )


if __name__ == "__main__":
    unittest.main()
