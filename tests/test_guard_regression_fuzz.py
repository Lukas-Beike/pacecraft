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

NAME = "guard_toy.py"
# A toy guard that asks its sibling module, as guard_shell asks the other guards.
HOOK = """
import importlib, json, sys

def main():
    command = json.loads(sys.stdin.buffer.read())["tool_input"]["command"]
    return importlib.import_module("toy_rule").verdict(command)
"""


class GuardRegressionFuzzTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # The old guard blocks the name in any case.
        self.old = self.tree("old", 'return 2 if "secret" in command.lower() else 0')
        self.inputs = fuzz.commands(("cat secret.txt", "echo hello"), 300, seed=3)

    def tree(self, label: str, rule: str) -> Path:
        hooks = self.root / label
        hooks.mkdir()
        (hooks / NAME).write_text(HOOK, encoding="utf-8")
        (hooks / "toy_rule.py").write_text(
            f"def verdict(command):\n    {rule}\n", encoding="utf-8"
        )
        return hooks

    def test_a_loosened_sibling_shows_newly_allowed_commands(self) -> None:
        # Each version runs with its own sibling (Gitar on #3529): matching only
        # lower case in the new sibling lets an upper-case spelling through.
        new = self.tree("loose", 'return 2 if "secret" in command else 0')
        result = fuzz.compare(self.old, new, NAME, self.inputs)
        self.assertGreater(len(result.newly_allowed), 0)
        self.assertTrue(all("secret" in item.lower() for item in result.newly_allowed))

    def test_a_tightened_guard_allows_nothing_new(self) -> None:
        new = self.tree(
            "tight",
            'return 2 if "secret" in command.lower() or "hello" in command else 0',
        )
        result = fuzz.compare(self.old, new, NAME, self.inputs)
        self.assertEqual(([], 302), (result.newly_allowed, result.checked))
        self.assertGreater(result.newly_blocked, 0)

    def test_a_crashing_hook_counts_as_blocking(self) -> None:
        # The hooks fail closed, so a crash is never a newly allowed command.
        new = self.tree("crash", "raise RuntimeError('boom')")
        self.assertEqual(
            [], fuzz.compare(self.old, new, NAME, self.inputs).newly_allowed
        )

    def test_the_cli_compares_a_real_guard_with_its_base(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fuzz.main(
                ["scripts/hooks/guard_shell.py", "--base", "HEAD", "--count", "200"]
            )
        self.assertEqual(0, code)
        self.assertIn("0 newly allowed", out.getvalue())

    def test_only_a_guard_hook_and_a_plain_ref_are_accepted(self) -> None:
        for argv in (
            ["../outside.py"],
            ["scripts/hooks/guard_shell.py", "--base", "--output=x"],
        ):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                fuzz.main(argv)


if __name__ == "__main__":
    unittest.main()
