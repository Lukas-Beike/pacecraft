"""scripts/ledger_line.py with fake gh and git; no network."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gh_cli
import ledger_line

SHA = "0123456789abcdef0123456789abcdef01234567"
MERGED = {
    "state": "MERGED",
    "mergedAt": "2026-11-11T20:15:00Z",
    "mergeCommit": {"oid": SHA},
    "title": "chore(skills): add the ledger line",
}
NUMSTAT = "10\t2\tbackend/a.py\n6\t0\ttests/test_a.py\n3\t1\tdocs/a.md\n"


def fake_run(pr, issue_state="CLOSED", local=True, fetch_ok=True):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["gh", "issue"]:
            data = json.dumps({"state": issue_state})
            return subprocess.CompletedProcess(argv, 0, data, "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, json.dumps(pr), "")
        if argv[1:3] == ["cat-file", "-e"]:
            return subprocess.CompletedProcess(argv, int(not local), "", "")
        if argv[1] == "fetch":
            return subprocess.CompletedProcess(
                argv, int(not fetch_ok), "", "" if fetch_ok else "fatal: not our ref"
            )
        return subprocess.CompletedProcess(argv, 0, NUMSTAT, "")

    run.calls = calls
    return run


class LedgerLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_script(self, pr, *args, issue_state="CLOSED", **git_state):
        fake = fake_run(pr, issue_state, **git_state)
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(gh_cli.subprocess, "run", fake),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = ledger_line.main(["--issue", "2275", "--pr", "2280", *args])
        return code, out.getvalue(), err.getvalue(), fake

    def test_prints_one_line_with_the_merge_commit_balance_and_pr_title(self):
        code, out, _, fake = self.run_script(MERGED)
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            "2026-11-11 #2275 closed via PR #2280 01234567 prod +10/-2 tests +6/-0 "
            "other +3/-1: chore(skills): add the ledger line\n",
        )
        self.assertEqual(fake.calls[-1][-1], f"{SHA}^..{SHA}")
        self.assertFalse([c for c in fake.calls if "fetch" in c])  # already local

    def test_a_merge_commit_not_yet_local_is_fetched_once(self):
        # #2920: right after an auto-merge the squash commit is not fetched yet.
        code, out, _, fake = self.run_script(MERGED, local=False)
        self.assertEqual(code, 0)
        self.assertIn("prod +10/-2", out)
        fetches = [c[1:] for c in fake.calls if c[1:2] == ["fetch"]]
        self.assertEqual(fetches, [["fetch", "--quiet", "origin", SHA]])
        code, _, err, _ = self.run_script(MERGED, local=False, fetch_ok=False)
        self.assertEqual(code, 2)
        self.assertIn("not our ref", err)

    def test_open_issue_is_written_as_part(self):
        code, out, _, _ = self.run_script(MERGED, issue_state="OPEN")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("2026-11-11 #2275 part via PR #2280 "))

    def test_first_nonblank_note_line_replaces_the_title(self):
        note = Path(self.tmp.name) / "note.md"
        note.write_text("\n  Four base skills landed\nmore detail\n", encoding="utf-8")
        _, out, _, _ = self.run_script(MERGED, "--note-file", str(note))
        self.assertTrue(out.endswith(": Four base skills landed\n"))

    def test_refuses_unmerged_prs_without_computing_a_balance(self):
        for pr in (
            {**MERGED, "state": "OPEN", "mergedAt": None, "mergeCommit": None},
            {**MERGED, "state": "CLOSED", "mergedAt": None},
            {**MERGED, "mergeCommit": None},
        ):
            with self.subTest(state=pr["state"]):
                code, out, _, fake = self.run_script(pr)
                self.assertEqual(code, 1)
                self.assertIn("not merged", out)
                self.assertEqual(len(fake.calls), 1)

    def test_secret_shaped_note_is_refused(self):
        note = Path(self.tmp.name) / "note.md"
        note.write_text("ghp_" + "a" * 36, encoding="utf-8")
        code, _, err, fake = self.run_script(MERGED, "--note-file", str(note))
        self.assertEqual(code, 2)
        self.assertNotIn("ghp_", err)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
