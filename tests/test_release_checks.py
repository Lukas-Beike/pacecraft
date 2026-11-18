"""scripts/release_dispatch.py and scripts/image_verify.py with fakes; no network."""

import io
import json
import subprocess
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
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
    read, and that head's container runs, one list of ``develop_runs`` per read."""
    heads = list(heads)
    runs = {sha: list(reads) for sha, reads in (develop_runs or {}).items()}

    def answer(argv, **kwargs):
        args = argv[1:]
        if args[:1] == ["api"]:
            data = {"sha": next_answer(heads)}
        elif "--commit" in args:
            data = next_answer(runs.get(args[args.index("--commit") + 1], [[]]))
        else:
            return run(argv, **kwargs)
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    return answer


def fake_gh(prs, release=None, publish=(), errors=(), heads=(), develop_runs=None):
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
        elif args[:2] == ["run", "list"]:
            data = list(publish)
        elif args[:2] == ["workflow", "run"]:
            data = None
        else:
            return subprocess.CompletedProcess(argv, 1, "", "unexpected")
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

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
