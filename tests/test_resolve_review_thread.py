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
import time
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

    def test_padded_brackets_are_read_in_linear_time(self):
        # #3507 review: each bracket scan is bounded, so 20 KB of `[`, `[.` or
        # `[:` reads at once (a quadratic scan took minutes); a padding that
        # stays open blocks.
        for padding in ("[" * 20000, "[." * 10000, "[:" * 10000):
            with self.subTest(padding[:2]):
                started = time.monotonic()
                guard.bracket_wildcards(f"echo '{padding}'; ls")
                with self.assertRaises(guard.OverlongBracket):
                    guard.bracket_wildcards(padding)
                self.assertLess(time.monotonic() - started, 5)

    def test_the_block_names_the_word_that_matched(self):
        # #3459: a variable path still blocks (fail closed) and the message says
        # why; the plain name keeps the plain message.
        err = io.StringIO()
        command = 'for f in a b; do git show "FETCH_HEAD:$f" > "$S/$f"; done'
        self.assertEqual(self.decide(command, err=err), 2)
        self.assertIn("Matched word `*/*` (variables read as `*`)", err.getvalue())
        self.assertIn("spell the path out", err.getvalue())
        err = io.StringIO()
        self.assertEqual(
            self.decide("python scripts/resolve_review_thread.py", err=err), 2
        )
        self.assertNotIn("Matched word", err.getvalue())
        # #3509 (Gitar): a ripgrep block says what is blocked, not "spell it out".
        err = io.StringIO()
        self.assertEqual(self.decide("rg --pre python x scripts", err=err), 2)
        self.assertIn("ripgrep's --pre, --hostname-bin", err.getvalue())
        self.assertNotIn("spell the path out", err.getvalue())

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

    def test_spelling_variants_of_the_script_name_block(self):
        # Case, quoting, escapes, globs and short names all reach the same file
        # (#3367); only the documented exact form runs.
        for command in (
            "python scripts/Resolve_Review_Thread.py 2671 PRRT_a1",
            "PYTHON SCRIPTS/RESOLVE_REVIEW_THREAD.PY 2671 PRRT_a1",
            "python scripts/resolve_'review'_thread.py 2671 PRRT_a1",
            'python scripts/resolve_"review"_thread.py 2671 PRRT_a1',
            "python scripts/resolve\\_review_thread.py 2671 PRRT_a1",
            "python scripts\\resolve_review_thread.py 2671 PRRT_a1",
            "python scripts/resolve_review_threa[d].py 2671 PRRT_a1",
            "python scripts/resolve_review_threa[!x].py 2671 PRRT_a1",
            "python scripts/resolve_review_threa[^x].py 2671 PRRT_a1",
            "python scripts/resolve_review_thr?ad.py 2671 PRRT_a1",
            "python scripts/resolve_*.py 2671 PRRT_a1",
            "python scripts/r*d.py 2671 PRRT_a1",
            "python scripts/*thread.py 2671 PRRT_a1",
            "python scripts/resolve_review_{thread,x}.py 2671 PRRT_a1",
            "python scripts/resolve_review_@(thread).py 2671 PRRT_a1",
            "python scripts/RESOLV~1.PY 2671 PRRT_a1",
            "cd scripts && python r* 2671 PRRT_a1",
            "python -m resolve_review_threa? 2671 PRRT_a1",
            "find scripts -name 'resolve*' -exec python {} 2671 PRRT_a1 ;",
            "python s*/r*.py 2671 PRRT_a1",
            "python scripts/resolve_review_threa[[:alpha:]].py 2671 PRRT_a1",
            # #3507: bash collating symbols and equivalence classes.
            "python scripts/[[.r.]]esolve_review_thread.py",
            "python scripts/[[=r=]]esolve_review_thread.py",
            "python scripts/resolve_review_threa[[.d.][.x.]].py",
            "python scripts/r[[=e=]]solve_review_thread.py 2671 PRRT_a1",
            # Bypass review of #3507: a top-level class, negated classes, `=`
            # and `:` inside a class, and a class pattern spanning words.
            "python scripts/[.r.]esolve_review_thread.py 1 PRRT_x",
            "python scripts/[![.x.]]esolve_review_thread.py",
            "python scripts/[^[=x=]]esolve_review_thread.py",
            "python scripts/[![:digit:]]esolve_review_thread.py",
            "python scripts/[=r=]esolve_review_thread.py",
            "python scripts/[:r:]esolve_review_thread.py",
            "echo [. ; python scripts/r?solve_review_thread.py 1 x.]",
            "echo [= ; python scripts/r?solve_review_thread.py 1 x=]",
            # Second round: an escaped or quoted `]` or separator inside a class,
            # and an unclosed `[.`, `[=` or `[:` as a plain member.
            *(
                # Third round: also after a stray `[` in an earlier word.
                f"{prefix}python scripts/resolve_review_th{spelling}ead.py"
                for prefix in ("", "echo [ ; ", "echo a[b ; ", "x=[ ")
                for spelling in (
                    "[\\]r]",
                    '["]"r]',
                    "[$'\\x5d'r]",
                    "[r[.]",
                    "[r[=]",
                    "[r[:]",
                    "[\\ r]",
                    "[';'r]",
                    "[\\;r]",
                    "['|'r]",
                    "['('r]",
                    "['<'r]",
                )
            ),
            # Fourth round: a quoted `[` earlier is a literal, not an opener;
            # an ANSI-C quote keeps its escapes; a padded class blocks.
            "echo '['; python scripts/r[[.e.]]solve_review_thread.py 'x]'",
            'echo "["; python scripts/r[[.e.]]solve_review_thread.py "x]"',
            "echo '[' && python scripts/r[[=e=]]solve_review_thread.py 'x]'",
            "a='['; python scripts/r[[.e.]]solve_review_thread.py b=']'",
            "echo $'\\''; python scripts/r[[.e.]]solve_review_thread.py 'x]'",
            "python scripts/r[" + "a" * 300 + "[.e.]]solve_review_thread.py",
            # Gitar on #3517: a quote in a comment or a substitution desyncs
            # the quote reading; the readings from each `[` read no quotes.
            "echo x # it's\npython scripts/r[[.e.]]solve_review_thread.py 1 x",
            'echo "$(echo "\'")"; python scripts/r[[.e.]]solve_review_thread.py 1 x',
            "echo x # it's\npython scripts/r[' 'e]solve_review_thread.py",
            "echo x # it's\necho [ ; python scripts/resolve_review_th[' 'r]ead.py",
            "echo x # it's\npython scripts/r["
            + "a" * 300
            + "' 'e]solve_review_thread.py",
            # Brackets swallowed again and again past the readings' work cap
            # block where the shell runs the word.
            "python '" + "[" * 4000 + "]'",
            "python {scripts/r,x}*.py 2671 PRRT_a1",
            "python scripts/@(resolve|x)_review_thread.py 2671 PRRT_a1",
            "python scripts/@(res@(olve))_review_thread.py 2671 PRRT_a1",
            "python $(ls scripts/r*) 2671 PRRT_a1",
            "gci scripts -Filter:r*.py | % { python $_ 2671 PRRT_a1 }",
            "gci scripts\\x,scripts\\r* | % { python $_ }",
            "python C:scripts\\RESOLV~1.PY 2671 PRRT_a1",
            "python scripts.\\RESOLV~1.PY\\. 2671 PRRT_a1",
            "python scripts/RE1A2B~1.PY 2671 PRRT_a1",
            "python scripts/resolve$''_review_thread.py 2671 PRRT_a1",
            "python scripts/$'\\x72'esolve_review_thread.py 2671 PRRT_a1",
            "python scripts/resolve_\\\nreview_thread.py 2671 PRRT_a1",
            "cmd /c python scripts\\res^olve_review_thread.py 2671 PRRT_a1",
            "python scripts/resolve_‘review’_thread.py 2671 PRRT_a1",
            'python "scripts/resolve`u{5f}review_thread.py" 2671 PRRT_a1',
            "python -c \"exec(open('scripts/resolve_' 'review_thread.py').read())\"",
            "python -c \"import runpy; runpy.run_path('scripts/\\x72esolve_review_thread.py')\"",
            "python -c \"__import__('\\N{LATIN SMALL LETTER R}esolve_review_thread')\"",
            "python -c 'import ｒesolve_review_thread'",
            "printf '\\N{'; python scripts/resolve_review_thread.py 2671 PRRT_a1; echo }",
            "python scripts/\\U00000072esolve_review_thread.py 2671 PRRT_a1",
            "for %f in (scripts\\RESOLV~?.PY) do python %f 2671 PRRT_a1",
            'cmd /c for %f in ("scripts\\r<.py") do python %f',
            'for %f in ("scripts::$INDEX_ALLOCATION\\r*") do python %f',
            "cd scripts/x && python ../r* 2671 PRRT_a1",
            "python ./r* 2671 PRRT_a1",
            "cd scripts && python $PWD/r* 2671 PRRT_a1",
            "cd scripts && python ${PWD}/r* 2671 PRRT_a1",
            "cd scripts && python ~+/r* 2671 PRRT_a1",
            "cd scripts && python %CD%\\r* 2671 PRRT_a1",
            # #3509: quoted words a blank apart stay apart in bash; ripgrep's
            # --pre and --hostname-bin run a program on the files it searches.
            "python '-u' 'scripts/'resolve_review_threa?.py 1 PRRT_x",
            "python 'x' 'scripts/'r?solve_review_thread.py",
            "rg --pre=python x scripts",
            'rg "--pre" python x .',
            "/usr/bin/rg -n --hostname-bin=python x",
            "ls; rg -l x --pre python scripts",
            # Bypass review: quoted, pathed, expanded or indirect ripgrep runs.
            "rg '--pre' 'python' x scripts",
            "rg '--hostname-bin' 'python' x",
            "C:\\tools\\rg.exe --pre python x scripts",
            "a=`rg --pre python x scripts`",
            "&{rg --pre python x scripts}",
            "rg --pr${x}e python x scripts",
            "rg --pr$@e python x scripts",
            "o=--pre; rg $o python x scripts",
            "x=rg; $x --pre python x scripts",
            "echo --pre python x scripts | xargs rg",
            "/usr/bin/r[g] --pre python x scripts",
            "printf -- '--pre=python' > c; RIPGREP_CONFIG_PATH=c rg x scripts",
            # Second round: empty substitutions and brace lists inside the option.
            "rg --p$()re python x scripts",
            "rg --p$(:)re python x scripts",
            "rg --hostname$()-bin python x",
            "export RIPGREP_CONFIG$()_PATH=c; rg x scripts",
            "rg --{pre,pre} python x scripts",
            # Gitar on #3523: every item of a brace list, and combined lists.
            "rg --pr{etty,e} python x scripts",
            "rg --{pretty,pre} python x scripts",
            "rg --hostname{x,-bin}=python x",
            "rg --{x,p}r{y,e} python x scripts",
            # Gitar: a quoted or escaped blank inside a brace list.
            "rg -{' ',-}pre python x scripts",
            "rg -{\\ ,-}pre python x scripts",
            # Gitar: a line continuation or a quoted line break in the list.
            "rg -{\\\n,-}pre python x scripts",
            "rg -{'\n',-}pre python x scripts",
            # A brace expansion too large to read blocks.
            "rg x --" + "{a,b}" * 20,
            # #3648: a folder glob that can still reach scripts/ or its short
            # name keeps blocking: a start of `scripts`, a tilde (8.3 name), a
            # dot start (`..`), a trailing dot Windows drops, a bare wildcard.
            "python scr*/r*.py 2671 PRRT_a1",
            "python sc?ipts/r*.py 2671 PRRT_a1",
            "python s[c]ripts/r*.py 2671 PRRT_a1",
            "python script~1/r*.py 2671 PRRT_a1",
            "python SCRIPT~1/resolv~1.py 2671 PRRT_a1",
            "python sc*~1/r*.py 2671 PRRT_a1",
            "cd scripts/x && python .*/r*.py 2671 PRRT_a1",
            "python scripts.*/r*.py 2671 PRRT_a1",
            "python */r*.py 2671 PRRT_a1",
            "python ../*/r*.py 2671 PRRT_a1",
            # Bypass review of #3648: a verbatim word that a substitution,
            # pipe, eval or chained link rewrites before the shell globs it.
            "w=qs*/res*.py; python ${w#q}",
            "w=qs*/res*.py; python ${w:1}",
            "w=x*/res*.py; python ${w/x/s}",
            "set -- qs*/res*.py; python ${1#q}",
            "read w <<< qs*/res*.py; python ${w#q}",
            "python $(echo qs*/res*.py | tr -d q)",
            "python $(echo qs*/res*.py | cut -c2-)",
            "w=qs*/res*.py; eval python ${w#q}",
            'w=qs*/res*.py; bash -c "python ${w#q}"',
            "w=qs*/res*.py\npython ${w#q} 1 x",
            "python (rvpa (echo qs*/res*.py).substring(1))",
            "python (gci (echo qs*/res*.py).trimstart('q'))",
            "$w = echo qs*/res*.py; python (rvpa $w.substring(1))",
            "ln -s scripts abc; python abc*/r*.py",
            "cmd /c mklink /j abc scripts && python abc*/r*.py",
            "ln -s scripts abc\npython abc*/r*.py",
            # Bypass reviews of #3759: a glob still counts wherever a program
            # other than a known inert one, or a construct, can run a word.
            "cat scripts/r*.py | python - 2671 PRRT_a1",
            "echo 'python scripts/r*.py 2671 PRRT_a1' | bash",
            "cp scripts/r*.py x.py && python x.py 2671 PRRT_a1",
            "env -u FOO python scripts/r*.py 2671 PRRT_a1",
            "timeout -s KILL 60 python scripts/r*.py 2671 PRRT_a1",
            "/usr/bin/env python scripts/r*.py 2671 PRRT_a1",
            "sudo -u root python scripts/r*.py 2671 PRRT_a1",
            "uv run --with pkg python scripts/r*.py 2671 PRRT_a1",
            "pypy3 scripts/r*.py 2671 PRRT_a1",
            "python3.14t scripts/r*.py 2671 PRRT_a1",
            "coverage run scripts/r*.py 2671 PRRT_a1",
            "poetry run python scripts/r*.py 2671 PRRT_a1",
            "git bisect run python scripts/r*.py 2671 PRRT_a1",
            "git rebase --exec 'python scripts/r*.py 1 x' HEAD~1",
            "git -c alias.x='!python scripts/r*.py' x",
            "GIT_SSH_COMMAND='python scripts/r*.py' git fetch",
            "python scripts/r*.py 2671 PRRT_a1 -m mypy",
            "if true; then python scripts/r*.py 2671 PRRT_a1; fi",
            "! python scripts/r*.py 2671 PRRT_a1",
            "<# x #> python scripts/r*.py 2671 PRRT_a1",
            'echo "C:\\"; python scripts/r*.py 2671 PRRT_a1; echo "D:\\"',
            "awk 'BEGIN{system(\"python scripts/r*.py 1 x\")}'",
            "$g = 'python'; & $g scripts/r*.py 2671 PRRT_a1",
            "$cat scripts/r*.py",
            "python $'scripts/resolve_review_\\x54hread.py' 2671 PRRT_a1",
            "ls \\x27 ; python scripts/r*.py 1 PRRT_x ; ls \\x27",
            "ls #'\npython scripts/r*.py 1 PRRT_x\nls #'",
            "echo $'\\'\\'' ; python scripts/r*.py 1 PRRT_x #'",
            "ls <# ' #> ; python scripts\\RESOLV~1.PY 1 PRRT_x ; ls <# ' #>",
            "ls 'a’; python scripts\\RESOLV~1.PY 1 PRRT_x; ls ‘b'",
            "ls C:\\; python scripts\\RESOLV~1.PY 1 PRRT_x",
            "ls .\\| python scripts\\RESOLV~1.PY 1 PRRT_x",
            "git config core.fsmonitor 'python scripts/r*.py 1 PRRT_x'",
            "./ls scripts/r*.py",
            "git bis''ect run python scripts/r*.py 1 PRRT_x",
            "git rebase -xpython scripts/r*.py HEAD~1",
            "git rebase --exe 'python scripts/r*.py' HEAD~1",
            "git grep -Opython x scripts/r*.py",
            "echo '' ; eval ls ';' python 'scripts/resolve_review_t*' 1 PRRT_abc",
            "''eval ls ';' python scripts/r*.py 1 PRRT_abc",
            "python -m mypy --python-executable scripts/r*.py x.py",
            "ls\rpython scripts/r*.py 1 PRRT_abc",
            "ls'/../x' scripts/r*.py 1 PRRT_abc",
            'cat"" scripts/r*.py',
            "j'ython' scripts/r*.py 1 PRRT_abc",
            "uni'x' scripts/r*.py",
            # #3982 advisor: a heredoc named like an inert program, whose body's
            # lone quote pairs across lines in this reading.
            (
                "cat <<true\necho '\ntrue\npython scripts/resolve_review_threa?.py 1 PRRT_x\n"
                "cat <<true\necho '\ntrue"
            ),
            # #4155: a heredoc's body is skipped only when bash keeps it literal and
            # the rest is inert; here the lines after `<<` run, or feed a runner.
            "echo \"<<'EOF'\"\npython scripts/r*.py 1 PRRT_x\nEOF",
            "echo \\<<'EOF'\npython scripts/r*.py 1 PRRT_x\nEOF",
            "echo # <<'EOF'\npython scripts/r*.py 1 PRRT_x\nEOF",
            "echo '\n<<'EOF'\npython scripts/r*.py 1 PRRT_x\nEOF",
            "cat <<EOF\n$(python scripts/r*.py 1 PRRT_x)\nEOF",
            "bash <<'EOF'\npython scripts/r*.py 1 PRRT_x\nEOF",
            "cat <<'EOF' | bash\npython scripts/r*.py 1 PRRT_x\nEOF",
            "python - <<'EOF'\nimport runpy; runpy.run_path(next(__import__('glob').iglob('scripts/r*.py')))\nEOF",
            "cat > f <<'EOF'\nx\nEOF\npython scripts/r*.py 1 PRRT_x",
            "cat > f <<'EOF'\nx\nEOF \npython scripts/r*.py 1 PRRT_x",
            "cat <<'EOF' <<'END'\nx\nEOF\nEND\npython scripts/r*.py 1 PRRT_x",
            "cat scripts/*; python scripts/*/r*.py 1 PRRT_x",
            # git grep runs a program only through -O/--open-files-in-pager.
            "git grep -O python x scripts/r*.py",
            "git grep -Opython x scripts/r*.py",
            "git grep --open-files-in-pager=python x scripts/r*.py",
            "git grep --open python x scripts/r*.py",
            "git grep -nOpython x -- 'scripts/r*'",
            "git grep --op=python x -- 'scripts/r*'",
            "git grep -[O]python x -- 'scripts/r*'",
            "(( ls <<'EOF' ))\npython scripts/r*.py 1 PRRT_x\nEOF",
            'cat <<\'echo\' "\n" ; python scripts/r*.py 1 PRRT_x ; echo "\necho\n"\necho',
            "echo @'\nx'\n<<'EOF'\n'@ ; python scripts/r*.py 1 PRRT_x\nEOF",
            # #4155: a double-quoted pattern keeps the old rule (1b declined).
            'git grep -n "planning_store\\.\\(insert\\|update\\)[a-z_]*" -- backend',
            # #3982: a single-quoted inside is read literally, nothing else is.
            "grep 'x’ ; python scripts/r*.py 1 PRRT_x ; grep ‘y' f",
            "grep $'\\x27' f; python scripts/r*.py 1 PRRT_x; grep $'\\x27' f",
            'grep "\'" f; python scripts/r*.py 1 PRRT_x; grep "\'" f',
            # A single-quoted run glued to a word keeps its inside read (fuzz).
            "cat scripts/RESOLV~1.p'\\'y",
            "echo 'a\\' ; python scripts/r*.py 1 PRRT_x ; echo '\\'",
            'echo "x\\" \'y" ; python scripts/r*.py 1 PRRT_x ; echo "\' z"',
            "grep '\r' f\rpython scripts/r*.py 1 PRRT_x",
            # sed only as `sed -n 'N[,M]p'` with plain non-option words after it.
            "sed -n '1p' -e 'e python scripts/r*.py 1 PRRT_x' f",
            "sed -n '1p' --expression='e python scripts/r*.py' f",
            "sed -ni '1p' f; python scripts/r*.py 1 PRRT_x",
            "sed -n '1p;e python scripts/r*.py 1 PRRT_x' f",
            "sed -n '1p' 'f' ; python scripts/r*.py 1 PRRT_x",
            "sed -n '1p' f; python scripts/r*.py 1 PRRT_x",
            "sed -n '1p' f\x00sed scripts/r*.py",
            # #3982 bypass review: a glob in sed's files can expand to an option.
            "sed -n '1p' scripts/r*.py",
            # #4091 gate: a sed shape that is a redirection's target starts no command.
            "> sed -n '5p' ls scripts/r*.py",
            "ls x 2> sed -n '5p' scripts/r*.py",
            "< sed -n '5p' ls scripts/r*.py",
            "touch -- '--expression=1e python scripts/r*.py 1 PRRT_x' a; sed -n '1p' *",
            # Quote marks the shells do not read as ours: fullwidth ones NFKC
            # turns into quotes, and PowerShell's low-9 and reversed ones.
            "echo ＇a ; python scripts/r*.py 1 PRRT_x ; echo b＇",
            "echo ＂x ; python scripts/RESOLV~1.PY 1 PRRT_x ; echo y＂",
            "echo 'a‚ ; python scripts/RESOLV~1.PY 1 PRRT_x ; echo ‚b'",
            "echo 'a‛ ; python scripts/RESOLV~1.PY 1 PRRT_x ; echo „b'",
            "echo a python scripts/RESOLV~1.PY 1 PRRT_x",
        ):
            with self.subTest(command):
                self.assertEqual(self.decide(command), 2)
        # Globs and names that cannot reach the script stay as they were.
        for command in (
            "node --test tests/*.cjs",
            "git diff -- openspec/specs/*/spec.md",
            "python scripts/release_dispatch.py follow",
            "ls tests/test_?.py",
            "for f in tests/*.py; do echo $f; done",
            "gh pr list -q '.[] | {n: .number}'",
            "awk '{print $1, $2}' notes.txt",
            "find . -name '*.py'",
            "ls *",
            "git commit -m 'fix: resolve review thread hook'",
            "grep -n resolveReviewThread docs/notes.md",
            'echo "costs \\$5 or \\${x}" > notes.txt',
            "git log -1 > notes.txt 2>&1",
            # Gitar on #3517: many brackets or a long open one in ordinary
            # commands neither block nor cost one reading per bracket.
            "echo " + "[a]" * 300,
            "python - <<'EOF'\n" + "x = [1, [2]]\n" * 300 + "EOF",
            'git commit -m "fix: [' + "words " * 60 + '"',
            "rg -n 'pre' docs",
            "rg -n x --pretty docs",
            "npm install --prefix tools",
            # Brace lists without `-` or `_` cannot spell a ripgrep option and
            # stay unexpanded; long commands stay within the budget (#3509).
            "echo " + "{a,b}" * 20,
            "cat <<'EOF'\n" + '{"key":1,"other":2}\n' * 4000 + "EOF",
            "python - <<'EOF'\n# it's a test\n"
            + "row = {'first_key': 1, 'second_key': 2}\n" * 4000
            + "EOF",
            "git log --pretty=oneline",
            # #3648: a folder glob whose literal start is not a start of
            # `scripts` can never be its folder.
            "grep -n x backend/providers/garmin*/*.py",
            "ls docs/perf*/m1*.md",
            "gh pr view 1 --json state -q '.state+\" \"+.title'",
            # #3759: globs and variables in words the shell does not run.
            "git add scripts/*.py",
            "ls scripts/*.py",
            "ruff format --check scripts/*.py",
            "python -m mypy scripts/*.py",
            "git ls-files 'scripts/*'",
            'echo "$a/$b"',
            'cp "$src"/* "$dst"/',
            "ls $d/*",
            "ls */*.py",
            'gh run list -q \'.[] | .name+" "+(.conclusion//"-")\'',
            "echo '" + "[" * 4000 + "]'",
            # #4155: git grep is inert; its single-quoted pattern is literal (1a).
            "git grep -n 'planning_store\\.\\(insert\\|update\\)[a-z_]*' -- backend",
            # #4155: a quoted-delimiter heredoc written to a file is literal text.
            "cat > body.md <<'EOF'\nGlobs such as */* or scripts/r*.py are text here.\nEOF",
            'cat > body.md <<"EOF"\nscripts/*/r*.py\nEOF\ngit status',
            # #3982: quoted grep patterns and a printed line range are no paths.
            "sed -n '10,40p' backend/performance/threshold_staleness.py | grep -n '\"[a-z_]*\":'",
            "grep -n 'within the weeks ahead.\\s*$' docs/features.md",
            "sed -n '1,80p' test.log | grep -v '^\\s*\\^*$\\|~~~'",
        ):
            with self.subTest(command):
                self.assertEqual(self.decide(command), 0)

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
