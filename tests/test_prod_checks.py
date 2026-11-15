"""Post-release base scripts against a local fake app; no real server or network."""

import io
import json
import secrets
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock

from server_test_support import ServerTestCase, server

from backend.canonical.vocabulary import RecordingOrigin, Sport

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prod_client
import prod_diagnostics
import prod_guards
import prod_logs

# The fake app password, generated per run so no fixed credential-like literal exists.
FAKE_LOGIN = secrets.token_hex(12)
RECENT = (datetime.now(UTC) - timedelta(hours=2)).isoformat()


def report(messages=40):
    return {
        "app": {"version": "2.1.2"},
        "sync": {
            "last_error": "provider said: secret detail",
            "providers": {
                "garmin": {"last_sync_at": RECENT, "last_plan_sync_at": None},
                "wahoo": {"last_sync_at": None, "last_plan_sync_at": None},
                "intervals": {"last_sync_at": None, "last_plan_sync_at": RECENT},
            },
        },
        "database": {
            "schema_version": 27,
            "messages": messages,
            "competitions": 3,
            "workout_library_state": {"pending": 1, "note": "text never printed"},
            "file_bytes": 4096,
        },
        "recovery_copies": {"count": 1, "newest_age_hours": 2.0},
        "plan_sync": {
            "unsynced_total": 2,
            "unsynced_units": [{"has_error": True}, {"has_error": False}],
            "recent_jobs": [
                {
                    "status": "failed",
                    "error_class": "HTTPError",
                    "created_at": RECENT,
                    "items": [{"status": "failed", "error_class": "HTTPError"}],
                }
            ],
            # Open jobs past the stuck limit, every provider (#2953).
            "stuck_jobs": {"garmin": 1},
            # Every push of the count window per provider, settled items too.
            "push_counts": {
                "garmin": {
                    "jobs": {"failed": 1},
                    "items": {"failed": 1, "completed": 4},
                    "job_errors": {"HTTPError": 1},
                    "item_errors": {"HTTPError": 1, "boom: remote said no": 1},
                },
                "intervals": {"jobs": {"completed": 2}, "items": {"completed": 6}},
                "text never printed": {"jobs": {"Done!": 1}},
            },
        },
        "runtime": {"process_memory": {"rss_bytes": 1000}},
        "fit_inventory": {
            "status": "ready",
            "note": "text never printed",
            "garmin": {
                "ride": {
                    "files": 5,
                    "messages": {"216": 5},
                    "label": "text never printed",
                },
            },
            "act_secret": {"ride": {"files": 1}},
        },
    }


class FakeApp(BaseHTTPRequestHandler):
    state: ClassVar[dict] = {}

    def log_message(self, *_args):
        pass

    def send(self, code, body=None, cookies=()):
        self.send_response(code)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body or {}).encode())

    def session(self):
        return "ic_session=s1" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        if self.path == "/api/health":
            return self.send(200, {"status": "ok"})
        if not self.session():
            return self.send(401, {"error": "Anmeldung erforderlich."})
        if self.path == "/api/diagnostics":
            return self.send(200, report(self.state.get("messages", 40)))
        if self.path.startswith("/api/logs"):
            entries = [
                {"level": "ERROR", "event": "plan_push_failed", "message": FAKE_LOGIN},
                {
                    "level": "INFO",
                    "event": "garmin_past_workouts_removed",
                    "context": {"count": 3},
                },
            ]
            return self.send(200, {"entries": entries})
        return self.send(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/login":
            if body.get("password") != FAKE_LOGIN:
                return self.send(401)
            self.state["logins"] = self.state.get("logins", 0) + 1
            return self.send(
                200, {"csrf": "c1"}, ("ic_session=s1; Path=/", "ic_csrf=c1; Path=/")
            )
        if self.path == "/api/logout":
            if not self.session():
                return self.send(401)
            if self.headers.get("X-CSRF-Token") != "c1" and not self.state.get(
                "no_csrf"
            ):
                return self.send(403)
            self.state["logouts"] = self.state.get("logouts", 0) + 1
            return self.send(200, {"status": "ok"})
        return self.send(404)


class ProdChecksTests(unittest.TestCase):
    def setUp(self):
        FakeApp.state = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApp)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.secret = Path(self.tmp.name) / "secret"
        self.secret.write_text(FAKE_LOGIN + "\n", encoding="utf-8")

    def run_main(self, module, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = module.main(list(args))
        text = out.getvalue()
        self.assertNotIn(FAKE_LOGIN, text)
        self.assertNotIn("secret detail", text)
        self.assertNotIn("text never printed", text)
        return code, text

    def test_diagnostics_prints_named_fields_and_logs_out(self):
        result = Path(self.tmp.name) / "first.json"
        code, text = self.run_main(
            prod_diagnostics, "--url", self.url, "--password-file", str(self.secret),
            "--expect-version", "2.1.2", "--expect-schema", "27", "--out", str(result),
        )  # fmt: skip
        self.assertEqual(code, 1)  # sync.last_error is set
        self.assertIn("ok version 2.1.2", text)
        self.assertIn("ok schema 27", text)
        self.assertIn("FAIL sync last_error set: True", text)
        self.assertIn("ok provider garmin last sync", text)
        self.assertIn("info provider wahoo last sync never", text)
        # Error kinds of the whole count window (#2878).
        self.assertIn("2 unsynced, 1 with error; error kinds: ? 1, HTTPError 2", text)
        self.assertIn("info workout library: pending 1", text)
        # Per provider and level (#2849); other text prints as `?`.
        self.assertIn(
            "info plan push garmin: item completed 4, item error ? 1, "
            "item error HTTPError 1, item failed 1, job error HTTPError 1, "
            "job failed 1",
            text,
        )
        self.assertIn(
            "info plan push intervals: item completed 6, job completed 2", text
        )
        self.assertIn("info plan push ?: job ? 1", text)
        # A stuck job of any provider fails the check, not only among the newest
        # ten jobs (#2953).
        self.assertIn("FAIL stuck plan-sync jobs: 1", text)
        # The newest app backup is shown; a schema release requires it (#2996).
        self.assertIn("info backup unknown", text)
        fresh = {
            "backup": {"known": True, "age_hours": 3.5, "bytes": 9, "verified": True}
        }
        checks = {
            "fresh": ({"backup": prod_diagnostics.backup_state(fresh["backup"])}, "ok"),
            "old": (
                {
                    "backup": prod_diagnostics.backup_state(
                        fresh["backup"] | {"age_hours": 30}
                    )
                },
                "FAIL",
            ),
            "unchecked": (
                {
                    "backup": prod_diagnostics.backup_state(
                        fresh["backup"] | {"verified": False}
                    )
                },
                "FAIL",
            ),
            "unknown": ({"backup": prod_diagnostics.backup_state(None)}, "FAIL"),
            "future": (
                {
                    "backup": prod_diagnostics.backup_state(
                        fresh["backup"] | {"age_hours": -2}
                    )
                },
                "FAIL",
            ),
        }
        for name, (case, state) in checks.items():
            with self.subTest(backup=name):
                self.assertEqual(prod_diagnostics.backup_line(case, 24)[0], state)
        # Without the field the recent jobs decide; an unavailable section is
        # unknown, which fails.
        old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        recent = {"recent_jobs": [{"status": "running", "created_at": old}]}
        now = datetime.now(UTC)
        self.assertEqual(prod_diagnostics.stuck_count(recent, now), 1)
        self.assertIsNone(prod_diagnostics.stuck_count({"status": "unavailable"}, now))
        self.assertEqual(
            prod_diagnostics.error_kind({"error_class": "boom: remote said no"}), "?"
        )
        # A malformed provider entry or count prints nothing instead of crashing;
        # a report without the counts says so.
        self.assertEqual(
            prod_diagnostics.plan_pushes(
                {"garmin": ["x"], "wahoo": {"jobs": {"failed": float("nan")}}}
            ),
            {"garmin": {}, "wahoo": {}},
        )
        self.assertEqual(
            prod_diagnostics.push_lines({"push_counts_reported": False}),
            [("info", "plan push counts: not in this report")],
        )
        self.assertIn("ok logout 200", text)
        self.assertEqual(FakeApp.state["logouts"], 1)
        self.assertEqual(json.loads(result.read_text())["counts"]["messages"], 40)

        FakeApp.state["messages"] = 39
        code, text = self.run_main(
            prod_diagnostics, "--url", self.url, "--password-file", str(self.secret),
            "--compare", str(result),
        )  # fmt: skip
        self.assertIn("FAIL count messages 40 -> 39", text)
        # Storage is shown with the previous release's value (#2848).
        self.assertIn("info storage file_bytes 4096 (was 4096)", text)
        self.assertIn("info storage recovery_count 1 (was 1)", text)
        self.assertIn("ok count competitions 3 -> 3", text)
        self.assertEqual(
            prod_diagnostics.comparison_lines(
                {
                    "counts": {"external_calendar_events": 20, "competitions": 2},
                    "memory": {},
                },
                {"counts": {"external_calendar_events": 21, "competitions": 3}},
            ),
            [
                ("info", "count external_calendar_events 21 -> 20"),
                ("FAIL", "count competitions 3 -> 2"),
            ],
        )
        self.assertEqual(
            prod_diagnostics.comparison_lines(
                {"counts": {"external_calendar_events": 20}, "memory": {}}, None
            ),
            [("info", "count external_calendar_events 20")],
        )

    def test_section_prints_only_allow_listed_fields_in_path_order(self):
        code, text = self.run_main(
            prod_diagnostics, "--url", self.url, "--password-file", str(self.secret),
            "--section", "fit_inventory",
        )  # fmt: skip
        self.assertEqual(code, 0)  # the checks (sync.last_error) are not run
        prefix = "info fit_inventory."
        self.assertEqual(
            text.splitlines(),
            [
                "ok health",
                "ok logout 200",
                f"{prefix}garmin.ride.files 5",
                f"{prefix}garmin.ride.messages.216 5",
                f'{prefix}status "ready"',
            ],
        )
        self.assertEqual(
            prod_diagnostics.section_lines({}, "fit_inventory"),
            [("FAIL", "section fit_inventory missing")],
        )
        failed = {"fit_inventory": {"status": "failed"}}
        self.assertEqual(
            prod_diagnostics.section_lines(failed, "fit_inventory"),
            [("FAIL", 'fit_inventory.status "failed"')],
        )
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            prod_diagnostics.main(
                ["--url", self.url, "--password-file", str(self.secret),
                 "--section", "fit_inventory", "--expect-version", "2.1.2"]
            )  # fmt: skip

    def test_post_release_sections_print_counts_and_state_words_only(self):
        # Shapes as the report builds them, with planted text, ids and times.
        report = {
            "database": {
                "schema_version": 29,
                "messages": 40,
                "file_bytes": 4096,
                "wal_bytes": 512,
                "data_dir_free_bytes": 10**9,
                "stored_payload_bytes": {"garmin_state": 7},
                "workout_library_state": {"synced": 3, "note": "text never printed"},
            },
            "runtime": {
                "python": "3.14.0",
                "platform": "Linux-text never printed",
                "process_memory": {"rss_bytes": 1000},
                "uptime_s": 3600,
                "malloc_arena_max": 2,
                "reader_slots": {
                    "slots": 2,
                    "waits_over_1s": 3,
                    "timeouts": 0,
                    "longest_wait_s": 1.25,
                },
                "heavy_read_gate": {"waits_over_1s": 4, "timeouts": 1},
            },
            "recovery_copies": {
                "count": 1,
                "newest_age_hours": 2.0,
                "total_bytes": 2048,
            },
            "garmin_strength_push": {
                "garmin": {"units": 2, "several_exercises": 1, "with_kg": 1},
                "intervals": {"units": 2, "without_structure": 1},
                "unit_name": "text never printed",
            },
            "backup": {
                "known": True,
                "age_hours": 3.5,
                "bytes": 4096,
                "verified": True,
                "name": "text never printed",
            },
            "curve_build": {
                "status": "building",
                "builds": 3,
                "failures": 0,
                "last_build_s": 41.5,
                "building_s": 12.0,
                "failed_age_s": None,
                "curves_loaded": 900,
                "curve_store_failures": 0,
            },
            "garmin_past_workout_removal": {
                "one_time_done": True,
                "checked": 4,
                "removed": 2,
                "last_run_at": RECENT,
                "last_outcome": "ok",
                "past_links_pending": 0,
            },
            "stream_backfill": {
                "garmin": {"stored": 12, "cursor": "finished", "finished": True},
                "wahoo": {"stored": 3, "cursor": "text never printed"},
            },
            "fit_inventory": {
                "garmin": {
                    "ride": {
                        "files": 5,
                        "messages": {"216": 5},
                        "unmapped_fields": {"20.134": 4},
                    },
                },
                "act_secret": {"ride": {"files": 1}},
            },
        }
        expected = {
            "garmin_past_workout_removal": [
                "checked 4", 'last_outcome "ok"', "last_run_hours 2.0",
                "one_time_done true", "past_links_pending 0", "removed 2",
            ],
            "stream_backfill": [
                'garmin.cursor "finished"', "garmin.finished true",
                "garmin.stored 12", "wahoo.stored 3",
            ],
            "fit_inventory": [
                "garmin.ride.files 5", "garmin.ride.messages.216 5",
                "garmin.ride.unmapped_fields.20.134 4",
            ],
            "database": [
                "data_dir_free_bytes 1000000000", "file_bytes 4096",
                "messages 40", "schema_version 29",
                "stored_payload_bytes.garmin_state 7", "wal_bytes 512",
                "workout_library_state.synced 3",
            ],
            "runtime": [
                "heavy_read_gate.timeouts 1", "heavy_read_gate.waits_over_1s 4",
                "malloc_arena_max 2", "process_memory.rss_bytes 1000",
                "reader_slots.longest_wait_s 1.25", "reader_slots.slots 2",
                "reader_slots.timeouts 0", "reader_slots.waits_over_1s 3",
                "uptime_s 3600",
            ],
            "recovery_copies": [
                "count 1", "newest_age_hours 2.0", "total_bytes 2048",
            ],
            "garmin_strength_push": [
                "garmin.several_exercises 1", "garmin.units 2", "garmin.with_kg 1",
                "intervals.units 2", "intervals.without_structure 1",
            ],
            "backup": [
                "age_hours 3.5", "bytes 4096", "known true", "verified true",
            ],
            "curve_build": [
                "building_s 12.0", "builds 3", "curve_store_failures 0",
                "curves_loaded 900", "failed_age_s null", "failures 0",
                "last_build_s 41.5", 'status "building"',
            ],
        }  # fmt: skip
        now = datetime.fromisoformat(RECENT) + timedelta(hours=2)
        for name, fields in expected.items():
            with self.subTest(section=name):
                self.assertEqual(
                    prod_diagnostics.section_lines(report, name, now),
                    [("info", f"{name}.{field}") for field in fields],
                )
        # One login reads several sections; a missing one fails the run.
        code, text = self.run_main(
            prod_diagnostics, "--url", self.url, "--password-file", str(self.secret),
            "--section", "fit_inventory",
            "--section", "garmin_past_workout_removal",
        )  # fmt: skip
        self.assertEqual(code, 1)
        self.assertIn("info fit_inventory.garmin.ride.files 5", text)
        self.assertIn("FAIL section garmin_past_workout_removal missing", text)
        # A stopped or failed state is a FAIL line; an empty section is no failure.
        stopped = {
            "stream_backfill": {"garmin": {"cursor": "blocked"}},
            "garmin_past_workout_removal": {"last_outcome": "failed"},
            "fit_inventory": {},
        }
        self.assertEqual(
            [
                line
                for name in ("stream_backfill", "garmin_past_workout_removal")
                for line in prod_diagnostics.section_lines(stopped, name)
            ],
            [
                ("FAIL", 'stream_backfill.garmin.cursor "blocked"'),
                ("FAIL", 'garmin_past_workout_removal.last_outcome "failed"'),
            ],
        )
        self.assertEqual(
            prod_diagnostics.section_lines(stopped, "fit_inventory"),
            [("info", "section fit_inventory empty")],
        )
        # The FIT allow-list names every origin and sport of the vocabulary.
        fit = prod_diagnostics.SECTION_FIELDS["fit_inventory"]
        for origin in RecordingOrigin:
            for sport in [*Sport, "unknown"]:
                sport_name = getattr(sport, "value", sport)
                self.assertTrue(fit.fullmatch(f"{origin.value}.{sport_name}.files"))

    def test_diagnostics_version_mismatch_and_wrong_password(self):
        code, text = self.run_main(
            prod_diagnostics, "--url", self.url, "--password-file", str(self.secret),
            "--expect-version", "9.9.9",
        )  # fmt: skip
        self.assertIn("FAIL version 2.1.2 (expected 9.9.9)", text)
        self.secret.write_text("wrong password value", encoding="utf-8")
        code, text = self.run_main(
            prod_diagnostics, "--url", self.url, "--password-file", str(self.secret)
        )
        self.assertEqual(code, 2)
        self.assertIn("FAIL login answered 401", text)
        self.assertNotIn("wrong password value", text)

    def test_result_files_stay_outside_the_repository(self):
        inside = Path(prod_client.ROOT) / "result.json"
        code, text = self.run_main(
            prod_diagnostics, "--url", self.url, "--password-file", str(self.secret),
            "--out", str(inside),
        )  # fmt: skip
        self.assertEqual(code, 2)
        self.assertIn("system temp directory", text)
        self.assertFalse(inside.exists())
        # The password file may come from the T3 secret folder, read only;
        # outputs stay in the temp directory and nothing reads the repository.
        t3 = Path.home() / "t3-folder-for-a-test" / "secrets"
        with mock.patch.object(prod_client, "T3_SECRETS", t3):
            secret = t3 / "app-password"
            self.assertEqual(prod_client.secret_path(str(secret)), secret.resolve())
            for path in (
                prod_client.ROOT / ".env",
                t3.parent / "other",
                "app-password",
            ):
                with self.subTest(path=path), self.assertRaises(prod_client.ProdError):
                    prod_client.secret_path(str(path))
            with self.assertRaisesRegex(prod_client.ProdError, "system temp directory"):
                prod_client.scratch_path(str(t3 / "result.json"))
        # The repository stays excluded even inside an allowed folder.
        with (
            mock.patch.object(prod_client, "T3_SECRETS", prod_client.ROOT.parent),
            self.assertRaises(prod_client.ProdError),
        ):
            prod_client.secret_path(str(prod_client.ROOT / ".env"))

    def test_guards_hold_and_detect_a_missing_csrf_check(self):
        code, text = self.run_main(
            prod_guards, "--url", self.url, "--password-file", str(self.secret)
        )
        self.assertEqual(code, 0, text)
        self.assertIn("ok no session -> 401", text)
        self.assertIn("ok no CSRF token -> 403", text)
        FakeApp.state["no_csrf"] = True
        code, text = self.run_main(
            prod_guards, "--url", self.url, "--password-file", str(self.secret)
        )
        self.assertEqual(code, 1)
        self.assertIn("FAIL no CSRF token -> 200", text)

    def test_logs_from_the_app_and_from_a_saved_file(self):
        code, text = self.run_main(
            prod_logs, "--url", self.url, "--password-file", str(self.secret)
        )
        self.assertEqual(code, 1)
        self.assertIn("  ERROR plan_push_failed 1", text)
        self.assertIn("garmin past workouts removed: 3", text)
        self.assertIn("plan sync error events: 1", text)
        log = Path(self.tmp.name) / "container.log"
        log.write_text(
            '2026-11-11T10:00:00Z {"timestamp":"2026-11-11T10:00:00","level":"WARNING",'
            '"event":"weather_refresh_failed","message":"token=abc"}\n'
            "plain line with ERROR and a payload\n"
            '{"timestamp":"2026-11-10T00:00:00","level":"ERROR","event":"old"}\n'
            '{"level":"ERROR","event":"Bad Event; drop table"}\n',
            encoding="utf-8",
        )
        code, text = self.run_main(
            prod_logs, "--log-file", str(log), "--since", "2026-11-11"
        )
        self.assertIn("  WARNING weather_refresh_failed 1", text)
        self.assertIn("  ERROR unparsed 1", text)
        self.assertIn("  ERROR unnamed 1", text)
        self.assertNotIn("old", text)
        self.assertNotIn("token=abc", text)
        self.assertNotIn("payload", text)
        code, text = self.run_main(
            prod_logs, "--log-file", str(Path(prod_client.ROOT) / "README.md")
        )
        self.assertEqual(code, 2)
        self.assertIn("system temp directory", text)


# Leaves of an allow-listed section that `--section` never prints, each with its reason (#3131).
NOT_PRINTED = {
    "runtime": {
        "python": "free text: the interpreter version",
        "platform": "free text: the host platform",
        "slowest_routes": "a list of route patterns, read in the full export (#3017)",
    },
}


class SectionFieldsCoverTheProducersTests(ServerTestCase):
    """A new diagnostics key fails here until prod_diagnostics prints or names it (#3131)."""

    def test_every_key_a_section_emits_is_printed_or_named(self):
        now = datetime.now(UTC)
        # A time not set yet still prints, as a null age.
        self.assertEqual(
            prod_diagnostics.aged(("last_run_at", None), now), ("last_run_hours", None)
        )
        # The real producers, on the empty temporary storage of this app.
        report = server.DIAGNOSTICS.report_service().report()
        for name, allowed in prod_diagnostics.SECTION_FIELDS.items():
            section = report.get(name)
            with self.subTest(section=name):
                self.assertIsInstance(section, dict)
                self.assertNotEqual(section.get("status"), "unavailable")
                for leaf in prod_diagnostics.leaves(section):
                    path = prod_diagnostics.aged(leaf, now)[0]
                    self.assertTrue(
                        allowed.fullmatch(path) or path in NOT_PRINTED.get(name, {}),
                        f"diagnostics section {name} emits {path}: add it to "
                        "SECTION_FIELDS in scripts/prod_diagnostics.py, or to "
                        "NOT_PRINTED in this test with the reason",
                    )


if __name__ == "__main__":
    unittest.main()
