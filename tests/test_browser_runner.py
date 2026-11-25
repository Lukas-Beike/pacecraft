"""Synthetic orchestration checks: never invoke Docker or connected providers."""

import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from contextlib import redirect_stderr
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "browser_runner", ROOT / "scripts/run_browser_tests.py"
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def report(files=("a.spec.js", "b.spec.js", "c.spec.js")):
    return {
        "errors": [],
        "suites": [
            {
                "specs": [
                    {
                        "file": file,
                        "id": "id-" + file,
                        "title": file,
                        "line": 1,
                        "tests": [{"projectName": "desktop", "projectId": "desktop"}],
                    }
                    for file in files
                ]
            }
        ],
    }


def identities(children):
    return Counter(
        json.dumps(identity, sort_keys=True)
        for child in children
        for identity in child["identities"]
    )


class FakeProcesses:
    """Docker's observed-ID/label protocol, with deterministic failure injection."""

    def __init__(
        self,
        fail_browser=False,
        interrupt=False,
        partial_start=False,
        cleanup_error=False,
        listing=None,
        engine_down=False,
    ):
        self.listing = listing or report()
        # Docker Desktop's engine answering 500 while it is stuck starting (#3304).
        self.engine_down = engine_down
        self.stop = threading.Event()
        self.commands = []
        self.ids = {}
        self.removed = []
        self.fail_browser, self.interrupt = fail_browser, interrupt
        self.partial_start, self.cleanup_error = partial_start, cleanup_error
        self.active = self.maximum = 0
        self.lock = threading.Lock()
        self.stopped = False
        self.overlap = None
        self.errors = threading.local()
        # Containers of other runs by run id, as "<id> <name>" rows (#2721).
        self.other_runs = {}

    def stop_all(self):
        self.stopped = True
        self.stop.set()

    def run(self, command, **kwargs):
        with self.lock:
            self.commands.append(command)
        if command[:2] == ["docker", "create"]:
            cid = "%064x" % (len(self.ids) + 1)
            label = command[command.index("--label") + 1].split("=", 1)[1]
            self.ids[cid] = label
            Path(command[command.index("--cidfile") + 1]).write_text(cid)
            if self.partial_start and "python" in command:
                return 9, "discard synthetic sensitive output"
            return 0, cid
        if command[:2] == ["docker", "inspect"]:
            if command[3] == "{{.State.ExitCode}}":
                return 0, "0"
            if self.cleanup_error and self.stopped:
                return 2, "never saved"
            return 0, self.ids[command[-1]]
        if command[:2] == ["docker", "rm"]:
            self.removed.append(command[-1])
            return 0, ""
        if command[:2] == ["docker", "version"]:
            if self.engine_down:
                return 1, (
                    "request returned 500 Internal Server Error for API route and "
                    "version http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.56/"
                    "version, check if the server supports the requested API version"
                )
            return 0, "29.9.3"
        if command[:2] == ["docker", "ps"]:
            run_id = command[command.index("--filter") + 1].rsplit("=", 1)[1]
            return 0, self.other_runs.get(run_id, "")
        if command[:2] == ["docker", "start"]:
            return 0, ""
        if command[:2] == ["docker", "logs"]:
            # A container's stderr arrives on docker's own stderr.
            self.errors.last = (
                "Error: fixture stderr line " + runner.FAKE_OPENAI_KEY
            ).encode()
            return 0, "Traceback: fixture import failed\n" + runner.FAKE_PASSWORD
        if command[0] == "ip":
            return 0, ""
        if "--version" in command:
            return 0, "v24.22.1"
        if "--list" in command:
            return 0, json.dumps(self.listing)
        if command[:2] == ["git", "rev-parse"]:
            return 0, "c7ff60519520d553a00bfd33f6cf884b8de879b8"
        if command[:2] == ["git", "status"]:
            return 0, " M scripts/run_browser_tests.py"
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            overlap = self.overlap
        try:
            if self.interrupt:
                raise InterruptedError
            if overlap is not None:
                overlap.wait()
            self.stop.wait(0.03)
            return (
                7 if self.fail_browser else 0
            ), "never retained session-bearing output"
        finally:
            with self.lock:
                self.active -= 1


class BrowserRunnerTests(unittest.TestCase):
    def test_lpt_frozen_median_new_files_and_ties(self):
        files = ["e2e/a", "e2e/b", "e2e/c", "e2e/new"]
        weights = {"e2e/a": 10, "e2e/b": 8, "e2e/c": 2}
        expected = [["e2e/a", "e2e/c"], ["e2e/b", "e2e/new"]]
        self.assertEqual(runner.assign_files(files, weights, 2), expected)
        self.assertEqual(runner.assign_files(reversed(files), weights, 2), expected)
        self.assertEqual(
            runner.assign_files(files, {}, 2),
            [["e2e/a", "e2e/c"], ["e2e/b", "e2e/new"]],
        )

    def test_malformed_weights_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "durations.json"
            for value in (
                None,
                [],
                {},
                {"version": 2, "projects": {}},
                {"version": 1, "projects": {"desktop": {"e2e/a": True}}},
                {"version": 1, "projects": {"desktop": {"e2e/a": -1}}},
                {"version": 1, "projects": {"desktop": {"e2e/a": float("nan")}}},
            ):
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    with self.assertRaises(runner.RunnerError):
                        runner.read_weights(path)

    def test_bare_spec_name_resolves_to_its_single_listed_path(self):
        listing = runner.parse_listing(report())
        timing = runner.read_weights(None)
        (child,) = runner.select(listing, "desktop", ["b.spec.js"], 1, 1, timing)
        self.assertEqual(child["file"], "e2e/b.spec.js")
        with self.assertRaisesRegex(runner.RunnerError, "e2e/<name>.spec.js"):
            runner.select(listing, "desktop", ["absent.spec.js"], 1, 1, timing)

    def test_changed_project_follows_the_ci_classifier(self):
        # --project changed reuses .github/scripts/ci_changes.py (#2759, #2748).
        isolated = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        with (
            mock.patch.dict(os.environ, isolated),
            tempfile.TemporaryDirectory() as directory,
        ):
            root = Path(directory)

            def git(*args):
                subprocess.run(
                    ["git", "-C", str(root), *args], check=True, capture_output=True
                )

            git("init", "-q", "-b", "base")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            (root / "server.py").write_text("", encoding="utf-8")
            git("add", ".")
            git("commit", "-q", "-m", "base")
            git("switch", "-q", "-c", "work")
            self.assertEqual(runner.changed_project("base", root)[0], "desktop")
            (root / "docs").mkdir()
            (root / "docs" / "notes.md").write_text("", encoding="utf-8")
            # Documentation alone: CI runs no browser project, so none runs.
            self.assertIsNone(runner.changed_project("base", root)[0])
            (root / "server.py").write_text("x = 1\n", encoding="utf-8")
            self.assertEqual(runner.changed_project("base", root)[0], "desktop")
            # An untracked client file counts as a UI change.
            (root / "public").mkdir()
            (root / "public" / "app.js").write_text("", encoding="utf-8")
            project, reason = runner.changed_project("base", root)
            self.assertEqual(project, "all")
            self.assertIn("public/app.js", reason)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.main(["--project", "desktop", "--base", "origin/main"])

    def test_today_names_the_specs_whose_browser_clock_it_does_not_pin(self):
        # #3410: --today pinned only the fixture day of a spec without the helper,
        # and the browser showed the real day (#3371, #3399).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "e2e").mkdir()
            (root / "e2e/week.spec.js").write_text(
                'const { test } = require("./helpers/today");\n', encoding="utf-8"
            )
            (root / "e2e/plain.spec.js").write_text(
                'const { test } = require("@playwright/test");\n', encoding="utf-8"
            )
            children = [
                {"file": "e2e/week.spec.js"},
                {"file": "e2e/plain.spec.js"},
                {"file": "e2e/plain.spec.js"},
            ]
            with redirect_stderr(io.StringIO()) as err:
                runner.note_unpinned_specs(date(2026, 11, 16), children, root)
            self.assertEqual(
                err.getvalue(),
                "--today pins only the fixture day for e2e/plain.spec.js; "
                "its browser clock shows the real day\n",
            )
            # Without --today nothing is said.
            with redirect_stderr(io.StringIO()) as err:
                runner.note_unpinned_specs(None, children, root)
            self.assertEqual(err.getvalue(), "")

    def test_changed_specs_that_read_the_weekday_are_found(self):
        # #3324: a changed spec importing e2e/helpers/today.js reads the weekday.
        isolated = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        week = 'const { test } = require("./helpers/today");\n'
        with (
            mock.patch.dict(os.environ, isolated),
            tempfile.TemporaryDirectory() as directory,
        ):
            root = Path(directory)

            def git(*args):
                subprocess.run(
                    ["git", "-C", str(root), *args], check=True, capture_output=True
                )

            (root / "e2e").mkdir()
            (root / "e2e/kept.spec.js").write_text(week, encoding="utf-8")
            git("init", "-q", "-b", "base")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            git("add", ".")
            git("commit", "-q", "-m", "base")
            (root / "e2e/week.spec.js").write_text(week, encoding="utf-8")
            plain = 'const { test } = require("@playwright/test");\n'
            (root / "e2e/plain.spec.js").write_text(plain, encoding="utf-8")
            self.assertEqual(runner.weekday_specs("base", root), ["e2e/week.spec.js"])
            # #4210: a changed spec computing a date runs pinned with the helper, and
            # without it is refused, naming the spec and the fix.
            dated = "addDateKey(planWeekStart(today), 7);\n"
            (root / "e2e/dated.spec.js").write_text(week + dated, encoding="utf-8")
            self.assertEqual(
                runner.weekday_specs("base", root),
                ["e2e/dated.spec.js", "e2e/week.spec.js"],
            )
            (root / "e2e/dated.spec.js").write_text(plain + dated, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"e2e/dated\.spec\.js computes a date"):
                runner.weekday_specs("base", root)
            (root / "e2e/dated.spec.js").unlink()
            # #3610: without the base ref the weekday runs are skipped, and the
            # line says it does not affect the run, so it is not read as its cause.
            args = runner.parser().parse_args(["--base", "refs/missing"])
            with redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual([], runner.weekday_runs(args))
            self.assertIn(
                "Weekday runs skipped (this run is unaffected): cannot diff "
                "against refs/missing",
                stderr.getvalue(),
            )

    def test_a_changed_weekday_spec_lists_and_runs_on_every_other_weekday(self):
        # #3324: as CI's pinned-day step, after the real day's run. #4210: a
        # Wednesday-only failure passed the Monday and Sunday runs, so every other
        # day of this week is pinned; the real day's run is today's.
        fake = FakeProcesses()
        weekday = ["e2e/a.spec.js"]
        self.run_main(fake, list_only=True, weekday=weekday)
        listed = json.loads(self.stdout)["weekday_runs"]
        days = [date.fromisoformat(run["today"]) for run in listed]
        today = datetime.now(UTC).date()
        self.assertEqual(
            [day.weekday() for day in days],
            [n for n in range(7) if n != today.weekday()],
        )
        self.assertEqual({day.isocalendar()[:2] for day in days}, {today.isocalendar()[:2]})
        self.assertEqual(
            [(run["project"], run["specs"]) for run in listed],
            [("desktop", weekday)] * 6,
        )
        # A selection without the changed weekday spec: one run, as before.
        self.run_main(
            fake, list_only=True, weekday=weekday, extra_args=("--spec", "b.spec.js")
        )
        self.assertEqual(json.loads(self.stdout)["weekday_runs"], [])
        code, values = self.run_main(FakeProcesses(), weekday=weekday)
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(
            sorted((value["selection"]["today"] or "") for value in values),
            ["", *(run["today"] for run in listed)],
        )
        pinned = [value for value in values if value["selection"]["today"]]
        self.assertEqual(
            [[c["file"] for c in value["children"]] for value in pinned],
            [weekday] * 6,
        )
        self.assertIn("Weekday runs (#3324): real day exit 0; ", self.stderr)
        # An explicit --today is one pinned run already.
        self.run_main(
            fake, list_only=True, weekday=weekday, extra_args=("--today", "2026-11-18")
        )
        self.assertEqual(json.loads(self.stdout)["weekday_runs"], [])
        # Gitar on #3327: a spec the (--grep filtered) desktop listing lacks gets
        # no pinned run, so a passing focused run stays passing.
        others = FakeProcesses(listing=report(("b.spec.js",)))
        code, values = self.run_main(
            others, weekday=weekday, extra_args=("--grep", "settings")
        )
        self.assertEqual(
            (code, [value["selection"]["today"] for value in values]),
            (0, [None]),
            self.stderr,
        )
        self.assertNotIn("Weekday runs", self.stderr)

    def test_spec_a_project_does_not_list_is_skipped_with_a_note(self):
        data = report()
        data["suites"][0]["specs"][0]["tests"].append(
            {"projectName": "mobile", "projectId": "mobile"}
        )
        listing = runner.parse_listing(data)
        timing = runner.read_weights(None)
        specs = ["e2e/a.spec.js", "e2e/b.spec.js"]
        with redirect_stderr(io.StringIO()) as err:
            children = runner.select(listing, "all", specs, 1, 1, timing)
        self.assertEqual(
            sorted((c["project"], c["file"]) for c in children),
            [
                ("desktop", "e2e/a.spec.js"),
                ("desktop", "e2e/b.spec.js"),
                ("mobile", "e2e/a.spec.js"),
            ],
        )
        self.assertEqual(
            err.getvalue(), "Skipped e2e/b.spec.js on mobile: not listed\n"
        )

    def test_invalid_and_zero_selection_fail(self):
        listing = runner.parse_listing(report())
        timing = runner.read_weights(None)
        for project, specs, shard, total in (
            ("missing", [], 1, 1),
            ("desktop", ["e2e/absent"], 1, 1),
            ("desktop", [], 0, 1),
            ("desktop", [], 2, 1),
            ("desktop", [], 1, 0),
            ("desktop", [], 4, 4),
        ):
            with (
                self.subTest(project=project, specs=specs, shard=shard, total=total),
                self.assertRaises(runner.RunnerError),
            ):
                runner.select(listing, project, specs, shard, total, timing)
        with self.assertRaises(runner.RunnerError):
            runner.parse_listing({"errors": [{"message": "discard"}]})

    def test_atomic_write_retains_previous_manifest_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            runner.atomic_json(path, {"status": "incomplete"})
            with (
                mock.patch.object(Path, "replace", side_effect=OSError),
                self.assertRaises(OSError),
            ):
                runner.atomic_json(path, {"status": "passed"})
            self.assertEqual(json.loads(path.read_text()), {"status": "incomplete"})

    def test_ports_reserved_disjoint_and_collision_is_not_modified(self):
        first, second = runner.reserve_ports(2), runner.reserve_ports(2)
        try:
            ports = [sock.getsockname()[1] for sock in first + second]
            self.assertEqual(len(set(ports)), 4)
            with socket.socket() as conflicting, self.assertRaises(OSError):
                conflicting.bind(("127.0.0.1", ports[0]))
            self.assertEqual(first[0].getsockname()[1], ports[0])
        finally:
            for sock in first + second:
                sock.close()

    def run_main(
        self,
        fake,
        jobs=1,
        list_only=False,
        extra_args=(),
        current=True,
        ready=None,
        weekday=(),
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        args = [
            "--project",
            "desktop",
            "--jobs",
            str(jobs),
            "--results-dir",
            temporary.name,
        ]
        args.extend(extra_args)
        if list_only:
            args.append("--list")
        # Each run makes its own Processes; a weekday run gets a fresh fake (#3324).
        fakes = iter([fake])
        with (
            mock.patch.object(
                runner,
                "Processes",
                side_effect=lambda: (
                    next(fakes, None) or FakeProcesses(listing=fake.listing)
                ),
            ),
            mock.patch.object(runner, "LOCK_DIR", Path(temporary.name) / "locks"),
            mock.patch.object(
                runner,
                "toolchain",
                return_value=("node", Path("cli.js"), {"@playwright/test": "1.64.1"}),
            ),
            mock.patch.object(
                runner, "image_identity", return_value="sha256:" + "a" * 64
            ),
            mock.patch.object(
                runner,
                "image_matches_source",
                return_value=current,
                side_effect=current if isinstance(current, list) else None,
            ),
            mock.patch.object(
                runner, "verify_browser", return_value={"mode": "native"}
            ),
            mock.patch.object(runner, "ready", side_effect=ready),
            # The checkout's own diff never adds weekday runs here (#3324).
            mock.patch.object(runner, "weekday_specs", return_value=list(weekday)),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            code = runner.main(args)
        self.stdout, self.stderr = stdout.getvalue(), stderr.getvalue()
        manifests = list(Path(temporary.name).glob("*/manifest.json"))
        if weekday and not list_only:
            return code, [json.loads(path.read_text()) for path in manifests]
        self.assertEqual(len(manifests), 1)
        return code, json.loads(manifests[0].read_text()), manifests[0]

    def test_a_run_holds_its_lock_during_the_run_and_removes_it_after(self):
        fake = FakeProcesses()
        code, value, manifest = self.run_main(fake)
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(value["stale_containers_removed"], 0)
        locks = manifest.parent.parent / "locks"
        self.assertEqual(list(locks.glob("*.lock")), [])

    def test_list_does_not_start_containers_and_never_reports_passed(self):
        fake = FakeProcesses()
        code, value, _ = self.run_main(fake, list_only=True)
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "listed")
        self.assertFalse(fake.ids)
        self.assertTrue(
            all(child["status"] == "pending" for child in value["children"])
        )
        # #3094: --grep also filters the listing, so selection matches what runs.
        listings = [c for c in fake.commands if "--list" in c]
        self.assertNotIn("--grep", listings[0])
        fake = FakeProcesses()
        self.run_main(fake, list_only=True, extra_args=("--grep", "queued"))
        listing = next(c for c in fake.commands if "--list" in c)
        self.assertEqual(listing[-2:], ["--grep", "queued"])

    def test_serial_and_two_lane_bounded_fresh_fixture_resources(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                fake = FakeProcesses()
                if jobs == 2:
                    fake.overlap = threading.Barrier(
                        2,
                        action=lambda fake=fake: setattr(fake, "overlap", None),
                        timeout=5,
                    )
                code, value, _ = self.run_main(fake, jobs=jobs)
                self.assertEqual(code, 0)
                self.assertEqual(value["status"], "passed")
                self.assertEqual(len(value["children"]), 3)
                self.assertEqual(fake.maximum, jobs)
                self.assertEqual(set(fake.removed), set(fake.ids))
                commands = fake.commands
                fixtures = [
                    item
                    for item in commands
                    if item[:2] == ["docker", "create"] and "python" in item
                ]
                self.assertEqual(len(fixtures), 3)
                self.assertEqual(
                    len({item[item.index("--name") + 1] for item in fixtures}), 3
                )
                for item in fixtures:
                    self.assertIn("100:101", item)
                    self.assertIn("--read-only", item)
                    self.assertIn("AI_COACH_DISPOSABLE_FIXTURE=1", item)
                    self.assertFalse(any("FIXTURE_TODAY" in str(word) for word in item))
                    self.assertIn("/data:uid=100,gid=101,mode=0700", item)
                    self.assertTrue(
                        item[item.index("--network") + 1].startswith("container:")
                    )
                network_creates = [
                    index
                    for index, item in enumerate(commands)
                    if item[:2] == ["docker", "create"] and "sleep" in item
                ]
                browsers = [
                    index
                    for index, item in enumerate(commands)
                    if item[0] == "node" and "test" in item and "--list" not in item
                ]
                self.assertLess(max(network_creates), min(browsers))
                self.assertEqual(len(value["resources"]["ports"]), jobs * 2)
        # #3200: --today pins every fixture's athlete day.
        fake = FakeProcesses()
        code, _, _ = self.run_main(fake, extra_args=("--today", "2026-11-15"))
        self.assertEqual(code, 0)
        fixtures = [
            c for c in fake.commands if c[:2] == ["docker", "create"] and "python" in c
        ]
        self.assertTrue(fixtures)
        for item in fixtures:
            self.assertEqual(item[item.index("FIXTURE_TODAY=2026-11-15") - 1], "--env")

    def test_failed_child_survives_cleanup_failure_and_does_not_save_raw_output(self):
        fake = FakeProcesses(fail_browser=True, cleanup_error=True)
        code, value, path = self.run_main(fake)
        self.assertNotEqual(code, 0)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["children"][0]["exit_code"], 7)
        self.assertTrue(value["cleanup_errors"])
        text = path.read_text() + "".join(
            p.read_text() for p in path.parent.rglob("process.txt")
        )
        for forbidden in (
            runner.FAKE_PASSWORD,
            "state.json",
            "session-bearing",
            "E2E_APP_PASSWORD",
        ):
            self.assertNotIn(forbidden, text)

    def test_host_network_error_needs_every_failing_test_to_name_it(self):
        marker = '"console: Failed to load resource: net::ERR_NO_BUFFER_SPACE"\n'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.assertIsNone(runner.host_network_error(output))

            def report(name, text):
                path = output / "artifacts" / name / "error-context.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            report("first", marker)
            self.assertEqual(
                "net::ERR_NO_BUFFER_SPACE", runner.host_network_error(output)
            )
            report("second", "expect(received).toEqual(expected)\n")
            self.assertIsNone(runner.host_network_error(output))

    def test_host_network_error_reads_only_per_test_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            nested = output / "report" / "data" / "error-context.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("net::ERR_NO_BUFFER_SPACE\n", encoding="utf-8")
            self.assertIsNone(runner.host_network_error(output))
            outside = output / "outside.md"
            outside.write_text("net::ERR_NO_BUFFER_SPACE\n", encoding="utf-8")
            link = output / "artifacts" / "case" / "error-context.md"
            link.parent.mkdir(parents=True)
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks are not available")
            self.assertIsNone(runner.host_network_error(output))

    def test_host_network_failure_is_reported_as_infrastructure(self):
        fake = FakeProcesses(fail_browser=True)
        with mock.patch.object(
            runner, "host_network_error", return_value="net::ERR_NO_BUFFER_SPACE"
        ):
            code, value, _path = self.run_main(fake)
        self.assertNotEqual(code, 0)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(
            value["children"][0]["infrastructure_error"], "net::ERR_NO_BUFFER_SPACE"
        )
        self.assertIn("host network error", value["error"])
        self.assertIn("--project desktop", value["error"])

    def test_failed_tests_report_location_and_first_error_line(self):
        marker = "synthetic-fixture-value"
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(runner, "FAKE_PASSWORD", marker),
        ):
            output = Path(directory)
            self.assertEqual([], runner.failed_tests(output))
            path = output / "artifacts" / "case" / "error-context.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Test info\n\n- Name: login >> opens chat\n"
                "- Location: e2e/login.spec.js:12:3\n\n# Error details\n\n```\n"
                f"Error: fill {marker} failed\n"
                "second line stays out\n```\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [
                    {
                        "location": "e2e/login.spec.js:12:3",
                        "error": "Error: fill [redacted] failed",
                    }
                ],
                runner.failed_tests(output),
            )
            # #3161: a long worktree put the report past Windows' 260-character MAX_PATH.
            base = output.resolve()
            if os.name == "nt":  # only the extended form can create the deep folder
                base = Path("\\\\?\\" + str(base))
            # 240 characters: under every file system's 255 per name, past 260 in total.
            deep = base / "artifacts" / ("long-test-title-" * 15)
            deep.mkdir()
            (deep / "error-context.md").write_text(
                "- Location: e2e/long.spec.js:1:1\n\n# Error details\n\nError: deep\n",
                encoding="utf-8",
            )
            report = output.resolve() / "artifacts" / deep.name / "error-context.md"
            self.assertGreater(len(str(report)), 260)
            found = runner.failed_tests(output)
            # The temporary directory's own cleanup cannot reach past MAX_PATH either.
            (deep / "error-context.md").unlink()
            deep.rmdir()
            self.assertIn(
                {"location": "e2e/long.spec.js:1:1", "error": "Error: deep"}, found
            )

    def test_failed_child_surfaces_first_error_in_output_and_manifest(self):
        failure = {"location": "e2e/a.spec.js:3:1", "error": "Error: expected 1"}
        fake = FakeProcesses(fail_browser=True)
        with mock.patch.object(runner, "failed_tests", return_value=[failure] * 2):
            code, value, _path = self.run_main(fake)
        self.assertNotEqual(code, 0)
        self.assertEqual([failure] * 2, value["children"][0]["failures"])
        self.assertIn("e2e/a.spec.js:3:1: Error: expected 1", value["error"])
        self.assertIn("+1 more", value["error"])
        self.assertIn("Browser run failed: " + value["error"], self.stderr)
        self.assertNotIn("session-bearing", self.stderr)

    def test_the_failure_line_names_the_first_error_report(self):
        # #4158: the full report is one open away; the 300-character line stays as it is.
        child = {"project": "mobile", "file": "e2e/a.spec.js"}
        failure = {"location": "e2e/a.spec.js:3:1", "error": "Error: x"}
        report = Path("out") / "artifacts" / "a" / "error-context.md"
        message = runner.failure_message(child, 1, [failure], report)
        self.assertTrue(message.endswith(f"; report: {report}"))
        extended = runner.failure_message(
            child, 1, [failure], "\\\\?\\C:\\r\\error-context.md"
        )
        self.assertTrue(extended.endswith("; report: C:\\r\\error-context.md"))
        self.assertNotIn("report:", runner.failure_message(child, 1, [failure]))
        # The path is the first readable report's, the one the shown error comes from.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.assertIsNone(runner.first_report(output))
            for name in ("a", "b"):
                path = output / "artifacts" / name / "error-context.md"
                path.parent.mkdir(parents=True)
                path.write_text("- Location: e2e/x.spec.js:1:1\n", encoding="utf-8")
            unreadable = output / "artifacts" / "a" / "error-context.md"
            original = Path.read_bytes

            def read_bytes(path):
                if path.name == "error-context.md" and path.parent.name == "a":
                    raise OSError("locked")
                return original(path)

            with mock.patch.object(Path, "read_bytes", read_bytes):
                self.assertEqual("b", runner.first_report(output).parent.name)
            self.assertEqual(unreadable.name, runner.first_report(output).name)

    def test_first_error_line_stays_inside_the_error_block(self):
        self.assertEqual("", runner.first_error_line(["# Test info"]))
        for block in (
            ["# Error details", "", "```", "```", "# Page snapshot", "- generic"],
            ["# Error details", "", "# Page snapshot", "- generic"],
        ):
            self.assertEqual("", runner.first_error_line(block))

    def test_retry_reports_of_one_failure_are_listed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("case", "case-retry1"):
                path = output / "artifacts" / name / "error-context.md"
                path.parent.mkdir(parents=True)
                path.write_text(
                    "- Location: e2e/a.spec.js:3:1\n# Error details\n```\nError: x\n",
                    encoding="utf-8",
                )
            self.assertEqual(1, len(runner.failed_tests(output)))

    def test_keep_going_runs_every_child_and_reports_all_failures(self):
        code, value, _path = self.run_main(
            FakeProcesses(fail_browser=True), extra_args=["--keep-going"]
        )
        self.assertNotEqual(code, 0)
        statuses = [child["status"] for child in value["children"]]
        self.assertEqual(statuses, ["failed"] * 3)
        self.assertTrue(all(child.get("exit_code") for child in value["children"]))
        self.assertIn("3 browser children failed", value["error"])

    def test_without_keep_going_the_first_failure_stops_the_run(self):
        code, value, _path = self.run_main(FakeProcesses(fail_browser=True))
        self.assertNotEqual(code, 0)
        ran = [child for child in value["children"] if child.get("exit_code")]
        self.assertEqual(len(ran), 1)

    def test_child_without_test_report_says_it_failed_outside_a_test(self):
        code, value, _path = self.run_main(FakeProcesses(fail_browser=True))
        self.assertNotEqual(code, 0)
        self.assertEqual([], value["children"][0]["failures"])
        self.assertIn("no test report", self.stderr)

    def test_outdated_application_image_stops_before_any_fixture(self):
        # A stale image would run new specs against old app code (#2644).
        fake = FakeProcesses()
        code, value, _path = self.run_main(fake, current=False)
        self.assertNotEqual(code, 0)
        self.assertEqual(value["status"], "failed")
        self.assertIn(
            f"docker build -t {runner.worktree_image_tag(runner.ROOT)} .",
            value["error"],
        )
        # #3737: the stale-image error names --build too.
        self.assertIn("or pass --build", value["error"])
        self.assertFalse(fake.ids)
        # The manifest still names the rejected image and never reached the browser.
        self.assertIs(False, value["images"]["application_matches_source"])
        self.assertNotIn("browser", value["images"])
        # An image that cannot be compared stops before any fixture too (#3243);
        # its children would otherwise stay "pending" without an error line.
        fake = FakeProcesses()
        code, value, _path = self.run_main(fake, current=None)
        self.assertNotEqual(code, 0)
        self.assertIn("could not compare", value["error"])
        self.assertIn("--build", value["error"])
        self.assertFalse(fake.ids)
        # A current image runs silently, and --build rebuilds one it cannot compare.
        unknown = FakeProcesses()
        code, _value, _path = self.run_main(
            unknown, current=[None, True], extra_args=("--build",)
        )
        self.assertEqual(code, 0, self.stderr)
        self.assertIn(
            ["docker", "build", "-t", runner.worktree_image_tag(runner.ROOT), "."],
            unknown.commands,
        )
        self.run_main(FakeProcesses(), current=True)
        self.assertNotIn("Warning", self.stderr)
        build = ["docker", "build", "-t", runner.worktree_image_tag(runner.ROOT), "."]
        self.assertNotIn(build, fake.commands)
        # #3106: --build rebuilds a stale image, checks it again and runs.
        fake = FakeProcesses()
        code, value, _path = self.run_main(
            fake, current=[False, True], extra_args=("--build",)
        )
        self.assertEqual(code, 0, self.stderr)
        self.assertIn(build, fake.commands)
        self.assertIs(True, value["images"]["application_matches_source"])
        # A build that still does not match stops before any fixture.
        fake = FakeProcesses()
        code, value, _path = self.run_main(
            fake, current=[False, False], extra_args=("--build",)
        )
        self.assertNotEqual(code, 0)
        self.assertIn(build, fake.commands)
        self.assertIn("does not match", value["error"])
        self.assertNotIn("pass --build", value["error"])  # #3737: already passed
        self.assertFalse(fake.ids)
        # A current image is never rebuilt.
        fake = FakeProcesses()
        self.run_main(fake, current=True, extra_args=("--build",))
        self.assertNotIn(build, fake.commands)

    def test_an_engine_that_does_not_answer_stops_first_with_the_fix(self):
        # #3304: Docker Desktop stuck "starting" answers every call with 500;
        # the runner says so and names the fix before any other Docker call.
        fake = FakeProcesses(engine_down=True)
        code, value, _path = self.run_main(fake, current=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(value["status"], "failed")
        self.assertIn("Docker engine does not answer", value["error"])
        self.assertIn("wsl --shutdown", value["error"])
        self.assertIn("500 Internal Server Error", value["error"])
        self.assertEqual(
            [command[:2] for command in fake.commands if command[0] == "docker"],
            [["docker", "version"]],
        )
        self.assertFalse(fake.ids)

    def test_the_engine_fix_names_docker_desktop_only_where_it_runs(self):
        # #3305 review: a Linux daemon gets no Docker Desktop or WSL advice.
        desktop = "request returned 500 for ... dockerDesktopLinuxEngine ..."
        daemon = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."
        self.assertIn("wsl --shutdown", runner.engine_fix(desktop))
        linux = runner.engine_fix(daemon)
        self.assertNotIn("wsl", linux)
        self.assertNotIn("Docker Desktop", linux)
        self.assertIn("Start the Docker daemon", linux)

    def test_each_checkout_gets_its_own_application_image_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first", Path(directory) / "second"
            first.mkdir()
            second.mkdir()
            tag = runner.worktree_image_tag(first)
            self.assertRegex(tag, r"^pacecraft:e2e-[0-9a-f]{8}$")
            self.assertEqual(tag, runner.worktree_image_tag(first))
            self.assertEqual(tag, runner.worktree_image_tag(first / "." / "../first"))
            self.assertNotEqual(tag, runner.worktree_image_tag(second))
        self.assertEqual(
            runner.worktree_image_tag(runner.ROOT),
            runner.parser().parse_args(["--project", "all"]).image,
        )

    def test_print_image_needs_no_project_and_starts_nothing(self):
        with (
            mock.patch.object(runner, "Processes") as processes,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(0, runner.main(["--print-image"]))
        self.assertEqual(
            runner.worktree_image_tag(runner.ROOT), stdout.getvalue().strip()
        )
        processes.assert_not_called()

    def test_image_source_comparison_hashes_content_and_ignores_bytecode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend" / "__pycache__").mkdir(parents=True)
            source = root / "backend" / "module.py"
            source.write_text("one", encoding="utf-8")
            digest = runner.source_digest(root, runner.APPLICATION_SOURCES)
            (root / "backend" / "__pycache__" / "module.pyc").write_text("x")
            # The image job writes the changelog; a checkout never has it (#2899).
            (root / "public").mkdir()
            (root / "public" / "changelog.json").write_text('{"releases": []}')
            self.assertEqual(
                digest, runner.source_digest(root, runner.APPLICATION_SOURCES)
            )
            containers = mock.Mock(run_id="run")
            processes = mock.Mock()
            processes.run.return_value = (0, digest + "\n")
            self.assertTrue(
                runner.image_matches_source(processes, containers, "image", root)
            )
            script = containers.create.call_args.args[1][-1]
            self.assertIn("def source_digest", script)
            source.write_text("two", encoding="utf-8")
            # A mismatch is an answer: it is never retried (#3610).
            containers.create.reset_mock()
            self.assertFalse(
                runner.image_matches_source(processes, containers, "image", root)
            )
            self.assertEqual(1, containers.create.call_count)
            # A check that did not finish, or could not start, runs once more,
            # logged, and only that run's real answer counts (#3610).
            source.write_text("one", encoding="utf-8")
            for failure in ("run", "create"):
                with self.subTest(failure=failure):
                    containers.create.reset_mock(side_effect=True)
                    processes.run.reset_mock(side_effect=True)
                    stalled = getattr(
                        processes if failure == "run" else containers, failure
                    )
                    stalled.side_effect = [runner.RunnerError("stalled"), mock.DEFAULT]
                    processes.run.return_value = (0, digest + "\n")
                    with redirect_stderr(io.StringIO()) as stderr:
                        self.assertTrue(
                            runner.image_matches_source(
                                processes, containers, "image", root
                            )
                        )
                    self.assertIn("stalled; retrying once", stderr.getvalue())
                    self.assertEqual(2, containers.create.call_count)
            containers.create.reset_mock(side_effect=True)
            processes.run.reset_mock(side_effect=True)

            # Unknown stays None (the run stops on it), and each says why, so a
            # stalled check never reads as a stale image (#3610).
            def unknown(reason):
                with redirect_stderr(io.StringIO()) as stderr:
                    self.assertIsNone(
                        runner.image_matches_source(
                            processes, containers, "image", root
                        )
                    )
                self.assertIn(reason, stderr.getvalue())

            processes.run.return_value = (1, "")
            unknown("source check exited with code 1")
            processes.run.return_value = (0, "not a digest\n")
            unknown("source check printed no digest")
            # Neither is retried: the check ran and gave no answer.
            self.assertEqual(2, containers.create.call_count)
            # A compare that times out still removes its container, so a second
            # compare after --build can create one with the same name (#3243);
            # a second timeout stays unknown.
            containers.remove.reset_mock()
            processes.run.side_effect = runner.RunnerError("timed out")
            unknown("source check did not finish: timed out")
            self.assertEqual(
                [mock.call(containers.create.return_value)] * 2,
                containers.remove.call_args_list,
            )
            containers.create.side_effect = runner.RunnerError("unavailable")
            unknown("source check could not start: unavailable")

    def run_with_cleanup_exception(self, fake):
        snapshots = []
        write = runner.atomic_json

        def record(path, value):
            write(path, value)
            snapshots.append(json.loads(path.read_text()))

        with (
            mock.patch.object(
                runner.Containers,
                "cleanup",
                side_effect=OSError("synthetic private detail"),
            ),
            mock.patch.object(runner, "atomic_json", side_effect=record),
        ):
            code, value, path = self.run_main(fake)
        self.assertTrue(value["cleanup_errors"])
        self.assertEqual(value["phases"]["cleanup"]["status"], "failed")
        self.assertEqual(
            set(value["resources"]["containers_not_confirmed_removed"]),
            set(fake.ids) - set(fake.removed),
        )
        self.assertTrue(value["resources"]["containers_not_confirmed_removed"])
        self.assertTrue(fake.stopped)
        self.assertEqual(fake.active, 0)
        self.assertNotIn("synthetic private detail", path.read_text())
        for snapshot in snapshots:
            if any(
                phase["status"] == "failed" for phase in snapshot["phases"].values()
            ):
                self.assertNotEqual(snapshot["status"], "passed")
                self.assertNotEqual(snapshot["exit_code"], 0)
        self.assertEqual(snapshots[-1], value)
        return code, value

    def test_cleanup_exception_after_passed_children_cannot_pass(self):
        code, value = self.run_with_cleanup_exception(FakeProcesses())
        self.assertEqual(code, 1)
        self.assertEqual(value["exit_code"], 1)
        self.assertEqual(value["status"], "failed")
        self.assertTrue(all(child["status"] == "passed" for child in value["children"]))

    def test_cleanup_exception_retains_original_child_failure(self):
        original_code, original, _ = self.run_main(FakeProcesses(fail_browser=True))
        code, value = self.run_with_cleanup_exception(FakeProcesses(fail_browser=True))
        self.assertEqual(code, original_code)
        self.assertEqual(value["exit_code"], original_code)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["error"], original["error"])
        self.assertEqual(value["children"][0]["exit_code"], 7)
        self.assertEqual(value["children"][0]["status"], "failed")

    def test_cleanup_exception_retains_interruption(self):
        code, value = self.run_with_cleanup_exception(FakeProcesses(interrupt=True))
        self.assertEqual(code, 130)
        self.assertEqual(value["exit_code"], 130)
        self.assertEqual(value["status"], "interrupted")
        self.assertEqual(value["error"], "Run interrupted")
        self.assertTrue(
            any(child["status"] == "interrupted" for child in value["children"])
        )

    def test_interrupted_children_are_reaped_before_owned_cleanup(self):
        fake = FakeProcesses(interrupt=True)
        code, value, _ = self.run_main(fake)
        self.assertEqual(code, 130)
        self.assertEqual(value["status"], "interrupted")
        self.assertTrue(fake.stopped)
        self.assertEqual(fake.active, 0)
        self.assertEqual(set(fake.removed), set(fake.ids))
        self.assertTrue(
            any(child["status"] == "interrupted" for child in value["children"])
        )

    def test_partial_start_collects_cid_and_cleanup_only_observed_ids(self):
        fake = FakeProcesses(partial_start=True)
        code, value, _ = self.run_main(fake)
        self.assertNotEqual(code, 0)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(set(fake.removed), set(fake.ids))
        with tempfile.TemporaryDirectory() as temporary:
            owned = runner.Containers(fake, Path(temporary), "my-run")
            with self.assertRaises(runner.RunnerError):
                owned.remove("f" * 64)
            owned.observed.add("e" * 64)
            fake.ids["e" * 64] = "unrelated-run"
            self.assertEqual(owned.cleanup(), ["Owned container cleanup failed"])
            self.assertNotIn("e" * 64, fake.removed)

    def test_auth_and_output_paths_are_disjoint_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = runner.parser().parse_args(["--project", "desktop"])
            evidence = runner.Evidence(root, "run", args)
            execute = runner.Execution(
                ROOT,
                args,
                evidence,
                FakeProcesses(),
                None,
                root,
                "image",
                {},
                "node",
                "cli",
            )
            command_a, env_a, _ = execute.browser_command(
                {"project": "desktop", "file": "e2e/a.spec.js"},
                9100,
                root / "out-a",
                root / "private-a",
            )
            _, env_b, _ = execute.browser_command(
                {"project": "desktop", "file": "e2e/b.spec.js"},
                9101,
                root / "out-b",
                root / "private-b",
            )
            self.assertNotEqual(
                env_a["E2E_AUTH_STATE_PATH"], env_b["E2E_AUTH_STATE_PATH"]
            )
            self.assertNotEqual(
                env_a["PLAYWRIGHT_HTML_OUTPUT_DIR"], env_b["PLAYWRIGHT_HTML_OUTPUT_DIR"]
            )
            self.assertFalse(
                Path(env_a["E2E_AUTH_STATE_PATH"]).is_relative_to(root / "out-a")
            )
            self.assertIn("--trace=off", command_a)
            # #2843: end-state screenshots only with --screenshots; an inherited
            # value never switches them on.
            self.assertNotIn("E2E_SCREENSHOTS", env_a)
            with mock.patch.dict(os.environ, {"E2E_SCREENSHOTS": "1"}):
                _, inherited, _ = execute.browser_command(
                    {"project": "desktop", "file": "e2e/a.spec.js"},
                    9102, root / "out-c", root / "private-c",
                )  # fmt: skip
            self.assertNotIn("E2E_SCREENSHOTS", inherited)
            args.screenshots = True
            _, shots, _ = execute.browser_command(
                {"project": "desktop", "file": "e2e/a.spec.js"},
                9103, root / "out-d", root / "private-d",
            )  # fmt: skip
            self.assertEqual(shots["E2E_SCREENSHOTS"], "1")
            # #3094: one test repeats without editing playwright.config.cjs.
            self.assertIn("--repeat-each=1", command_a)
            self.assertNotIn("--grep", command_a)
            args.repeat_each, args.grep = 30, "queued message"
            repeated, _, _ = execute.browser_command(
                {"project": "desktop", "file": "e2e/a.spec.js"},
                9104, root / "out-e", root / "private-e",
            )  # fmt: skip
            self.assertIn("--repeat-each=30", repeated)
            self.assertEqual(repeated[-2:], ["--grep", "queued message"])
            selection = runner.Evidence(root, "run", args).data["selection"]
            self.assertEqual(
                (selection["mode"], selection["repeat_each"], selection["grep"]),
                ("focused", 30, "queued message"),
            )
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                runner.main(["--project", "desktop", "--repeat-each", "0"])
            # #3200: --today pins the browser clock of the pinned specs.
            self.assertNotIn("E2E_TODAY", env_a)
            args.today = (
                runner.parser()
                .parse_args(["--project", "desktop", "--today", "2026-11-16"])
                .today
            )
            _, pinned, _ = execute.browser_command(
                {"project": "desktop", "file": "e2e/a.spec.js"},
                9105, root / "out-f", root / "private-f",
            )  # fmt: skip
            self.assertEqual(pinned["E2E_TODAY"], "2026-11-16")
            selection = runner.Evidence(root, "run", args).data["selection"]
            self.assertEqual(
                (selection["mode"], selection["today"]), ("focused", "2026-11-16")
            )
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                runner.parser().parse_args(
                    ["--project", "desktop", "--today", "16.11.2026"]
                )

    def test_screenshot_listing_names_only_playwright_end_states(self):
        # #2843: a spec's own crops and the HTML report's copies are not listed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "desktop" / "a.spec.js-1" / "artifacts" / "a-test"
            artifacts.mkdir(parents=True)
            for name in ("test-finished-1.png", "test-failed-1.png", "crop.png"):
                (artifacts / name).write_bytes(b"png")
            (root / "desktop" / "report" / "data").mkdir(parents=True)
            (root / "desktop" / "report" / "data" / "abc.png").write_bytes(b"png")
            self.assertEqual(
                [shot.name for shot in runner.screenshots(root)],
                ["test-failed-1.png", "test-finished-1.png"],
            )
            # #3161: an end state past Windows' MAX_PATH is listed too.
            base = root.resolve()
            if os.name == "nt":  # only the extended form can create the deep folder
                base = Path("\\\\?\\" + str(base))
            deep = base / "desktop" / "artifacts" / ("long-test-title-" * 15)
            deep.mkdir(parents=True)
            (deep / "test-finished-1.png").write_bytes(b"png")
            found = [shot.parent.name for shot in runner.screenshots(root)]
            # The temporary directory's own cleanup cannot reach past MAX_PATH either.
            (deep / "test-finished-1.png").unlink()
            deep.rmdir()
            self.assertIn(deep.name, found)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.main(["--project", "desktop", "--list", "--screenshots"])

    def test_synthetic_identity_parity_across_all_projects_and_shards(self):
        listing_report = report()
        projects = {"desktop", "mobile", "tablet-landscape"}
        for spec in listing_report["suites"][0]["specs"]:
            spec["tests"] = [
                {"projectName": project, "projectId": project}
                for project in sorted(projects)
            ]
        listing = runner.parse_listing(listing_report)
        timing = runner.read_weights(None)
        baseline = runner.select(listing, "all", [], 1, 1, timing)
        self.assertEqual({child["project"] for child in baseline}, projects)
        self.assertEqual(sum(identities(baseline).values()), 9)
        combined = []
        for project in sorted(projects):
            for shard in (1, 2):
                combined += runner.select(listing, project, [], shard, 2, timing)
        self.assertEqual(identities(baseline), identities(combined))
        self.assertTrue(all(count == 1 for count in identities(combined).values()))

    def test_missing_or_mismatched_locked_toolchain_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = ("@playwright/test", "playwright", "playwright-core")
            (root / "package-lock.json").write_text(
                json.dumps(
                    {
                        "packages": {
                            "node_modules/" + package: {"version": "1.64.1"}
                            for package in packages
                        }
                    }
                )
            )
            with self.assertRaisesRegex(
                runner.RunnerError, "Locked local Playwright missing"
            ):
                runner.toolchain(root)
            for package in packages:
                installed = root / "node_modules" / package / "package.json"
                installed.parent.mkdir(parents=True, exist_ok=True)
                installed.write_text(json.dumps({"version": "1.64.1"}))
            with (
                mock.patch.object(runner.shutil, "which", return_value=None) as which,
                self.assertRaisesRegex(runner.RunnerError, "Node is unavailable"),
            ):
                runner.toolchain(root)
            which.assert_called_once_with("node")
            installed.write_text(json.dumps({"version": "wrong"}))
            with self.assertRaisesRegex(
                runner.RunnerError, "differs from package-lock"
            ):
                runner.toolchain(root)

    def test_manifest_exists_before_the_first_tool_child(self):
        fake = FakeProcesses()
        original = fake.run
        observed = []

        def inspect_first(command, **kwargs):
            if not observed:
                # The test helper's fresh parent is the only evidence directory in its
                # patched Evidence constructor, captured before any child is launched.
                value = json.loads(created[0].read_text())
                self.assertEqual(value["status"], "incomplete")
                self.assertIsNone(value["exit_code"])
                observed.append(True)
            return original(command, **kwargs)

        created = []
        init = runner.Evidence.__init__

        def record(instance, directory, run_id, args):
            init(instance, directory, run_id, args)
            created.append(directory / "manifest.json")

        fake.run = inspect_first
        with mock.patch.object(runner.Evidence, "__init__", record):
            self.run_main(fake, list_only=True)
        self.assertTrue(observed)

    def test_cleanup_orders_browser_then_fixture_then_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = FakeProcesses()
            containers = runner.Containers(fake, Path(temporary), "my-run")
            network = containers.create(
                "acb-my-run-net-0", ["image", "sleep", "infinity"]
            )
            fixture = containers.create("acb-my-run-fixture-0", ["image", "python"])
            browser = containers.create("acb-my-run-browser-0", ["image", "node"])
            self.assertEqual(containers.cleanup(), [])
            self.assertEqual(fake.removed, [browser, fixture, network])

    def test_container_name_collision_does_not_authorize_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = FakeProcesses()

            def collision(command, **kwargs):
                return 1, "Unrelated container name already in use"

            fake.run = collision
            containers = runner.Containers(fake, Path(temporary), "my-run")
            with self.assertRaises(runner.RunnerError):
                containers.create("collision", ["image"])
            self.assertEqual(containers.cleanup(), [])
            self.assertFalse(fake.removed)

    def test_fixture_that_never_gets_ready_shows_its_last_log_lines(self):
        # A fixture that crashes at startup is visible without manual docker logs (#2674).
        fake = FakeProcesses()
        refused = runner.RunnerError("Disposable fixture failed bounded readiness")
        code, value, _path = self.run_main(fake, ready=refused)
        self.assertNotEqual(code, 0)
        self.assertIn("failed bounded readiness", value["error"])
        self.assertIn("Traceback: fixture import failed", value["error"])
        self.assertNotIn(runner.FAKE_PASSWORD, value["error"])
        self.assertIn(
            ["docker", "logs", "--tail", "20"], [c[:4] for c in fake.commands]
        )
        self.assertIn("Error: fixture stderr line", value["error"])
        self.assertNotIn(runner.FAKE_OPENAI_KEY, value["error"])

    def test_failed_tool_command_names_itself_and_its_output(self):
        # A failed build or container start shows which command and why (#2656).
        script = (
            "import sys; print('st' + 'ep 1'); "
            "sys.stderr.write('ERR' + 'OR: no space ' + sys.argv[1] + chr(10)); "
            "sys.exit(3)"
        )
        with self.assertRaises(runner.RunnerError) as raised:
            runner.checked(
                runner.Processes(), [sys.executable, "-c", script, runner.FAKE_PASSWORD]
            )
        header, *tail = str(raised.exception).splitlines()
        self.assertRegex(header, r"\(exit 3\): python[\w.]*$")
        self.assertEqual(tail, ["step 1", "ERROR: no space [redacted]"])

    def test_readiness_is_bounded_and_cancelled_without_fallback(self):
        with self.assertRaises(runner.RunnerError):
            runner.ready(1, threading.Event(), timeout=0)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(InterruptedError):
            runner.ready(1, cancelled)

    def test_cancellation_terminates_and_reaps_owned_process_group(self):
        processes = runner.Processes()
        child = mock.Mock()
        child.pid = 123
        child.poll.return_value = None

        def cancelled(**kwargs):
            processes.stop.set()
            raise subprocess.TimeoutExpired(["synthetic"], 0.2)

        child.communicate.side_effect = cancelled
        with (
            mock.patch.object(runner.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(processes, "terminate") as terminate,
            self.assertRaises(InterruptedError),
        ):
            processes.run(["synthetic"])
        terminate.assert_called_once_with(child)
        child.wait.assert_called_once()
        self.assertFalse(processes.active)
        if runner.os.name == "nt":
            flags = popen.call_args.kwargs["creationflags"]
            self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
            self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_prepared_compatibility_requires_declared_version_and_observed_revision(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "node_modules/playwright-core").mkdir(parents=True)
            (root / "scripts").mkdir()
            (root / "node_modules/playwright-core/browsers.json").write_text(
                json.dumps(
                    {
                        "browsers": [
                            {"name": "chromium", "revision": "1248"},
                            {"name": "chromium-headless-shell", "revision": "1248"},
                        ]
                    }
                )
            )
            pin = root / "scripts/browser_environment.json"
            image = "example:v1@sha256:" + "a" * 64
            pin.write_text(json.dumps({"version": "1.64.1", "image": image}))
            args = runner.parser().parse_args(
                ["--project", "desktop", "--browser-image", image]
            )
            versions = {"@playwright/test": "1.64.1"}
            fake = FakeProcesses()
            containers = mock.Mock(run_id="my-run")
            containers.create.return_value = "a" * 64

            def observed(command, **kwargs):
                if command[:2] == ["docker", "start"]:
                    return 0, json.dumps(
                        {
                            "revisions": {
                                "chromium": "wrong",
                                "chromium_headless_shell": "1248",
                            }
                        }
                    )
                return 0, "0"

            fake.run = observed
            with (
                mock.patch.object(runner.sys, "platform", "linux"),
                mock.patch.object(runner, "browser_options", return_value=[]),
                mock.patch.object(runner, "image_identity", return_value="image-id"),
            ):
                with self.assertRaisesRegex(runner.RunnerError, "revisions differ"):
                    runner.verify_browser(
                        root, args, "node", versions, fake, containers, root
                    )
                containers.remove.assert_not_called()
                pin.write_text(json.dumps({"version": "wrong", "image": image}))
                containers.reset_mock()
                with self.assertRaisesRegex(runner.RunnerError, "locked declared"):
                    runner.verify_browser(
                        root, args, "node", versions, fake, containers, root
                    )
                containers.create.assert_not_called()
            with (
                mock.patch.object(runner.sys, "platform", "win32"),
                self.assertRaisesRegex(runner.RunnerError, "Linux host"),
            ):
                runner.verify_browser(
                    root, args, "node", versions, fake, containers, root
                )

    def test_a_missing_native_chromium_names_the_browser_and_the_prepare_step(self):
        # #3342: after a Playwright bump the native check exits 2, and the run
        # said only "node.EXE exit 2".
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "node_modules/playwright-core").mkdir(parents=True)
            (root / "node_modules/playwright-core/browsers.json").write_text(
                json.dumps({"browsers": [{"name": "chromium", "revision": "1255"}]})
            )
            args = runner.parser().parse_args(["--project", "desktop"])
            versions = {"@playwright/test": "1.65.1"}
            fake = FakeProcesses()
            fake.run = lambda command, **kwargs: (2, "")
            with self.assertRaises(runner.RunnerError) as caught:
                runner.verify_browser(root, args, "node", versions, fake, None, root)
            message = str(caught.exception)
            for part in (
                "Chromium",
                "1.65.1",
                "python scripts/prepare_worktree.py",
                "install chromium",
            ):
                self.assertIn(part, message)
            # Another failure of the check keeps the generic message.
            fake.run = lambda command, **kwargs: (3, "")
            with self.assertRaisesRegex(runner.RunnerError, r"\(exit 3\): node"):
                runner.verify_browser(root, args, "node", versions, fake, None, root)

    def test_expected_identities_survive_spec_and_shard_filters(self):
        listing = report()
        for spec in listing["suites"][0]["specs"]:
            spec["tests"].append({"projectName": "mobile", "projectId": "mobile"})
        for project in ("desktop", "mobile", "all"):
            expected = [
                identity
                for (name, _), values in runner.parse_listing(listing).items()
                if project == "all" or name == project
                for identity in values
            ]
            for options in (
                ("--shard", "1", "--total", "2"),
                ("--shard", "2", "--total", "2"),
                ("--spec", "e2e/b.spec.js"),
            ):
                with self.subTest(project=project, options=options):
                    code, value, _ = self.run_main(
                        FakeProcesses(listing=listing),
                        list_only=True,
                        extra_args=("--project", project, *options),
                    )
                    self.assertEqual(code, 0)
                    self.assertCountEqual(
                        value["selection"]["expected_identities"], expected
                    )
                    self.assertLess(len(identities(value["children"])), len(expected))
                    selection = value["selection"]
                    self.assertEqual(selection["project"], project)
                    self.assertEqual(
                        selection["mode"],
                        "complete"
                        if project == "all" and "--spec" not in options
                        else "focused",
                    )
                    self.assertEqual(
                        selection["specs"],
                        ["e2e/b.spec.js"] if "--spec" in options else [],
                    )
                    self.assertEqual(
                        selection["total"], 2 if "--total" in options else 1
                    )
                    self.assertEqual(
                        selection["shard"],
                        int(options[1]) if "--shard" in options else 1,
                    )

    def test_manifest_hashes_complete_e2e_sources_and_public_inputs(self):
        code, value, _ = self.run_main(FakeProcesses(), list_only=True)
        self.assertEqual(code, 0)
        expected = {
            path
            for path in (
                "scripts/run_browser_tests.py",
                "playwright.config.cjs",
                "public/index.html",
                "public/service-worker.js",
                "package.json",
                "package-lock.json",
                "e2e/browser_durations.json",
                "scripts/browser_environment.json",
            )
            if (ROOT / path).is_file()
        }
        expected.update(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "e2e").rglob("*")
            if path.suffix in {".js", ".py"} and path.is_file()
        )
        self.assertEqual(set(value["inputs"]), expected)
        self.assertTrue(
            {
                "e2e/auth.setup.js",
                "e2e/fixture_runtime.py",
                "e2e/read-fixture.js",
                "e2e/helpers/ui.js",
                "e2e/helpers/connections.js",
            }.issubset(expected)
        )
        for path in expected:
            self.assertEqual(
                value["inputs"][path],
                hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
            )

    def test_source_hashes_ignore_auth_runtime_and_detect_helper_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "e2e/helpers/nested").mkdir(parents=True)
            helper = root / "e2e/helpers/nested/helper.py"
            helper.write_text("# synthetic source v1")
            (root / "e2e/state.json").write_text('{"synthetic": "runtime"}')
            (root / "e2e/helpers/output.txt").write_text("synthetic output")
            (root / "e2e/.auth").mkdir()
            (root / "e2e/.auth/state.json").write_text("{}")
            (root / "e2e/.auth/ignored.js").write_text("// runtime, not source")
            durations = root / "durations.json"
            durations.write_text("{}")
            before = runner.source_hashes(root, durations)
            self.assertEqual(
                set(before), {"e2e/helpers/nested/helper.py", "durations.json"}
            )
            helper.write_text("# synthetic source v2")
            after = runner.source_hashes(root, durations)
            self.assertNotEqual(
                before["e2e/helpers/nested/helper.py"],
                after["e2e/helpers/nested/helper.py"],
            )
            self.assertEqual(before["durations.json"], after["durations.json"])

    def test_prepared_commands_select_both_source_reading_specs(self):
        node, cli = "synthetic-node", Path("synthetic-cli.js")
        listing = runner.parse_listing(
            report(("contracts.spec.js", "css-cascade.spec.js"))
        )
        timing = runner.read_weights(None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = runner.parser().parse_args(
                [
                    "--project",
                    "desktop",
                    "--browser-image",
                    "synthetic-image",
                ]
            )
            containers = mock.Mock(run_id="prepared-commands")
            containers.create.return_value = "a" * 64
            execute = runner.Execution(
                ROOT,
                args,
                runner.Evidence(root, "run", args),
                FakeProcesses(),
                containers,
                root,
                "app-image",
                {"id": "browser-image-id"},
                node,
                cli,
            )
            for file in ("e2e/contracts.spec.js", "e2e/css-cascade.spec.js"):
                with self.subTest(file=file):
                    child = runner.select(listing, "desktop", [file], 1, 1, timing)[0]
                    # Only command construction is modeled as Linux here; no host-network run.
                    with (
                        mock.patch.object(
                            runner.os, "getuid", return_value=1000, create=True
                        ),
                        mock.patch.object(
                            runner.os, "getgid", return_value=1000, create=True
                        ),
                    ):
                        command, _, cid = execute.browser_command(
                            child, 9100, root / "results", root / "private"
                        )
                    self.assertEqual(command, ["docker", "start", "--attach", cid])
                    creation = containers.create.call_args.args[1]
                    self.assertEqual(creation[:2], ["--network", "host"])
                    mounts = [
                        creation[i + 1]
                        for i, item in enumerate(creation)
                        if item == "--volume"
                    ]
                    self.assertEqual(
                        set(mounts),
                        {
                            *(runner.browser_mounts(ROOT)[1::2]),
                            str(root / "results") + ":/results",
                            str(root / "private") + ":/private",
                        },
                    )
                    self.assertIn("E2E_AUTH_STATE_PATH=/private/state.json", creation)
                    self.assertIn(
                        "E2E_PHASE_TIMINGS_PATH=/private/phase.json", creation
                    )
                    self.assertIn("E2E_BASE_URL=http://127.0.0.1:9100", creation)
                    self.assertIn("E2E_APP_PASSWORD=" + runner.FAKE_PASSWORD, creation)
                    self.assertIn(
                        "PLAYWRIGHT_HTML_OUTPUT_DIR=/results/report", creation
                    )
                    self.assertIn("--read-only", creation)
                    self.assertIn("--cap-drop=ALL", creation)
                    self.assertIn("no-new-privileges:true", creation)
                    self.assertEqual(
                        creation[creation.index("--user") + 1], "1000:1000"
                    )
                    arguments = creation[creation.index("browser-image-id") + 3 :]
                    self.assertIn("--workers=1", arguments)
                    self.assertIn("--trace=off", arguments)
                    self.assertEqual(
                        arguments[arguments.index("--project") + 1], "desktop"
                    )
                    self.assertEqual(arguments[1], re.escape(file) + "$")
                    observed = [
                        {"identities": values}
                        for (project, candidate), values in listing.items()
                        if project == "desktop" and re.search(arguments[1], candidate)
                    ]
                    self.assertEqual(identities([child]), identities(observed))

    def test_prepared_mounts_exclude_repository_root_live_data_and_socket(self):
        mounts = runner.browser_mounts(ROOT)
        mounted = [
            mounts[index + 1] for index, item in enumerate(mounts) if item == "--volume"
        ]
        self.assertEqual(
            set(mounted),
            {
                str(ROOT / "node_modules") + ":/work/node_modules:ro",
                str(ROOT / "e2e") + ":/work/e2e:ro",
                str(ROOT / "playwright.config.cjs") + ":/work/playwright.config.cjs:ro",
                str(ROOT / "public/index.html") + ":/work/public/index.html:ro",
                str(ROOT / "public/service-worker.js")
                + ":/work/public/service-worker.js:ro",
                str(ROOT / "scripts/ui_walk.mjs") + ":/work/scripts/ui_walk.mjs:ro",
                str(ROOT / "scripts/ui_explore.mjs")
                + ":/work/scripts/ui_explore.mjs:ro",
            },
        )
        self.assertEqual(len(mounted), 7)
        for source in mounted:
            self.assertTrue(source.endswith(":ro"))
            self.assertNotIn(".env", source)
            self.assertNotIn("docker.sock", source)
            self.assertFalse(source.startswith(str(ROOT) + ":"))


class StaleRunTests(unittest.TestCase):
    """A run killed before its own cleanup leaves containers (#2721)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.locks = Path(temporary.name)
        patcher = mock.patch.object(runner, "LOCK_DIR", self.locks)
        patcher.start()
        self.addCleanup(patcher.stop)

    def old_lock(self, run_id):
        """An unheld lock file older than the sweep's grace period."""
        path = self.locks / f"{run_id}.lock"
        path.write_bytes(b"")
        past = time.time() - runner.LOCK_GRACE_SECONDS - 60
        os.utime(path, (past, past))
        return path

    def test_a_young_unheld_lock_is_left_for_a_starting_run(self):
        # The run created its file but has not locked it yet (Gitar on #2726).
        young = "9" * 32
        (self.locks / f"{young}.lock").write_bytes(b"")
        fake = FakeProcesses()
        fake.other_runs = {young: f"{'7' * 12} acb-{young}-net-0\n"}
        self.assertEqual(runner.sweep_stale(fake), 0)
        self.assertEqual(fake.commands, [])
        self.assertTrue((self.locks / f"{young}.lock").exists())

    @unittest.skipIf(os.name == "nt", "Windows cannot unlink an open file, so no race")
    def test_a_lock_file_replaced_before_locking_is_taken_again(self):
        run_id = "8" * 32
        real_lock = runner._lock
        calls = []

        def lock_after_a_sweep(handle, take=True):
            if take and not calls:
                calls.append(1)
                (self.locks / f"{run_id}.lock").unlink()  # a sweep's unlink
            return real_lock(handle, take)

        with mock.patch.object(runner, "_lock", side_effect=lock_after_a_sweep):
            handle = runner.hold_run_lock(run_id)
        path = self.locks / f"{run_id}.lock"
        self.assertTrue(os.path.samestat(os.fstat(handle.fileno()), path.stat()))
        runner.release_run_lock(handle, run_id)

    def test_a_failed_lock_file_removal_does_not_escape(self):
        run_id = "7" * 32
        handle = runner.hold_run_lock(run_id)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("open")):
            runner.release_run_lock(handle, run_id)
        self.assertTrue(handle.closed)

    def test_only_runs_whose_lock_nobody_holds_are_removed(self):
        dead, live, unknown = "a" * 32, "b" * 32, "c" * 32
        self.old_lock(dead)
        held = runner.hold_run_lock(live)
        self.addCleanup(runner.release_run_lock, held, live)
        fake = FakeProcesses()
        fake.other_runs = {
            dead: f"{'1' * 12} acb-{dead}-net-0\n{'2' * 12} acb-{dead}-fixture-0-0\n",
            live: f"{'3' * 12} acb-{live}-fixture-0-0\n",
            unknown: f"{'4' * 12} acb-{unknown}-fixture-0-0\n",
        }
        self.assertEqual(runner.sweep_stale(fake), 2)
        removals = [c for c in fake.commands if c[:2] == ["docker", "rm"]]
        # One call, the network namespace last, like Containers.cleanup.
        self.assertEqual(removals, [["docker", "rm", "--force", "2" * 12, "1" * 12]])
        self.assertFalse((self.locks / f"{dead}.lock").exists())
        self.assertTrue((self.locks / f"{live}.lock").exists())
        queried = [c for c in fake.commands if c[:2] == ["docker", "ps"]]
        self.assertEqual(len(queried), 1)  # no lock file, no query: never touched

    def test_a_failed_removal_keeps_the_lock_for_the_next_sweep(self):
        dead = "d" * 32
        self.old_lock(dead)
        fake = FakeProcesses()
        fake.other_runs = {dead: f"{'5' * 12} acb-{dead}-fixture-0-0\n"}
        with mock.patch.object(fake, "run", side_effect=runner.RunnerError("busy")):
            self.assertEqual(runner.sweep_stale(fake), 0)
        self.assertTrue((self.locks / f"{dead}.lock").exists())

    def test_a_run_holds_its_lock_and_releases_it(self):
        run_id = "e" * 32
        handle = runner.hold_run_lock(run_id)
        with (self.locks / f"{run_id}.lock").open("a+b") as other:
            self.assertFalse(runner._lock(other))
        with self.assertRaises(runner.RunnerError):
            runner.hold_run_lock(run_id)
        runner.release_run_lock(handle, run_id)
        self.assertFalse((self.locks / f"{run_id}.lock").exists())

    def test_cleanup_stale_only_sweeps_and_exits(self):
        dead = "f" * 32
        self.old_lock(dead)
        fake = FakeProcesses()
        fake.other_runs = {dead: f"{'6' * 12} acb-{dead}-fixture-0-0\n"}
        with (
            mock.patch.object(runner, "Processes", return_value=fake),
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            self.assertEqual(runner.main(["--cleanup-stale"]), 0)
        self.assertIn("Removed 1 container", out.getvalue())
        self.assertEqual(
            [c[:2] for c in fake.commands], [["docker", "ps"], ["docker", "rm"]]
        )


if __name__ == "__main__":
    unittest.main()
