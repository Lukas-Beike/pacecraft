import unittest

from backend.performance.activity_records import activity_records, session_totals


def row(activity_id, sport, day, environment="outdoor", **totals):
    summary = {**totals, "sources": dict.fromkeys(totals, "Garmin")}
    return {
        "activity_id": activity_id,
        "sport": sport,
        "environment": environment,
        "date": day,
        "observed_at": f"{day}T08:00:00+00:00",
        "name": activity_id,
        "totals": session_totals(summary),
    }


class ActivityRecordsTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row(
                "ride_long",
                "ride",
                "2026-06-14",
                distance_m=182400.0,
                moving_duration_s=20000.0,
                elevation_gain_m=1200.0,
            ),
            # A tie with ride_long's distance: the earlier ride set it first.
            row(
                "ride_tie_early",
                "ride",
                "2026-05-01",
                distance_m=182400.0,
                moving_duration_s=22320.0,
            ),
            row(
                "ride_hilly",
                "ride",
                "2025-08-23",
                distance_m=90000.0,
                moving_duration_s=15000.0,
                elevation_gain_m=2430.0,
            ),
            # Indoor and virtual rides never count (#3826).
            row(
                "ride_indoor",
                "ride",
                "2026-02-02",
                "indoor",
                distance_m=250000.0,
                moving_duration_s=30000.0,
                elevation_gain_m=5000.0,
            ),
            # A run without elevation leaves the elevation record to another run.
            row(
                "run_long",
                "run",
                "2026-03-29",
                distance_m=32100.0,
                moving_duration_s=10680.0,
            ),
            row(
                "run_trail",
                "run",
                "2025-08-09",
                distance_m=18000.0,
                moving_duration_s=8000.0,
                elevation_gain_m=1120.0,
            ),
            row("run_zero", "run", "2025-01-01", distance_m=0.0, elevation_gain_m=None),
        ]

    def test_records_per_sport_and_field(self):
        cases = [
            ("ride", "distance_m", 182400.0, "ride_tie_early"),
            ("ride", "moving_duration_s", 22320.0, "ride_tie_early"),
            ("ride", "elevation_gain_m", 2430.0, "ride_hilly"),
            ("run", "distance_m", 32100.0, "run_long"),
            ("run", "moving_duration_s", 10680.0, "run_long"),
            ("run", "elevation_gain_m", 1120.0, "run_trail"),
        ]
        records = activity_records(self.rows)
        self.assertEqual(
            cases,
            [(r["sport"], r["field"], r["value"], r["activity_id"]) for r in records],
        )
        self.assertEqual({"Garmin"}, {r["source"] for r in records})
        self.assertEqual("2025-08-23", records[2]["date"])

    def test_a_missing_field_stays_unknown(self):
        rides_without_elevation = [
            r
            for r in self.rows
            if r["activity_id"] in ("ride_tie_early", "ride_indoor")
        ]
        records = activity_records(rides_without_elevation)
        self.assertEqual(
            [
                ("ride", "distance_m", "ok"),
                ("ride", "moving_duration_s", "ok"),
                ("ride", "elevation_gain_m", "unknown"),
            ]
            + [
                ("run", field, "unknown")
                for field in ("distance_m", "moving_duration_s", "elevation_gain_m")
            ],
            [(r["sport"], r["field"], r["status"]) for r in records],
        )

    def test_totals_keep_only_positive_numbers(self):
        self.assertEqual(
            {"distance_m": {"value": 5.0, "source": None}},
            session_totals(
                {"distance_m": 5, "moving_duration_s": 0, "elevation_gain_m": True}
            ),
        )


if __name__ == "__main__":
    unittest.main()
