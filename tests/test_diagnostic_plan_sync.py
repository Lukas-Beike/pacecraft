"""Plan-sync diagnostics expose job outcomes and unsynced units."""

from __future__ import annotations

import json
import sqlite3
import unittest

from backend.diagnostics.plan_sync import (
    summarize_garmin_removal,
    summarize_garmin_strength_push,
    summarize_plan_sync,
)
from backend.planning.push_state import garmin_fingerprint


class PlanSyncDiagnosticsTests(unittest.TestCase):
    def test_reports_failed_items_and_unsynced_units_without_error_text(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE sync_jobs (id, provider, type, status, payload,
                requested_by, attempts, available_at, started_at, finished_at,
                error_class, created_at, updated_at);
            CREATE TABLE sync_job_items (id, job_id, item_key, operation,
                remote_id, status, attempts, error_class, error_detail,
                created_at, updated_at);
            CREATE TABLE planned_units (local_id, payload, sync_state,
                sync_error, tombstone);
            """
        )
        db.execute(
            "INSERT INTO sync_jobs VALUES ('j1','intervals','plan_push','failed',?,"
            "'coach',1,NULL,NULL,'t2','conflict','t1','t2')",
            (json.dumps({"entries": [{"a": 1}, {"a": 2}]}),),
        )
        db.execute(
            "INSERT INTO sync_job_items VALUES ('j1-0','j1','a','plan_push',NULL,"
            "'failed',1,'conflict','secret-token boom','t1','t1')"
        )
        db.execute(
            "INSERT INTO planned_units VALUES ('u1',?,'sync_error','secret-token',0)",
            (json.dumps({"date": "2026-11-10", "sport": "VirtualRide"}),),
        )
        db.execute(
            "INSERT INTO planned_units VALUES ('u2',?,'synced',NULL,0)",
            (json.dumps({"date": "2026-11-11"}),),
        )
        # Runs, strength and outdoor rides are no Intervals.icu backlog (#2419).
        for local_id, sport in (
            ("u3", "Run"),
            ("u4", "WeightTraining"),
            ("u5", "Ride"),
        ):
            db.execute(
                "INSERT INTO planned_units VALUES (?,?,'local',NULL,0)",
                (local_id, json.dumps({"date": "2026-11-12", "sport": sport})),
            )

        report = summarize_plan_sync(db)

        job = report["recent_jobs"][0]
        self.assertEqual((job["status"], job["entries"]), ("failed", 2))
        self.assertEqual(
            job["items"], [{"status": "failed", "error_class": "conflict"}]
        )
        self.assertNotIn("secret-token", json.dumps(report))
        self.assertEqual(report["unsynced_total"], 1)
        self.assertEqual(
            report["unsynced_units"],
            [
                {
                    "local_id": "u1",
                    "date": "2026-11-10",
                    "sync_state": "sync_error",
                    "has_error": True,
                }
            ],
        )
        self.assertNotIn("push_counts", report)

        # #2878: every push of the window per provider, settled items included,
        # however many newer jobs of other providers exist; older finished ones
        # are out, an old job still running stays in.
        db.execute("UPDATE sync_jobs SET created_at='2026-11-01T08:00:00+00:00'")
        for job_id, status, error, created in (
            ("g1", "partial", "plan_push_error", "2026-11-10T08:00:00+00:00"),
            ("g0", "failed", "HTTPError", "2026-11-01T08:00:00+00:00"),
            ("g2", "running", None, "2026-10-20T08:00:00+00:00"),
        ):
            db.execute(
                "INSERT INTO sync_jobs VALUES (?,'garmin','plan_push',?,'{}','coach',"
                "1,NULL,NULL,NULL,?,?,?)",
                (job_id, status, error, created, created),
            )
        for item_id, status, error in (
            ("g1-0", "completed", None),
            ("g1-1", "failed", "HTTPError"),
            ("g0-0", "failed", "HTTPError"),
        ):
            db.execute(
                "INSERT INTO sync_job_items VALUES (?,?,'a','plan_push',NULL,?,1,"
                "?,NULL,'t','t')",
                (item_id, item_id[:2], status, error),
            )
        windowed = summarize_plan_sync(db, "2026-11-07T00:00:00+00:00")
        self.assertEqual(
            windowed["push_counts"],
            {
                "garmin": {
                    "jobs": {"partial": 1, "running": 1},
                    "items": {"completed": 1, "failed": 1},
                    "job_errors": {"plan_push_error": 1},
                    "item_errors": {"HTTPError": 1},
                }
            },
        )
        self.assertNotIn("secret-token", json.dumps(windowed["push_counts"]))

    def test_a_unit_with_an_invalid_payload_has_no_date_and_does_not_fail(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE sync_jobs (id, provider, type, status, payload,
                requested_by, attempts, available_at, started_at, finished_at,
                error_class, created_at, updated_at);
            CREATE TABLE sync_job_items (id, job_id, item_key, operation,
                remote_id, status, attempts, error_class, error_detail,
                created_at, updated_at);
            CREATE TABLE planned_units (local_id, payload, sync_state,
                sync_error, tombstone);
            """
        )
        db.execute(
            "INSERT INTO planned_units VALUES ('bad','{not json','pending',NULL,0)"
        )

        report = summarize_plan_sync(db)

        self.assertEqual(report["unsynced_total"], 1)
        self.assertIsNone(report["unsynced_units"][0]["date"])


class GarminRemovalDiagnosticsTests(unittest.TestCase):
    """#2408: the past-workout removal's state is visible as counts only."""

    def database(self, state: dict | None = None) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE kv (key, value, updated_at)")
        if state is not None:
            db.execute(
                "INSERT INTO kv VALUES ('garmin_app_workout_cleanup', ?, 't')",
                (json.dumps(state),),
            )
        return db

    def test_reports_counts_outcome_and_last_run_without_ids(self):
        db = self.database(
            {
                "done": True,
                "checked": ["701", "702"],
                "removed": 3,
                "last_run_at": "2026-11-12T16:23:00+00:00",
                "last_outcome": "failed",
                "pending": 1,
            }
        )

        report = summarize_garmin_removal(db)

        self.assertEqual(
            {
                "one_time_done": True,
                "checked": 2,
                "removed": 3,
                "last_run_at": "2026-11-12T16:23:00+00:00",
                "last_outcome": "failed",
                "past_links_pending": 1,
            },
            report,
        )
        self.assertNotIn("701", json.dumps(report))

    def test_a_removal_that_never_ran_or_unknown_values_report_nothing(self):
        for db in (
            self.database(),
            self.database({"removed": True, "last_outcome": "Garmin text"}),
        ):
            self.assertEqual(
                {
                    "one_time_done": False,
                    "checked": 0,
                    "removed": 0,
                    "last_run_at": None,
                    "last_outcome": None,
                    "past_links_pending": 0,
                },
                summarize_garmin_removal(db),
            )

    def test_strength_push_counts_garmin_steps_and_intervals_only_units(self):
        # #2850: upcoming units the app sent to Garmin by their exercise steps and
        # loads, and units that reach the watch only through Intervals.icu (#1802).
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(
            "CREATE TABLE planned_units (local_id, payload, tombstone, updated_at);"
            "CREATE TABLE workout_library (payload, updated_at);"
            "CREATE TABLE remote_links (entity_kind, local_id, provider, remote_id,"
            " baseline_hash);"
        )
        row = {"name": "Rudern", "sets": 3, "reps": 10}
        loaded = {"name": "Bankdrücken", "sets": 3, "reps": 8, "kg": 60}
        warm = {"name": "Kniebeuge", "sets": 3, "reps": 5, "pct_1rm": 80,
                "warmup": {"sets": 1, "reps": 10, "kg": 20}}  # fmt: skip
        units = (
            ("g1", [row, loaded], {}, ("garmin", "intervals"), True),
            ("g2", [row], {}, ("garmin",), True),
            ("g3", [warm], {}, ("garmin",), True),  # the warm-up sends a weight
            ("c1", [row, loaded], {}, ("garmin",), False),  # changed since push
            ("p1", [row], {"date": "2026-11-01"}, ("garmin",), True),  # past
            ("i1", None, {}, ("intervals",), False),
            ("i2", [row], {}, ("intervals",), False),
            ("l1", None, {"source": "intervals"}, ("intervals",), False),  # import
            ("r1", None, {"sport": "Run"}, ("garmin",), True),  # not strength
            ("d1", [row], {"local_deleted": True}, ("garmin",), True),
            ("n1", [row], {}, (), False),  # never sent
        )
        for local_id, structure, extra, providers, pushed in units:
            unit = {"date": "2026-11-20", "sport": "WeightTraining", **extra}
            if structure:
                unit["strength_structure"] = structure
            db.execute(
                "INSERT INTO planned_units VALUES (?,?,0,'t')",
                (local_id, json.dumps(unit)),
            )
            # A push stores the fingerprint of what it sent.
            baseline = garmin_fingerprint(unit) if pushed else "sent-earlier"
            for provider in providers:
                db.execute(
                    "INSERT INTO remote_links VALUES ('planned_unit',?,?,?,?)",
                    (local_id, provider, f"remote-{local_id}", baseline),
                )
        # No remote copy and a tombstone count as not sent.
        for local_id, payload, tombstone, remote_id in (
            ("u1", '{"sport":"WeightTraining","date":"2026-11-20"}', 0, None),
            ("t1", '{"sport":"WeightTraining","date":"2026-11-20"}', 1, "x"),
        ):
            db.execute(
                "INSERT INTO planned_units VALUES (?,?,?,'t')",
                (local_id, payload, tombstone),
            )
            db.execute(
                "INSERT INTO remote_links VALUES ('planned_unit',?,'garmin',?,NULL)",
                (local_id, remote_id),
            )

        report = summarize_garmin_strength_push(db, "2026-11-14")

        self.assertEqual(
            report,
            {
                "garmin": {
                    "units": 4,
                    "changed_since_push": 1,
                    "without_structure": 0,
                    "one_exercise": 2,
                    "several_exercises": 1,
                    "with_kg": 2,
                },
                "intervals": {"units": 2, "without_structure": 1},
            },
        )
        self.assertNotIn("Rudern", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
