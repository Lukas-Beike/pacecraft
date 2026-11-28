"""scripts/develop_run_check.py with a fake GitHub CLI; no network."""

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import develop_run_check
import gh_cli

# A full SHA: gh run list --commit matches only all 40 characters (#3796).
FULL_SHA = "abc12345def" + "0" * 29
MERGED = {"mergedAt": "2026-11-11T10:00:00Z", "mergeCommit": {"oid": FULL_SHA}}


def run_record(
    run_id,
    workflow,
    sha,
    created,
    status="completed",
    conclusion="success",
    event="push",
):
    return {
        "event": event,
        "databaseId": run_id,
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion,
        "url": f"https://example.test/runs/{run_id}",
        "headSha": sha,
        "createdAt": created,
    }


class FakeGh:
    def __init__(self, pr=MERGED, runs=(), jobs=(), later=()):
        self.pr, self.runs, self.jobs = pr, list(runs), list(jobs)
        # (call count, runs): from that call on, the run list is those runs.
        self.later = dict(later)
        self.calls = []

    def __call__(self, argv, **_kwargs):
        self.runs = self.later.get(len(self.calls), self.runs)
        self.calls.append(argv)
        if argv[1:2] == ["api"] and "/commits/" in argv[2]:
            # GitHub resolves only the fixture commit's prefixes (HTTP 422 else).
            known = FULL_SHA.startswith(argv[2].rsplit("/", 1)[1])
            return subprocess.CompletedProcess(
                argv,
                0 if known else 1,
                FULL_SHA + "\n" if known else "",
                "" if known else "HTTP 422: No commit found for SHA",
            )
        if argv[1:3] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.pr), "")
        if argv[1:3] == ["run", "list"]:
            runs = self.runs
            if "--event" in argv:
                runs = [
                    r for r in runs if r["event"] == argv[argv.index("--event") + 1]
                ]
            if "--commit" in argv:
                sha = argv[argv.index("--commit") + 1]
                runs = [r for r in runs if r["headSha"] == sha]
            return subprocess.CompletedProcess(argv, 0, json.dumps(runs), "")
        if argv[1:3] == ["run", "view"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"jobs": self.jobs}), ""
            )
        return subprocess.CompletedProcess(argv, 1, "", "unexpected call")


def check(*args, clock=None, **fake_kwargs):
    """Run main against a fake gh; ``clock`` (a one-item list of seconds) is
    the fake time that --wait reads and sleeps on."""
    fake = FakeGh(**fake_kwargs)
    clock = clock if clock is not None else [0.0]
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.object(gh_cli.subprocess, "run", fake),
        mock.patch.object(develop_run_check, "monotonic", lambda: clock[0]),
        mock.patch.object(
            develop_run_check, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)
        ),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        code = develop_run_check.main(list(args))
    return code, out.getvalue(), err.getvalue(), fake


SHA = FULL_SHA
CI = ("CI", SHA, "2026-11-11T10:01:00Z")
FAILED_JOBS = [
    {"name": "tests (3.14)", "conclusion": "failure"},
    {"name": "lint", "conclusion": "success"},
]


class DevelopRunCheckTests(unittest.TestCase):
    def test_refuses_unmerged_pr_without_listing_runs(self):
        for pr in ({"mergedAt": None, "mergeCommit": None}, {"mergedAt": "x"}):
            with self.subTest(pr=pr):
                code, out, _, fake = check("--pr", "5", pr=pr)
                self.assertEqual(code, 1)
                self.assertIn("not merged", out)
                self.assertFalse([c for c in fake.calls if c[1:3] == ["run", "list"]])

    def test_all_green_resolves_the_merge_commit(self):
        runs = [run_record(1, *CI), run_record(2, "Docs", SHA, "2026-11-11T10:02:00Z")]
        code, out, _, fake = check("--pr", "#5", runs=runs)
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("CI: green", out)
        listing = fake.calls[1]
        self.assertEqual(listing[listing.index("--commit") + 1], SHA)
        self.assertEqual(listing[listing.index("--branch") + 1], "develop")
        self.assertEqual(listing[listing.index("--event") + 1], "push")

    def test_pending_and_missing_runs_exit_three(self):
        runs = [run_record(1, *CI, status="in_progress", conclusion=None)]
        code, out, _, _ = check("--sha", SHA, runs=runs)
        self.assertEqual((code, "CI: pending" in out), (3, True))
        code, out, _, _ = check("--sha", SHA, runs=[])
        self.assertEqual((code, "no develop push runs" in out), (3, True))

    def test_wait_polls_pending_runs_until_green_or_the_end(self):
        # #3721: --wait polls while the answer would be 3, on a fake clock;
        # --pr resolves the merge commit once.
        pending = [run_record(1, *CI, status="in_progress", conclusion=None)]
        green = [run_record(1, *CI)]
        failed = [run_record(1, *CI, conclusion="failure")]
        code, out, _, fake = check(
            "--pr", "5", "--wait", "5", runs=pending, later=[(4, green)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "CI: green (success) https://example.test/runs/1\n")
        self.assertEqual(
            [c[1:3] for c in fake.calls], [["pr", "view"]] + [["run", "list"]] * 4
        )
        clock = [0.0]
        code, out, _, fake = check(
            "--sha", SHA, "--wait", "2", clock=clock, runs=pending
        )
        self.assertEqual((code, out.count("CI: pending")), (3, 1))
        self.assertIn("still pending after 2 min", out)
        self.assertEqual((len(fake.calls), clock[0]), (5, 120))  # 0, 30, ..., 120 s
        # A run that fails while waiting ends the wait at once.
        code, out, _, fake = check(
            "--sha", SHA, "--wait", "5", runs=pending, later=[(1, failed)]
        )
        self.assertEqual((code, "CI: failed" in out), (1, True))
        self.assertNotIn("still pending", out)
        # Without --wait one pending look is the answer.
        self.assertEqual(check("--sha", SHA, runs=pending)[3].calls.__len__(), 1)

    def test_failure_beats_pending(self):
        runs = [
            run_record(1, *CI, conclusion="failure"),
            run_record(2, "Docs", SHA, "2026-11-11T10:02:00Z", "queued", None),
        ]
        code, _, _, _ = check("--sha", SHA, runs=runs, jobs=FAILED_JOBS)
        self.assertEqual(code, 1)

    def test_failed_run_reports_previous_conclusion_and_failed_jobs(self):
        previous = run_record(0, "CI", "older", "2026-11-10T10:00:00Z")
        # A newer develop push cancels the run before it (#3353); the hint
        # compares with the last run that finished.
        cancelled = run_record(
            2, "CI", "between", "2026-11-11T09:00:00Z", conclusion="cancelled"
        )
        runs = [previous, cancelled, run_record(1, *CI, conclusion="failure")]
        code, out, _, fake = check("--sha", SHA, runs=runs, jobs=FAILED_JOBS)
        self.assertEqual(code, 1)
        self.assertIn("CI: failed (failure)", out)
        self.assertIn("possible flake: previous push run 0 success", out)
        self.assertIn("failed jobs: tests (3.14)", out)
        self.assertNotIn("lint", out)
        self.assertIn(["gh", "run", "view", "1", "--json", "jobs"], fake.calls)

    def test_failed_run_after_failed_previous_is_not_a_flake(self):
        previous = run_record(
            0, "CI", "older", "2026-11-10T10:00:00Z", conclusion="failure"
        )
        runs = [previous, run_record(1, *CI, conclusion="failure")]
        _, out, _, _ = check("--sha", SHA, runs=runs, jobs=FAILED_JOBS)
        self.assertIn("previous push run 0 failure", out)
        self.assertNotIn("possible flake", out)

    def test_failed_run_with_newer_push_run_is_superseded(self):
        newer = run_record(
            3, "CI", "newer", "2026-11-11T11:00:00Z", "in_progress", None
        )
        runs = [run_record(1, *CI, conclusion="failure"), newer]
        code, out, _, fake = check("--sha", SHA, runs=runs, jobs=FAILED_JOBS)
        self.assertEqual(code, 1)
        self.assertIn("superseded: newer push run 3", out)
        self.assertIn("do not rerun", out)
        self.assertNotIn("possible flake", out)
        self.assertFalse([c for c in fake.calls if c[1:3] == ["run", "view"]])

    def test_cancelled_run_follows_the_newest_push_run(self):
        # #2956: a newer develop push cancels the run; the newer run's head
        # contains the commit, so its result is the answer.
        cancelled = run_record(1, *CI, conclusion="cancelled")
        later = "2026-11-11T11:00:00Z"
        for newer, expect in (
            (run_record(3, "CI", "newer", later), 0),
            (run_record(3, "CI", "newer", later, conclusion="failure"), 1),
            (run_record(3, "CI", "newer", later, "queued", None), 3),
        ):
            with self.subTest(newer=newer["conclusion"] or newer["status"]):
                code, out, _, _ = check(
                    "--sha", SHA, runs=[cancelled, newer], jobs=FAILED_JOBS
                )
                self.assertEqual(code, expect)
                self.assertIn("runs/3 (newer run at newer; 1 was cancelled)", out)
        # Cancelled with no newer run of its own workflow stays red: nothing
        # at all, an older run, or a newer run of another workflow.
        older = run_record(0, "CI", "older", "2026-11-10T10:00:00Z")
        other = run_record(4, "Docs", "newer", "2026-11-11T11:00:00Z")
        for history in ([cancelled], [older, cancelled], [cancelled, other]):
            with self.subTest(history=[r["databaseId"] for r in history]):
                code, out, _, _ = check("--sha", SHA, runs=history, jobs=FAILED_JOBS)
                self.assertEqual(code, 1)
                self.assertIn("CI: failed (cancelled)", out)
                self.assertNotIn("newer run at", out)

    def test_a_cancelled_push_run_follows_a_newer_run_of_the_same_commit(self):
        # #4368: the auto-rebase workflow also runs on workflow_run; that instance's
        # concurrency group cancels the push run of the same commit a few seconds
        # later, and it is the one that finished.
        cancelled = run_record(1, *CI, conclusion="cancelled")
        for event, conclusion, expect in (
            ("workflow_run", "success", 0),
            ("workflow_run", "failure", 1),
            ("workflow_run", None, 3),
        ):
            status = "completed" if conclusion else "in_progress"
            other = run_record(
                2, "CI", SHA, "2026-11-11T10:01:03Z", status, conclusion, event
            )
            with self.subTest(event=event, conclusion=conclusion):
                code, out, _, _ = check(
                    "--sha", SHA, runs=[cancelled, other], jobs=FAILED_JOBS
                )
                self.assertEqual(code, expect, out)
                self.assertIn("runs/2 (newer run at abc12345; 1 was cancelled)", out)
        # No newer run of any event: the cancelled run stands as failed. A newer run
        # of another workflow or of an older commit does not count.
        for history in (
            [cancelled],
            [
                cancelled,
                run_record(
                    5, "Docs", SHA, "2026-11-11T10:01:03Z", event="workflow_run"
                ),
            ],
            [
                cancelled,
                run_record(6, "CI", SHA, "2026-11-11T09:00:00Z", event="workflow_run"),
            ],
            # A manual run of the same commit says nothing about a cancelled push run.
            [
                cancelled,
                run_record(
                    7, "CI", SHA, "2026-11-11T10:01:03Z", event="workflow_dispatch"
                ),
            ],
        ):
            with self.subTest(history=[r["databaseId"] for r in history]):
                code, out, _, _ = check("--sha", SHA, runs=history, jobs=FAILED_JOBS)
                self.assertEqual(code, 1, out)
                self.assertIn("CI: failed (cancelled)", out)
        # A failed push run is not "superseded" by a later run of another event: the
        # hint stays the failed job names and the previous push run.
        failed = run_record(1, *CI, conclusion="failure")
        later = run_record(
            2,
            "CI",
            SHA,
            "2026-11-11T10:01:03Z",
            event="workflow_run",
            conclusion="failure",
        )
        code, out, _, _ = check("--sha", SHA, runs=[failed, later], jobs=FAILED_JOBS)
        self.assertEqual(code, 1, out)
        self.assertNotIn("superseded", out)
        self.assertIn("failed jobs: tests (3.14)", out)

    def test_an_abbreviated_sha_is_resolved_to_the_full_commit(self):
        # #3796: the short form never matched a run before.
        runs = [run_record(1, *CI)]
        code, out, _, fake = check("--sha", SHA[:10], runs=runs)
        self.assertEqual((code, "CI: green" in out), (0, True))
        listing = next(c for c in fake.calls if c[1:3] == ["run", "list"])
        self.assertEqual(listing[listing.index("--commit") + 1], FULL_SHA)
        # Uppercase is the same SHA; a full one needs no lookup.
        code, _, _, fake = check("--sha", SHA.upper(), runs=runs)
        self.assertEqual(code, 0)
        self.assertFalse([c for c in fake.calls if c[1:2] == ["api"]])
        for sha, reason in (
            (
                "fff1234",
                "unknown commit 'fff1234' (gh api repos/{owner}/{repo}/commits/fff1234 failed: HTTP 422",
            ),
            ("develop", "not a commit SHA: 'develop'"),
            ("HEAD~1", "not a commit SHA"),
            ("-h", "not a commit SHA"),
        ):
            with self.subTest(sha=sha):
                code, _, err, fake = check(f"--sha={sha}", runs=runs)
                self.assertEqual(code, 2)
                self.assertIn(reason, err)
                self.assertFalse([c for c in fake.calls if c[1:3] == ["run", "list"]])

    def test_bad_input_and_failed_gh_exit_two(self):
        for args in (["--pr", "0"], ["--pr", "x"], [], ["--pr", "1", "--sha", "a"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    develop_run_check.main(args)
                self.assertEqual(raised.exception.code, 2)
        fake = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 1, "", "HTTP 401: Bad credentials"
            )
        )
        err = io.StringIO()
        with (
            mock.patch.object(gh_cli.subprocess, "run", fake),
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
        ):
            code = develop_run_check.main(["--sha", SHA])
        self.assertEqual(code, 2)
        self.assertIn("failed", err.getvalue())
        # A run record without a field is a malformed answer, not an unmerged PR.
        broken = {k: v for k, v in run_record(1, *CI).items() if k != "conclusion"}
        code, out, _, _ = check("--sha", SHA, runs=[broken])
        self.assertEqual(code, 2)
        self.assertNotIn("refused", out)


if __name__ == "__main__":
    unittest.main()
