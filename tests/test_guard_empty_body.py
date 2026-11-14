"""The agent shell hook blocks a gh PR or issue write with an empty body (#2954)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "hooks" / "guard_empty_body.py"


def run_hook(command: str, cwd: Path) -> int:
    body = json.dumps({"tool_input": {"command": command}, "cwd": str(cwd)})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=body.encode("utf-8"),
        capture_output=True,
        check=False,
        env={**os.environ, "BODY_DIR": str(cwd)},
    ).returncode


class GuardEmptyBodyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name)
        (self.cwd / "empty.md").write_text("")
        (self.cwd / "blank.md").write_text(" \n\t\n")
        (self.cwd / "body.md").write_text("## Change\n\nCloses #1\n")
        # PowerShell's Set-Content of '' writes a byte-order mark and a newline.
        (self.cwd / "bom.md").write_bytes(b"\xef\xbb\xbf\r\n")
        (self.cwd / "utf16.md").write_bytes(" \r\n".encode("utf-16"))

    def test_empty_bodies_are_blocked(self):
        # PR #2938: an empty file from a failed `gh pr view > F` blanked the body.
        absolute = (self.cwd / "empty.md").as_posix()
        for command in (
            "gh pr edit 2938 --body-file empty.md",
            "gh pr edit 2938 --body-file blank.md",
            "gh pr create --base develop --title t --body-file=empty.md",
            "gh issue create --title t --label retro -F empty.md",
            "gh issue comment 5 --body-file empty.md",
            "gh pr review 7 --comment -F empty.md",
            f'gh pr edit 2938 --body-file "{absolute}"',
            'gh pr edit 2938 --body-file "$BODY_DIR/empty.md"',  # exported variable
            'gh pr edit 2938 --body ""',
            "gh issue edit 5 -b '  '",
            "git status && gh pr edit 2938 --body-file empty.md",
            "gh.exe pr edit 2938 --body-file empty.md",
            "gh pr edit 2938 -Fempty.md",
            "gh pr edit 2938 --body-file bom.md",
            "gh pr edit 2938 --body-file utf16.md",
            "gh pr edit 2938 \\\n  --body-file empty.md",  # Bash continuation
            "gh pr edit 2938 `\n  --body-file empty.md",  # PowerShell continuation
            "echo $(gh pr edit 2938 --body-file empty.md)",
            "gh pr edit 2938 --body-file $env:BODY_DIR/empty.md",
            # Gitar P2: separators inside a quoted title stay inside it.
            'gh pr create --title "fix(hooks): a; b & c | d" --body-file empty.md',
            "gh pr edit 2938 --title 'x (y)' --body-file empty.md",
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command, self.cwd), 2)

    def test_other_commands_pass(self):
        for command in (
            "gh pr edit 2938 --body-file body.md",
            "gh pr edit 2938 --body-file missing.md",  # gh refuses it itself
            "gh pr edit 2938 --body-file -",  # stdin cannot be checked
            "gh pr view 2938 --json body > empty.md",
            "gh api graphql -F owner=o -F name=r",
            "gh issue view 5 --json body",
            "gh pr edit 2938 --title t",
            'gh pr comment 7 --body "Fixed in abc"',
            "cat empty.md",
            'gh pr review 7 --approve --body ""',  # an approval needs no text
            # The command writes the file first: its new content is unknown.
            "gh pr view 1 --json body -q .body > empty.md && gh pr edit 1 -F empty.md",
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command, self.cwd), 0)


if __name__ == "__main__":
    unittest.main()
