"""Post-release base scripts against a local fake app; no real server or network."""

import io
import itertools
import json
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock

from server_test_support import ServerTestCase, server

from backend.canonical.vocabulary import RecordingOrigin, Sport
from backend.coach.read_tools import LOOKUP_OUTCOMES
from backend.coach.receipt_compaction import compact_tool_result
from backend.coach.turn_failures import coach_error_metadata
from backend.errors import AppError
from backend.nutrition.energy_inputs import RESTING_FROM_SKINFOLDS

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
        # The Coach history as the diagnostics project it, with planted text.
        "coach_commands": [
            {
                "id": "act_secret",
                "created_at": RECENT,
                "steps": [
                    {
                        "tool": "lookup_food",
                        "ok": True,
                        "error": None,
                        "lookup_outcomes": {"local_hit": 2, "external_requested": 1},
                    },
                    {
                        "tool": "manage_training_templates",
                        "ok": False,
                        "error": {"reason": "template_exists_in_call"},
                    },
                ],
            },
            {
                "id": "turn-2",
                "steps": [
                    {
                        "tool": "lookup_food",
                        "ok": True,
                        "error": None,
                        "lookup_outcomes": {
                            "external_after_miss": 1,
                            "text never printed": 7,
                        },
                    },
                    {
                        "tool": "save_competition",
                        "ok": True,
                        "error": None,
                        "created_despite_candidate": 1,
                    },
                    {
                        "tool": "save_competition",
                        "ok": False,
                        "error": {"reason": "competition_exists"},
                    },
                    "text never printed",
                ],
            },
        ],
        "fit_inventory": {
            "status": "ready",
            "note": "text never printed",
            "garmin": {
                "ride": {
                    "files": 5,
                    "messages": {"216": 5},
                    "new_decodes": {
                        "respiration_record.in_bounds": 3,
                        "workout_steps_max": 12,
                        "note text": 1,
                    },
                    "label": "text never printed",
                },
            },
            "act_secret": {"ride": {"files": 1}},
        },
        # #3595: the re-projection pass after a mapper bump, counts only.
        "canonical_backfill": {
            "status": "running",
            "batches": 3,
            "providers": {
                "garmin": {
                    "reprojected": 412,
                    "added": 7,
                    "retired": 0,
                    "skipped_invalid": 0,
                    "duplicates": 0,
                    "cursor": 9,
                },
                "act_secret": {"reprojected": 1},
            },
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
        self.state.setdefault("gets", []).append(self.path)
        if self.path == "/api/health":
            return self.send(200, {"status": "ok"})
        if not self.session():
            return self.send(401, {"error": "Anmeldung erforderlich."})
        if self.path == "/api/diagnostics":
            data = report(self.state.get("messages", 40))
            # One curve build status per read; the last one stays (#3346).
            if curves := self.state.get("curves"):
                status = curves.pop(0) if len(curves) > 1 else curves[0]
                data["curve_build"] = {"status": status, "building_s": 7}
            return self.send(200, data)
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
        # The checks compare a checkout with origin/develop (#3344): here a
        # directory without git state, not this repository, unless a test sets one.
        self.checkout = Path(self.tmp.name)
        for module in (prod_diagnostics, prod_guards):
            patcher = mock.patch.object(
                module,
                "checkout_current",
                lambda required: prod_client.checkout_current(required, self.checkout),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

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
        # --expect-schema checks the docs (#3454): on a temporary root, never
        # this checkout (Gitar #3456).
        with mock.patch.object(prod_diagnostics, "ROOT", Path(self.tmp.name)):
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
            # An automatic copy that recent passes without a download (#3760).
            "automatic": (
                {
                    "backup": prod_diagnostics.backup_state(
                        {
                            "known": False,
                            "automatic": {"enabled": True, "newest_age_hours": 5.0},
                        }
                    )
                },
                "ok",
            ),
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
                    "counts": {
                        "external_calendar_events": 20,
                        "competitions": 2,
                        "nutrition_logs": 410,
                    },
                    "memory": {},
                },
                {
                    "counts": {
                        "external_calendar_events": 21,
                        "competitions": 3,
                        "nutrition_logs": 412,
                    }
                },
            ),
            [
                ("info", "count external_calendar_events 21 -> 20"),
                ("FAIL", "count competitions 3 -> 2"),
                # A diary that lost entries across a release fails (#3234).
                ("FAIL", "count nutrition_logs 412 -> 410"),
            ],
        )
        self.assertEqual(
            prod_diagnostics.comparison_lines(
                {"counts": {"external_calendar_events": 20}, "memory": {}}, None
            ),
            [("info", "count external_calendar_events 20")],
        )

    def test_the_canonical_backfill_section_prints_its_pass_counts_only(self):
        # #3595: after a mapper bump, whether the re-projection pass is done; an
        # unknown counter or provider key is never printed.
        code, text = self.run_main(
            prod_diagnostics, "--url", self.url, "--password-file", str(self.secret),
            "--section", "canonical_backfill",
        )  # fmt: skip
        self.assertEqual(code, 0)
        prefix = "info canonical_backfill."
        self.assertEqual(
            text.splitlines(),
            [
                "ok health",
                "ok logout 200",
                f"{prefix}batches 3",
                f"{prefix}providers.garmin.added 7",
                f"{prefix}providers.garmin.duplicates 0",
                f"{prefix}providers.garmin.reprojected 412",
                f"{prefix}providers.garmin.retired 0",
                f"{prefix}providers.garmin.skipped_invalid 0",
                f'{prefix}status "running"',
            ],
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
                f"{prefix}garmin.ride.new_decodes.respiration_record.in_bounds 3",
                f"{prefix}garmin.ride.new_decodes.workout_steps_max 12",
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

    def test_the_garmin_threshold_store_section_prints_counts_only(self):
        # #4135: the one-time store's result, never a value.
        report = {
            "garmin_threshold_store": {
                "status": "complete",
                "stored_count": 3,
                "stored_sports": {"run": 2, "ride": 1},
                "history_rows": 3,
                "planted": "value",
            }
        }
        lines = [text for _, text in prod_diagnostics.section_lines(
            report, "garmin_threshold_store"
        )]  # fmt: skip
        self.assertEqual(
            lines,
            [
                "garmin_threshold_store.history_rows 3",
                'garmin_threshold_store.status "complete"',
                "garmin_threshold_store.stored_count 3",
                "garmin_threshold_store.stored_sports.ride 1",
                "garmin_threshold_store.stored_sports.run 2",
            ],
        )

    def test_the_sleep_key_section_prints_its_paths_and_dates_only(self):
        # #4017: the key names are the point; an unknown field is never printed.
        report = {
            "garmin_sleep_temperature_keys": {
                "collectors": {
                    "get_sleep_data": {"dailySleepDTO.someSkinTempKey": 3},
                    "stored": {"dailySleepDTO.someSkinTempKey": 2},
                    "get_sleep_daily": {},
                },
                "scanned_days": {"get_sleep_data": 20, "get_sleep_daily": 4},
                "date_range": {"from": "2026-11-01", "to": "2026-11-20"},
                "none_found": False,
                "planted": "value",
            }
        }
        lines = [text for _, text in prod_diagnostics.section_lines(
            report, "garmin_sleep_temperature_keys"
        )]  # fmt: skip
        joined = "\n".join(lines)
        self.assertIn("collectors.get_sleep_data.dailySleepDTO.someSkinTempKey", joined)
        self.assertIn("collectors.stored.dailySleepDTO.someSkinTempKey", joined)
        self.assertIn("none_found", joined)
        # A collector that ran shows its scanned nights, also without matches (#4017).
        self.assertIn("scanned_days.get_sleep_data", joined)
        self.assertIn("scanned_days.get_sleep_daily", joined)
        # Dates and unknown fields are never printed.
        self.assertNotIn("date_range", joined)
        self.assertNotIn("planted", joined)

    def test_push_notifications_print_counts_only(self):
        # #3914: device and delivery counts are readable on prod; nothing else is.
        report = {
            "push_notifications": {
                "subscriptions": 1,
                "per_type": {"upcoming": {"sent": 2, "failed": 1}},
                "endpoint": "https://fcm.googleapis.com/fcm/send/planted",
            }
        }
        lines = [
            text
            for _, text in prod_diagnostics.section_lines(report, "push_notifications")
        ]
        self.assertEqual(
            sorted(lines),
            [
                "push_notifications.per_type.upcoming.failed 1",
                "push_notifications.per_type.upcoming.sent 2",
                "push_notifications.subscriptions 1",
            ],
        )

    def test_energy_balance_prints_the_body_codes_and_counts(self):
        # #3362: the energy table's body source and reason are readable on prod.
        report = {
            "energy_balance": {
                "status": "ready",
                # The backend's own codes, so a rename fails here (Gitar #3366).
                "rmr_source": RESTING_FROM_SKINFOLDS,
                "latest_window_insufficient": "weight_span",
                "body_resolved": False,
                "body_missing": "no_skinfold",
                # #3640: an entered body fat prints; the profile is no source.
                "body_source": "entered",
                "body_source_windows": {"skinfold": 0, "device": 2, "entered": 9},
                "skinfolds_with_body_fat": 0,
                "newest_skinfold_age_days": None,
                "today_resting_days_back": 1,
                # #3577: formulas whose learned everyday fit lies below 0.
                "learned_everyday_negative": 1,
                # #3723: the formula rows' learned everyday per state.
                "learned_everyday_state": {"none": 0, "provisional": 1, "qualified": 1},
                # #3739: the 3-month deficit's weigh-ins per edge window.
                "deficit_edge_weighins_91d": {"first": 12, "last": 9},
                # #3530: days whose Garmin summary names its wellness end.
                "wellness_end_days": 6,
                # #3557: weigh-in days per source, dropped days per provider.
                "latest_window_weighin_sources": {
                    "by_source": {"app": 12, "garmin": 3},
                    "dropped_days": {"wahoo": 28},
                },
                # #3553: complete days the former activity-scaled rule dropped.
                "latest_window_former_rule_days": 4,
                # #3554: the fit's estimated windows per store rule.
                "calibration_store_methods": {"measured": 4, "prior": 0},
                # #3611: the walks behind NEAT's step length.
                "step_source": "walks",
                "step_walks": {"walks": 9, "used": 6, "former_implausible": 7},
                # #3718: Garmin's weight rows by origin, days before the list.
                "garmin_weight_origins": {
                    "current": {"weigh_in_list": 40},
                    "not_current": {"performance_history": 2},
                    "current_days_before_first_weighin_list_day": 0,
                    "not_current_days_before_first_weighin_list_day": 1,
                    "answer_weighin_days": 30,
                    "answer_days_without_current_row": 0,
                    "refetch_state": "finished",
                    "refetch_added_days": 12,
                },
                "planted": "never printed",
            }
        }
        lines = [
            text for _, text in prod_diagnostics.section_lines(report, "energy_balance")
        ]
        prefix = "energy_balance."
        for expected in (
            f'{prefix}body_missing "no_skinfold"',
            f'{prefix}body_source "entered"',
            f"{prefix}body_source_windows.device 2",
            f"{prefix}body_source_windows.entered 9",
            f"{prefix}body_resolved false",
            f'{prefix}rmr_source "{RESTING_FROM_SKINFOLDS}"',
            f'{prefix}latest_window_insufficient "weight_span"',
            f"{prefix}newest_skinfold_age_days null",
            f"{prefix}today_resting_days_back 1",
            f"{prefix}learned_everyday_negative 1",
            f"{prefix}learned_everyday_state.provisional 1",
            f"{prefix}learned_everyday_state.qualified 1",
            f"{prefix}deficit_edge_weighins_91d.first 12",
            f"{prefix}deficit_edge_weighins_91d.last 9",
            f"{prefix}wellness_end_days 6",
            f"{prefix}latest_window_weighin_sources.by_source.app 12",
            f"{prefix}latest_window_weighin_sources.by_source.garmin 3",
            f"{prefix}latest_window_weighin_sources.dropped_days.wahoo 28",
            f"{prefix}latest_window_former_rule_days 4",
            f"{prefix}calibration_store_methods.measured 4",
            f"{prefix}calibration_store_methods.prior 0",
            f'{prefix}step_source "walks"',
            f"{prefix}step_walks.used 6",
            f"{prefix}step_walks.former_implausible 7",
            f"{prefix}garmin_weight_origins.current.weigh_in_list 40",
            f"{prefix}garmin_weight_origins.not_current.performance_history 2",
            f"{prefix}garmin_weight_origins.current_days_before_first_weighin_list_day 0",
            f"{prefix}garmin_weight_origins.not_current_days_before_first_weighin_list_day 1",
            f"{prefix}garmin_weight_origins.answer_weighin_days 30",
            f"{prefix}garmin_weight_origins.answer_days_without_current_row 0",
            f'{prefix}garmin_weight_origins.refetch_state "finished"',
            f"{prefix}garmin_weight_origins.refetch_added_days 12",
        ):
            self.assertIn(expected, lines)
        self.assertFalse(any("planted" in line for line in lines))

    def test_coach_local_first_prints_the_3080_sums_only(self):
        code, text = self.run_main(
            prod_diagnostics, "--url", self.url, "--password-file", str(self.secret),
            "--section", "coach_local_first",
        )  # fmt: skip
        self.assertEqual(code, 0)
        prefix = "info coach_local_first."
        self.assertEqual(
            text.splitlines(),
            [
                "ok health",
                "ok logout 200",
                f"{prefix}commands 2",
                f"{prefix}created_despite_candidate.manage_training_templates 0",
                f"{prefix}created_despite_candidate.save_competition 1",
                f"{prefix}lookup_outcomes.external_after_miss 1",
                f"{prefix}lookup_outcomes.external_requested 1",
                f"{prefix}lookup_outcomes.local_hit 2",
                f"{prefix}refusals.competition_exists 1",
                f"{prefix}refusals.template_exists 0",
                f"{prefix}refusals.template_exists_in_call 1",
            ],
        )
        self.assertNotIn("secret", text)
        self.assertNotIn("never printed", text)
        # A history the report could not build fails, as a missing section does.
        for broken in ({}, {"coach_commands": {"status": "unavailable"}}):
            with self.subTest(report=broken):
                self.assertEqual(
                    prod_diagnostics.named_section_lines(broken, "coach_local_first"),
                    [("FAIL", "section coach_commands missing")],
                )

    def test_route_timings_print_each_route_s_counts_and_milliseconds(self):
        # #3630: the slowest routes by pattern, counts and ms only; a planted
        # field or a non-numeric value never prints, and a malformed row is skipped.
        report = {
            "runtime": {
                "slowest_routes": [
                    {"route": "GET /api/context-preview", "count": 4, "p50_ms": 900,
                     "p95_ms": 3300, "max_ms": 3370, "disconnects": 0,
                     "planted": "secret"},
                    {"route": "GET /api/body/energy", "count": 2, "p50_ms": 2100,
                     "p95_ms": None, "max_ms": 2400, "disconnects": 1},
                    {"route": 5, "count": 1},
                    "not a row",
                ]
            }
        }  # fmt: skip
        preview = "count 4 p50_ms 900 p95_ms 3300 max_ms 3370 disconnects 0"
        energy = "count 2 p50_ms 2100 p95_ms null max_ms 2400 disconnects 1"
        self.assertEqual(
            prod_diagnostics.named_section_lines(report, "route_timings"),
            [
                ("info", f"route_timings.GET /api/context-preview {preview}"),
                ("info", f"route_timings.GET /api/body/energy {energy}"),
            ],
        )
        for broken in ({}, {"runtime": {}}, {"runtime": {"slowest_routes": None}}):
            with self.subTest(report=broken):
                self.assertEqual(
                    prod_diagnostics.named_section_lines(broken, "route_timings"),
                    [("FAIL", "section runtime.slowest_routes missing")],
                )

    def test_post_release_sections_print_counts_and_state_words_only(self):
        # Shapes as the report builds them, with planted text, ids and times.
        report = {
            "database": {
                "schema_version": 29,
                "messages": 40,
                # #4161: adopted local zones carrying a maximum.
                "adopted_zone_maximums": 2,
                "file_bytes": 4096,
                "wal_bytes": 512,
                "data_dir_free_bytes": 10**9,
                "stored_payload_bytes": {"garmin_state": 7},
                "workout_library_state": {"synced": 3, "note": "text never printed"},
                "workout_library_archive": {"waiting": 2, "last_purged": 1},
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
            "garmin_body_push": {
                "sent": 3,
                "removed": 0,
                "failed": 1,
                "retried": 1,
                "linked_days": 2,
                "pending_days": 1,
                "unlinked_uploads": 0,
                "day": "2026-11-15 never printed",
            },
            "competition_sync": {
                "status": "ok",
                "failures": 3,
                "failed_at": RECENT,
                "last_success_at": None,
                "failure_kinds": {"fetch.http_422": 1},
                "error": "provider text never printed",
            },
            "wahoo_weight_push": {
                "sent": 2,
                "failed": 1,
                "retried": 1,
                "pending": 0,
                "sent_kg": "80.4 never printed",
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
                "adopted_zone_maximums 2",
                "data_dir_free_bytes 1000000000", "file_bytes 4096",
                "messages 40", "schema_version 29",
                "stored_payload_bytes.garmin_state 7", "wal_bytes 512",
                "workout_library_archive.last_purged 1",
                "workout_library_archive.waiting 2",
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
            "garmin_body_push": [
                "failed 1", "linked_days 2", "pending_days 1", "removed 0",
                "retried 1", "sent 3", "unlinked_uploads 0",
            ],
            "wahoo_weight_push": [
                "failed 1", "pending 0", "retried 1", "sent 2",
            ],
            "competition_sync": [
                "failed_hours 2.0", "failure_kinds.fetch.http_422 1", "failures 3",
                "last_success_hours null", 'status "ok"',
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

    def test_wait_ready_polls_the_curve_build_until_ready_or_timeout(self):
        wait = ("--url", self.url, "--password-file", str(self.secret))
        FakeApp.state["curves"] = ["pending", "pending", "building", "ready"]
        with mock.patch.object(prod_diagnostics.time, "sleep") as slept:
            code, text = self.run_main(
                prod_diagnostics, *wait, "--wait-ready", "600",
                "--section", "curve_build",
            )  # fmt: skip
        self.assertEqual(code, 0, text)
        self.assertEqual(slept.call_count, 3)
        # A pending build is started once with the Analyse view's own read.
        self.assertEqual(FakeApp.state["gets"].count("/api/analysis/power-profiles"), 1)
        self.assertIn("ok curve build ready, waited", text)
        self.assertIn('info curve_build.status "ready"', text)
        self.assertIn("ok logout 200", text)
        # Still building when the time runs out: exit 3, logged out.
        FakeApp.state["curves"] = ["building"]
        clock = itertools.count(0, 20)
        with (
            mock.patch.object(prod_diagnostics.time, "sleep"),
            mock.patch.object(prod_diagnostics.time, "monotonic", lambda: next(clock)),
        ):
            code, text = self.run_main(prod_diagnostics, *wait, "--wait-ready", "60")
        self.assertEqual(code, 3, text)
        self.assertIn("FAIL curve build building, waited 60 s", text)
        self.assertIn("ok logout 200", text)
        self.assertNotIn("ok version", text)
        # A failed build stops at once.
        FakeApp.state["curves"] = ["failed"]
        code, text = self.run_main(prod_diagnostics, *wait, "--wait-ready", "60")
        self.assertEqual(code, 1, text)
        self.assertIn("FAIL curve build failed, waited 0 s", text)

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

    def test_missing_or_empty_secret_names_the_request_step(self):
        # An expired secret held a deploy for hours behind a bare "cannot read" (#3354).
        line = (
            "FAIL secret missing, empty or unreadable: "
            "request a new one with request_secret (file delivery)"
        )
        # The UI walk prints the same line.
        walk = (prod_client.ROOT / "scripts" / "ui_walk.mjs").read_text("utf-8")
        self.assertIn(line.removeprefix("FAIL "), walk)
        missing = Path(self.tmp.name) / "expired-secret"
        for module in (prod_diagnostics, prod_guards, prod_logs):
            code, text = self.run_main(
                module, "--url", self.url, "--password-file", str(missing)
            )
            self.assertEqual(code, 2)
            self.assertIn(line, text)
            self.assertNotIn("expired-secret", text)
        for content in (b"  \n", b"\xe4" + FAKE_LOGIN.encode()):
            self.secret.write_bytes(content)
            code, text = self.run_main(
                prod_diagnostics, "--url", self.url, "--password-file", str(self.secret)
            )
            self.assertEqual(code, 2)
            self.assertIn(line, text)

    def test_check_docs_fails_on_a_stale_production_line(self):
        # After schema v30 shipped, AGENTS.md still said v29 (#3351).
        root = Path(self.tmp.name) / "repo"
        source = root / "backend/db/schema.py"
        source.parent.mkdir(parents=True)

        def git(*args):
            identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
            subprocess.run(["git", *identity, "-C", str(root), *args], check=True,
                           capture_output=True)  # fmt: skip

        git("init", "-q")
        # Release 2.1.2 wrote schema 27; 2.1.3 and 2.1.4 wrote schema 28.
        for tag, schema in (("2.1.2", 27), ("2.1.3", 28), ("2.1.4", 28)):
            source.write_text(f"CURRENT_SCHEMA_VERSION = {schema}\n", "utf-8")
            git("add", "-A")
            git("commit", "-q", "--allow-empty", "-m", tag)
            git("tag", tag)
        skill_doc = ".agents/skills/pacecraft-db-migrations/SKILL.md"
        (root / skill_doc).parent.mkdir(parents=True)
        (root / "docs").mkdir()

        def docs(history, since, skill=True):
            (root / "docs/schema-history.md").write_text(history, "utf-8")
            line = f"Production\n  runs {since} (the only instance)"
            (root / "AGENTS.md").write_text(line, "utf-8")
            (root / skill_doc).unlink(missing_ok=True)
            if skill:
                (root / skill_doc).write_text(line.replace("\n ", ""), "utf-8")

        docs("| 26 | 2.0.5 |\n|27|  since 2.1.0 | \n| 28 | not released yet |\n",
             "v27 since release 2.1.0")  # fmt: skip
        self.assertEqual(
            prod_diagnostics.docs_lines(root, 27, "2.1.2"),
            [
                ("ok", "docs/schema-history.md: schema 27 row since 2.1.0"),
                ("ok", "AGENTS.md: production runs v27 since release 2.1.0"),
                ("ok", f"{skill_doc}: production runs v27 since release 2.1.0"),
            ],
        )
        # A hotfix 2.1.4 shipped before the docs PR: the change still names
        # 2.1.3, the first release of schema 28, and closes the 27 row.
        docs("| 27 | since 2.1.0 |\n| 28 | not released yet |\n",
             "v27 since release 2.1.0", skill=False)  # fmt: skip
        change = "change it to `Production runs v28 since release 2.1.3`"
        rows = "set `| 28 | since 2.1.3 |`; set `| 27 | 2.1.0 to 2.1.2 |`"
        self.assertEqual(
            prod_diagnostics.docs_lines(root, 28, "2.1.4"),
            [
                ("FAIL", f"docs/schema-history.md: schema 28 row stale; {rows}"),
                (
                    "FAIL",
                    f"AGENTS.md: production runs v27 since release 2.1.0; {change}",
                ),
                ("FAIL", f"{skill_doc}: production runs missing; {change}"),
            ],
        )
        # A schema that only one release wrote closes as that release.
        docs("| 27 | since 2.1.2 |\n| 28 | not released yet |\n",
             "v27 since release 2.1.2")  # fmt: skip
        self.assertTrue(
            prod_diagnostics.docs_lines(root, 28, "2.1.4")[0][1].endswith(
                "set `| 28 | since 2.1.3 |`; set `| 27 | 2.1.2 |`"
            )
        )
        # Two open rows are stale too.
        docs("| 27 | since 2.1.0 |\n| 28 | since 2.1.3 |\n", "v28 since release 2.1.3")
        self.assertEqual(prod_diagnostics.docs_lines(root, 28, "2.1.4")[0][0], "FAIL")
        # The fake app reports schema 27 and 2.1.2 against these docs.
        docs("| 27 | 2.1.0 to 2.1.2 |\n| 28 | since 2.1.3 |\n",
             "v28 since release 2.1.3")  # fmt: skip
        # A post-release run with --expect-schema checks the docs without the
        # flag: 2.1.38 to 2.1.43 left it out and stale lines went unseen (#3454).
        for flag in ("--check-docs", "--expect-schema=27"):
            with mock.patch.object(prod_diagnostics, "ROOT", root):
                code, text = self.run_main(
                    prod_diagnostics, "--url", self.url,
                    "--password-file", str(self.secret), flag,
                )  # fmt: skip
            self.assertEqual(code, 1, flag)
            self.assertIn(
                "FAIL AGENTS.md: production runs v28 since release 2.1.3; change it to "
                "`Production runs v27 since release 2.1.2`",
                text,
            )

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

    def test_the_app_address_comes_from_url_or_url_file(self):
        # #4125: --url-file keeps the address out of the command; it is never printed.
        url_file = Path(self.tmp.name) / "app-url.txt"
        url_file.write_text(f"  {self.url}\n", encoding="utf-8")
        secret = ("--password-file", str(self.secret))
        for module, extra in (
            (prod_guards, ()),
            (prod_diagnostics, ()),
            (prod_logs, ()),
        ):
            with self.subTest(module=module.__name__):
                code, text = self.run_main(
                    module, "--url-file", str(url_file), *secret, *extra
                )
                self.assertIn(code, (0, 1), text)
                self.assertNotIn(self.url, text)
        for content, why in (("", "one URL"), (f"{self.url}\n{self.url}\n", "one URL")):
            url_file.write_text(content, encoding="utf-8")
            code, text = self.run_main(
                prod_guards, "--url-file", str(url_file), *secret
            )
            self.assertEqual(code, 2)
            self.assertIn(why, text)
            self.assertNotIn(self.url, text)
        # A byte-order mark is dropped, as in the Node scripts (Gitar on #4127).
        url_file.write_text(f"﻿{self.url}\r\n", encoding="utf-8")
        self.assertEqual(prod_client.app_url(None, str(url_file)), self.url)
        # A file that is no UTF-8 text fails like any unreadable one.
        url_file.write_bytes(self.url.encode("utf-16"))
        code, text = self.run_main(prod_guards, "--url-file", str(url_file), *secret)
        self.assertEqual((code, "cannot read --url-file" in text), (2, True))
        # A file outside the scratchpad is refused; --url and --url-file exclude each other.
        with self.assertRaises(prod_client.ProdError):
            prod_client.app_url(None, str(Path(prod_client.ROOT) / "app-url.txt"))
        for module in (prod_guards, prod_diagnostics, prod_logs):
            with (
                self.subTest(module=module.__name__),
                self.assertRaises(SystemExit),
                redirect_stderr(io.StringIO()),
            ):
                module.main(["--url", self.url, "--url-file", str(url_file), *secret])

    def test_check_secret_reads_only_the_file(self):
        # #3768: checked before a release dispatch, without any request (a
        # Session would fail the test); run_main checks the secret is not printed.
        with (
            mock.patch.object(prod_guards, "Session", side_effect=AssertionError),
            mock.patch.object(
                prod_guards, "checkout_current", side_effect=AssertionError
            ),
        ):
            code, text = self.run_main(
                prod_guards, "--check-secret", "--password-file", str(self.secret)
            )
            self.assertEqual((code, text.strip()), (0, "ok secret readable"))
            empty = Path(self.tmp.name) / "empty-secret"
            empty.write_text("\n", encoding="utf-8")
            for path, reason in (
                (empty, "secret missing, empty or unreadable"),
                (
                    Path(self.tmp.name) / "missing",
                    "secret missing, empty or unreadable",
                ),
                # A refused path names itself, not a new secret request.
                (prod_client.ROOT / "x", "system temp directory or the T3 secret"),
                ("secret", "must be absolute"),
            ):
                with self.subTest(path=path):
                    code, text = self.run_main(
                        prod_guards, "--check-secret", "--password-file", str(path)
                    )
                    self.assertEqual(code, 1)
                    self.assertIn(reason, text)
        # The address and the secret check exclude each other; one is required.
        for argv in (
            ["--password-file", str(self.secret)],
            ["--url", self.url, "--check-secret", "--password-file", str(self.secret)],
        ):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                prod_guards.main(argv)

    def test_checks_warn_or_refuse_when_the_checkout_is_behind_develop(self):
        # A checkout one commit behind origin/develop, local state only (#3344).
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()

        def git(*args):
            subprocess.run(
                ["git", "-C", str(repo), *args], check=True, capture_output=True
            )

        git("init", "-q")
        for message in ("a", "b"):
            git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                "commit", "-q", "--allow-empty", "-m", message)  # fmt: skip
        git("update-ref", "refs/remotes/origin/develop", "HEAD")
        git("checkout", "-q", "--detach", "HEAD~1")
        login = ("--url", self.url, "--password-file", str(self.secret))
        fix = (
            "update it: git fetch origin develop && git switch --detach origin/develop"
        )
        self.checkout = repo
        code, text = self.run_main(prod_guards, *login)
        self.assertEqual(code, 0, text)
        self.assertIn(
            f"WARN this checkout is 1 commit(s) behind origin/develop; {fix}", text
        )
        code, text = self.run_main(prod_diagnostics, *login, "--require-current")
        self.assertEqual(code, 2, text)
        self.assertTrue(
            text.startswith("FAIL this checkout is 1 commit(s) behind"), text
        )
        self.assertNotIn("health", text)
        git("checkout", "-q", "--detach", "origin/develop")
        code, text = self.run_main(prod_guards, *login, "--require-current")
        self.assertEqual(code, 0, text)
        self.assertNotIn("checkout", text)
        # Without git state to compare, only --require-current refuses.
        self.checkout = Path(self.tmp.name)
        self.assertNotIn("checkout", self.run_main(prod_guards, *login)[1])
        code, text = self.run_main(prod_guards, *login, "--require-current")
        self.assertEqual(code, 2)
        self.assertIn("FAIL this checkout cannot be compared with origin/develop", text)

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
            '{"level":"ERROR","event":"Bad Event; drop table"}\n'
            # #3460: Coach tool failures per allow-listed tool and reason code,
            # else the exception type; other text prints as `?`.
            '{"level":"WARNING","event":"coach_tool_failed","context":{"tool":'
            '"read_nutrition","reason":"tool_arguments_invalid","type":"AppError"}}\n'
            '{"level":"WARNING","event":"coach_tool_failed","context":{"tool":'
            '"read_nutrition","reason":"tool_arguments_invalid"}}\n'
            '{"level":"WARNING","event":"coach_tool_failed","context":{"tool":'
            '"unknown","type":"KeyError"}}\n'
            '{"level":"WARNING","event":"coach_tool_failed","context":{"tool":'
            '"x; rm -rf","reason":"secret=1"}}\n'
            # #3903: a refused request binding counts by its rule's code.
            '{"level":"WARNING","event":"coach_tool_failed","context":{"tool":'
            '"save_nutrition_entry","reason":"request_invalid",'
            '"validation_reason":"request_provenance","type":"AppError"}}\n'
            # #3475: failed external requests per service, status and code; a host
            # or error text never prints.
            '{"level":"WARNING","event":"external_request_failed","context":{"service":'
            '"open-meteo-forecast-ecmwf","status":504,"error_code":"timeout",'
            '"error":"GET https://user:pw@example.invalid/x failed"}}\n'
            '{"level":"WARNING","event":"external_request_failed","context":{"service":'
            '"open-meteo-forecast-ecmwf","status":504,"error_code":"timeout"}}\n'
            '{"level":"WARNING","event":"external_request_failed","context":{"service":'
            '"api.example.invalid","error_type":"URLError"}}\n'
            '{"level":"ERROR","event":"upstream_network_error","context":{"service":'
            '"wahoo","error_code":"connection_reset","error_type":"OSError"}}\n',
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
        self.assertIn("  WARNING coach_tool_failed 5", text)
        self.assertIn(
            "coach tool failed save_nutrition_entry request_provenance 1", text
        )
        self.assertIn("coach tool failed read_nutrition tool_arguments_invalid 2", text)
        self.assertIn("coach tool failed unknown KeyError 1", text)
        self.assertIn("coach tool failed ? ? 1", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("rm -rf", text)
        self.assertIn(
            "external request failed open-meteo-forecast-ecmwf 504 timeout 2", text
        )
        self.assertIn("external request failed ? - URLError 1", text)
        self.assertIn("upstream network error wahoo - connection_reset 1", text)
        for private in ("example.invalid", "pw@", "https"):
            self.assertNotIn(private, text)
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
        "slowest_routes": "a list of route patterns, printed by --section route_timings (#3630)",
    },
    "garmin_sleep_temperature_keys": {
        "date_range": "athlete dates: never printed; the night counts carry the evidence (#4017)",
    },
}


# Top-level diagnostics blocks without a `--section` choice, each with its reason
# (#3430). Every other block needs SECTION_FIELDS or DERIVED_SECTIONS in
# scripts/prod_diagnostics.py, so the post-release check can read it.
_DEFAULT_RUN = "read by the default check run (no --section)"
_EXPORT_ONLY = "read only in the full export; no --section yet (before #3430)"
NOT_SECTIONED: dict[str, str] = {
    "app": _DEFAULT_RUN,
    "sync": _DEFAULT_RUN,
    "plan_sync": _DEFAULT_RUN,
    "external_calendar": _DEFAULT_RUN,
    "generated_at": "the report's own time, not a count",
    "note": "free text",
    "coach_commands": "summed by --section coach_local_first",
    **dict.fromkeys(
        (
            "activity_routes",
            "activity_weather",
            "canonical_coverage",
            "canonical_provider_composition",
            "canonical_read",
            "canonical_session_conflicts",
            "configuration",
            "debug_capture",
            "garmin",
            "garmin_change_check",
            "garmin_detail_fallback",
            "local_load",
            "logs",
            "lt1_ride_coverage",
            "morning_checkin",
            "openai",
            "profile_weekly_plan_removal",
            "provider_freshness",
            "session_analysis",
            "session_focus_backfill",
            "session_metrics",
            "session_rebundle",
            "wahoo",
        ),
        _EXPORT_ONLY,
    ),
}


class SectionFieldsCoverTheProducersTests(ServerTestCase):
    """A new diagnostics key fails here until prod_diagnostics prints or names it (#3131)."""

    def test_every_block_the_report_emits_has_its_section(self):
        # #3430: energy_balance and garmin_body_push shipped without a section,
        # so the post-release check could not read the counts they were for.
        report = server.DIAGNOSTICS.report_service().report()
        sectioned = set(prod_diagnostics.SECTION_FIELDS)
        missing = sorted(set(report) - sectioned - set(NOT_SECTIONED))
        self.assertEqual(
            missing,
            [],
            "diagnostics blocks without a --section: add each to SECTION_FIELDS in "
            "scripts/prod_diagnostics.py (with its test), or to NOT_SECTIONED in "
            "this test with the reason",
        )
        # A named exception that the report no longer emits, or that now has its
        # section, is stale and leaves the list.
        self.assertEqual(sorted(set(NOT_SECTIONED) - set(report)), [])
        self.assertEqual(sorted(set(NOT_SECTIONED) & sectioned), [])
        # The check itself: a fixture block without a section is reported.
        self.assertEqual(
            sorted({"new_block", *report} - sectioned - set(NOT_SECTIONED)),
            ["new_block"],
        )

    def test_coach_local_first_sums_the_real_history_projection(self):
        # #3080: the backend's names and shapes, so a rename fails here
        # instead of printing zeros.
        self.assertEqual(prod_diagnostics.LOOKUP_OUTCOMES, LOOKUP_OUTCOMES)

        def refused(tool: str, reason: str) -> dict:
            try:
                raise AppError(409, "Schon gespeichert.", reason=reason)
            except AppError as exc:
                metadata = coach_error_metadata(exc, Path(server.__file__).parent)
            return {
                "tool": tool,
                "result": {"ok": False, "reason": reason},
                "diagnostic_error": metadata,
            }

        lookup = {
            "ok": True,
            "results": [
                {"query": "Skyr", "lookup_outcome": "local_hit"},
                {"query": "Banane", "lookup_outcome": "external_requested"},
            ],
        }
        receipt = {
            "command_receipts": [
                {
                    "tool": "lookup_food",
                    "result": compact_tool_result("lookup_food", lookup),
                },
                {
                    "tool": "save_competition",
                    "result": {"ok": True, "created_despite_candidate": 1},
                },
                refused("manage_training_templates", "template_exists"),
                refused("manage_training_templates", "template_exists_in_call"),
                refused("save_competition", "competition_exists"),
            ]
        }
        steps = server.DIAGNOSTICS.coach_history_service()._command_steps(receipt)
        self.assertEqual(
            {
                "commands": 1,
                "lookup_outcomes.local_hit": 1,
                "lookup_outcomes.external_after_miss": 0,
                "lookup_outcomes.external_requested": 1,
                "created_despite_candidate.manage_training_templates": 0,
                "created_despite_candidate.save_competition": 1,
                "refusals.template_exists": 1,
                "refusals.template_exists_in_call": 1,
                "refusals.competition_exists": 1,
            },
            prod_diagnostics.local_first_counts([{"steps": steps}, "not a command"]),
        )

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
