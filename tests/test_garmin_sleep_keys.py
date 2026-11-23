"""Skin-temperature key names of Garmin sleep answers: paths only, never values (#4017).

Synthetic payloads and temporary storage only.
"""

from __future__ import annotations

import json
import unittest
from datetime import date

from support import temp_database

from backend.canonical.section_items import split_section
from backend.db import garmin_store
from backend.db.repositories import KeyValueRepository
from backend.sync.garmin_sleep_keys import (
    STATE_KEY,
    SleepKeyRecorder,
    summarize_sleep_temperature_keys,
    temperature_key_paths,
)

TODAY = date(2026, 11, 23)
VALUE = "value-that-must-never-appear"


class TemperatureKeyPathsTests(unittest.TestCase):
    def test_top_level_dto_and_nested_paths_match_case_insensitively(self) -> None:
        payload = {
            "skinTempData": {"avgDeviation": 0.4, "Temperature": 1},
            "dailySleepDTO": {"AvgSkinTemp": 0.2, "sleepTimeSeconds": 100},
            "other": {"a": {"b": {"skin": 1}}, "tempOk": 1},
        }
        self.assertEqual(
            temperature_key_paths(payload),
            {
                "skinTempData",
                "skinTempData.Temperature",
                "dailySleepDTO.AvgSkinTemp",
                "other.tempOk",
            },
        )

    def test_depth_is_capped_and_lists_are_not_entered(self) -> None:
        payload = {"a": {"b": {"c": {"skin": 1}, "skinB": 1}}, "rows": [{"skin": 1}]}
        self.assertEqual(temperature_key_paths(payload), {"a.b.skinB"})

    def test_only_safe_paths_survive(self) -> None:
        payload = {
            "skin temp": 1,
            "skin-x": 1,
            "ok_Skin": 1,
            "t" + "emp" * 50: 1,
            "dailySleepDTO": {"skin;DROP": 1},
        }
        self.assertEqual(temperature_key_paths(payload), {"ok_Skin"})
        self.assertEqual(temperature_key_paths([payload]), set())


class SleepKeyDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = temp_database(self, schema=True)
        self.values = KeyValueRepository(lambda: "2026-11-23T10:00:00+00:00")
        self.recorder = SleepKeyRecorder(self.manager, self.values, lambda: TODAY)

    def summary(self) -> dict:
        with self.manager.reader() as db:
            return summarize_sleep_temperature_keys(self.values, db, TODAY)

    def night(self, day: str, **extra) -> dict:
        return {
            "dailySleepDTO": {"calendarDate": day, "note": VALUE, **extra},
            "avgSkinTempDeviation": VALUE,
        }

    def test_nothing_scanned_is_not_reported_as_none_found(self) -> None:
        self.assertEqual(
            self.summary(),
            {"collectors": {}, "date_range": None, "none_found": False},
        )

    def test_a_scan_without_matches_reports_none_found(self) -> None:
        self.recorder.observe("sleep_night", {"dailySleepDTO": {"calendarDate": "x"}})
        self.recorder.observe("sleep_night", {"dailySleepDTO": {"note": VALUE}})
        self.assertEqual(self.summary()["none_found"], True)

    def test_paths_accumulate_per_collector_and_a_reread_adds_no_night(self) -> None:
        first = self.night("2026-11-20", skinTemp=VALUE)
        self.recorder.observe("sleep_night", first)
        self.recorder.observe("morning_sleep", first)
        self.recorder.observe("sleep_night", self.night("2026-11-21"))
        self.recorder.observe(
            "sleep", [self.night("2026-11-19"), self.night("2026-11-20")]
        )
        self.recorder.observe("unrelated", self.night("2026-01-01"))

        summary = self.summary()

        self.assertEqual(
            summary["collectors"],
            {
                "get_sleep_data": {
                    "avgSkinTempDeviation": 2,
                    "dailySleepDTO.skinTemp": 1,
                },
                "get_sleep_daily": {"avgSkinTempDeviation": 2},
            },
        )
        self.assertEqual(
            summary["date_range"], {"from": "2026-11-19", "to": "2026-11-21"}
        )
        self.assertFalse(summary["none_found"])

    def test_the_stored_set_is_capped(self) -> None:
        payload = {f"skin{index}": 1 for index in range(80)}
        self.recorder.observe("sleep_night", payload)
        self.assertEqual(len(self.summary()["collectors"]["get_sleep_data"]), 50)

    def test_stored_nights_are_scanned_by_their_daily_sleep_dto_only(self) -> None:
        rows = [
            {
                "calendarDate": "2026-11-22",
                "skinTempTop": VALUE,
                "dailySleepDTO": {"calendarDate": "2026-11-22", "skinTempDeviation": 1},
            },
            {"calendarDate": "2026-01-01", "dailySleepDTO": {"skinTempOld": 1}},
        ]
        with self.manager.unit_of_work() as db:
            garmin_store.put_section(
                db,
                "sleep",
                split_section(rows),
                fetched_at="2026-11-23T07:00:00+00:00",
                now="2026-11-23T08:00:00+00:00",
                mapper_version=1,
                replace=False,
            )
        self.assertEqual(
            self.summary()["collectors"],
            {"stored": {"dailySleepDTO.skinTempDeviation": 1}},
        )

    def test_only_paths_and_counts_reach_the_state_and_the_report(self) -> None:
        self.recorder.observe(
            "sleep_night",
            self.night("2026-11-20", **{"skin;x": VALUE, "skinOk": VALUE}),
        )
        with self.manager.reader() as db:
            stored = self.values.get(db, STATE_KEY) or ""
        report = json.dumps(self.summary())
        for text in (stored, report):
            self.assertNotIn(VALUE, text)
            self.assertNotIn("skin;x", text)
        self.assertIn("dailySleepDTO.skinOk", report)

    def test_a_failed_write_never_raises(self) -> None:
        class Broken:
            def unit_of_work(self):
                raise RuntimeError("down")

        SleepKeyRecorder(Broken(), self.values, lambda: TODAY).observe(
            "sleep", [self.night("2026-11-20")]
        )


if __name__ == "__main__":
    unittest.main()
