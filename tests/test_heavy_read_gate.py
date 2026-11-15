"""Heavy view reads run one at a time, with a bounded wait (#3029)."""

from __future__ import annotations

import ast
import threading
import unittest
from pathlib import Path

from backend.http_api.route_table import Access, Route
from backend.runtime.heavy_reads import HeavyReadGate

ROOT = Path(__file__).resolve().parents[1]
HEAVY_ROUTES = {
    "/api/activities",
    "/api/analysis/power-profiles",
    "/api/analysis/training-records",
    "/api/context-preview",
    "/api/performance",
}


class HeavyReadGateTests(unittest.TestCase):
    def test_a_second_read_waits_for_the_first(self) -> None:
        gate = HeavyReadGate(wait_s=5.0)
        events: list[str] = []
        first_inside, release = threading.Event(), threading.Event()

        def first() -> None:
            with gate.held():
                events.append("first in")
                first_inside.set()
                release.wait(5)
                events.append("first out")

        def second() -> None:
            with gate.held():
                events.append("second in")

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        threads[0].start()
        first_inside.wait(5)
        threads[1].start()
        threads[1].join(0.2)
        self.assertEqual(events, ["first in"], "the second read waits")
        release.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(events, ["first in", "first out", "second in"])
        self.assertEqual(gate.summary()["timeouts"], 0)

    def test_the_wait_is_bounded_and_counted(self) -> None:
        gate = HeavyReadGate(wait_s=0.05)
        inside, release = threading.Event(), threading.Event()

        def stuck() -> None:
            with gate.held():
                inside.set()
                release.wait(5)

        thread = threading.Thread(target=stuck)
        thread.start()
        inside.wait(5)
        ran = []
        with gate.held():  # a stuck read never blocks the next one for long
            ran.append(True)
        release.set()
        thread.join(5)
        self.assertEqual(ran, [True])
        summary = gate.summary()
        self.assertEqual(summary["timeouts"], 1)
        self.assertGreaterEqual(summary["longest_wait_s"], 0.04)
        self.assertEqual(set(summary), {"waits_over_1s", "timeouts", "longest_wait_s"})

    def test_a_failing_read_releases_the_gate(self) -> None:
        gate = HeavyReadGate(wait_s=0.05)
        with self.assertRaises(RuntimeError), gate.held():
            raise RuntimeError("read failed")
        with gate.held():
            pass
        self.assertEqual(gate.summary()["timeouts"], 0)

    def test_the_heavy_routes_are_the_history_view_reads(self) -> None:
        heavy = set()
        for path in sorted((ROOT / "backend" / "http_api").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "Route"
                    and any(
                        keyword.arg == "heavy"
                        and getattr(keyword.value, "value", False) is True
                        for keyword in node.keywords
                    )
                ):
                    method, route = (ast.literal_eval(arg) for arg in node.args[:2])
                    self.assertEqual(method, "GET", path.name)
                    heavy.add(route)
        self.assertEqual(heavy, HEAVY_ROUTES)
        # Only a GET route can be heavy: a POST would silently skip the gate.
        with self.assertRaises(ValueError):
            Route("POST", "/api/x", Access.SESSION_CSRF, lambda *_: None, heavy=True)


if __name__ == "__main__":
    unittest.main()
