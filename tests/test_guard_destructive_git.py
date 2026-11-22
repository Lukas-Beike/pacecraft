"""The agent shell hook blocks git commands that discard work (#2635)."""

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "hooks" / "guard_destructive_git.py"


def run_hook(command: str) -> int:
    body = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=body.encode("utf-8"),
        capture_output=True,
        check=False,
    ).returncode


class GuardDestructiveGitTests(unittest.TestCase):
    def test_destructive_commands_are_blocked(self) -> None:
        for command in (
            "git reset --hard",
            "git reset --hard origin/develop",
            "git -C ../other reset -q --hard HEAD~1",
            "git clean -f",
            "git clean -fd",
            "git clean -xdf",
            "git clean --force",
            "git branch -D feat/x",
            "git branch -Dr origin/x",
            "git branch --delete --force feat/x",
            "git checkout .",
            "git checkout -- .",
            "git restore .",
            "git restore --staged --worktree .",
            "git push --force",
            "git push -f origin feat/x",
            "git push origin +feat/x",
            "git status && git reset --hard",
            "git fetch; git push --force origin feat/x",
            # Forms a shell still runs as the destructive command.
            "git clean -fdx;git status",
            "(git clean -fdx)",
            'bash -c "git push -f origin x"',
            'powershell -Command "git clean -fdx"',
            'git reset "--hard"',
            "git reset '--hard'",
            "git reset `--hard",
            "git push origin '+feat/x'",
            'git checkout -- "."',
            "git reset \\\n  --hard",
            "git push `\n  --force origin x",
            "Git reset --hard",
            "GIT.exe reset --hard",
            "git reset --ha",
            "git branch -d --force x",
            "git branch -df x",
            "git branch --delete x --force",
            "git restore ./",
            "git checkout .\\",
            "git restore :/",
            "git checkout -- *",
            "git checkout -f",
            "git switch --discard-changes develop",
            "git clean --forc",
            "git push --forc origin x",
            "git switch --discard develop",
            "git --git-dir /tmp/x reset --hard",
            "git -C .. -c core.hooksPath=x push --force",
            "/usr/bin/git reset --hard",
            # Rules without exceptions (#3923).
            "git commit --no-verify -m x",
            "git commit -n -F msg.txt",
            "git commit -anm x",
            "git push --no-verify origin feat/x",
            "git push --no-v",
            "git -C ../x merge --no-verify develop",
            "git add -A",
            "git add --all",
            "git add .",
            "git add -- :/",
            "git -C ../x add -vA",
            "gh pr merge 12 --auto",
            "gh pr merge --auto --merge 12",
            "gh pr merge 12 --auto --rebase",
            "gh pr merge 12 --auto --squash=false",
            "docker rm -v pacecraft",
            "docker rm -f -v pacecraft",
            "docker rm -fv pacecraft",
            "docker container rm --volumes pacecraft",
            "docker compose down -v",
            "docker compose -f compose.yml down --volumes",
            "docker-compose rm -v",
            "DOCKER.exe rm -v x",
            "ruff format",
            "ruff format tests",
            "ruff format .",
            "ruff format server.py backend",
            "python -m ruff format tests",
            "ruff format --config line-length=88 tests",
            "git stage -A",
            "git add ./*",
            "git add **",
            "gh pr -R owner/repo merge 12 --auto",
            "docker compose --ansi never down -v",
            "docker --tlscacert ca.pem rm -v x",
            "docker container remove -v x",
            "ruff format tests 2>&1",
            "ruff format . # all",
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command), 2)

    def test_safe_commands_pass(self) -> None:
        for command in (
            "git push",
            "git push -u origin feat/x",
            "git push --force-with-lease origin feat/x",
            "git push --force-with-lease=feat/x:abc123 --force-if-includes origin feat/x",
            "git reset --soft HEAD~1",
            "git reset HEAD file.py",
            "git clean -n",
            "git branch -d feat/x",
            "git checkout feat/x",
            "git checkout -- public/app.js",
            "git restore public/app.js",
            "git restore --staged public/app.js",
            "git worktree remove ../l16-2614",
            "git log --format=%h -5",
            "echo reset --hard",
            "git restore --staged .",
            'git stash push -m "wip 2 +3"',
            "git switch develop",
            "git -C ../l16-2614 status",
            "git commit -m x",
            "git commit --amend --no-edit",
            "git commit -am x",
            "git push -n origin feat/x",
            "git add backend/x.py tests/test_x.py",
            "git add -u",
            "git add -p backend/x.py",
            "git log --all",
            "git fetch --all",
            "gh pr merge 12 --auto --squash",
            "gh pr merge 12 --squash --auto --delete-branch",
            "gh pr merge 12 -s --auto",
            "gh pr merge 12 --disable-auto",
            "gh pr view 12 --json autoMergeRequest",
            "docker rm pacecraft",
            "docker rm -f pacecraft",
            "docker run --rm -v data:/data pacecraft:local",
            "docker compose down",
            "docker compose up -d",
            "docker volume ls",
            "ruff format server.py backend/config.py",
            "ruff format --check tests",
            "ruff format --diff backend",
            "ruff format --range 3:1-9:1 backend/x.py",
            "ruff check --select I backend",
            "git stage backend/x.py",
            "ruff format a.py 2>&1",
            "ruff format a.py > out.txt",
            "ruff format a.py # changed only",
            "ruff format --extension py:ipynb a.py",
            "ruff format tool.pyw",
            "ruff format --help",
            'rg "ruff format" docs',
            'grep -rn "docker rm -v" README.md docs',
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command), 0)

    def test_long_commands_are_decided_well_within_the_hook_budget(self) -> None:
        # #3124: these took 40 s and more, past the shared 10 s hook budget.
        for command, code in (
            ("git " * 50_000 + "push --force", 2),
            ("git -C x " * 30_000 + "reset --hard", 2),
            ("git push " * 30_000, 0),
            ("git push a" + " b" * 60_000, 0),
            ("gh " * 50_000 + "pr merge --auto", 2),
            ("docker -f x " * 30_000 + "rm -v x", 2),
            ("ruff format a.py" + " b.py" * 60_000, 0),
            ("ruff " * 50_000 + "format tests", 2),
        ):
            with self.subTest(command=command[-20:]):
                started = time.monotonic()
                self.assertEqual(run_hook(command), code)
                self.assertLess(time.monotonic() - started, 5)

    def test_unreadable_input_is_blocked(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=b"not json",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)

    def test_hook_runs_beside_the_private_path_guard(self) -> None:
        settings = json.loads(
            (ROOT / ".claude/settings.json").read_text(encoding="utf-8-sig")
        )
        shell = next(
            entry
            for entry in settings["hooks"]["PreToolUse"]
            if entry["matcher"] == "Bash|PowerShell"
        )
        # One hook process runs every shell guard in turn (#2982).
        commands = [hook["command"] for hook in shell["hooks"]]
        self.assertTrue(any("guard_shell.py" in c for c in commands))
        guards = (ROOT / "scripts/hooks/guard_shell.py").read_text(encoding="utf-8")
        for name in (
            "guard_private_paths",
            "guard_destructive_git",
            "guard_resolve_thread",
            "guard_empty_body",  # #2954
        ):
            self.assertIn(f'    "{name}",\n', guards, name)
        self.assertTrue(all(hook["onFailure"] == "block" for hook in shell["hooks"]))
        # The plain prefixes are also deny rules for both shells (#3923).
        for shell_name in ("Bash", "PowerShell"):
            for prefix in ("git commit --no-verify", "git add -A", "docker rm -v"):
                rule = f"{shell_name}({prefix}*)"
                self.assertIn(rule, settings["permissions"]["deny"])


if __name__ == "__main__":
    unittest.main()
