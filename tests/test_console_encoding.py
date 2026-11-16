"""The repository scripts print issue, PR and athlete text on a cp1252 console (#3312).

Each check runs a real interpreter whose standard streams are cp1252, as on the
Windows host, so a script that prints α, an emoji or an umlaut cannot crash after
it already wrote to GitHub.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TEXT = "DFA-α1 ✓ Ü 🏃"


def cp1252_run(code: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
    )


def gh_cli_scripts() -> list[str]:
    """Every script module that imports the shared CLI helper."""
    uses = re.compile(r"^(?:import gh_cli|from gh_cli import)", re.MULTILINE)
    return sorted(
        path.stem
        for path in SCRIPTS.glob("*.py")
        if path.stem != "gh_cli" and uses.search(path.read_text(encoding="utf-8"))
    )


class ConsoleEncodingTests(unittest.TestCase):
    def test_every_script_on_the_shared_helper_prints_unicode(self):
        modules = gh_cli_scripts()
        self.assertIn("issue_update", modules)
        for module in modules:
            with self.subTest(script=module):
                done = cp1252_run(
                    f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); "
                    f"import {module}; print({TEXT!r}); print({TEXT!r}, file=sys.stderr)"
                )
                self.assertEqual(
                    done.returncode, 0, done.stderr.decode(errors="replace")
                )
                self.assertIn(TEXT, done.stdout.decode("utf-8"))
                self.assertIn(TEXT, done.stderr.decode("utf-8"))

    def test_a_tick_with_alpha_confirms_instead_of_crashing_after_the_write(self):
        # L24's case: the box was ticked, then printing the confirmation crashed.
        driver = (
            f"import sys; sys.path[:0] = [{str(SCRIPTS)!r}, {str(ROOT / 'tests')!r}]; "
            "import gh_cli, issue_update; "
            "from test_issue_update import FakeGh; "
            f"fake = FakeGh(body='- [ ] Check {TEXT}\\n'); "
            "gh_cli.subprocess.run = fake; "
            "code = issue_update.main(['tick', '12', '--part', 'Check DFA-α1']); "
            "assert fake.edited_body is not None; sys.exit(code)"
        )
        done = cp1252_run(driver)
        self.assertEqual(done.returncode, 0, done.stderr.decode(errors="replace"))
        self.assertIn(f"Check {TEXT}", done.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
