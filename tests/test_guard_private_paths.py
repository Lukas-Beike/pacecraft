"""The agent shell hook blocks commands that touch private runtime paths."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = (
    Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard_private_paths.py"
)
ENV = "." + "env"
DATA = "da" + "ta/"
TOKENS = "garmin_" + "tokens"


def run_hook(command: str, *, bom: bool = False, cwd: str = "") -> int:
    body = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
    )
    raw = (("\ufeff" if bom else "") + body).encode("utf-8")
    return subprocess.run(
        [sys.executable, str(HOOK)], input=raw, capture_output=True, check=False
    ).returncode


class GuardPrivatePathsTests(unittest.TestCase):
    def test_private_paths_are_blocked(self) -> None:
        bare = DATA.rstrip("/")
        for command in (
            f"cat {ENV}",
            f"cat -v {ENV}",
            f"cat *{ENV}",
            f"type {ENV}.local",
            f"cat {ENV}.production.local",
            f"docker run --rm --env-file {ENV} alpine env",
            f"ls {DATA}",
            f"ls ./{bare}",
            f"Get-Content ./{DATA}coach.db",
            f"cat ../{DATA}coach.db",
            f"Get-Content C:\\Users\\x\\ai-coach\\{bare}\\coach.db",
            f"Get-Content C:\\Users\\x\\pacecraft\\{bare}\\coach.db",
            f"cat /home/x/pacecraft/{bare}/coach.db",
            f"docker exec ai-coach ls /{bare}",
            f"docker exec ai-coach ls /{DATA}backups",
            f"type C:\\x\\{bare}\\{TOKENS}",
            f"cat {TOKENS}/oauth1_token.json",
            f"cat /srv/{TOKENS}/oauth2_token.json",
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command), 2)
        self.assertEqual(run_hook(f"cat {ENV}", bom=True), 2)

    def test_a_block_names_its_match_and_the_grep_tool(self) -> None:
        # A refused source search named no word to adapt (#3924).
        for command, word in (
            (f"rg -n {TOKENS}/ backend", f"{TOKENS}/"),
            (f"cat '{ENV}'", ENV),
        ):
            body = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
            result = subprocess.run(
                [sys.executable, str(HOOK)],
                input=body.encode("utf-8"),
                capture_output=True,
                check=False,
            )
            with self.subTest(command=command):
                self.assertEqual(result.returncode, 2)
                message = result.stderr.decode("utf-8")
                self.assertIn(f"(matched `{word}`)", message)
                self.assertIn("use the Grep tool", message)

    def test_ordinary_commands_pass(self) -> None:
        bare = DATA.rstrip("/")
        for command in (
            f"cat {ENV}.example",
            "git status",
            "grep -rn metadata backend",
            f'rg -n "{bare}" backend',
            f"git commit -m 'fix {bare} import'",
            f"cat tests/{DATA}fixture.json",
            f"grep -r {TOKENS} backend",
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command), 0)

    def test_secret_folder_is_blocked_outside_a_prod_script_password_file(self) -> None:
        # T3's secret folder holds the app password and database key (#3758).
        secret = "C:\\Users\\x\\.t3\\user" + "data\\secrets\\files\\abc"
        posix = "~/.t3/user" + "data/secrets/files/abc"
        for command in (
            f'cat "{posix}"',
            f"head -1 {posix}",
            f'Get-Content -Raw "{secret}"',
            f'Get-Content -Raw "{secret}" | node scripts/post_release_actions.mjs --url u',
            f'node scripts/post_release_actions.mjs --url u < "{posix}"',
            f'gh pr comment 1 --body-file "{secret}"',
            f"ls ~/.t3/user{'data'}/secrets",
            f"cd ~/.t3 && cat user{'data'}/secrets/files/abc",
            f'python scripts/prod_guards.py --url u --password-file "{secret}"; cat "{secret}"',
            f'python scripts/prod_guards.py --url u --password-file "$(cat {posix})"',
            f"python scripts/prod_guards.py --url u --password-file {posix} --out {posix}",
            f"python scripts/prod_guards.py --url u --password-file={posix}x`whoami`",
            f'python scripts/other.py --password-file "{secret}"',
            f'python -c "print(1)" scripts/prod_guards.py --password-file "{secret}"',
            f'node scripts/evil.mjs --password-file "{secret}"',
            # #3913: only the vetted explorer itself, and only its --password-file value.
            f'node scripts/ui_explorer.mjs --password-file "{secret}"',
            f'node scripts/ui_explore.js --password-file "{secret}"',
            f'node scratch/ui_explore.mjs --password-file "{secret}"',
            f'node C:\\Temp\\scripts\\ui_explore.mjs --password-file "{secret}"',
            f'node --import ./x.mjs scripts/ui_explore.mjs --password-file "{secret}"',
            f'node scripts/ui_explore.mjs --password-file "{secret}" --plan "{secret}"',
            f'node scripts/ui_explore.mjs --password-file "{secret}" --plan - < "{secret}"',
            f"python scripts/prod_guards.py --url u --password-f {posix}",
            f"python scripts/prod_client.py --password-file {posix}",
            f"python scripts/prod_dump.py --password-file {posix}",
            f"cd /tmp/x && python scripts/prod_dump.py --password-file {posix}",
            # Forms that reach the folder without the literal path.
            f"cat ~/.t3/user{'data'}/./secrets/files/abc",
            f"cat ~/.t3/user{'data'}/x/../secrets/files/abc",
            f'cat ~/.t3/user{"data"}/sec""rets/files/abc',
            f"cat ~/.t3/user{'data'}/'secrets'/files/abc",
            f"Get-Content ~/.t3/user{'data'}/sec`rets/files/abc",
            f"cat ~/.t3/user{'data'}/s*/files/*",
            f"cat ~/.t3/user{'data'}/{{secrets,x}}/abc",
            f"cat ~/.t3/user{'data'}/*/files/abc",
            "type C:\\Users\\x\\.t3\\USERDA~1\\SECRET~1\\abc",
            f"cd ~/.t3/user{'data'}; gh issue comment 1 --body-file secrets/abc",
            f"Get-ChildItem ~/.t3/user{'data'} -Recurse -File | Get-Content",
            f"New-Item -ItemType Junction -Path l -Target ~/.t3/user{'data'}",
            "cd ~/.t3; cd userdata; cat secrets/abc",
            f"cd ~/.t?/user{'data'}; cat secrets/abc",
            f"cd ~/.t*/user{'data'} && cat secrets/*",
            "cd ~/.t*; cat user*/secrets/abc",
            "Get-ChildItem C:\\Users\\x\\.t3 -Recurse",
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command), 2)
        # #3940: an earlier segment that could put other code behind the vetted
        # name (directory, interpreter options, a function or alias, the file
        # itself) blocks the run, for every allowed script.
        runs = [
            "python scripts/prod_diagnostics.py", "python scripts/prod_guards.py",
            "python scripts/prod_logs.py", "node scripts/ui_walk.mjs",
            "node scripts/ui_explore.mjs", "node scripts/post_release_actions.mjs",
        ]  # fmt: skip
        before = [
            "cd ../other", "Set-Location C:\\x", "pushd x", "sl x",
            "export NODE_OPTIONS=--require=./x.js", "$env:NODE_OPTIONS='--import ./x.mjs'",
            "PYTHONPATH=x", "export PYTHONSTARTUP=x.py", "set PYTHONPATH=x",
            "node() { cat $4; }", "function python { cat $args }", "alias node=cat",
            "Set-Alias node Get-Content", "cp x.mjs scripts/ui_walk.mjs",
            "git checkout other -- scripts", "cat x > scripts/prod_guards.py",
            "git status && cd x",
            # An escaped separator joins both into one command that reads the file.
            "cat a\\", "cat a`", "git status\\",
        ]  # fmt: skip
        # #4075 review: a segment that runs alongside the run (a pipe or `&`
        # after it) could rewrite the script while it starts.
        after = [
            "| cp x.mjs scripts/ui_walk.mjs", "| head > scripts/prod_guards.py",
            "& cp x.py scripts/prod_logs.py", "| tee scripts/ui_explore.mjs",
            "| head -n $(cp x scripts/ui_walk.mjs)", "| Select-Object -First 1 | sh",
        ]  # fmt: skip
        # #4075 gate: an escaped `>` before `&` is no stream copy; the `&` splits.
        for command in (
            f"python scripts/prod_logs.py \\>&7z a -- out.7z --password-file {posix}",
            f"python scripts/prod_logs.py `>&7z a -- out.7z --password-file {posix}",
            f"python scripts/prod_logs.py '>'&7z a -- o.7z --password-file {posix}",
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command), 2)
        for run in runs:
            for last in after:
                command = f'{run} --url u --password-file "{secret}" {last}'
                with self.subTest(command=command):
                    self.assertEqual(run_hook(command), 2)
        for run in runs:
            for first in before:
                command = f'{first}; {run} --url u --password-file "{secret}"'
                with self.subTest(command=command):
                    self.assertEqual(run_hook(command), 2)
        for command in (
            f'python scripts/prod_diagnostics.py --url u --password-file "{secret}"',
            f"python scripts/prod_logs.py --url u --password-file={posix} --since x",
            f"py .\\scripts\\prod_guards.py --url u --password-file '{secret}'",
            f'node scripts/ui_walk.mjs --url u --password-file "{secret}" --out-dir t',
            f'node scripts/ui_explore.mjs --url u --password-file "{secret}" --out-dir t --plan - < p.json',
            f"Get-Content p.json | node .\\scripts\\ui_explore.mjs --url u --password-file={posix} --plan -",
            f'& node scripts/post_release_actions.mjs --url u --password-file "{secret}"',
            f'git status && node scripts/post_release_actions.mjs --password-file "{secret}" --url u',
            f"cat p.json | node scripts/ui_explore.mjs --url u --password-file={posix} --plan -",
            # A later segment cannot change what already ran.
            f'node scripts/ui_walk.mjs --password-file "{secret}" --url u; cd x',
            # A reader of its output may run alongside (#4075 review).
            f'python scripts/prod_guards.py --url u --password-file "{secret}" 2>&1 | Select-Object -Last 5',
            f"node scripts/ui_walk.mjs --password-file {posix} --url u | tail -3 && cd x",
            f'py.exe -3 scripts/prod_guards.py --url u --password-file "{secret}"',
            f'python3.14.exe -I scripts/prod_logs.py --password-file "{secret}"',
            f'python scripts/prod_diagnostics.py --url u \\\n  --password-file "{secret}"',
            f'node scripts/ui_walk.mjs --url u `\n  --password-file "{secret}" --out-dir t',
            "grep -n T3_SECRETS scripts/prod_client.py",
            f"ls ~/.t3/user{'data'}/attachments",
            f'grep -c -i "user{"data"}\\|post_release" x',
            "git -C C:\\Users\\x\\.t3\\worktrees\\a status",
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command), 0)

    def test_unreadable_input_is_blocked(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=b"not json",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)


class GuardServerImportTests(unittest.TestCase):
    """Importing or running server.py loads local settings files (#3076)."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "checkout"
        self.data = Path(temporary.name) / "fixture-data"
        self.root.mkdir()
        self.data.mkdir()

    def test_importing_or_running_the_server_is_blocked_without_a_temporary_data_dir(
        self,
    ) -> None:
        for command in (
            'python -c "import server"',
            'python -c "import os, server"',
            'python3 -c "from server import ROOT"',
            "python server.py",
            "python -u ./server.py",
            "py -m server",
            "python - <<'EOF'\nimport server\nEOF",
            'python -c "import runpy; runpy.run_path(\'server.py\')"',
            'python -c "import importlib; importlib.import_module(\'server\')"',
            'DATA_DIR=./fixture python -c "import server"',
        ):
            with self.subTest(command=command):
                self.assertEqual(2, run_hook(command, cwd=str(self.root)))

    def test_a_temporary_data_dir_does_not_cover_another_checkout_or_process(
        self,
    ) -> None:
        data = self.data
        for command in (
            # Another checkout's server.py loads that checkout's settings file.
            f"DATA_DIR={data} python ../other/server.py",
            f'cd ../other && DATA_DIR={data} python -c "import server"',
            f'PYTHONPATH=../other DATA_DIR={data} python -c "import server"',
            f"DATA_DIR={data} python -c \"import os; os.chdir('..'); import server\"",
            f"DATA_DIR={data} python -c \"import sys; sys.path.insert(0, '..'); import server\"",
            # The assignment binds another process, or is no assignment at all.
            f'DATA_DIR={data} python -m compileall -q x.py && python -c "import server"',
            f"echo DATA_DIR={data}; python server.py",
            f'python -c "import server"; export DATA_DIR={data}',
            # A shell expansion makes the resolved path not the one the server gets.
            f'DATA_DIR={data}/${{X:-..}}/other python -c "import server"',
            f'DATA_DIR={data}/`echo ..` python -c "import server"',
            # The assignment may never apply, or is undone before the import.
            f'false && export DATA_DIR={data}; python -c "import server"',
            f'(export DATA_DIR={data}); python -c "import server"',
            f'export DATA_DIR={data}; unset DATA_DIR; python -c "import server"',
            f"DATA_DIR={data} python -c \"import os; os.environ.pop('DATA_DIR'); import server\"",
            f"$env:DATA_DIR = '{data}'; Remove-Item Env:DATA_DIR; python -c \"import server\"",
        ):
            with self.subTest(command=command):
                self.assertEqual(2, run_hook(command, cwd=str(self.root)))

    def test_a_temporary_data_dir_in_a_checkout_without_local_settings_is_allowed(
        self,
    ) -> None:
        for command in (
            f'DATA_DIR={self.data} python -c "import server"',
            f'APP_PASSWORD=x DATA_DIR={self.data} python -c "import server"',
            f'export DATA_DIR={self.data}; python -c "import server"',
            f"$env:DATA_DIR = '{self.data}'; python -c \"import server\"",
            f"DATA_DIR={self.data} python server.py",
        ):
            with self.subTest(command=command):
                self.assertEqual(0, run_hook(command, cwd=str(self.root)))

    def test_a_local_settings_file_blocks_even_with_a_temporary_data_dir(self) -> None:
        command = f'DATA_DIR={self.data} python -c "import server"'
        for holder in (self.root, self.data):
            with self.subTest(holder=holder.name):
                settings = holder / ENV
                settings.write_text("", encoding="utf-8")
                self.assertEqual(2, run_hook(command, cwd=str(self.root)))
                settings.unlink()

    def test_text_about_the_server_without_running_it_passes(self) -> None:
        for command in (
            'grep -n "import server" docs/development.md',
            "python tests/run_tests.py --module test_server_module_architecture",
            'python -c "import server_composition_check"',
            "git log -- server.py",
            "python -m compileall -q server.py backend tests",
        ):
            with self.subTest(command=command):
                self.assertEqual(0, run_hook(command, cwd=str(self.root)))


if __name__ == "__main__":
    unittest.main()
