"""The old-versus-new guard fuzz finds a loosening and passes a tightening (#3527)."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import guard_regression_fuzz as fuzz

HOOK = """
import json, sys

def main():
    command = json.loads(sys.stdin.buffer.read())["tool_input"]["command"]
    {body}
"""


def hook(directory: str, name: str, body: str):
    path = Path(directory) / f"{name}.py"
    path.write_text(HOOK.format(body=body), encoding="utf-8")
    return fuzz.load(path, name)


class GuardRegressionFuzzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # The old guard blocks the name in any case.
        self.old = hook(
            self.directory.name,
            "old_guard",
            'return 2 if "secret" in command.lower() else 0',
        )
        self.seeds = ("cat secret.txt", "echo hello")

    def test_a_loosened_guard_shows_newly_allowed_commands(self) -> None:
        # Matching only lower case lets an upper-case spelling through.
        new = hook(
            self.directory.name, "loose_guard", 'return 2 if "secret" in command else 0'
        )
        result = fuzz.compare(self.old, new, fuzz.commands(self.seeds, 400, seed=3))
        self.assertGreater(len(result.newly_allowed), 0)
        self.assertTrue(all("secret" in item.lower() for item in result.newly_allowed))

    def test_a_tightened_guard_allows_nothing_new(self) -> None:
        new = hook(
            self.directory.name,
            "tight_guard",
            'return 2 if "secret" in command.lower() or "hello" in command else 0',
        )
        result = fuzz.compare(self.old, new, fuzz.commands(self.seeds, 400, seed=3))
        self.assertEqual(([], 402), (result.newly_allowed, result.checked))
        self.assertGreater(result.newly_blocked, 0)

    def test_a_crashing_hook_counts_as_blocking(self) -> None:
        # The hooks fail closed, so a crash is never a newly allowed command.
        new = hook(self.directory.name, "crash_guard", "raise RuntimeError('boom')")
        result = fuzz.compare(self.old, new, fuzz.commands(self.seeds, 50, seed=3))
        self.assertEqual([], result.newly_allowed)

    def test_the_cli_compares_a_real_guard_with_its_base(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fuzz.main(
                [
                    "scripts/hooks/guard_resolve_thread.py",
                    "--base",
                    "HEAD",
                    "--count",
                    "300",
                ]
            )
        self.assertEqual(0, code)
        self.assertIn("0 newly allowed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
