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
# A read that fails as a dropped connection does (#4059): in `views` or `gitar`.
DOWN = "error connecting to api.github.com"
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


def gitar_runs(state):
    """Gitar's check runs as the commit's check-runs API lists them (#3308)."""
    if state == "missing":
        return []
    if state == "older":  # only a run for the head before the push
        return [{"id": 1, "name": "Gitar", "head_sha": "0ldhead",
                 "status": "completed", "conclusion": "success"}]  # fmt: skip
    done = state in ("success", "failure")
    return [{"id": 2, "name": "Gitar", "head_sha": "feedc0de",
             "status": "completed" if done else "in_progress",
             "conclusion": state if done else None}]  # fmt: skip


def rest_answer(argv, gitar, compare):
    """The REST answers: Gitar's check runs (one state, or one per query from a
    list) and the head's compare with develop; None for any other call."""
    if argv[1:2] != ["api"]:
        return None
    if "/check-runs" in argv[2]:
        state = next_count(gitar) if isinstance(gitar, list) else gitar
        if state == DOWN:
            return DOWN
        return json.dumps({"check_runs": gitar_runs(state)})
    if "/compare/feedc0de...develop" in argv[2]:
        return json.dumps(compare)
    return None


def answer(argv, out):
    """A read's answer: `out` as JSON (text as it is), or the dropped connection."""
    if out == DOWN:
        return subprocess.CompletedProcess(argv, 1, "", DOWN)
    text = out if isinstance(out, str) else json.dumps(out)
    return subprocess.CompletedProcess(argv, 0, text, "")


def merge_answer(argv, auto_error):
    """A `pr merge` answer: `auto_error` fails every --auto call, or a list answers
    one --auto call each in order ("" succeeds, #3432)."""
    if "--auto" not in argv:
        error = ""
    elif isinstance(auto_error, list):
        error = auto_error.pop(0) if auto_error else ""
    else:
        error = auto_error
    return subprocess.CompletedProcess(argv, int(bool(error)), "", error)


def run(
    case, *args, auto_error="", views=None, behind=0, moved=(), threads=0, more=False,
    pushed=("", set()), freeze=None, gitar="success", calls=None,
):  # fmt: skip
    """Run main against a stub that answers `pr view` from `views` in order; this
    checkout's origin/<branch> is `pushed`: its head and the heads it held before."""
    calls = [] if calls is None else calls
    views = list(views or [view()])
    # Fixed answers: the repository, and `gh variable get MERGE_FREEZE` unset, set or failing.
    variable = {
        None: (1, "", "variable MERGE_FREEZE was not found"),
        "ERROR": (1, "ERROR\n", ""),
    }
    fixed = {
        ("repo", "view"): (0, json.dumps({"owner": {"login": "o"}, "name": "r"}), ""),
        ("variable", "get"): variable.get(freeze, (0, f"{freeze}\n", "")),
    }

    def fake(argv, **_kwargs):
        calls.append(argv[1:])
        if argv[1:3] == ["pr", "view"]:
            return answer(argv, views.pop(0))
        if tuple(argv[1:3]) in fixed:
            return subprocess.CompletedProcess(argv, *fixed[tuple(argv[1:3])])
        if argv[1:3] == ["api", "graphql"]:
            page = json.dumps(thread_page(next_count(threads), more))
            return subprocess.CompletedProcess(argv, 0, page, "")
        if argv[1:3] == ["pr", "diff"]:  # the full file list, past 100 files
            files = "\n".join([*(f"f{n}" for n in range(150)), "scripts/pr_merge.py"])
            return subprocess.CompletedProcess(argv, 0, files, "")
        rest = rest_answer(argv, gitar, {"behind": behind, "files": list(moved)})
        if rest is not None:
            return answer(argv, rest)
        return merge_answer(argv, auto_error)

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
    def test_a_merge_freeze_refuses_and_disarms_unless_it_names_the_pr(self):
        # #3203: the orchestrator's MERGE_FREEZE variable, not a chat message, stops arming.
        code, out, writes, _ = run(self, 7, freeze="v31 ships alone")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("merge freeze (untrusted text): v31 ships alone", out)
        armed = view(autoMergeRequest={"mergeMethod": "SQUASH"})
        code, _, writes, _ = run(self, 7, views=[armed], freeze="v31")
        self.assertEqual((code, writes), (1, [["pr", "merge", "7", "--disable-auto"]]))
        # The release PR the freeze names is armed; #70 does not count as #7.
        code, _, writes, _ = run(self, 7, freeze="v31 ships alone, only #7")
        self.assertEqual((code, writes), (0, [["pr", "merge", "7", "--auto", *PIN]]))
        code, _, writes, _ = run(self, 7, freeze="only #70")
        self.assertEqual((code, writes), (1, []))
        # A freeze that cannot be read stops arming too.
        code, out, writes, _ = run(self, 7, freeze="ERROR")
        self.assertEqual((code, writes), (2, []))

    def test_arms_only_after_gitar_completed_on_the_current_head(self):
        # #3308: right after a push Gitar's check for the new head is pending or
        # missing, and a run for the head before the push does not count.
        for state, reason in (
            ("in_progress", "Gitar has not reviewed feedc0de yet"),
            ("missing", "Gitar has not reviewed feedc0de yet"),
            ("older", "Gitar has not reviewed feedc0de yet"),
            ("failure", "Gitar's check on feedc0de ended failure"),
        ):
            with self.subTest(gitar=state):
                code, out, writes, _ = run(self, 7, gitar=state)
                self.assertEqual((code, writes), (1, []))
                self.assertIn(reason, out)
        code, _, writes, _ = run(self, 7, gitar="success")
        self.assertEqual((code, writes), (0, [["pr", "merge", "7", "--auto", *PIN]]))
        # An auto-merge armed by hand on an unreviewed head is disarmed.
        armed = view(autoMergeRequest={"mergeMethod": "SQUASH"})
        code, _, writes, _ = run(self, 7, views=[armed], gitar="in_progress")
        self.assertEqual((code, writes), (1, [["pr", "merge", "7", "--disable-auto"]]))

    def test_wait_waits_for_gitar_before_arming(self):
        merged = view(state="MERGED")
        code, _, writes, sleeps = run(
            self, 7, "--wait", "5", views=[view(), merged],
            gitar=["in_progress", "in_progress", "success"],
        )  # fmt: skip
        self.assertEqual((code, writes), (0, [["pr", "merge", "7", "--auto", *PIN]]))
        self.assertGreaterEqual(sleeps, 2)
        # A wait that ends before Gitar finished arms nothing.
        code, out, writes, _ = run(self, 7, "--wait", "1", gitar="in_progress")
        self.assertEqual((code, writes), (3, []))
        self.assertIn("Gitar has not reviewed feedc0de yet", out)
        # #4059: a dropped connection while waiting is read again; one that lasts
        # to the end of the wait exits 2 and names it.
        code, out, writes, _ = run(
            self, 7, "--wait", "5", views=[view(), view("MERGED")],
            gitar=[DOWN, DOWN, "success"],
        )  # fmt: skip
        self.assertEqual((code, writes), (0, [["pr", "merge", "7", "--auto", *PIN]]))
        self.assertEqual(out.count("a read failed, polling on"), 1)
        code, out, writes, _ = run(self, 7, "--wait", "1", gitar=DOWN)
        self.assertEqual((code, writes), (2, []))
        self.assertRegex(out, f"error: gh .* failed: {DOWN}")

    def test_the_other_gates_run_after_the_gitar_wait(self):
        # #3310 review: Gitar posts its threads when it finishes, and a freeze or
        # develop can move during the wait; those gates must see that state.
        calls = []
        merged = view(state="MERGED")
        run(
            self, 7, "--wait", "5", views=[view(), merged],
            gitar=["in_progress", "success"], calls=calls,
        )  # fmt: skip
        checks = [n for n, call in enumerate(calls) if "/check-runs" in str(call)]
        gates = [
            n
            for n, call in enumerate(calls)
            if call[:2] in (["api", "graphql"], ["variable", "get"])
            or "/compare/" in str(call)
        ]
        self.assertTrue(checks and gates)
        self.assertLess(max(checks), min(gates))
        # A thread Gitar posts as it finishes refuses, though none was open before.
        code, out, writes, _ = run(
            self, 7, "--wait", "5", gitar=["in_progress", "success"], threads=1
        )
        self.assertEqual((code, writes), (1, []))
        self.assertIn("unresolved review thread", out)

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

    def test_a_transient_merge_error_reads_the_state_before_trying_again(self):
        # #3432: a 502, then "Merge already in progress"; GitHub merged on its own.
        gateway = "non-200 OK status code: 502 Bad Gateway"
        busy = "GraphQL: Merge already in progress (mergePullRequest)"
        code, out, writes, sleeps = run(
            self, 7, auto_error=[gateway, ""], views=[view(), view()]
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(writes, [["pr", "merge", "7", "--auto", *PIN]] * 2)
        self.assertIn("auto-merge armed on feedc0de", out)
        self.assertEqual(sleeps, 1)
        code, out, writes, _ = run(
            self, 7, auto_error=[gateway, busy],
            views=[view(), view(), view("MERGED")],
        )  # fmt: skip
        self.assertEqual(code, 0, out)
        self.assertEqual(len(writes), 2)
        self.assertIn("merged meanwhile", out)
        # Armed meanwhile: no further write.
        code, out, writes, _ = run(
            self, 7, auto_error=[busy],
            views=[view(), view(autoMergeRequest={"mergeMethod": "SQUASH"})],
        )  # fmt: skip
        self.assertEqual((code, len(writes)), (0, 1), out)
        self.assertIn("auto-merge already armed", out)

    def test_a_transient_merge_error_never_arms_a_moved_head_or_retries_forever(self):
        gateway = "non-200 OK status code: 502 Bad Gateway"
        code, out, writes, _ = run(
            self, 7, auto_error=[gateway], views=[view(), view(headRefOid="b0b")]
        )
        self.assertEqual((code, len(writes)), (2, 1), out)
        self.assertIn("head changed", out)
        tries = len(pr_merge.MERGE_RETRY_SECONDS) + 1
        code, out, writes, _ = run(
            self, 7, auto_error=[gateway] * tries, views=[view()] * tries
        )
        self.assertEqual((code, len(writes)), (2, tries), out)
        self.assertIn("502", out)
        # Gitar on #3434: the pause can bring a freeze or a review thread, so every
        # gate runs again before the write is repeated; a refusal ends with its code.
        code, out, writes, _ = run(
            self, 7, auto_error=[gateway, ""], views=[view(), view()], threads=[0, 1]
        )
        self.assertEqual((code, len(writes)), (1, 1), out)

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
        # #4059: a dropped connection in one poll ends no wait, and nothing is
        # disarmed; one in the last poll exits 2 and names it.
        views = [view(), DOWN, DOWN, view("MERGED")]
        code, out, writes, _ = run(self, 7, "--wait", 5, views=views)
        self.assertEqual((code, writes[1:]), (0, []))
        self.assertIn("merged: 0123456789", out)
        # gh_cli reads a dropped connection once more itself: two answers a poll.
        code, out, writes, _ = run(self, 7, "--wait", 1, views=[view(), *[DOWN] * 8])
        self.assertEqual((code, writes[1:]), (2, []))
        self.assertRegex(out, f"error: gh .* failed: {DOWN}")
        # Said once, never after the last poll.
        self.assertEqual(out.count("polling on"), 1)
        # An error that is no blip (here an unreadable answer) ends the wait at once.
        code, out, writes, _ = run(self, 7, "--wait", 5, views=[view(), "not json"])
        self.assertEqual((code, writes[1:]), (2, []))
        self.assertIn("invalid JSON", out)

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
            # #4064: the command that asks Gitar for a review asks nothing here.
            {"author": {"login": "athlete"}, "body": "gitar review"},
            {"author": {"login": "lane"}, "body": " Gitar Review\n"},
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
        more_than_trigger = view(
            comments=[{"author": {"login": "athlete"}, "body": "gitar review, then fix"}],
            **armed,
        )
        blocking = view(
            reviews=[{"author": {"login": "gitar-bot"}, "state": "CHANGES_REQUESTED"}],
            **armed,
        )
        for views, threads, more in (
            ([view(), person], 0, False),
            ([view(), more_than_trigger], 0, False),
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

    def test_a_behind_pr_is_rebased_after_arming_and_once_after_the_first_ci_round(
        self,
    ):
        # #3872: the develop push's auto-rebase ran before arming and GitHub runs the
        # scheduled fallback only every few hours, so an armed BEHIND PR would wait.
        update = [
            "api", "-X", "PUT", "repos/{owner}/{repo}/pulls/7/update-branch",
            "-f", "update_method=rebase", "-f", "expected_head_sha=feedc0de",
        ]  # fmt: skip

        def updates(calls):
            return [call for call in calls if "update-branch" in " ".join(call)]

        calls = []
        code, out, writes, _ = run(
            self, 7, views=[view(mergeStateStatus="BEHIND")], calls=calls
        )
        self.assertEqual((code, writes), (0, [["pr", "merge", "7", "--auto", *PIN]]))
        self.assertEqual(updates(calls), [update])
        self.assertIn("PR #7 was behind develop: rebase of feedc0de requested", out)
        # A PR that is not behind is not touched.
        for state in ("CLEAN", "BLOCKED", None):
            with self.subTest(state=state):
                calls = []
                run(self, 7, views=[view(mergeStateStatus=state)], calls=calls)
                self.assertEqual(updates(calls), [])
        # With --wait: not while checks run, once after the first finished round, not again.
        pending = {**GREEN, "status": "IN_PROGRESS", "conclusion": None}
        armed = {"autoMergeRequest": {"mergeMethod": "SQUASH"}}
        calls = []
        code, _, _, _ = run(
            self, 7, "--wait", 5, calls=calls, views=[
                view(mergeStateStatus="CLEAN"),
                view(checks=(pending,), mergeStateStatus="BEHIND", **armed),
                view(mergeStateStatus="BEHIND", **armed),
                view(mergeStateStatus="BEHIND", **armed),
                view(state="MERGED"),
            ],
        )  # fmt: skip
        self.assertEqual(code, 0)
        self.assertEqual(updates(calls), [update])
        # A head GitHub has not rated yet (UNKNOWN) is read once more.
        calls = []
        run(
            self, 7, calls=calls,
            views=[view(mergeStateStatus="UNKNOWN"), view(mergeStateStatus="BEHIND")],
        )  # fmt: skip
        self.assertEqual(updates(calls), [update])
        # Rebased at arming: the old head's finished round does not use up the
        # recheck; the new head's finished round does (#3872 review).
        newer = [*update[:-1], "expected_head_sha=beefcafe"]
        calls = []
        code, _, _, _ = run(
            self, 7, "--wait", 5, calls=calls, views=[
                view(mergeStateStatus="BEHIND"),
                view(mergeStateStatus="BEHIND", **armed),
                view(mergeStateStatus="BEHIND", headRefOid="beefcafe", **armed),
                view(state="MERGED"),
            ],
        )  # fmt: skip
        self.assertEqual(code, 0)
        self.assertEqual(updates(calls), [update, newer])
        # A PR merged directly at arming is not rebased.
        calls = []
        code, _, writes, _ = run(
            self, 7, views=[view(mergeStateStatus="BEHIND")], auto_error=CLEAN,
            calls=calls,
        )  # fmt: skip
        self.assertEqual((code, writes[-1]), (0, ["pr", "merge", "7", *PIN]))
        self.assertEqual(updates(calls), [])
        # Never a merge-style update.
        self.assertFalse(any("update_method=merge" in " ".join(c) for c in calls))

    def test_a_refused_rebase_is_reported_and_arming_stands(self):
        calls = []
        with mock.patch.object(pr_merge, "gh", wraps=pr_merge.gh) as wrapped:

            def refuse_update(*argv):
                if "update_method=rebase" in argv:
                    raise gh_cli.CliError(
                        "HTTP 422: merge conflict between base and head"
                    )
                return gh_cli.gh(*argv)

            wrapped.side_effect = refuse_update
            code, out, writes, _ = run(
                self, 7, views=[view(mergeStateStatus="BEHIND")], calls=calls
            )
        self.assertEqual((code, writes), (0, [["pr", "merge", "7", "--auto", *PIN]]))
        self.assertIn("could not rebase #7 onto develop: HTTP 422", out)


if __name__ == "__main__":
    unittest.main()
