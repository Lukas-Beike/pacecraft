"""scripts/release_dispatch.py and scripts/image_verify.py with fakes; no network."""

import io
import json
import subprocess
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gh_cli
import image_verify
import release_dispatch

V = "2.1.2"
VERSION_PR = {
    "number": 1, "title": f"chore(release): set application version to {V}",
    "state": "MERGED", "mergedAt": "x", "createdAt": "2026-11-11T10:00:00Z", "url": "u",
}  # fmt: skip
PROMOTION_PR = {
    "number": 2, "title": f"chore(release): promote {V} to main",
    "state": "MERGED", "mergedAt": "x", "createdAt": "2026-11-11T10:05:00Z", "url": "u",
}  # fmt: skip


def next_answer(answers):
    """The next of a fake's answers; the last one stays."""
    return answers.pop(0) if len(answers) > 1 else answers[0]


def with_develop(run, calls, heads, develop_runs):
    """``run`` that also answers the develop head (#3539), one of ``heads`` per
    read, and that head's container runs, one list of ``develop_runs`` per read;
    ``run`` keeps answering the injected errors first."""
    heads = list(heads)
    runs = {sha: list(reads) for sha, reads in (develop_runs or {}).items()}

    def answer(argv, **kwargs):
        args = argv[1:]
        if run.errors:
            return run(argv, **kwargs)
        if args[:1] == ["api"]:
            data = {"sha": next_answer(heads)}
        elif "--commit" in args:
            data = next_answer(runs.get(args[args.index("--commit") + 1], [[]]))
        else:
            return run(argv, **kwargs)
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    return answer


def run_answer(args, publish, create_runs, jobs):
    """``gh run view`` answers the jobs; ``gh run list`` the Daily release runs
    of the release workflow event, else the publish runs."""
    if args[1] == "view":
        return {"jobs": list(jobs)}
    return list(create_runs if "workflow_run" in args else publish)


def fake_gh(
    prs,
    release=None,
    publish=(),
    errors=(),
    heads=(),
    develop_runs=None,
    create_runs=(),
    jobs=(),
):
    calls = []
    errors = list(errors)

    def run(argv, **_kwargs):
        calls.append(argv)
        args = argv[1:]
        if errors:
            return subprocess.CompletedProcess(argv, 1, "", errors.pop(0))
        if args[:2] == ["pr", "list"]:
            base = args[args.index("--base") + 1]
            data = [p for p in prs if ("promote" in p["title"]) == (base == "main")]
        elif args[:2] == ["release", "view"]:
            if release is None:
                return subprocess.CompletedProcess(argv, 1, "", "release not found")
            data = release
        elif args[:1] == ["run"]:
            data = run_answer(args, publish, create_runs, jobs)
        elif args[:2] == ["workflow", "run"]:
            data = None
        else:
            return subprocess.CompletedProcess(argv, 1, "", "unexpected")
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    run.errors = errors
    if heads:
        return with_develop(run, calls, heads, develop_runs), calls
    return run, calls


def follow(*args, **fakes):
    run, calls = fake_gh(**fakes)
    out = io.StringIO()
    with (
        mock.patch.object(gh_cli.subprocess, "run", run),
        mock.patch.object(release_dispatch.time, "sleep"),
        redirect_stdout(out),
        mock.patch("sys.stderr", io.StringIO()),
    ):
        code = release_dispatch.main(["follow", *args])
    return code, out.getvalue(), calls


class ReleaseDispatchTests(unittest.TestCase):
    def test_complete_chain_is_published(self):
        code, out, calls = follow(
            "--version", V,
            prs=[VERSION_PR, PROMOTION_PR],
            release={"tagName": V, "isDraft": False, "targetCommitish": "abc123"},
            publish=[{"databaseId": 9, "status": "completed", "conclusion": "success", "headBranch": V, "url": "u"}],
        )  # fmt: skip
        self.assertEqual(code, 0, out)
        self.assertIn("ok version PR #1 MERGED", out)
        self.assertIn("ok promotion PR #2 MERGED", out)
        self.assertIn(f"ok release {V} published", out)
        self.assertIn("ok publish run 9 success", out)
        for call in calls:
            self.assertNotIn("merge", call[1:3])

    def test_pending_and_failed_links(self):
        code, out, _ = follow("--version", V, prs=[VERSION_PR])
        self.assertEqual(code, 3)
        self.assertIn("pending promotion PR not created yet", out)
        self.assertIn("pending release", out)
        closed = dict(PROMOTION_PR, state="CLOSED")
        code, out, _ = follow("--version", V, prs=[VERSION_PR, closed])
        self.assertEqual(code, 1)
        self.assertIn("failed promotion PR #2 CLOSED", out)
        failed_run = {"databaseId": 9, "status": "completed", "conclusion": "failure", "headBranch": V, "url": "u"}  # fmt: skip
        code, out, _ = follow("--version", V, prs=[VERSION_PR], publish=[failed_run])
        self.assertEqual(code, 1)
        self.assertIn("failed publish run 9 failure", out)

    def test_failed_create_release_run_is_named_with_the_rerun_command(self):
        # The promotion merged, no release exists and the Daily release run for
        # that commit failed: no waiting for the timeout (#4264).
        merged = dict(PROMOTION_PR, mergeCommit={"oid": "abc"})
        failed = {"databaseId": 77, "status": "completed", "conclusion": "failure", "headSha": "abc"}  # fmt: skip
        jobs = [{"name": "create-release", "steps": [
            {"name": "Check out main", "conclusion": "success"},
            {"name": "Create release with commit changelog", "conclusion": "failure"},
        ]}]  # fmt: skip
        for args in (("--version", V), ("--version", V, "--wait", "--timeout", "1")):
            code, out, calls = follow(
                *args, prs=[VERSION_PR, merged], create_runs=[failed], jobs=jobs
            )
            self.assertEqual(code, 1, out)
            self.assertIn(
                f"failed release {V} run 77 failed at step Create release with commit changelog; rerun: gh run rerun 77 --failed",
                out,
            )
            for call in calls:
                self.assertNotIn("rerun", call[1:3])
        # A running rerun, another commit's run, or an unmerged promotion stays pending.
        pending = [
            dict(failed, conclusion=None, status="in_progress"),
            dict(failed, headSha="other"),
        ]
        for run in pending:
            code, out, _ = follow(
                "--version", V, prs=[VERSION_PR, merged], create_runs=[run], jobs=jobs
            )
            self.assertEqual(code, 3, out)
            self.assertIn(f"pending release {V} not published yet", out)
        code, out, _ = follow(
            "--version",
            V,
            prs=[VERSION_PR, dict(merged, state="OPEN")],
            create_runs=[failed],
            jobs=jobs,
        )
        self.assertEqual(code, 3, out)

    def test_transient_github_errors_are_pending_and_real_ones_stay_terminal(self):
        # One 503 is retried and the chain still completes (#3345).
        chain = {
            "prs": [VERSION_PR, PROMOTION_PR],
            "release": {"tagName": V, "isDraft": False, "targetCommitish": "abc"},
            "publish": [{"databaseId": 9, "status": "completed", "conclusion": "success", "headBranch": V, "url": "u"}],
        }  # fmt: skip
        outage = "HTTP 503: No server is currently available to service your request"
        code, out, _ = follow("--version", V, errors=[outage], **chain)
        self.assertEqual(code, 0, out)
        # An outage that outlasts the retries is pending, then the next follow is ok.
        # Each of the four attempts is a gh read that gh_cli tries twice (#3393).
        code, out, _ = follow("--version", V, errors=[outage] * 8, **chain)
        self.assertEqual(code, 3, out)
        self.assertIn("pending GitHub unavailable", out)
        self.assertEqual(follow("--version", V, **chain)[0], 0)
        # A 4xx is not retried and stays an error.
        code, out, calls = follow(
            "--version", V, errors=["HTTP 404: Not Found"], **chain
        )
        self.assertEqual((code, out, len(calls)), (2, "", 1))

    def test_follow_finds_the_version_from_the_newest_version_pr(self):
        code, out, _ = follow("--since", "2026-11-11T09:00:00Z", prs=[VERSION_PR])
        self.assertIn("ok version PR #1 MERGED", out)
        code, out, _ = follow("--since", "2026-11-12T00:00:00Z", prs=[VERSION_PR])
        self.assertEqual(code, 3)
        self.assertIn("none found yet", out)

    def test_dispatch_passes_the_version_and_rejects_bad_ones(self):
        run, calls = fake_gh(prs=[])
        with (
            mock.patch.object(gh_cli.subprocess, "run", run),
            redirect_stdout(io.StringIO()),
            mock.patch.object(release_dispatch.time, "sleep"),
        ):
            release_dispatch.main(["dispatch", "--version", V])
        self.assertEqual(
            calls[0][1:],
            [
                "workflow",
                "run",
                "daily-release.yml",
                "--ref",
                "develop",
                "-f",
                f"release_version={V}",
            ],
        )
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            release_dispatch.main(["dispatch", "--version", "v2.1"])

    def run_main(self, argv, **fakes):
        run, calls = fake_gh(**fakes)
        out = io.StringIO()
        clock = iter(range(0, 10**6, 30))
        with (
            mock.patch.object(gh_cli.subprocess, "run", run),
            mock.patch.object(release_dispatch.time, "sleep"),
            mock.patch.object(release_dispatch.time, "monotonic", lambda: next(clock)),
            redirect_stdout(out),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            code = release_dispatch.main(argv)
        dispatched = [call for call in calls if call[1:3] == ["workflow", "run"]]
        return code, out.getvalue(), calls, dispatched

    def test_dispatch_when_green_waits_for_the_develop_heads_container_run(self):
        # #3539: the head comes from the GitHub API, never a local fetch; the run
        # of that very commit must succeed before the release starts.
        pending = [
            {
                "databaseId": 5,
                "status": "in_progress",
                "conclusion": "",
                "headSha": "a1",
            }
        ]
        green = [
            {
                "databaseId": 5,
                "status": "completed",
                "conclusion": "success",
                "headSha": "a1",
            }
        ]
        started = [
            {
                "databaseId": 7,
                "status": "queued",
                "createdAt": "2999-01-01T00:00:00Z",
                "url": "u",
            }
        ]
        code, out, calls, dispatched = self.run_main(
            ["dispatch", "--when-green"], prs=[], heads=["a1"], publish=started,
            develop_runs={"a1": [[], pending, green]},
        )  # fmt: skip
        self.assertEqual((0, 1), (code, len(dispatched)), out)
        self.assertIn("develop a1 green", out)
        self.assertTrue(any(call[1:2] == ["api"] for call in calls))
        self.assertFalse(any("fetch" in call for call in calls))
        # The head moved while a1 ran: the newer head's run decides.
        new_green = [
            {
                "databaseId": 6,
                "status": "completed",
                "conclusion": "success",
                "headSha": "b2",
            }
        ]
        code, out, calls, dispatched = self.run_main(
            ["dispatch", "--when-green"], prs=[], heads=["a1", "b2"], publish=started,
            develop_runs={"a1": [pending], "b2": [[], new_green]},
        )  # fmt: skip
        self.assertEqual((0, 1), (code, len(dispatched)))
        self.assertIn("develop b2 green", out)

    def test_when_green_and_wait_outlast_outages_and_superseded_runs(self):
        # Gitar on #3543: a run a newer push cancelled is no red verdict, and a
        # GitHub outage inside the loop is a later look, not the end.
        cancelled = [
            {
                "databaseId": 5,
                "status": "completed",
                "conclusion": "cancelled",
                "headSha": "a1",
            }
        ]
        new_green = [
            {
                "databaseId": 6,
                "status": "completed",
                "conclusion": "success",
                "headSha": "b2",
            }
        ]
        started = [
            {
                "databaseId": 7,
                "status": "queued",
                "createdAt": "2999-01-01T00:00:00Z",
                "url": "u",
            }
        ]
        outage = [
            "HTTP 503: No server is currently available to service your request"
        ] * 8
        code, out, _calls, dispatched = self.run_main(
            ["dispatch", "--when-green"], prs=[], heads=["a1", "b2"], publish=started,
            develop_runs={"a1": [cancelled], "b2": [new_green]}, errors=outage,
        )  # fmt: skip
        self.assertEqual((0, 1), (code, len(dispatched)), out)
        self.assertIn("develop b2 green", out)
        published = {"tagName": V, "isDraft": False, "targetCommitish": "abc"}
        done = [
            {
                "databaseId": 9,
                "status": "completed",
                "conclusion": "success",
                "headBranch": V,
                "url": "u",
            }
        ]
        code, out, _calls, _ = self.run_main(
            ["follow", "--version", V, "--wait"], prs=[VERSION_PR, PROMOTION_PR],
            release=published, publish=done, errors=outage,
        )  # fmt: skip
        self.assertEqual(0, code, out)
        self.assertIn("pending GitHub unavailable", out)

    def test_dispatch_when_green_stops_on_red_or_at_the_time_limit(self):
        red = [
            {
                "databaseId": 5,
                "status": "completed",
                "conclusion": "failure",
                "headSha": "a1",
            }
        ]
        code, out, _calls, dispatched = self.run_main(
            ["dispatch", "--when-green"],
            prs=[],
            heads=["a1"],
            develop_runs={"a1": [red]},
        )
        self.assertEqual((1, []), (code, dispatched))
        self.assertIn("develop a1 failure", out)
        code, out, _calls, dispatched = self.run_main(
            ["dispatch", "--when-green", "--timeout", "2"], prs=[], heads=["a1"]
        )
        self.assertEqual((3, []), (code, dispatched))
        self.assertIn("no green container run", out)

    def test_quiet_means_no_recent_merge_and_no_armed_pr_about_to_merge(self):
        # #3922: each version commit restarted every open PR's CI.
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        running = {"name": "browser", "status": "IN_PROGRESS", "conclusion": ""}
        done = {"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"}
        red = {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"}
        armed = {"autoMergeRequest": {"mergeMethod": "SQUASH"}}
        for prs, merged_at, reason in (
            ([], "2026-01-01T11:57:00Z", "#5 merged 3 min ago"),
            ([], "2026-01-01T11:40:00Z", None),
            ([dict(armed, number=8, statusCheckRollup=[done, running])], None,
             "#8 is armed with 1 check(s) left"),
            ([dict(armed, number=8, statusCheckRollup=[red, running])], None, None),
            ([{"number": 8, "statusCheckRollup": [running]}], None, None),
            ([dict(armed, number=8, statusCheckRollup=[running] * 4)], None, None),
            ([dict(armed, number=8, statusCheckRollup=[done])], None, None),
        ):  # fmt: skip
            last = {"number": 5, "mergedAt": merged_at} if merged_at else None
            with self.subTest(prs=prs, merged_at=merged_at):
                self.assertEqual(
                    release_dispatch.quiet_reason(prs, last, now, 10), reason
                )

    def test_dispatch_when_quiet_waits_and_stops_at_the_time_limit(self):
        idle = [{"number": 8, "autoMergeRequest": None, "statusCheckRollup": []}]
        # gh lists by creation: the newest merge is found by mergedAt.
        recent = [
            {"number": 5, "mergedAt": "2000-01-01T00:00:00Z"},
            {"number": 6, "mergedAt": "2999-01-01T00:00:00Z"},
        ]
        outage = release_dispatch.CliError("HTTP 503: unavailable")
        # #4045: GitHub's GraphQL failure and a dropped connection are only pending too.
        graphql = release_dispatch.CliError(
            "gh pr list failed: GraphQL: Something went wrong while executing your query."
        )
        dropped = release_dispatch.CliError("error connecting to api.github.com")
        answers = [idle, recent, outage, graphql, dropped, idle, [], []]
        clock = iter(range(0, 10**6, 30))
        out = io.StringIO()
        with (
            mock.patch.object(release_dispatch, "gh_json", side_effect=answers) as read,
            mock.patch.object(release_dispatch.time, "sleep"),
            mock.patch.object(release_dispatch.time, "monotonic", lambda: next(clock)),
            redirect_stdout(out),
        ):
            self.assertEqual(release_dispatch.when_quiet(60, 10), 0)
        self.assertIn("waiting for quiet: #6 merged", out.getvalue())
        self.assertIn("waiting for quiet: GitHub unavailable", out.getvalue())
        self.assertIn("develop quiet", out.getvalue())
        self.assertTrue(read.call_args.args[-3].startswith("merged:>="))
        with (
            mock.patch.object(release_dispatch, "when_quiet", return_value=3),
            mock.patch.object(release_dispatch, "dispatch") as start,
        ):
            self.assertEqual(release_dispatch.main(["dispatch", "--when-quiet"]), 3)
        start.assert_not_called()

    def test_follow_lists_the_prs_the_release_contains(self):
        # #3922: late merges missed a release three times unnoticed.
        title = "chore(release): set application version to {}"
        versions = [
            {"title": title.format("2.1.1"), "mergedAt": "2026-01-01T10:00:00Z"},
            {"title": title.format(V), "mergedAt": "2026-01-01T12:00:00Z"},
        ]
        merged = [
            {"number": 40, "title": "fix: a", "mergedAt": "2026-01-01T11:00:00Z"},
            {
                "number": 41,
                "title": title.format(V),
                "mergedAt": "2026-01-01T12:00:00Z",
            },
            {"number": 39, "title": "fix: b", "mergedAt": "2026-01-01T10:00:00Z"},
            {"number": 42, "title": "feat: c", "mergedAt": "2026-01-01T11:59:00Z"},
        ]
        with mock.patch.object(
            release_dispatch, "gh_json", side_effect=[versions, merged]
        ) as read:
            self.assertEqual(release_dispatch.contained(V), [40, 42])
        self.assertIn(
            "merged:2026-01-01T10:00:00Z..2026-01-01T12:00:00Z", read.call_args.args
        )
        with mock.patch.object(release_dispatch, "gh_json", return_value=versions[:1]):
            self.assertIsNone(release_dispatch.contained("2.1.1"))
        with mock.patch.object(release_dispatch, "contained", return_value=[40, 42]):
            _code, out, _ = follow("--version", V, prs=[VERSION_PR])
        self.assertIn("contains #40 #42", out)
        # Best effort: an unreadable list never changes a published verdict.
        failing = release_dispatch.CliError("HTTP 422: search failed")
        with mock.patch.object(release_dispatch, "contained", side_effect=failing):
            code, out, _ = follow(
                "--version", V, prs=[VERSION_PR, PROMOTION_PR],
                release={"tagName": V, "isDraft": False, "targetCommitish": "abc"},
                publish=[{"databaseId": 9, "status": "completed", "conclusion": "success", "headBranch": V, "url": "u"}],
            )  # fmt: skip
        self.assertEqual(code, 0, out)
        self.assertIn("contains unknown (GitHub unavailable)", out)

    def test_follow_wait_loops_until_published_and_prints_only_changes(self):
        published = {"tagName": V, "isDraft": False, "targetCommitish": "abc"}
        done = [
            {
                "databaseId": 9,
                "status": "completed",
                "conclusion": "success",
                "headBranch": V,
                "url": "u",
            }
        ]
        code, out, _calls, _ = self.run_main(
            ["follow", "--version", V, "--wait"],
            prs=[VERSION_PR, PROMOTION_PR], release=published, publish=done,
        )  # fmt: skip
        self.assertEqual(0, code, out)
        self.assertEqual(1, out.count("ok version PR #1 MERGED"))
        # A chain that stays pending ends at the time limit with exit 3.
        code, out, _calls, _ = self.run_main(
            ["follow", "--version", V, "--wait", "--timeout", "2"], prs=[VERSION_PR]
        )
        self.assertEqual(3, code)
        self.assertEqual(1, out.count("pending promotion PR not created yet"))
        self.assertIn("still pending after 2 min", out)


class FakeResponse(io.BytesIO):
    def __init__(self, status, headers, body=b""):
        super().__init__(body)
        self.status, self.headers = status, headers

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def registry(digests):
    def urlopen(request, timeout):
        url = request.full_url
        if "/token?" in url:
            return FakeResponse(200, {}, json.dumps({"token": "anon"}).encode())
        tag = url.rsplit("/", 1)[1]
        if tag not in digests:
            raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO())
        return FakeResponse(200, {"Docker-Content-Digest": digests[tag]})

    return urlopen


A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64


class ImageVerifyTests(unittest.TestCase):
    def verify(self, digests, *args):
        out = io.StringIO()
        with (
            mock.patch.object(
                image_verify.urllib.request, "urlopen", registry(digests)
            ),
            redirect_stdout(out),
        ):
            code = image_verify.main(["--tag", V, *args])
        return code, out.getvalue()

    def test_matching_tag_latest_and_running_digest(self):
        code, out = self.verify({V: A, "latest": A}, "--running", A)
        self.assertEqual(code, 0, out)
        self.assertIn(f"ok {V} {A}", out)

    def test_mismatch_missing_tag_and_wrong_running_digest(self):
        for digests, args, expect in (
            ({V: A, "latest": B}, (), f"FAIL latest {B}"),
            ({"latest": A}, (), f"FAIL {V} missing"),
            ({V: A, "latest": A}, ("--running", B), f"FAIL running {B}"),
        ):
            with self.subTest(expect=expect):
                code, out = self.verify(digests, *args)
                self.assertEqual(code, 1)
                self.assertIn(expect, out)


if __name__ == "__main__":
    unittest.main()
