"""scripts/resolve_review_thread.py and its guard hook with a fake GitHub CLI;
no network (#2629)."""

import importlib.util
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import resolve_review_thread

_spec = importlib.util.spec_from_file_location(
    "guard_resolve_thread", ROOT / "scripts/hooks/guard_resolve_thread.py"
)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

ME = "lane-agent"
FIX = "98566e738d2f"
FINDING_AT = "2026-11-13T10:00:00Z"


def thread(
    author=ME,
    resolved=False,
    reply_by=ME,
    reply=f"Fixed in {FIX[:8]}.",
    number=2671,
    fixed_at="2026-11-13T11:00:00Z",
):
    comments = [{"author": {"login": "gitar-bot"}, "body": "finding"}]
    if reply_by:
        comments.append({"author": {"login": reply_by}, "body": reply})
    return {
        "isResolved": resolved,
        "pullRequest": {
            "number": number,
            "author": {"login": author},
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": "abcdef1" + "0" * 33,
                            "committedDate": "2026-11-13T09:00:00Z",
                        }
                    },
                    {"commit": {"oid": FIX + "0" * 28, "committedDate": fixed_at}},
                ]
            },
        },
        "first": {"nodes": [{"createdAt": FINDING_AT}]},
        "comments": {"nodes": comments},
    }


def run(node, thread_id="PRRT_a1", resolved_after=True):
    calls = []

    def fake(*args):
        # Only the fixed reads and the one mutation may reach GitHub.
        calls.append(args)
        if args == ("api", "user"):
            return {"login": ME}
        query = f"query={resolve_review_thread.QUERY}"
        mutation = f"query={resolve_review_thread.MUTATION}"
        if args == ("api", "graphql", "-f", query, "-f", f"id={thread_id}"):
            return {"data": {"node": node}}
        if args == ("api", "graphql", "-f", mutation, "-f", f"id={thread_id}"):
            return {
                "data": {
                    "resolveReviewThread": {"thread": {"isResolved": resolved_after}}
                }
            }
        raise AssertionError(f"unexpected gh call: {args}")

    out = io.StringIO()
    with (
        mock.patch.object(resolve_review_thread, "gh_json", side_effect=fake),
        redirect_stdout(out),
        redirect_stderr(io.StringIO()),
    ):
        code = resolve_review_thread.main(["2671", thread_id])
    mutations = [a for a in calls if any("mutation" in str(x) for x in a)]
    return code, out.getvalue(), mutations


class ResolveReviewThreadTests(unittest.TestCase):
    def test_own_pr_with_a_reply_naming_the_fix_commit_is_resolved(self):
        code, out, mutations = run(thread())
        self.assertEqual(code, 0, out)
        self.assertEqual(len(mutations), 1)

    def test_every_other_case_is_refused_without_a_mutation(self):
        for label, node in (
            ("unknown thread", None),
            ("thread of another PR", thread(number=9)),
            ("someone else's PR", thread(author="someone-else")),
            ("already resolved", thread(resolved=True)),
            ("no reply", thread(reply_by=None)),
            ("reply by another user", thread(reply_by="someone-else")),
            ("reply without a commit", thread(reply="Fixed.")),
            ("commit of another PR", thread(reply="Fixed in 1234567.")),
            ("commit older than the finding", thread(reply="Fixed in abcdef1.")),
            ("fix before the finding", thread(fixed_at="2026-11-13T08:00:00Z")),
        ):
            with self.subTest(label):
                code, out, mutations = run(node)
                self.assertEqual(code, 1)
                self.assertIn("refused:", out)
                self.assertEqual(mutations, [])
        code, _out, mutations = run(thread(), thread_id="not-a-thread")
        self.assertEqual((code, mutations), (2, []))

    def test_an_unconfirmed_resolve_is_an_error(self):
        code, _out, mutations = run(thread(), resolved_after=False)
        self.assertEqual((code, len(mutations)), (2, 1))

    def test_only_the_resolve_mutation_is_allowed_and_only_through_the_script(self):
        # The athlete allowed exactly one GitHub write for agents (#2629).
        source = (ROOT / "scripts/resolve_review_thread.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"mutation\(", source), ["mutation("])
        self.assertEqual(
            re.findall(r"\{(\w+)\(input:", source), ["resolveReviewThread"]
        )
        settings = json.loads(
            (ROOT / ".claude/settings.json").read_text(encoding="utf-8-sig")
        )
        allowed = settings["permissions"]["allow"]
        github = [rule for rule in allowed if re.search(r"gh |graphql|resolve", rule)]
        self.assertEqual(github, ["Bash(python scripts/resolve_review_thread.py:*)"])


class GuardHookTests(unittest.TestCase):
    def decide(self, command, unchanged=True):
        stdin = mock.Mock()
        stdin.buffer.read.return_value = json.dumps(
            {"tool_input": {"command": command}}
        ).encode()
        with (
            mock.patch.object(guard.sys, "stdin", stdin),
            mock.patch.object(guard, "unchanged", return_value=unchanged),
            redirect_stderr(io.StringIO()),
        ):
            return guard.main()

    def test_only_the_exact_call_from_an_unchanged_checkout_runs(self):
        exact = "python scripts/resolve_review_thread.py 2671 PRRT_a1"
        self.assertEqual(self.decide(exact), 0)
        self.assertEqual(self.decide("git status"), 0)
        self.assertEqual(self.decide(exact, unchanged=False), 2)
        for command in (
            exact + "; gh api graphql -f query=mutation",
            exact + " && rm -rf x",
            exact + " $(gh pr merge 1)",
            "python scripts/resolve_review_thread.py 2671 PRRT_a1 extra",
            "python other/resolve_review_thread.py 2671 PRRT_a1",
            "python -c 'import resolve_review_thread'",
        ):
            with self.subTest(command):
                self.assertEqual(self.decide(command), 2)

    def test_scripts_is_compared_with_develop_including_new_files(self):
        calls = []

        def fake(args, **_kwargs):
            calls.append(args)
            return mock.Mock(returncode=0, stdout="")

        with mock.patch.object(guard.subprocess, "run", side_effect=fake):
            self.assertTrue(guard.unchanged())
        self.assertIn(
            ["git", "diff", "--name-only", "origin/develop", "--", "scripts"], calls
        )
        self.assertIn(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "scripts"],
            calls,
        )

    def test_only_changes_that_can_reach_the_run_block(self):
        """#3072: the script, its imports and shadowing modules block; the rest does not."""
        self.assertIn("gh_cli", guard.run_modules())
        for paths, allowed in (
            ([], True),
            (["scripts/ui_walk.mjs"], True),
            (["scripts/hooks/guard_private_paths.py"], True),
            (["scripts/release_changelog.py", "scripts/new_tool.py"], True),
            (["scripts/resolve_review_thread.py"], False),
            (["scripts/gh_cli.py"], False),
            # A committed or new module named like one the run imports shadows it.
            (["scripts/json.py"], False),
            (["scripts/re.pyc"], False),
            (["scripts/json/__init__.py"], False),
            (["scripts/sitecustomize.py"], False),
            (["scripts/ui_walk.mjs", "scripts/argparse.py"], False),
            (None, False),
        ):
            with self.subTest(paths=paths), mock.patch.object(
                guard, "changed_paths", return_value=paths
            ):
                self.assertEqual(guard.unchanged(), allowed)


if __name__ == "__main__":
    unittest.main()
