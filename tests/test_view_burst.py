"""The tracked view-burst measurement (#3052) against a loopback stub, never a real app."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

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
        self._json({"ok": True}, "ic_session=fake; Path=/")

    def do_GET(self) -> None:
        type(self).seen.append(self.path)
        if self.path == "/api/diagnostics":
            self._json({"curve_build": {"status": "ready"}})
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
            self.assertEqual(set(result[key]), {"median", "min", "max"})
        # Two rounds, each read once alone and once in the burst, after the wait.
        for path in view_burst.READS:
            self.assertGreaterEqual(_Stub.seen.count(path), 4)
        self.assertIn("/api/diagnostics", _Stub.seen)
        self.assertIsNotNone(result["waited_for_ready_s"])

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

    def test_spread_is_median_min_and_max(self) -> None:
        self.assertEqual(
            view_burst.spread([3.0, 1.0, 2.0, 10.0]),
            {"median": 2.5, "min": 1.0, "max": 10.0},
        )


if __name__ == "__main__":
    unittest.main()
