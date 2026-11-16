"""The tracked view-burst measurement (#3052) against a loopback stub, never a real app."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import view_burst


class _Stub(BaseHTTPRequestHandler):
    seen: ClassVar[list[str]] = []
    logins: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, *_: Any) -> None:
        pass

    def _json(self, body: Any, cookie: str | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        type(self).logins.append(json.loads(self.rfile.read(length)))
        type(self).seen.append("login")
        self._json({"ok": True}, "ic_session=fake; Path=/")

    def do_GET(self) -> None:
        type(self).seen.append(self.path)
        if self.path == "/api/diagnostics":
            self._json({"curve_build": {"status": "ready"}})
        elif self.path == "/api/activities?limit=1":
            self._json({"activities": [{"id": "act_1 2"}]})
        else:
            self._json({})


class ViewBurstTests(unittest.TestCase):
    def setUp(self) -> None:
        _Stub.seen, _Stub.logins = [], []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def run_script(self, *args: str) -> dict[str, Any]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(view_burst.main(["--base-url", self.base, *args]), 0)
        return json.loads(output.getvalue())

    def test_rounds_of_the_ten_reads_report_median_and_spread(self) -> None:
        result = self.run_script("--rounds", "2")
        self.assertEqual(len(_Stub.logins), 1, "one login: a login is a write")
        self.assertEqual(_Stub.logins[0], {"password": view_burst.FIXTURE_PASSWORD})
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(set(result["reads"]), set(view_burst.READS))
        self.assertEqual(len(view_burst.READS), 10)
        for key in ("sequential_sum_s", "burst_max_s", "burst_to_sum"):
            self.assertEqual(
                set(result[key]), {"count", "mean", "median", "p95", "min", "max"}
            )
        # Two rounds, each read once alone and once in the burst, after the wait.
        for path in view_burst.READS:
            self.assertGreaterEqual(_Stub.seen.count(path), 4)
        self.assertIn("/api/diagnostics", _Stub.seen)
        self.assertIsNotNone(result["waited_for_ready_s"])

    def test_steady_state_waits_until_no_stage_runs(self) -> None:
        # A backfill stage still running is measured with the change (#3172).
        answers = iter(
            [
                {"curve_build": {"status": "building"}},
                {
                    "curve_build": {"status": "ready"},
                    "activity_weather": {"status": "running"},
                },
                {
                    "curve_build": {"status": "ready"},
                    "activity_weather": {"status": "running"},
                },
                {
                    "curve_build": {"status": "ready"},
                    "activity_weather": {"status": "complete"},
                },
            ]
        )
        polls: list[str] = []

        class Fake:
            def get(self, path: str) -> Any:
                polls.append(path)
                return next(answers) if path == "/api/diagnostics" else {}

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            view_burst.wait_until_ready(
                cast(view_burst.Client, Fake()), 60, lambda: 0.0, lambda _s: None
            )
        self.assertEqual(polls.count("/api/diagnostics"), 4)
        self.assertEqual(stderr.getvalue(), "waiting for activity_weather\n")
        stuck = {
            "curve_build": {"status": "ready"},
            "session_analysis": {"status": "running"},
        }
        times = iter([0.0, 61.0])

        class Stuck:
            def get(self, path: str) -> Any:
                return stuck if path == "/api/diagnostics" else {}

        with self.assertRaisesRegex(SystemExit, "running session_analysis"):
            view_burst.wait_until_ready(
                cast(view_burst.Client, Stuck()),
                60,
                lambda: next(times),
                lambda _s: None,
            )

    def test_right_after_start_does_not_wait_for_the_curve_build(self) -> None:
        result = self.run_script("--rounds", "1", "--right-after-start")
        self.assertNotIn("/api/diagnostics", _Stub.seen)
        self.assertIsNone(result["waited_for_ready_s"])

    def test_only_a_loopback_fixture_is_measured(self) -> None:
        for url in (
            "http://192.168.1.10:8090",
            "https://example.invalid",
            "ftp://127.0.0.1",
        ):
            with (
                self.subTest(url=url),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                view_burst.main(["--base-url", url])

    def test_spread_reports_count_mean_median_p95_min_and_max(self) -> None:
        # Nearest-rank p95 of four values is the fourth (ceil(3.8) = 4).
        self.assertEqual(
            view_burst.spread([3.0, 1.0, 2.0, 10.0]),
            {
                "count": 4,
                "mean": 4.0,
                "median": 2.5,
                "p95": 10.0,
                "min": 1.0,
                "max": 10.0,
            },
        )
        self.assertEqual(view_burst.spread([0.5])["p95"], 0.5)

    def test_a_routes_file_and_several_clients_log_in_before_measuring(self) -> None:
        # #3078: routes come from a file (no bare /api/ arguments for Git Bash),
        # {activity_id} is the newest fixture session, logins precede every read.
        # The routes file lives below the working directory, as the script requires.
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(contextlib.chdir(work))
        routes = work / "routes.txt"
        routes.write_text(
            "# detail and its list\n/api/activities/{activity_id}\n\n/api/performance\n",
            encoding="utf-8",
        )
        result = self.run_script(
            "--routes-file", "routes.txt", "--clients", "3", "--rounds", "2",
            "--right-after-start",
        )  # fmt: skip
        self.assertEqual(_Stub.seen[:3], ["login"] * 3)
        self.assertEqual(_Stub.seen.count("login"), 3)
        self.assertEqual(result["clients"], 3)
        self.assertEqual(
            set(result["reads"]), {"/api/activities/act_1%202", "/api/performance"}
        )
        outside = work.parent / f"{work.name}-outside.txt"
        outside.write_text("/api/performance\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        for name, content in (
            ("bad.txt", "C:/Program Files/Git/api/performance\n"),  # rewritten
            ("bad.txt", "/api/performance\n/api/performance\n"),  # duplicate
            (f"../{outside.name}", None),  # a file outside the working directory
        ):
            if content is not None:
                (work / name).write_text(content, encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(SystemExit):
                view_burst.main(
                    [
                        "--base-url",
                        self.base,
                        "--routes-file",
                        name,
                        "--right-after-start",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
