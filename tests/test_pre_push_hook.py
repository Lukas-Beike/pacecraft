"""The pre-push hook runs the secret scan first, then the quality ratchet, the
generated-document check (#2949) and the architecture guards (#3071) side by side."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".githooks/pre-push"
STUB = """import os, sys, time
from pathlib import Path
time.sleep(float(os.environ.get({name!r} + "_SLEEP", "0")))
# A stub whose export the hook deleted under it logs that instead.
name = Path(__file__).name if Path(__file__).exists() else "deleted"
with open(os.environ["HOOK_LOG"], "a", encoding="utf-8") as log:
    print(name, sys.stdin.read().strip(), file=log)
sys.exit(int(os.environ.get({name!r}, "0")))
"""


@unittest.skipUnless(shutil.which("sh") and shutil.which("git"), "needs sh and git")
class PrePushHookTests(unittest.TestCase):
    def run_hook(
        self,
        secrets="0",
        quality="0",
        dirty_quality=None,
        docs="0",
        docs_sleep="0",
        guards="0",
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            for path, name in (
                ("scripts/check_push_secrets.py", "SECRETS_EXIT"),
                ("tools/quality_baseline.py", "QUALITY_EXIT"),
                ("scripts/regenerate_docs.py", "DOCS_EXIT"),
                ("tests/run_tests.py", "GUARDS_EXIT"),
            ):
                (root / path).parent.mkdir(exist_ok=True)
                (root / path).write_text(STUB.format(name=name), encoding="utf-8")
            subprocess.run([*git, "add", "."], cwd=root, check=True)
            subprocess.run([*git, "commit", "-qm", "stubs"], cwd=root, check=True)
            if dirty_quality is not None:
                # An uncommitted edit that would fail if the hook ran it.
                (root / "tools/quality_baseline.py").write_text(
                    f"raise SystemExit({dirty_quality})\n", encoding="utf-8"
                )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "python"
            shim.write_text(
                f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n'
            )
            shim.chmod(0o755)
            log = root / "log"
            result = subprocess.run(
                ["sh", HOOK.as_posix()],
                cwd=root,
                input="refs/heads/x 1 refs/heads/x 0\n",
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "HOOK_LOG": str(log),
                    "SECRETS_EXIT": secrets,
                    "QUALITY_EXIT": quality,
                    "DOCS_EXIT": docs,
                    "DOCS_EXIT_SLEEP": docs_sleep,
                    "GUARDS_EXIT": guards,
                },
            )
            text = log.read_text(encoding="utf-8") if log.exists() else ""
            lines = [line.rstrip() for line in text.splitlines()]
            return result, lines

    def test_clean_push_runs_both_checks_in_order(self):
        result, lines = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            lines[0], "check_push_secrets.py refs/heads/x 1 refs/heads/x 0"
        )
        self.assertEqual(
            sorted(lines[1:]),
            ["quality_baseline.py", "regenerate_docs.py", "run_tests.py"],
        )

    def test_secret_scan_failure_stops_before_quality(self):
        result, lines = self.run_hook(secrets="3")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(len(lines), 1)

    def test_quality_regression_blocks_the_push(self):
        for quality, docs, reason, other in (
            ("1", "0", "found new findings", "could not run"),
            ("2", "0", "could not run", "found new findings"),
            ("0", "1", "generated-document check failed", "ratchet"),
            ("0", "2", "generated-document check failed", "ratchet"),
            ("1", "1", "found new findings", "generated-document check failed"),
        ):
            with self.subTest(quality=quality, docs=docs):
                result, _lines = self.run_hook(quality=quality, docs=docs)
                self.assertEqual(result.returncode, 1)
                self.assertIn(reason, result.stderr)
                self.assertNotIn(other, result.stderr)

    def test_a_failed_architecture_guard_blocks_the_push(self):
        result, lines = self.run_hook(guards="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("an architecture guard failed", result.stderr)
        self.assertIn("run_tests.py", lines)
        # A passing ratchet and document check do not hide the guard failure.
        self.assertNotIn("found new findings", result.stderr)

    def test_a_failed_ratchet_waits_for_the_document_check(self):
        # The check links node_modules into the export: the hook must not
        # delete the export under it, even when the ratchet fails first.
        result, lines = self.run_hook(quality="1", docs_sleep="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("regenerate_docs.py", lines)
        self.assertNotIn("deleted", lines)

    def test_quality_runs_on_committed_head_not_the_working_tree(self):
        result, lines = self.run_hook(dirty_quality=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("quality_baseline.py", lines)


if __name__ == "__main__":
    unittest.main()
