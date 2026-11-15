"""scripts/hooks/guard_shell.py runs every shell guard in one process (#2982)."""

import io
import json
import subprocess
import sys
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
            ("git reset --hard", "guard_destructive_git"),
            ("python - <<'EOF'\nprint('a\\\\b')\nEOF", "guard_shell_backslashes"),
            (
                "python scripts/resolve_review_thread.py 1 PRRT_x && echo",
                "guard_resolve_thread",
            ),
            ('gh pr edit 1 --body ""', "guard_empty_body"),
        ):
            with self.subTest(guard=guard):
                result = run_hook(command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(b"Blocked", result.stderr)

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
        broken = SimpleNamespace(__name__="broken", main=lambda: 1 / 0)
        stderr = io.StringIO()
        with (
            mock.patch.object(guard_shell, "GUARDS", (broken,)),
            mock.patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))),
            redirect_stderr(stderr),
        ):
            self.assertEqual(guard_shell.main(), 2)
        self.assertIn("broken failed (ZeroDivisionError)", stderr.getvalue())

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
            {guard.__name__ for guard in guard_shell.GUARDS},
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
