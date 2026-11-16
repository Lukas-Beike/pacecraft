"""scripts/branch_sync.py against temporary git repositories; no network."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import branch_sync
import gh_cli
from gh_cli import CliError

SONAR_NAME = "sonar.cmd" if os.name == "nt" else "sonar"


def git(cwd, *args):
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def commit(repo, name, text, subject):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", subject)


def make_sonar(directory):
    directory.mkdir(parents=True)
    tool = directory / SONAR_NAME
    tool.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n")
    tool.chmod(0o755)


class BranchSyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.origin = self.root / "origin.git"
        self.work = self.root / "work"
        git(self.root, "init", "-q", "--bare", "-b", "develop", str(self.origin))
        git(self.root, "init", "-q", "-b", "develop", str(self.work))
        for key, value in (
            ("user.name", "Test"),
            ("user.email", "test@example.test"),
            ("commit.gpgsign", "false"),
        ):
            git(self.work, "config", key, value)
        git(self.work, "remote", "add", "origin", str(self.origin))
        commit(self.work, "a.txt", "one\n", "chore: base")
        git(self.work, "push", "-q", "-u", "origin", "develop")
        git(self.work, "checkout", "-q", "-b", "feat/x")
        previous = Path.cwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, previous)

    def advance_develop(self, name="b.txt", text="two\n"):
        git(self.work, "checkout", "-q", "develop")
        git(self.work, "fetch", "-q", "origin")  # a second advance builds on the first
        git(self.work, "reset", "-q", "--hard", "origin/develop")
        commit(self.work, name, text, "chore: advance develop")
        git(self.work, "push", "-q", "origin", "develop")
        git(self.work, "reset", "-q", "--hard", "HEAD~1")
        git(self.work, "checkout", "-q", "feat/x")

    def sync(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = branch_sync.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_refuses_protected_and_detached_heads(self):
        git(self.work, "checkout", "-q", "develop")
        code, out, _ = self.sync()
        self.assertEqual((code, "refused: on develop" in out), (1, True))
        git(self.work, "checkout", "-q", "--detach")
        code, out, _ = self.sync()
        self.assertEqual((code, "detached HEAD" in out), (1, True))

    def test_refuses_a_dirty_tree_before_touching_anything(self):
        (self.work / "a.txt").write_text("dirty\n", encoding="utf-8")
        code, out, _ = self.sync()
        self.assertEqual((code, "uncommitted changes" in out), (1, True))

    def test_rebases_onto_the_fetched_target_and_reports_counts(self):
        commit(self.work, "c.txt", "mine\n", "feat(ui): add c")
        self.advance_develop()
        code, out, err = self.sync()
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"head [0-9a-f]+ on feat/x: ahead 1, behind 0")
        self.assertTrue((self.work / "b.txt").exists())
        self.assertEqual(git(self.work, "log", "-1", "--format=%s"), "feat(ui): add c")

    def test_a_stale_node_modules_is_refreshed_after_the_rebase(self):
        # #3133: a rebase onto a new lock file only failed later, in the
        # pre-push hook; a resumed or rerun sync must catch it too (Gitar P3).
        commit(self.work, "c.txt", "mine\n", "feat(ui): add c")
        real_run = subprocess.run

        def sync_with_npm(result, npm="npm"):
            def run(command, *args, **kwargs):
                if command[0] == "npm":  # git keeps running for real
                    return subprocess.CompletedProcess(command, result)
                return real_run(command, *args, **kwargs)

            with (
                mock.patch.object(branch_sync.shutil, "which", return_value=npm),
                mock.patch.object(
                    branch_sync.subprocess, "run", side_effect=run
                ) as spy,
            ):
                code, out, _ = self.sync()
            calls = [c.args[0] for c in spy.call_args_list if c.args[0][0] == "npm"]
            return code, out, calls

        ci = ["npm", "ci", "--ignore-scripts"]
        self.advance_develop()  # no lock file: nothing to install
        self.assertEqual(sync_with_npm(0)[::2], (0, []))
        locked = {"version": "2"}
        optional = {"version": "1", "optional": True}  # another platform's
        lock = {
            "packages": {"": {}, "node_modules/a": locked, "node_modules/o": optional}
        }
        self.advance_develop("package-lock.json", json.dumps(lock))
        self.assertEqual(sync_with_npm(0)[::2], (0, [ci]))
        # A rerun after a failed install, or a resumed rebase, still installs.
        code, out, calls = sync_with_npm(3)
        self.assertEqual((code, calls), (1, [ci]))
        self.assertIn("node_modules does not match package-lock.json", out)
        self.assertIn("refused: npm ci failed (exit 3)", out)
        code, out, _ = sync_with_npm(0, npm=None)
        self.assertEqual(code, 1)
        self.assertIn("refused: npm not found", out)
        installed = self.work / "node_modules/.package-lock.json"
        installed.parent.mkdir()
        installed.write_text(json.dumps({"packages": {"node_modules/a": locked}}))
        self.assertEqual(sync_with_npm(0)[::2], (0, []))

    def test_fetch_retries_a_ref_lock_and_fails_loudly_when_it_stays(self):
        # #2828: a parallel worktree's fetch holds a lock in the shared .git.
        moved = (
            "error: cannot lock ref 'refs/remotes/origin/develop': "
            "is at 1111 but expected 2222"
        )
        packed = "fatal: Unable to create '/r/.git/packed-refs.lock': File exists."
        clash = (
            "error: cannot lock ref 'refs/remotes/origin/foo': "
            "'refs/remotes/origin/foo/bar' exists"
        )
        real = branch_sync.git
        self.advance_develop()
        # (message, failing fetches, exit, sleeps); the stale cases run first.
        for message, failures, expect, sleeps in (
            (moved, 3, 2, 2),  # stays locked: fail loudly, ref named
            (clash, 1, 2, 0),  # a name conflict is permanent: no retry
            (packed, 1, 0, 1),  # retried once, then rebased
        ):
            left = [failures]

            def flaky(*args, left=left, message=message, **kwargs):
                if args[:1] == ("fetch",) and left[0]:
                    left[0] -= 1
                    raise CliError(f"git fetch origin failed: {message}")
                return real(*args, **kwargs)

            with (
                self.subTest(message=message),
                mock.patch.object(branch_sync, "git", flaky),
                mock.patch.object(branch_sync.time, "sleep") as sleep,
            ):
                code, _, err = self.sync()
                self.assertEqual((code, sleep.call_count), (expect, sleeps))
                self.assertEqual(err.count("retrying"), sleeps)
                rebased = (self.work / "b.txt").exists()
                self.assertEqual(rebased, expect == 0)  # never on a stale target
                if expect:
                    self.assertIn(message.split("'")[1], err)

    def test_conflict_stays_in_progress_and_lists_files(self):
        commit(self.work, "a.txt", "mine\n", "fix: mine")
        self.advance_develop("a.txt", "theirs\n")
        code, out, _ = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("a.txt", out)
        self.assertIn("git rebase --abort", out)
        self.assertTrue((self.work / ".git" / "rebase-merge").exists())

    def test_lists_invalid_subjects(self):
        commit(self.work, "c.txt", "1\n", "wip stuff")
        commit(self.work, "d.txt", "2\n", "feat(UI): bad scope")
        commit(self.work, "e.txt", "3\n", "fix(sync)!: fine")
        code, out, _ = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("wip stuff", out)
        self.assertIn("feat(UI): bad scope", out)
        self.assertNotIn("fix(sync)!", out)

    def routes(self, intro, section):
        begin, end = "<!-- BEGIN GENERATED ROUTES -->", "<!-- END GENERATED ROUTES -->"
        return f"{intro}\n{begin}\n{section}\n{end}\nTail\n"

    def conflicting_docs(self, develop_intro, branch_intro, name="docs/api-routes.md"):
        """develop and the branch both change a generated document's section."""
        (self.work / "docs").mkdir(exist_ok=True)
        git(self.work, "checkout", "-q", "develop")
        git(self.work, "reset", "-q", "--hard", "origin/develop")
        commit(self.work, name, self.routes("# Routes", "base"), "docs: add")
        git(self.work, "push", "-q", "origin", "develop")
        git(self.work, "checkout", "-q", "-B", "feat/x")
        commit(self.work, name, self.routes(branch_intro, "mine"), "docs: mine")
        git(self.work, "checkout", "-q", "develop")
        commit(self.work, name, self.routes(develop_intro, "up"), "docs: up")
        git(self.work, "push", "-q", "origin", "develop")
        git(self.work, "reset", "-q", "--hard", "HEAD~1")
        git(self.work, "checkout", "-q", "feat/x")

    def test_conflicts_only_in_generated_text_are_regenerated(self):
        # #3031: three manual rebase stops in one lane day for generated documents.
        self.conflicting_docs("# Routes", "# Routes")
        # A second branch commit conflicts again when it is replayed.
        commit(
            self.work,
            "docs/api-routes.md",
            self.routes("# Routes", "mine 2"),
            "docs: again",
        )
        asked = []

        def regenerate(root, documents):
            asked.append(documents)
            path = root / "docs/api-routes.md"
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("\nup\n", "\nregenerated\n"), encoding="utf-8")

        with mock.patch.object(branch_sync, "regenerate", regenerate):
            code, out, _ = self.sync()
        self.assertEqual(code, 0, out)
        self.assertEqual(
            out.count("regenerated after a conflict: docs/api-routes.md"), 2
        )
        self.assertEqual(asked, [("docs/api-routes.md",)] * 2)  # only its generator
        self.assertEqual(
            (self.work / "docs/api-routes.md").read_text(encoding="utf-8"),
            self.routes("# Routes", "regenerated"),
        )
        # A failing generator puts the conflict back and stops for a person.
        git(self.work, "reset", "-q", "--hard", "origin/develop")
        self.conflicting_docs("# Routes", "# Routes", "docs/view-catalogue.md")

        def failing(root, documents):
            (root / documents[0]).write_text("half written\n", encoding="utf-8")
            raise CliError("generator failed")

        with mock.patch.object(branch_sync, "regenerate", failing):
            code, out, _ = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("regenerating failed, conflicts restored", out)
        self.assertIn(
            "<<<<<<<",
            (self.work / "docs/view-catalogue.md").read_text(encoding="utf-8"),
        )
        git(self.work, "rebase", "--abort")
        # A conflict in the hand-written text still stops for a person.
        git(self.work, "reset", "-q", "--hard", "origin/develop")
        self.conflicting_docs("# Keys v2", "# My keys", "docs/kv-catalogue.md")
        with mock.patch.object(branch_sync, "regenerate", regenerate):
            code, out, _ = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("conflict: rebase onto origin/develop stopped in 1 file(s)", out)
        git(self.work, "rebase", "--abort")
        # A binary file under a generated path is never read as a document.
        chart = self.work / "docs/performance-methods/chart.md"
        chart.parent.mkdir(parents=True)
        chart.write_bytes(b"\xff\xfe\x00")
        with mock.patch.object(branch_sync, "regenerate", regenerate):
            self.assertEqual(
                branch_sync.resolve_generated(
                    self.work, ["docs/performance-methods/chart.md"]
                ),
                False,
            )

    def test_a_script_that_does_not_parse_stops_the_sync(self):
        # #3031: a broken hand merge of an e2e spec rode along into a push.
        (self.work / "e2e").mkdir()
        commit(self.work, "e2e/x.spec.js", "test('x', () => {\n", "test: broken")
        code, out, _ = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("scripts that do not parse (node --check):\n  e2e/x.spec.js", out)

    def test_push_uses_hooks_and_force_with_lease(self):
        hooks = self.work / ".githooks"
        hooks.mkdir()
        (hooks / "pre-push").write_text(
            "#!/bin/sh\ntouch hook-ran\n", encoding="utf-8", newline="\n"
        )
        (hooks / "pre-push").chmod(0o755)
        git(self.work, "add", ".githooks")
        git(self.work, "commit", "-q", "-m", "chore: add hook")
        with mock.patch.object(branch_sync, "push_env", return_value=dict(os.environ)):
            code, out, err = self.sync("--push")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("pushed: feat/x", out)
        self.assertEqual(
            git(self.origin, "rev-parse", "feat/x"), git(self.work, "rev-parse", "HEAD")
        )
        self.assertTrue((self.work / "hook-ran").exists())

        # #2944: every ratchet line reaches the refusal once, whether git
        # leaves the hook's stdout alone or sends it to stderr.
        findings = "".join(
            f"echo 'NEW mypy a.py:{n} misc: m{n}'{' >&2' * (n % 2)}\n" for n in range(7)
        )
        (hooks / "pre-push").write_text(
            f"#!/bin/sh\n{findings}echo 'OBSOLETE ruff b.py F401'\necho 'progress line'\n"
            "echo 'Push blocked: the quality ratchet could not run' >&2\nexit 1\n",
            encoding="utf-8",
            newline="\n",
        )
        git(self.work, "commit", "-q", "-am", "chore: refusing hook")
        with mock.patch.object(branch_sync, "push_env", return_value=dict(os.environ)):
            code, _, err = self.sync("--push")
        self.assertEqual(code, 2)
        self.assertIn("failed to push", err)
        self.assertIn("Push blocked: the quality ratchet could not run", err)
        for line in [f"NEW mypy a.py:{n} misc: m{n}" for n in range(7)] + [
            "OBSOLETE ruff b.py F401"
        ]:
            self.assertEqual(err.count(line), 1, line)
        self.assertNotIn("progress line", err)
        # A flood (a tool bump) is capped and says how much it left out.
        refused = subprocess.CompletedProcess(
            ["git"], 1, "".join(f"NEW ruff c.py:{n} E1\n" for n in range(60)), "x\n"
        )
        shown = gh_cli._push_refusal(refused, ["x"])
        self.assertEqual((len(shown), shown[50]), (52, "... and 10 more"))

    def test_push_keeps_remote_work_the_branch_has_not_seen(self):
        commit(self.work, "c.txt", "1\n", "feat(ui): add c")
        git(self.work, "push", "-q", "-u", "origin", "feat/x")
        other = self.root / "other"
        git(self.root, "clone", "-q", "-b", "feat/x", str(self.origin), str(other))
        git(other, "config", "user.name", "Other")
        git(other, "config", "user.email", "other@example.test")
        commit(other, "d.txt", "2\n", "feat(ui): remote only")
        git(other, "push", "-q", "origin", "feat/x")
        remote_tip = git(self.origin, "rev-parse", "feat/x")
        git(self.work, "commit", "-q", "--amend", "-m", "feat(ui): add c, reworded")
        with mock.patch.object(branch_sync, "push_env", return_value=dict(os.environ)):
            code, _, err = self.sync("--push", "--allow-root", "c.txt")
        self.assertEqual(code, 2)
        self.assertIn("rejected", err)
        self.assertEqual(git(self.origin, "rev-parse", "feat/x"), remote_tip)

    def test_invalid_subject_blocks_the_push(self):
        commit(self.work, "c.txt", "1\n", "wip")
        with mock.patch.object(branch_sync, "push_env", return_value=dict(os.environ)):
            code, _, _ = self.sync("--push")
        self.assertEqual(code, 1)
        self.assertEqual(git(self.origin, "branch", "--list", "feat/x"), "")

    def test_new_root_file_blocks_the_push_unless_allowed(self):
        commit(self.work, "stray-redirect", "temp path\n", "fix(ui): add file")
        with mock.patch.object(branch_sync, "push_env", return_value=dict(os.environ)):
            code, out, _ = self.sync("--push")
            self.assertEqual(code, 1)
            self.assertIn("  stray-redirect", out)
            self.assertEqual(git(self.origin, "branch", "--list", "feat/x"), "")
            code, out, _ = self.sync("--push", "--allow-root", "stray-redirect")
        self.assertEqual(code, 0)
        self.assertIn("pushed: feat/x", out)

    def test_push_without_sonar_exits_two_before_any_git_change(self):
        empty = self.root / "empty"
        empty.mkdir()
        with (
            mock.patch.object(branch_sync, "sonar_dirs", return_value=[empty]),
            mock.patch.object(branch_sync.shutil, "which", return_value=None),
        ):
            code, _out, err = self.sync("--push")
        self.assertEqual(code, 2)
        self.assertIn("SonarQube CLI", err)
        self.assertEqual(git(self.origin, "branch", "--list", "feat/x"), "")

    # Stacked pull requests (#3243): feat/x was built on PR #41 (feat/parent),
    # which develop has squash-merged; #42 is feat/x's own pull request.

    def stack(self, *, merged=True):
        """The parent's last head and its squash merge on origin/develop."""
        git(self.work, "checkout", "-q", "-b", "feat/parent", "develop")
        commit(self.work, "p.txt", "parent\n", "feat(ui): parent layer")
        parent_head = git(self.work, "rev-parse", "HEAD")
        git(self.work, "checkout", "-q", "-B", "feat/x", "feat/parent")
        commit(self.work, "c.txt", "child\n", "feat(ui): child layer")
        merge = "0" * 40
        if merged:
            self.advance_develop("p.txt", "parent\n")  # the squash of #41
            merge = git(self.origin, "rev-parse", "develop")
        return parent_head, merge

    def stacked_sync(self, parent, *args, state="MERGED"):
        parent_head, merge = parent
        calls = []

        def gh_json(*command):
            if command[:3] == ("pr", "view", "41"):
                return {
                    "state": state,
                    "headRefOid": parent_head,
                    "mergeCommit": {"oid": merge} if state == "MERGED" else None,
                    "baseRefName": "develop",
                }
            if command[:3] == ("pr", "view", "feat/x"):
                return {"number": 42, "baseRefName": "feat/parent"}
            raise AssertionError(command)

        def gh(*command):
            # The retarget comes before the push (#2934): nothing pushed yet.
            pushed = git(self.origin, "branch", "--list", "feat/x")
            calls.append((*command, "pushed" if pushed else "not pushed"))
            return ""

        with (
            mock.patch.object(branch_sync, "gh_json", side_effect=gh_json),
            mock.patch.object(branch_sync, "gh", side_effect=gh),
            mock.patch.object(branch_sync, "push_env", return_value=dict(os.environ)),
            mock.patch.object(branch_sync.time, "sleep") as self.settle,
        ):
            code, out, err = self.sync("--after-pr", "41", *args)
        return code, out, err, calls

    def test_a_stacked_layer_drops_its_merged_parent_and_is_retargeted(self):
        code, out, err, calls = self.stacked_sync(
            self.stack(), "--push", "--allow-root", "c.txt"
        )

        self.assertEqual((code, err), (0, ""))
        # Only the child's own commit stays on top of develop's squash.
        self.assertEqual(
            git(self.work, "log", "--format=%s", "origin/develop..HEAD"),
            "feat(ui): child layer",
        )
        self.assertEqual(
            calls, [("pr", "edit", "42", "--base", "develop", "not pushed")]
        )
        self.settle.assert_called_once_with(branch_sync.RETARGET_SETTLE_SECONDS)
        head = git(self.work, "rev-parse", "HEAD")
        self.assertEqual(git(self.origin, "rev-parse", "feat/x"), head)
        self.assertIn(f"PR #42: base develop, head {head[:10]}", out)

    def test_a_push_its_hook_refuses_fails_before_the_retarget_and_can_rerun(self):
        # #3247 review: a refused push must not leave the PR retargeted on the
        # old head, and a second run must finish the already restacked layer.
        parent = self.stack()
        hooks = self.work / ".githooks"
        hooks.mkdir()
        (hooks / "pre-push").write_text(
            "#!/bin/sh\n[ -f block ] && exit 1\nexit 0\n",
            encoding="utf-8",
            newline="\n",
        )
        (hooks / "pre-push").chmod(0o755)
        (self.work / "block").write_text("", encoding="utf-8")

        code, _out, _err, calls = self.stacked_sync(
            parent, "--push", "--allow-root", "c.txt"
        )
        self.assertEqual((code, calls), (2, []))
        self.assertEqual(git(self.origin, "branch", "--list", "feat/x"), "")

        (self.work / "block").unlink()
        code, _out, err, calls = self.stacked_sync(
            parent, "--push", "--allow-root", "c.txt"
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            calls, [("pr", "edit", "42", "--base", "develop", "not pushed")]
        )
        self.assertEqual(
            git(self.origin, "rev-parse", "feat/x"), git(self.work, "rev-parse", "HEAD")
        )
        self.assertEqual(
            git(self.work, "log", "--format=%s", "origin/develop..HEAD"),
            "feat(ui): child layer",
        )

    def test_a_base_without_the_parents_merge_is_refused_before_anything(self):
        # A failed or stale fetch: origin/develop does not hold #41's merge yet.
        parent = self.stack(merged=False)
        before = git(self.work, "rev-parse", "HEAD")
        code, out, _err, calls = self.stacked_sync(parent, "--push")

        self.assertEqual(code, 1)
        self.assertIn("refused: origin/develop does not contain the merge of #41", out)
        self.assertEqual((calls, git(self.work, "rev-parse", "HEAD")), ([], before))
        self.assertEqual(git(self.origin, "branch", "--list", "feat/x"), "")

    def test_an_unmerged_parent_or_a_foreign_head_is_refused(self):
        parent = self.stack()
        code, out, _err, calls = self.stacked_sync(parent, "--push", state="OPEN")
        self.assertEqual((code, calls), (1, []))
        self.assertIn("refused: #41 is OPEN, not merged", out)

        # A branch that was not built on #41's last head.
        code, out, _err, calls = self.stacked_sync(("f" * 40, parent[1]), "--push")
        self.assertEqual((code, calls), (1, []))
        self.assertIn("refused: feat/x is not built on #41's last head", out)

    def test_a_failed_fetch_stops_before_the_rebase_and_the_retarget(self):
        parent = self.stack()
        before = git(self.work, "rev-parse", "HEAD")
        with mock.patch.object(
            branch_sync, "fetch", side_effect=CliError("fatal: unable to access")
        ):
            code, _out, err, calls = self.stacked_sync(parent, "--push")
        self.assertEqual((code, calls), (2, []))
        self.assertIn("unable to access", err)
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), before)

    def test_without_a_push_the_layer_is_rebased_but_not_retargeted(self):
        code, out, _err, calls = self.stacked_sync(self.stack())
        self.assertEqual((code, calls), (0, []))
        self.assertIn("not retargeted: push the restacked layer with --push", out)


class SonarLookupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.base = {"PATH": str(Path(tmp.name) / "empty")}

    def test_leaves_the_environment_alone_when_sonar_is_on_path(self):
        make_sonar(self.home / "tools")
        env = {"PATH": str(self.home / "tools")}
        self.assertEqual(branch_sync.push_env(env, self.home), env)

    def test_prepends_the_first_known_install_directory(self):
        local = self.home / "AppData" / "sonarqube-cli" / "bin"
        make_sonar(local)
        make_sonar(self.home / ".local" / "bin")
        env = {**self.base, "LOCALAPPDATA": str(self.home / "AppData")}
        result = branch_sync.push_env(env, self.home)
        self.assertEqual(result["PATH"], f"{local}{os.pathsep}{self.base['PATH']}")
        self.assertEqual(env["PATH"], self.base["PATH"])

    def test_finds_the_home_directories(self):
        for parts in ((".sonar", "sonarqube-cli", "bin"), (".local", "bin")):
            with self.subTest(parts=parts):
                directory = self.home.joinpath(*parts)
                make_sonar(directory)
                result = branch_sync.push_env(self.base, self.home)
                self.assertTrue(result["PATH"].startswith(str(directory)))
                (directory / SONAR_NAME).unlink()

    def test_missing_cli_names_the_prerequisite(self):
        with self.assertRaisesRegex(CliError, "needs the SonarQube CLI"):
            branch_sync.push_env(self.base, self.home)


if __name__ == "__main__":
    unittest.main()
