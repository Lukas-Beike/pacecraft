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

    def test_a_temporary_data_dir_in_a_checkout_without_local_settings_is_allowed(
        self,
    ) -> None:
        for command in (
            f'DATA_DIR={self.data} python -c "import server"',
            f"$env:DATA_DIR = '{self.data}'; python -c \"import server\"",
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
