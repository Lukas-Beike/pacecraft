import math
import unittest
from datetime import date

from performance_day_support import (
    garmin_body_from_payload,
    garmin_metrics_from_payload,
)

from backend.performance.body import body_history as _body_history
from backend.performance.canonical_inputs import CanonicalPerformanceInputs
from backend.performance.freshness import GarminSyncStatus

TODAY = date(2026, 10, 6)


def body_history(snapshot, garmin, today, **kwargs):
    """The body history; the stored snapshot is not an input, so it is ignored."""
    del snapshot
    kwargs.setdefault("canonical", garmin_body_from_payload(garmin, today))
    kwargs.setdefault(
        "garmin_ftp",
        garmin_metrics_from_payload(garmin).performance_history.get(
            "cycling_ftp_watts", {}
        ),
    )
    return _body_history(
        GarminSyncStatus.from_state(garmin),
        today,
        **kwargs,
    )


def _series(result, window, metric, source):
    return next(
        item
        for item in result["windows"][window]["metrics"][metric]
        if item["source"] == source
    )


class BodyHistoryTests(unittest.TestCase):
    def test_a_longer_period_reads_one_window_named_after_its_days(self):
        garmin = {"weight": [{"date": "2026-10-04", "weightKg": 72}]}
        result = body_history({}, garmin, TODAY, periods=(365,))
        self.assertEqual(list(result["windows"]), ["365d"])
        self.assertEqual(result["default_window"], "365d")
        window = result["windows"]["365d"]
        self.assertEqual(
            (window["start"], window["end"], window["days"]),
            ("2025-10-07", "2026-10-06", 365),
        )
        self.assertEqual(len(window["metrics"]["weight_kg"][0]["points"]), 365)
        base = body_history({}, garmin, TODAY)
        self.assertEqual(list(base["windows"]), ["28d", "91d"])
        self.assertEqual(base["default_window"], "91d")

    def test_the_change_is_the_last_edge_mean_minus_the_first(self):
        # 28 days ending 2026-10-06: first edge 2026-09-09..15, last edge 2026-09-30..10-06.
        weigh_ins = [
            ("2026-09-09", 74.2),
            ("2026-09-12", 73.9),
            ("2026-09-15", 73.4),
            ("2026-09-20", 99.0),
            ("2026-09-30", 72.8),
            ("2026-10-02", 72.1),
            ("2026-10-06", 72.4),
        ]
        garmin = {"weight": [{"date": day, "weightKg": kg} for day, kg in weigh_ins]}
        result = body_history({}, garmin, TODAY)
        edges = _series(result, "28d", "weight_kg", "Garmin Connect")["edge_means"]
        # Hand-worked: (74.2 + 73.9 + 73.4) / 3 = 73.83; (72.8 + 72.1 + 72.4) / 3 = 72.43.
        self.assertEqual(
            edges,
            {
                "span_days": 7,
                "first_mean": 73.83,
                "last_mean": 72.43,
                "change": -1.4,
                "first_count": 3,
                "last_count": 3,
            },
        )
        # An edge without a weigh-in gives no change.
        sparse = {"weight": [{"date": "2026-09-09", "weightKg": 74.0}]}
        lone = body_history({}, sparse, TODAY)
        self.assertIsNone(
            _series(lone, "28d", "weight_kg", "Garmin Connect")["edge_means"]
        )

    def test_windows_and_gaps_come_from_garmin_alone(self):
        result = body_history(
            {},
            {
                "synced_at": "garmin-sync",
                "weight": [{"date": "2026-10-04", "weightKg": 72}],
                "source_freshness": {"weight": {"fetched_at": "garmin-fetch"}},
            },
            TODAY,
        )
        self.assertEqual(result["windows"]["28d"]["days"], 28)
        self.assertEqual(result["windows"]["91d"]["days"], 91)
        for metric in ("weight_kg", "body_fat_pct"):
            self.assertEqual(
                [
                    item["source"]
                    for item in result["windows"]["28d"]["metrics"][metric]
                ],
                ["Garmin Connect"],
            )
        points = _series(result, "28d", "weight_kg", "Garmin Connect")["points"]
        self.assertEqual(points[-3]["value"], 72)
        self.assertEqual(points[-3]["synced_at"], "garmin-fetch")
        self.assertIsNone(points[-1]["value"])

    def test_ftp_sources_and_weight_age_limit(self):
        # #1727: the Intervals.icu eFTP is not read; W/kg divides Garmin FTP only.
        snapshot = {
            "synced_at": "intervals-sync",
            "recent_wellness": [
                {
                    "id": "2026-10-01",
                    "sportInfo": [{"types": ["Ride"], "mmp_model": {"ftp": 999}}],
                }
            ],
            "recent_activities": [],
        }
        garmin = {
            "weight": [{"date": "2026-09-29", "weightKg": 70}],
            "performance_history": [
                {"date": "2026-10-01", "metrics": {"cycling_ftp_watts": 280}},
                {"date": "2026-10-06", "metrics": {"cycling_ftp_watts": 300}},
            ],
            "source_freshness": {
                "weight": {"fetched_at": "weight-fetch"},
                "cycling_ftp": {"fetched_at": "ftp-fetch"},
            },
        }
        result = body_history(snapshot, garmin, TODAY)
        garmin_series = _series(result, "91d", "cycling_w_per_kg", "Garmin Connect")[
            "points"
        ]
        self.assertAlmostEqual(garmin_series[-1]["value"], 300 / 70, places=3)
        self.assertEqual(garmin_series[-1]["weight_age_days"], 7)
        self.assertEqual(
            [
                item["source"]
                for item in result["windows"]["91d"]["metrics"]["cycling_w_per_kg"]
            ],
            ["Garmin Connect"],
        )
        self.assertEqual(
            _series(result, "91d", "cycling_w_per_kg", "Garmin Connect")[
                "power_method"
            ],
            "FTP",
        )

        too_old = body_history(
            snapshot,
            {
                "weight": [{"date": "2026-09-28", "weightKg": 70}],
                "performance_history": garmin["performance_history"],
            },
            TODAY,
        )
        self.assertIsNone(
            _series(too_old, "91d", "cycling_w_per_kg", "Garmin Connect")["points"][-1][
                "value"
            ]
        )

    def test_historical_ftp_keeps_history_freshness_timestamp(self):
        result = body_history(
            {"recent_activities": []},
            {
                "cycling_ftp_history": [
                    {"calendarDate": "2026-10-05", "functionalThresholdPower": 280}
                ],
                "source_freshness": {
                    "cycling_ftp": {"fetched_at": "current-fetch"},
                    "cycling_ftp_history": {"fetched_at": "history-fetch"},
                },
                "cycling_ftp": 300,
            },
            TODAY,
        )
        points = _series(result, "28d", "cycling_w_per_kg", "Garmin Connect")["points"]
        self.assertEqual(points[-2]["ftp_synced_at"], "history-fetch")

    def test_manual_profile_values_are_not_historical_and_invalid_garmin_values_drop(
        self,
    ):
        result = body_history(
            {"synced_at": "sync", "profile": {"weight_kg": 80, "body_fat_pct": 20}},
            {"weight": [{"date": "2026-10-06", "weightKg": math.inf, "bodyFat": 80}]},
            TODAY,
        )
        self.assertTrue(
            all(
                point["value"] is None
                for point in _series(result, "28d", "weight_kg", "Garmin Connect")[
                    "points"
                ]
            )
        )
        self.assertTrue(
            all(
                point["value"] is None
                for point in _series(result, "28d", "body_fat_pct", "Garmin Connect")[
                    "points"
                ]
            )
        )
        # C-505: W/kg divides only an FTP inside the current value's 50 to 700 W.
        outlier = body_history(
            {},
            {"weight": [{"date": "2026-10-06", "weightKg": 70}]},
            TODAY,
            garmin_ftp={TODAY: 1500.0},
        )
        points = _series(outlier, "28d", "cycling_w_per_kg", "Garmin Connect")["points"]
        self.assertIsNone(points[-1]["value"])

    def test_garmin_daily_record_uses_latest_timestamp_and_converts_pounds(self):
        snapshot = {
            "weight": [
                {
                    "timestampGMT": "2026-10-01T18:00:00Z",
                    "weight": 154.3,
                    "unit": "lb",
                    "bodyFat": 18,
                },
                {
                    "timestampGMT": "2026-10-01T07:00:00Z",
                    "weight": 160,
                    "unit": "lb",
                    "bodyFat": 20,
                },
            ]
        }
        self.assertEqual(
            garmin_body_from_payload(snapshot, TODAY).garmin_weight_kg,
            {date(2026, 10, 1): 69.99},
        )

    def test_weekly_quotient_median_counts_dates_and_chart_history_integration(self):
        from backend.performance.chart_history import analysis_history

        garmin = {
            "weight": [
                {"date": "2026-10-05", "weightKg": 70},
                {"date": "2026-10-06", "weightKg": 70},
            ],
            "performance_history": [
                {"date": "2026-10-05", "metrics": {"cycling_ftp_watts": 280}},
                {"date": "2026-10-06", "metrics": {"cycling_ftp_watts": 300}},
            ],
        }
        canonical = CanonicalPerformanceInputs(
            garmin=garmin_metrics_from_payload(garmin),
            body=garmin_body_from_payload(garmin, TODAY),
        )
        result = analysis_history(
            GarminSyncStatus.from_state({}), TODAY, canonical=canonical
        )
        self.assertIn("body", result)
        series = _series(result["body"], "91d", "cycling_w_per_kg", "Garmin Connect")
        self.assertEqual(len(series["weekly"]), 13)
        self.assertEqual(
            series["weekly"][-1],
            {
                "week_start": "2026-09-30",
                "week_end": "2026-10-06",
                "value": 4.29,
                "count": 2,
                "dates": ["2026-10-05", "2026-10-06"],
            },
        )
        self.assertEqual(series["weekly"][0]["count"], 0)
        self.assertIsNone(series["weekly"][0]["value"])

    def test_deeply_nested_garmin_ftp_is_bounded(self):
        nested: dict[str, object] = {"calendarDate": "2026-10-06"}
        current = nested
        for _ in range(25):
            child: dict[str, object] = {}
            current["nested"] = child
            current = child
        current["functionalThresholdPower"] = 300
        result = body_history(
            {},
            {"cycling_ftp": nested, "weight": [{"date": "2026-10-06", "weightKg": 70}]},
            TODAY,
        )
        series = _series(result, "28d", "cycling_w_per_kg", "Garmin Connect")
        self.assertIsNone(series["points"][-1]["value"])


if __name__ == "__main__":
    unittest.main()
