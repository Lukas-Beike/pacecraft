"""scripts/pr_merge.py with a stubbed GitHub CLI; no network, no sleeping."""

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gh_cli
import pr_merge

CLEAN = (
    "GraphQL: Pull request Pull request is in clean status (enablePullRequestAutoMerge)"
)
GREEN = {
    "__typename": "CheckRun",
    "name": "unit",
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
}
RED = {**GREEN, "conclusion": "FAILURE"}
PIN = ["--squash", "--match-head-commit", "feedc0de"]


def view(state="OPEN", checks=(GREEN,), **extra):
    merge = {"oid": "0123456789abcdef"} if state == "MERGED" else None
    return {
        "state": state, "baseRefName": "develop", "headRefOid": "feedc0de",
        "autoMergeRequest": None, "mergeCommit": merge,
        "statusCheckRollup": list(checks), "comments": [], "reviews": [],
        **extra,
    }  # fmt: skip


def thread_page(threads, more):
    nodes = [
        {"id": f"PRRT_{n}", "isResolved": False, "path": "a.py", "line": n,
         "comments": {"nodes": [{"author": {"login": "gitar"}, "body": "fix"}]}}
        for n in range(threads)
    ]  # fmt: skip
    page = {"pageInfo": {"hasNextPage": more}, "nodes": nodes}
    return {"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}


def next_count(threads):
    """A fixed thread count, or one per query from a list (the last one repeats)."""
    if isinstance(threads, int):
        return threads
    return threads.pop(0) if len(threads) > 1 else threads[0]


def run(
    case, *args, auto_error="", views=None, behind=0, moved=(), threads=0, more=False,
    pushed=("", set()),
):  # fmt: skip
    """Run main against a stub that answers `pr view` from `views` in order; this
    checkout's origin/<branch> is `pushed`: its head and the heads it held before."""
    calls, views = [], list(views or [view()])

    def fake(argv, **_kwargs):
        calls.append(argv[1:])
        if argv[1:3] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(views.pop(0)), "")
        if argv[1:3] == ["repo", "view"]:
            repo = {"owner": {"login": "o"}, "name": "r"}
            return subprocess.CompletedProcess(argv, 0, json.dumps(repo), "")
        if argv[1:3] == ["api", "graphql"]:
            page = json.dumps(thread_page(next_count(threads), more))
            return subprocess.CompletedProcess(argv, 0, page, "")
        if argv[1:3] == ["pr", "diff"]:  # the full file list, past 100 files
            files = "\n".join([*(f"f{n}" for n in range(150)), "scripts/pr_merge.py"])
            return subprocess.CompletedProcess(argv, 0, files, "")
        if argv[1:2] == ["api"] and "/compare/feedc0de...develop" in argv[2]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"behind": behind, "files": list(moved)}), ""
            )
        failed = auto_error and "--auto" in argv
        return subprocess.CompletedProcess(argv, int(bool(failed)), "", auto_error)

    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.object(gh_cli.subprocess, "run", fake),
        mock.patch.object(pr_merge.time, "sleep") as sleep,
        mock.patch.object(pr_merge, "pushed_over", return_value=pushed),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        code = pr_merge.main([str(arg) for arg in args])
    case.assertEqual(views, [], "every stubbed state was polled")
    writes = [call for call in calls if call[:2] == ["pr", "merge"]]
    return code, out.getvalue() + err.getvalue(), writes, sleep.call_count


class PrMergeTests(unittest.TestCase):
    def test_arms_squash_auto_merge_on_the_read_head(self):
        code, out, writes, _ = run(self, 7)
        self.assertEqual((code, writes), (0, [["pr", "merge", "7", "--auto", *PIN]]))
        self.assertIn("auto-merge armed", out)

    def test_a_just_pushed_head_is_awaited_before_the_develop_check(self):
        # #3174: right after a force-push GitHub still reported the old head, and
        # the develop check compared that one and asked for a rebase already done.
        arms = [["pr", "merge", "7", "--auto", *PIN]]
        pushed = ("feedc0de", {"0ld0ld"})  # this checkout pushed over 0ld0ld
        old = view(headRefName="fix/x", headRefOid="0ld0ld")
        code, out, writes, sleeps = run(
            self, 7, views=[old, old, view()], pushed=pushed
        )
        self.assertEqual((code, writes, sleeps), (0, arms, 2))
        # GitHub catches up on the last re-read: that read counts (Gitar P3).
        late = [old] * pr_merge.HEAD_POLLS + [view()]
        code, out, writes, sleeps = run(self, 7, views=late, pushed=pushed)
        self.assertEqual((code, writes, sleeps), (0, arms, pr_merge.HEAD_POLLS))
        # GitHub never catches up: a refusal that names the heads, not a rebase.
        stale = [old] * (pr_merge.HEAD_POLLS + 1)
        code, out, writes, _ = run(self, 7, views=stale, pushed=pushed)
        self.assertEqual((code, writes), (1, []))
        self.assertIn("GitHub still reports head 0ld0ld, not the pushed feedc0de", out)
        self.assertNotIn("rebase", out)
        # A head this checkout never held (someone else's push, an old fetch) is
        # checked as GitHub reports it, without waiting (Gitar P2).
        code, out, writes, sleeps = run(self, 7, pushed=("0ther", {"0ld0ld"}))
        self.assertEqual((code, writes, sleeps), (0, arms, 0))

    def test_clean_status_refusal_merges_the_same_head_directly(self):
        # #2776: GitHub refuses auto-merge for a PR whose checks already passed
        code, out, writes, _ = run(self, 7, auto_error=CLEAN)
        self.assertEqual((code, writes[-1]), (0, ["pr", "merge", "7", *PIN]))
        self.assertIn("merged feedc0de directly", out)

    def test_refusals_never_merge_directly(self):
        for kwargs, expect in (
            ({"auto_error": "GraphQL: Base branch policy"}, 2),
            ({"views": [view(baseRefName="feature/x")]}, 1),
            ({"views": [view(autoMergeRequest={"mergeMethod": "SQUASH"})]}, 0),
        ):
            with self.subTest(expect=expect):
                code, _, writes, _ = run(self, 7, **kwargs)
                self.assertEqual(code, expect)
                self.assertNotIn(["pr", "merge", "7", *PIN], writes)

    def test_develop_changes_to_this_prs_files_refuse_and_disarm(self):
        # #2901: rebase when develop moved in the PR's files (lane rule).
        armed = view(autoMergeRequest={"mergeMethod": "SQUASH"})
        mine, other = ["scripts/pr_merge.py"], ["backend/other.py"]
        arms = [["pr", "merge", "7", "--auto", *PIN]]
        for args, kwargs, expect, merge_calls in (
            ((), {"behind": 2, "moved": mine}, 1, []),
            ((), {"behind": 2, "moved": mine, "views": [armed]}, 1,
             [["pr", "merge", "7", "--disable-auto"]]),
            ((), {"behind": 2, "moved": other}, 0, arms),  # no overlap: arm
            ((), {"behind": None}, 2, []),  # no count: fail closed
            (("--allow-behind",), {"behind": 2, "moved": mine}, 0, arms),
        ):  # fmt: skip
            with self.subTest(args=args, **kwargs):
                code, out, writes, _ = run(self, 7, *args, **kwargs)
                self.assertEqual((code, writes), (expect, merge_calls))
                if expect == 1:
                    self.assertIn("develop changed scripts/pr_merge.py", out)

    def test_unresolved_review_threads_refuse_and_disarm(self):
        # #2947: an armed auto-merge does not wait for a review thread (#2520).
        armed = view(autoMergeRequest={"mergeMethod": "SQUASH"})
        disarm = [["pr", "merge", "7", "--disable-auto"]]
        for args, kwargs, merge_calls in (
            ((), {"threads": 1}, []),
            ((), {"threads": 2, "views": [armed]}, disarm),
            (("--allow-behind",), {"threads": 1}, []),
            ((), {"more": True}, []),  # beyond the first page: fail closed
        ):
            with self.subTest(args=args, **kwargs):
                code, out, writes, _ = run(self, 7, *args, **kwargs)
                self.assertEqual((code, writes), (1, merge_calls))
                self.assertIn("unresolved review thread(s)", out)
        self.assertIn("PRRT_1 a.py:1 @gitar: fix", run(self, 7, threads=2)[1])

    def test_wait_polls_in_turn_until_merged(self):
        # #2795: no yield on a watcher that may never wake on the merge
        views = [view(), view(), view(), view("MERGED")]
        code, out, _, sleeps = run(self, 7, "--wait", 5, views=views)
        self.assertEqual((code, sleeps), (0, 2))
        self.assertIn("merged: 0123456789", out)

    def test_wait_stops_on_close_failure_feedback_or_timeout(self):
        late = view(reviews=[{"body": "finding"}])
        for polls, minutes, expect in (
            ([view("CLOSED")], 5, 1),
            ([view(checks=(GREEN, RED))], 5, 1),
            ([late], 5, 1),
            ([view()] * 4, 1, 3),
        ):
            with self.subTest(expect=expect, polls=len(polls)):
                code, _, _, sleeps = run(
                    self, 7, "--wait", minutes, views=[view(), *polls]
                )
                self.assertEqual((code, sleeps), (expect, len(polls) - 1))
        # #2947: feedback on an armed PR disarms it; auto-merge would not wait.
        armed_late = {**late, "autoMergeRequest": {"mergeMethod": "SQUASH"}}
        _, _, writes, _ = run(self, 7, "--wait", 5, views=[view(), armed_late])
        self.assertEqual(writes[-1], ["pr", "merge", "7", "--disable-auto"])

    def test_status_bot_comments_are_not_feedback_but_threads_are(self):
        # #3034: Gitar's "Reviewing" comment and Sonar's gate comment disarmed every PR.
        armed = {"autoMergeRequest": {"mergeMethod": "SQUASH"}}
        status = [
            {"author": {"login": "gitar-bot"}, "body": "Reviewing your code"},
            {"author": {"login": "sonarqubecloud"}, "body": "Quality Gate passed"},
        ]
        bots = view(
            comments=status, reviews=[{"author": {"login": "gitar-bot"}}], **armed
        )
        code, _, writes, _ = run(
            self, 7, "--wait", 5, views=[view(), bots, view("MERGED")]
        )
        self.assertEqual(code, 0)
        self.assertNotIn(["pr", "merge", "7", "--disable-auto"], writes)
        # A person's comment, a bot review requesting changes, or an unresolved
        # thread (Gitar's findings, also past one page) still disarms.
        person = view(
            comments=[{"author": {"login": "athlete"}, "body": "wait"}], **armed
        )
        blocking = view(
            reviews=[{"author": {"login": "gitar-bot"}, "state": "CHANGES_REQUESTED"}],
            **armed,
        )
        for views, threads, more in (
            ([view(), person], 0, False),
            ([view(), blocking], 0, False),
            ([view(), view(**armed)], [0, 1], False),  # arming check, then the poll
        ):
            with self.subTest(views=views[1], threads=threads):
                code, _, writes, _ = run(
                    self, 7, "--wait", 5, views=views, threads=threads, more=more
                )
                self.assertEqual(code, 1)
                self.assertEqual(writes[-1], ["pr", "merge", "7", "--disable-auto"])
        # A second page of threads during the wait also disarms (fail closed).
        with mock.patch.object(
            pr_merge, "unresolved_threads", side_effect=[([], False), ([], True)]
        ):
            code, _, writes, _ = run(
                self, 7, "--wait", 5, views=[view(), view(**armed)]
            )
        self.assertEqual(
            (code, writes[-1]), (1, ["pr", "merge", "7", "--disable-auto"])
        )


if __name__ == "__main__":
    unittest.main()
