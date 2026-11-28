"""scripts/resolve_review_thread.py and its guard hook with a fake GitHub CLI;
no network (#2629)."""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
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
            ("resolved thread of another PR", thread(number=9, resolved=True)),
            (
                "resolved thread of someone else's PR",
                thread(author="someone-else", resolved=True),
            ),
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

    def test_an_already_resolved_thread_is_done_without_a_mutation(self):
        # #3317: Gitar resolves its thread once the reply names the fix commit;
        # the goal state is no error.
        for reply in ("Fixed in abcdef1234567.", "Fixed."):
            with self.subTest(reply=reply):
                code, out, mutations = run(thread(resolved=True, reply=reply))
                self.assertEqual((code, mutations), (0, []))
                self.assertIn("already resolved", out)
                self.assertNotIn("refused", out)

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
    def decide(self, command, unchanged=True, raw=None, err=None):
        stdin = mock.Mock()
        payload = json.dumps({"tool_input": {"command": command}})
        stdin.buffer.read.return_value = (payload if raw is None else raw).encode()
        with (
            mock.patch.object(guard.sys, "stdin", stdin),
            mock.patch.object(guard, "unchanged", return_value=unchanged),
            redirect_stderr(err or io.StringIO()),
        ):
            return guard.main()

    def test_the_hook_loads_without_warnings(self):
        # An unescaped `[` in a class printed a FutureWarning on every hook run (#4162).
        loaded = subprocess.run(
            [sys.executable, "-W", "error", "-c", "import runpy, sys; runpy.run_path(sys.argv[1])", _spec.origin],
            input="{}", capture_output=True, text=True, check=False,
        )  # fmt: skip
        self.assertNotIn("Warning", loaded.stderr)

    def test_the_exact_call_runs_only_from_an_unchanged_checkout(self):
        # #4289: the one call the allow rule lets run without a prompt; the script
        # checks its own preconditions, which a changed script cannot vouch for.
        exact = "python scripts/resolve_review_thread.py 2671 PRRT_a1"
        err = io.StringIO()
        self.assertEqual(self.decide(exact), 0)
        self.assertEqual(self.decide(f"  {exact}\n"), 0)
        self.assertEqual(self.decide(exact, unchanged=False, err=err), 2)
        self.assertIn("matches origin/develop", err.getvalue())
        # The exact call in the other spellings the allow rule matches: another
        # interpreter spelling, a leading folder mark, a stream copy, and the wrappers
        # and harmless environment assignments the rule may strip.
        for command in (
            exact + " 2>&1",
            "py scripts/resolve_review_thread.py 1 PRRT_x",
            "python3.14.exe ./scripts/resolve_review_thread.py 1 PRRT_x",
            "PYTHON scripts\\RESOLVE_REVIEW_THREAD.PY 1 PRRT_x",
            "timeout 60 " + exact,
            "nohup " + exact,
            "! " + exact,
            "PYTHONDONTWRITEBYTECODE=1 " + exact,
            "PYTHONUTF8=1 PYTHONUNBUFFERED=1 time -p " + exact,
        ):
            with self.subTest(command):
                self.assertEqual(self.decide(command), 0)
                self.assertEqual(self.decide(command, unchanged=False), 2)
        # Every other command that holds the start blocks, changed checkout or not:
        # more words, anything after it, a variable that changes what the script
        # imports or runs, and any start the hook cannot place first.
        for command in (
            exact + " extra",
            exact + " && echo done",
            exact + "; echo done",
            # A second start after the first, and a separator hidden in a quote or
            # a substitution before the start (second bypass review of #4289).
            exact + " && cd /tmp/clone && " + exact,
            exact + "; PYTHONPATH=/tmp/x " + exact,
            exact + " ';' $(cp /tmp/evil.py scripts/resolve_review_thread.py)",
            "X=$(cp /tmp/evil.py scripts/resolve_review_thread.py) " + exact,
            "$(true) " + exact,
            '"$(cmd)" ' + exact,
            "PYTHONPATH=/tmp/x " + exact,
            "PATH=/tmp/x:$PATH " + exact,
            "PYTHONSTARTUP=x.py " + exact,
            "A=1 PYTHONHOME=/tmp/x " + exact,
            "if true; then " + exact + "; fi",
            "echo $(" + exact + ")",
            # Gitar on #4367: what runs before or beside the script in its own command.
            exact + " $(cp evil.py scripts/resolve_review_thread.py)",
            exact + " `cp evil.py scripts/argparse.py`",
            exact + " <(cp evil.py scripts/json.py)",
            exact + " >(cp evil.py scripts/json.py)",
            exact + " & cp evil.py scripts/json.py",
            exact + " | cp evil.py scripts/json.py",
            exact + " |& cp evil.py scripts/json.py",
            # Gitar on #4367: a heredoc body and a continued line join the script's command.
            exact + " <<EOF\n$(cp evil.py scripts/json.py)\nEOF",
            exact + " <<'EOF'\nbody\nEOF",
            exact + " \\\n$(cp evil.py scripts/json.py)",
            exact + " \\\nextra",
            "echo `" + exact + "`",
            "git commit -m 'fix\n" + exact + "'",
        ):
            with self.subTest(command):
                self.assertEqual(self.decide(command), 2)
        # Anything before the script could change what the check saw (a copied or
        # added module, another directory, PYTHONPATH, PATH): a start after it
        # blocks, changed checkout or not (bypass review of #4289).
        for command in (
            "git status; " + exact,
            "echo x | " + exact,
            "(" + exact + ")",
            "cp /tmp/x.py scripts/resolve_review_thread.py && " + exact,
            "echo 'import os' > scripts/argparse.py; " + exact,
            "cd ../other-worktree && " + exact,
            "export PYTHONPATH=/tmp/x; " + exact,
            "export PATH=/tmp/x:$PATH; " + exact,
            "git checkout feature -- scripts; " + exact,
            "Set-Content scripts/json.py x; " + exact,
            "Get-Date\r" + exact,
            "Get-Date " + exact,
            "& " + exact,
        ):
            with self.subTest(command):
                self.assertEqual(self.decide(command), 2)

    def test_other_commands_are_not_read_for_the_scripts_name(self):
        # Three rounds of name recognition (#3759, #3982, #4155) kept blocking
        # read-only commands; the script refuses by its own checks, so a command
        # that does not start the script with the allow rule's spelling and is no
        # direct mutation passes, however it spells or mentions the script (#4289).
        for command in (
            "git status",
            "git diff origin/develop -- scripts/resolve_review_thread.py",
            "wc -l scripts/resolve_review_thread.py tests/test_resolve_review_thread.py",
            "rg -n 'resolveReviewThread' scripts/*.py",
            "cat scripts/resolve_*.py",
            "python -m mypy scripts/*.py",
            "git add scripts/*.py && git commit -m 'fix: resolve review thread'",
            'for f in a b; do git show "FETCH_HEAD:$f" > "$S/$f"; done',
            "python scripts/resolve_*.py 2671 PRRT_a1",
            "python scripts/resolve_review_threa?.py 2671 PRRT_a1",
            "python other/resolve_review_thread.py 2671 PRRT_a1",
            "python -c 'import resolve_review_thread'",
            "rg --pre cat x scripts",
        ):
            with self.subTest(command):
                self.assertEqual(self.decide(command, unchanged=False), 0)

    def test_input_of_an_unexpected_shape_blocks(self):
        # Fails closed (#3367): only an object with a command string is read.
        for payload in (
            [],
            "text",
            1,
            None,
            {},
            {"tool_input": None},
            {"tool_input": []},
            {"tool_input": "text"},
            {"tool_input": {}},
            {"tool_input": {"command": None}},
            {"tool_input": {"command": ["git", "status"]}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.decide("", raw=json.dumps(payload)), 2)
        self.assertEqual(self.decide("", raw='{"tool_input": {"command": ""}}'), 0)
        # Any failure inside the hook blocks as well.
        for error in (RecursionError, OSError, AttributeError):
            with (
                self.subTest(error=error),
                mock.patch.object(guard, "decide", side_effect=error),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(guard.main(), 2)

    def test_the_direct_resolve_mutation_blocks(self):
        # #3759: the resolve mutation runs only inside the script.
        err = io.StringIO()
        command = (
            "gh api graphql -f query='mutation { resolveReviewThread("
            'input: {threadId: "PRRT_x"}) { thread { isResolved } } }\''
        )
        self.assertEqual(self.decide(command, err=err), 2)
        self.assertIn("direct GraphQL resolve mutation", err.getvalue())
        for command in (
            "gh api graphql -f query=\"mutation{resolveReview'Thread'(input:{})}\"",
            "gh api GraphQL -F query=@q.txt -f x=RESOLVEREVIEWTHREAD",
            't=Thread; gh api graphql -f query="mutation{resolveReview$t(input:{})}"',
            'curl https://api.github.com/graphql -d \'{"query":"resolveReviewThread"}\'',
            "gh api graphql -f query='mutation{resolveReview\\u0054hread(input:{})}'",
            # Escapes of the name's letters, and pieces a shell or PowerShell joins.
            "gh api graphql -f query=$'mutation{\\x72esolveReviewThread(input:{})}'",
            "gh api graphql -f query=$'mutation{\\162esolveReviewThread(input:{})}'",
            'gh api graphql -f query="mutation{`u{72}esolveReviewThread(input:{})}"',
            "irm https://api.github.com/graphql -Body ('mutation{' + 'resolve' + 'ReviewThread(input:{})}')",
        ):
            with self.subTest(command):
                self.assertEqual(self.decide(command), 2)
        for command in (
            "gh api graphql -f query='{ viewer { login } }'",
            "git commit -m 'docs: graphql resolveReviewThread note'",
            "rg -n 'graphql.*resolveReviewThread' docs",
        ):
            with self.subTest(command):
                self.assertEqual(self.decide(command), 0)

    def test_the_run_is_compared_with_develop_by_content(self):
        calls = []

        def fake(args, **_kwargs):
            calls.append(args)
            return mock.Mock(returncode=0, stdout="blob\n")

        with (
            mock.patch.object(guard.subprocess, "run", side_effect=fake),
            mock.patch.object(guard.os, "listdir", return_value=["gh_cli.py"]),
        ):
            self.assertTrue(guard.unchanged())
        script = "scripts/resolve_review_thread.py"
        self.assertIn(["git", "hash-object", "--no-filters", "--", script], calls)
        self.assertIn(
            [
                "git",
                "--no-replace-objects",
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/develop:{script}",
            ],
            calls,
        )
        # The script imports no local module, so cached bytecode cannot stand in.
        self.assertEqual(guard.run_modules(), {"resolve_review_thread"})

    def test_a_differing_or_missing_run_file_blocks(self):
        for here, there in (("a\n", "b\n"), ("a\n", None), (None, "a\n")):

            def fake(args, here=here, there=there, **_kwargs):
                value = here if args[1] == "hash-object" else there
                return mock.Mock(returncode=0 if value else 1, stdout=value or "")

            with (
                self.subTest(here=here, there=there),
                mock.patch.object(guard.subprocess, "run", side_effect=fake),
            ):
                self.assertFalse(guard.unchanged())

    def test_only_entries_that_could_be_imported_by_the_run_block(self):
        """#3072: shadowing entries of any kind block; other scripts do not."""
        for entries, allowed in (
            (["ui_walk.mjs", "hooks", "__pycache__", "gh_cli.py", "new_tool.py"], True),
            (["json.py"], False),
            (["re.pyc"], False),
            (["json"], False),  # a package directory, nested repository or gitlink
            (["json/"], False),
            (["sitecustomize.py"], False),
            (["resolve_review_thread"], False),
            (["resolve_review_thread.pyc"], False),
            (["ui_walk.mjs", "argparse"], False),
            # A case-insensitive file system resolves these to the same module.
            (["JSON.py"], False),
            (["SiteCustomize.py"], False),
            (["Resolve_Review_Thread.pyc"], False),
        ):
            with (
                self.subTest(entries=entries),
                mock.patch.object(guard, "same_as_develop", return_value=True),
                mock.patch.object(guard.os, "listdir", return_value=entries),
            ):
                self.assertEqual(guard.unchanged(), allowed)

    def test_a_nested_repository_named_like_a_standard_module_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "scripts"
            (scripts / "json").mkdir(parents=True)
            (scripts / "json" / ".git").write_text(
                "gitdir: elsewhere\n", encoding="utf-8"
            )
            (scripts / "ui_walk.mjs").write_text("", encoding="utf-8")
            with (
                mock.patch.object(guard, "SCRIPTS", scripts),
                mock.patch.object(guard, "run_modules", return_value={guard.RUN}),
                mock.patch.object(guard, "same_as_develop", return_value=True),
            ):
                self.assertFalse(guard.unchanged())
                (scripts / "json" / ".git").unlink()
                (scripts / "json").rmdir()
                self.assertTrue(guard.unchanged())

    def test_the_repository_scripts_hold_no_shadowing_entry(self):
        entries = os.listdir(ROOT / "scripts")
        self.assertEqual(
            [entry for entry in entries if guard.shadows(entry, {guard.RUN})], []
        )

    def test_the_run_file_is_compared_by_its_raw_bytes(self):
        """An attribute that converts the working copy must not make it match."""
        git = [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "core.autocrlf=false",
        ]
        run_file = guard.SCRIPTS / f"{guard.RUN}.py"
        # The developer's global and system git config (signing, hooks) stay out.
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            ),
        ):
            repo = Path(directory)
            script = repo / run_file
            script.parent.mkdir()
            script.write_bytes(b"print('ok')\n")
            for command in (
                ["init", "-q"],
                ["add", "."],
                ["commit", "-qm", "base"],
                ["update-ref", "refs/remotes/origin/develop", "HEAD"],
            ):
                subprocess.run([*git, *command], cwd=repo, check=True)
            previous = os.getcwd()
            os.chdir(repo)
            try:
                self.assertTrue(guard.same_as_develop(run_file))
                (repo / ".gitattributes").write_bytes(
                    b"*.py working-tree-encoding=UTF-16\n"
                )
                script.write_bytes("print('ok')\n".encode("utf-16"))
                self.assertFalse(guard.same_as_develop(run_file))
            finally:
                os.chdir(previous)

    def test_cached_bytecode_of_a_local_module_blocks(self):
        run = {"resolve_review_thread", "helper"}
        with (
            mock.patch.object(guard, "run_modules", return_value=run),
            mock.patch.object(guard, "same_as_develop", return_value=True),
            mock.patch.object(guard.Path, "is_dir", return_value=True),
            mock.patch.object(
                guard.os,
                "listdir",
                side_effect=[["helper.py"], ["helper.cpython-314.pyc"]],
            ),
        ):
            self.assertFalse(guard.unchanged())

    def test_a_listing_failure_blocks(self):
        with (
            mock.patch.object(guard, "same_as_develop", return_value=True),
            mock.patch.object(guard.os, "listdir", side_effect=OSError),
        ):
            self.assertFalse(guard.unchanged())


if __name__ == "__main__":
    unittest.main()
