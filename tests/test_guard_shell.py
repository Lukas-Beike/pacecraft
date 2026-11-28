"""scripts/hooks/guard_shell.py runs every shell guard in one process (#2982)."""

import io
import json
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts/hooks/guard_shell.py"
sys.path.insert(0, str(HOOK.parent))
import guard_shell


def run_hook(command: str) -> subprocess.CompletedProcess[bytes]:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        check=False,
        cwd=ROOT,
    )


class GuardShellTests(unittest.TestCase):
    def test_each_guard_still_blocks_what_it_blocks(self) -> None:
        # One command per guard, each blocked by that guard alone.
        for command, guard in (
            ("cat .env", "guard_private_paths"),
            ("cat ~/.t3/userdata/secrets/files/x", "guard_private_paths"),
            ("git reset --hard", "guard_destructive_git"),
            ("python - <<'EOF'\nprint('a\\\\b')\nEOF", "guard_shell_backslashes"),
            (
                "gh api graphql -f query='mutation{resolveReviewThread(input:{})}'",
                "guard_resolve_thread",
            ),
            ('gh pr edit 1 --body ""', "guard_empty_body"),
        ):
            with self.subTest(guard=guard):
                result = run_hook(command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(b"Blocked", result.stderr)

    def test_long_commands_get_every_guard_within_the_budget(self) -> None:
        # #3119: 20k unterminated heredoc openers or 100k separators once took
        # longer than the 10 s hook budget, so later guards never ran.
        openers = "\n".join(f"cat <<E{n}" for n in range(20_000))
        heredoc = "python - <<'EOF'\nprint('a\\\\b')\nEOF"
        for command, code in (
            (openers + "\ngit status", 0),
            (openers + '\ngh pr edit 1 --body ""', 2),  # the last guard
            (openers + "\n" + heredoc, 2),
            ("echo" + ";" * 100_000 + 'gh pr edit 1 --body ""', 2),
            ("echo" + ";" * 100_000, 0),
            ("gh pr edit x " * 6_000 + '--body ""', 2),
            # A PR body names its issue (#3535), so the passing case carries one.
            ("gh pr edit x --body 'Closes #1' " * 6_000, 0),
            ("gh pr edit x --body y " * 6_000, 2),
        ):
            with self.subTest(command=command[-30:], code=code):
                started = time.monotonic()
                result = run_hook(command)
                self.assertLess(time.monotonic() - started, 5)  # budget: 10 s
                self.assertEqual(result.returncode, code, result.stderr)

    def test_a_harmless_command_passes_every_guard(self) -> None:
        for command in ("git status --short", "python -m unittest discover -s tests"):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command).returncode, 0)

    def test_unreadable_input_is_blocked(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=b"not json",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)

    def test_a_failing_guard_blocks_the_call(self) -> None:
        # A guard that crashes blocks, as a crashed hook process did (fail-closed).
        broken = SimpleNamespace(main=lambda: 1 / 0)
        stderr = io.StringIO()
        with (
            mock.patch.dict(sys.modules, {"broken": broken}),
            mock.patch.object(guard_shell, "GUARDS", ("broken",)),
            mock.patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))),
            redirect_stderr(stderr),
        ):
            self.assertEqual(guard_shell.main(), 2)
        self.assertIn("broken failed (ZeroDivisionError)", stderr.getvalue())

    def test_a_guard_that_cannot_be_imported_blocks_the_call(self) -> None:
        # An import error blocks explicitly instead of exiting 1 for all guards.
        stderr = io.StringIO()
        with (
            mock.patch.object(guard_shell, "GUARDS", ("guard_missing",)),
            mock.patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))),
            redirect_stderr(stderr),
        ):
            self.assertEqual(guard_shell.main(), 2)
        self.assertIn("guard_missing failed (ModuleNotFoundError)", stderr.getvalue())

    def test_a_powershell_call_runs_through_the_guards(self) -> None:
        payload = {
            "tool_name": "PowerShell",
            "tool_input": {"command": "Get-Content .env"},
        }
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload).encode(),
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_settings_run_every_guard_in_one_blocking_hook(self) -> None:
        # The five guards are all still run, now from one fail-closed hook (#2982).
        settings = json.loads(
            (ROOT / ".claude/settings.json").read_text(encoding="utf-8-sig")
        )
        shell = next(
            entry
            for entry in settings["hooks"]["PreToolUse"]
            if entry["matcher"] == "Bash|PowerShell"
        )
        (hook,) = shell["hooks"]
        self.assertIn("scripts/hooks/guard_shell.py", hook["command"])
        self.assertEqual(hook["onFailure"], "block")
        self.assertEqual(
            set(guard_shell.GUARDS),
            {
                "guard_private_paths",
                "guard_destructive_git",
                "guard_shell_backslashes",
                "guard_resolve_thread",
                "guard_empty_body",
            },
        )


if __name__ == "__main__":
    unittest.main()
