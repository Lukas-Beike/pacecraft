"""Server integration tests for athlete."""

import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from server_test_support import ServerTestCase, server

from backend.athlete.checkins import normalize_checkin
from backend.athlete.profile import normalize_profile
from backend.errors import AppError


class ServerAthleteTests(ServerTestCase):
    def test_session_summaries_are_kept_until_a_change_in_the_real_wiring(self):
        # #3017/#3043: equipment and the HTTP detail read the same kept summaries.
        summaries = server.ATHLETE_DATA.session_summaries
        self.assertIs(server.ATHLETE_DATA.equipment()._read_summaries, summaries)
        detail = server.ACTIVITY_READS.activity_read()
        self.assertIs(detail._session_summaries, summaries)
        read = server.CANONICAL_READ.activity_summaries
        # A row-changing commit (the profile name; it starts no worker) first, so
        # what an earlier test kept is not current.
        server.ATHLETE_DATA.profile().save({"name": "Ada"})
        with patch.object(
            server.CANONICAL_READ, "activity_summaries", side_effect=read
        ) as counted:
            summaries()
            summaries()
            self.assertEqual(counted.call_count, 1)
            # The next row-changing commit makes the next call read again.
            server.ATHLETE_DATA.profile().save({"name": "Grace"})
            summaries()
            self.assertEqual(counted.call_count, 2)

    def test_profile_only_accepts_known_fields_and_trims(self):
        profile = normalize_profile(
            {"name": "  Ada  ", "goals": "Finish strong", "admin": True}
        )
        self.assertEqual(profile["name"], "Ada")
        self.assertNotIn("admin", profile)

    def test_profile_timezone_is_validated_and_local_now_uses_it(self):
        with self.assertRaises(AppError) as raised:
            server.ATHLETE_DATA.profile().save({"timezone": "Mars/NotAZone"})
        self.assertEqual(raised.exception.status, 400)
        profile = server.ATHLETE_DATA.profile().save({"timezone": "UTC"})
        self.assertEqual(profile["timezone"], "UTC")
        self.assertEqual(getattr(server.ATHLETE_CLOCK.now().tzinfo, "key", None), "UTC")

    def test_structured_weekly_availability_is_not_part_of_profile(self):
        profile = normalize_profile(
            {
                "availability": "Dienstag abends möglich",
                "availability_schedule": [
                    {"weekday": 1, "late": {"start": "17:30", "end": "20:00"}}
                ],
            },
            validate_timezone=True,
        )
        self.assertEqual(profile["availability"], "Dienstag abends möglich")
        self.assertNotIn("availability_schedule", profile)
        server.ATHLETE_DATA.profile().save(
            {
                "availability": profile["availability"],
                "availability_schedule": [{"weekday": 1, "max_minutes": 90}],
            }
        )
        self.assertNotIn("availability_schedule", server.ATHLETE_DATA.profile().get())
        context = server.COACH_CONTEXT.structured_context_service().build()
        self.assertNotIn("weekly_availability", context)

    def test_checkin_uses_local_date_and_rejects_future_dates(self):
        fixed_now = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)
        with patch.object(server.ATHLETE_CLOCK, "now", return_value=fixed_now):
            self.assertEqual(
                normalize_checkin({}, today=fixed_now.date())["checkin_date"],
                "2026-08-31",
            )
            with self.assertRaises(AppError) as raised:
                server.ATHLETE_DATA.checkin().save({"checkin_date": "2026-09-01"})
        self.assertEqual(raised.exception.status, 400)

    def test_profile_save_resets_button_before_follow_up_refresh(self):
        app = (
            Path(__file__).resolve().parents[1] / "public" / "settings.js"
        ).read_text(encoding="utf-8")
        save_profile = app[
            app.index("async function saveProfile") : app.index(
                "async function downloadDatabaseBackup"
            )
        ]
        self.assertIn(
            'button.removeAttribute("aria-busy");\n      button.textContent = buttonLabel;\n    }\n    await load();',
            save_profile,
        )


if __name__ == "__main__":
    unittest.main()
