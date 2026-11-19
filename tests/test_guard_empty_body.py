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


WORKFLOW = ROOT / ".github" / "workflows" / "validate-conventions.yml"
# The validate step's regex and message the hook mirrors (#3535).
CI_REGEX = (
    "link_regex='(^|[^[:alnum:]_])([Cc]loses|[Ff]ixes|[Rr]esolves|[Pp]art of) #[0-9]+'"
)
CI_MESSAGE = (
    "The PR body must name its issue with 'Closes #N' (completes it) or "
    "'Part of #N' (one of several PRs)."
)


def run(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    body = json.dumps({"tool_input": {"command": command}, "cwd": str(cwd)})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=body,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        env={**os.environ, "BODY_DIR": str(cwd)},
    )


def run_hook(command: str, cwd: Path) -> int:
    return run(command, cwd).returncode


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

    def test_a_pr_body_without_its_issue_link_is_blocked_as_ci_would(self):
        # #3535: PR #3532 began with "Refs #3530" and failed validate.
        for name, body in (
            ("refs.md", "## Change\n\nRefs #3530\n"),
            ("glued.md", "This encloses #12 only.\n"),
            ("bare.md", "Fixes the issue link.\n"),
            ("utf16-refs.md", None),
        ):
            if body is not None:
                (self.cwd / name).write_text(body, encoding="utf-8")
        (self.cwd / "utf16-refs.md").write_bytes("Refs #1\r\n".encode("utf-16"))
        (self.cwd / "issue.md").write_text("Refs #1\n", encoding="utf-8")
        for name, body in (
            ("closes.md", "## Change\n\nCloses #3535\n"),
            ("lower.md", "closes #7"),
            ("part.md", "First part.\nPart of #3550\n"),
            ("fixes.md", "(Fixes #9)"),
            ("resolves.md", "Resolves #10."),
        ):
            (self.cwd / name).write_text(body, encoding="utf-8")
        for command in (
            "gh pr create --base develop --title t --body-file refs.md",
            "gh pr create --title t -F glued.md",
            "gh pr edit 3532 --body-file bare.md",
            "gh pr edit 3532 --body-file utf16-refs.md",
            'gh pr create --title t --body "Refs #3530"',
            "gh pr edit 3532 -b 'see #3530'",
            "git status && gh pr create --title t --body-file refs.md",
            'gh pr create --title "fix(x): a; b" --body-file refs.md',
            "gh pr edit 1 --body-file closes.md && gh pr edit 2 --body-file refs.md",
            "gh pr create --title t --body-file closes.md --body-file refs.md",
        ):
            with self.subTest(command=command):
                result = run(command, self.cwd)
                self.assertEqual(2, result.returncode)
                self.assertIn(CI_MESSAGE, result.stderr)
        for command in (
            "gh pr create --title t --body-file closes.md",
            "gh pr create --title t -F lower.md",
            "gh pr edit 3550 --body-file part.md",
            "gh pr edit 9 --body-file fixes.md",
            "gh pr edit 10 --body-file=resolves.md",
            'gh pr create --title t --body "Closes #1"',
            "gh pr create --title t --fill",  # the body is unknown here
            "gh pr edit 1 --title t --add-label retro",
            "gh pr edit 1 --body-file -",  # stdin cannot be checked
            "gh pr create --title t --body-file missing.md",  # gh refuses it
            # Comments, reviews and issues are not PR bodies.
            "gh pr comment 1 --body-file issue.md",
            "gh pr review 1 --comment -b 'Refs #1'",
            "gh issue create --title t --body-file issue.md",
            "gh issue comment 1 --body-file refs.md && gh pr edit 1 --body-file part.md",
            # The command writes the body first: its new content is unknown.
            "gh pr view 1 --json body -q .body > refs.md && gh pr edit 1 -F refs.md",
        ):
            with self.subTest(command=command):
                self.assertEqual(0, run_hook(command, self.cwd))

    def test_the_link_rule_is_the_validate_step_s(self):
        # #3535: the hook mirrors CI; a change there must change it here too.
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(CI_REGEX, workflow)
        self.assertIn(CI_MESSAGE, workflow)


if __name__ == "__main__":
    unittest.main()
