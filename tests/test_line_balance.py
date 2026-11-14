"""scripts/line_balance.py with a fake git; no repository access."""

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gh_cli
import line_balance

NUMSTAT = (
    "10\t2\tbackend/coach/tools.py\n"
    "3\t1\tserver.py\n"
    "5\t0\tpublic/app.js\n"
    "7\t4\ttests/test_tools.py\n"
    "2\t2\te2e/home.spec.js\n"
    "9\t1\tdocs/setup.md\n"
    "4\t0\tscripts/new.py\n"
    "-\t-\tpublic/icons/logo.png\n"
    "1\t1\tbackend_notes.md\n"
    "0\t0\t.agents/skills/{ai-coach-x => pacecraft-x}/SKILL.md\n"
    "1\t1\ttests/{old.py => new.py}\n"
    "2\t0\tdocs/a.md => backend/b.py\n"
)


class FakeGit:
    def __init__(self, missing=0):
        self.calls = []
        self.missing = missing  # diffs that fail like an unfetched commit

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        if argv[1] == "diff" and self.missing:
            self.missing -= 1
            return subprocess.CompletedProcess(argv, 128, "", "fatal: bad revision")
        return subprocess.CompletedProcess(argv, 0, NUMSTAT, "")


class LineBalanceTests(unittest.TestCase):
    def balance(self, missing=0, **kwargs):
        fake = FakeGit(missing)
        with mock.patch.object(gh_cli.subprocess, "run", fake):
            return line_balance.balance(**kwargs), fake

    def test_sorts_files_into_three_categories_and_counts_binary_as_zero(self):
        result, _ = self.balance()
        self.assertEqual(
            result,
            {"production": (20, 3), "tests": (10, 7), "other": (14, 2)},
        )

    def test_default_and_commit_ranges(self):
        _, fake = self.balance()
        self.assertEqual(fake.calls[0][1:4], ["diff", "--numstat", "-M"])
        self.assertEqual(fake.calls[0][-1], "origin/develop...HEAD")
        _, fake = self.balance(base="origin/main", head="topic")
        self.assertEqual(fake.calls[0][-1], "origin/main...topic")
        _, fake = self.balance(commit="abc123")
        self.assertEqual(fake.calls[0][-1], "abc123^..abc123")

    def test_an_unfetched_merge_commit_is_fetched_once_then_diffed(self):
        # #2920: right after an auto-merge the squash commit is not local yet.
        result, fake = self.balance(missing=1, commit="abc123")
        self.assertEqual(result["production"], (20, 3))
        self.assertEqual([call[1] for call in fake.calls], ["diff", "fetch", "diff"])
        self.assertEqual(fake.calls[1][1:], ["fetch", "--quiet", "origin", "abc123"])
        for kwargs in ({"missing": 2, "commit": "abc123"}, {"missing": 1}):
            with self.subTest(**kwargs), self.assertRaises(gh_cli.CliError):
                self.balance(**kwargs)  # still missing, or a range: no fetch

    def test_main_prints_three_pasteable_lines(self):
        out = io.StringIO()
        with (
            mock.patch.object(gh_cli.subprocess, "run", FakeGit()),
            redirect_stdout(out),
        ):
            code = line_balance.main(["--commit", "abc123"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.getvalue().splitlines(),
            ["production: +20 / -3", "tests: +10 / -7", "other: +14 / -2"],
        )


if __name__ == "__main__":
    unittest.main()
