"""Worktree pruning checks with mocked git, gh and disk calls; nothing is removed."""

import importlib.util
import io
import subprocess
import tempfile
import unittest
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prune_worktrees", ROOT / "scripts/prune_worktrees.py"
)
assert SPEC is not None and SPEC.loader is not None
prune = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prune)

DAY = 86400
Usage = namedtuple("Usage", "total used free")
# Subagent worktrees whose branch the main session squashed into another PR.
AGENTS = {
    ".claude/worktrees/agent-squashed": "t3/agent-squashed",
    ".claude/worktrees/agent-missing": "t3/agent-missing",
    ".claude/worktrees/agent-open": "t3/agent-open",
    ".claude/worktrees/agent-dirty": "t3/agent-dirty",
    "lane-squashed": "t3/lane-squashed",
}


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class FakeRepo:
    """Answers git/gh calls from a table; records every command."""

    def __init__(
        self,
        tmp,
        dirty=(),
        merged=(),
        pr_merged=(),
        remove_fails=(),
        reused=(),
        on_base=(),
        open_prs=(),
        patches=(),
    ):
        self.tmp = Path(tmp)
        self.reused = set(reused)
        self.dirty, self.merged = set(dirty), set(merged)
        self.pr_merged, self.remove_fails = set(pr_merged), set(remove_fails)
        # Branches whose changed files all read the same on the base (#3290).
        self.on_base, self.open_prs = set(on_base), set(open_prs)
        # Branches whose every commit has a patch-equivalent one on the base.
        self.patches = set(patches)
        self.calls = []
        for name in ("primary", "current", "old", "dirty", "open", "pr", *AGENTS):
            (self.tmp / name).mkdir(parents=True)

    def porcelain(self):
        rows = [
            ("primary", "develop"),
            ("current", "feat/current"),
            ("old", "feat/old"),
            ("dirty", "feat/dirty"),
            ("open", "feat/open"),
            ("pr", "feat/pr"),
            *AGENTS.items(),
        ]
        return "\n\n".join(
            f"worktree {self.tmp / d}\nHEAD abc\nbranch refs/heads/{b}\n"
            for d, b in rows
        )

    def git(self, *args, cwd=None):
        self.calls.append(("git", *args))
        if args[:2] == ("worktree", "list"):
            return done(self.porcelain())
        if args[0] == "rev-parse" and args[1] == "--show-toplevel":
            return done(str(self.tmp / "current"))
        if args[:2] == ("rev-parse", "--absolute-git-dir"):
            return done(str(Path(cwd) / ".git"))
        if args[0] == "rev-parse":
            return done(f"tip-of-{args[1]}\n")
        if args[0] == "status":
            return done(" M file\n" if Path(cwd).name in self.dirty else "")
        if args[0] in ("merge-base", "diff", "cherry", "--literal-pathspecs"):
            return self.content(args)
        if args[:2] == ("worktree", "remove"):
            bad = Path(args[2]).name in self.remove_fails
            return done(code=1 if bad else 0, stderr="contains changes\nmore")
        return done()

    def content(self, args):
        """Ancestry and the branch's changes against the base (#3290)."""
        if args[0] == "cherry":
            return done("- c1\n- c2\n" if args[2] in self.patches else "+ c1\n- c2\n")
        if args[1] == "--is-ancestor":
            return done(code=0 if args[2] in self.merged else 1)
        if args[0] == "merge-base":
            return done("fork-point\n")
        if args[0] == "diff" and "--name-only" in args:
            return done("backend/a.py\0docs/with space*.md\0")
        # diff --quiet base branch -- <paths>: only the exact, literal paths count.
        literal = args[-2:] == ("backend/a.py", "docs/with space*.md")
        on_base = args[args.index("--quiet") + 2] in self.on_base
        return done(code=0 if on_base and literal else 1)

    def run(self, args, cwd=None):
        self.calls.append(tuple(args))
        branch = args[args.index("--head") + 1]
        if args[args.index("--state") + 1] == "open":
            return done("17\n" if branch in self.open_prs else "")
        listed = ""
        if branch in self.pr_merged:
            listed = f"tip-of-{branch}\n"
        if branch in self.reused:
            listed = "head-of-an-older-pr\n"
        return done(listed)

    def removed(self):
        return [c for c in self.calls if c[1:3] == ("worktree", "remove")]


class PruneWorktreesTests(unittest.TestCase):
    def run_main(self, repo, argv, free_gb=50, age_days=10):
        out, err = io.StringIO(), io.StringIO()
        patches = [
            mock.patch.object(prune, "git", repo.git),
            mock.patch.object(prune, "run", repo.run),
            mock.patch.object(prune, "last_modified", lambda _p: 1000.0),
            mock.patch.object(
                prune.shutil,
                "disk_usage",
                lambda _p: Usage(0, 0, int(free_gb * 1024**3)),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with redirect_stdout(out), redirect_stderr(err):
            code = prune.main(argv, now=1000.0 + age_days * DAY)
        return code, out.getvalue(), err.getvalue()

    def make(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return FakeRepo(tmp.name, **kwargs)

    def test_parse_worktrees_reads_branch_detached_and_locked(self):
        text = (
            "worktree /a\nHEAD 1\nbranch refs/heads/develop\n\n"
            "worktree /b\nHEAD 2\ndetached\nlocked reason\n"
        )
        a, b = prune.parse_worktrees(text)
        self.assertEqual((a["path"], a["branch"]), ("/a", "develop"))
        self.assertTrue(b["detached"] and b["locked"] and b["branch"] is None)

    def test_dry_run_lists_candidates_and_removes_nothing(self):
        repo = self.make(
            dirty={"dirty"}, merged={"feat/old", "feat/dirty", "feat/current"}
        )
        code, out, _ = self.run_main(repo, [])
        self.assertEqual(code, 0)
        self.assertIn("prune  ", out)
        self.assertIn("feat/old", out.split("prune  ")[1])
        self.assertIn("uncommitted or untracked changes", out)
        self.assertIn("current worktree", out)
        self.assertIn("primary checkout", out)
        self.assertIn("branch not merged", out)
        self.assertEqual(repo.removed(), [])

    def test_apply_removes_without_force_and_deletes_branch(self):
        repo = self.make(merged={"feat/old"}, pr_merged={"feat/pr"})
        code, _out, _ = self.run_main(repo, ["--apply"])
        self.assertEqual(code, 0)
        removed = repo.removed()
        self.assertEqual(len(removed), 2)
        self.assertTrue(all("--force" not in c and "-f" not in c for c in removed))
        self.assertIn(("git", "branch", "-d", "feat/old"), repo.calls)
        self.assertIn(("git", "branch", "-D", "feat/pr"), repo.calls)
        for protected in ("feat/current", "feat/open", "feat/dirty", "develop"):
            self.assertNotIn(
                protected, [c[-1] for c in repo.calls if "branch" in c[:2]]
            )

    def test_reused_branch_name_with_new_commits_is_not_merged(self):
        repo = self.make(reused={"feat/old"})
        _, out, _ = self.run_main(repo, ["--apply"])
        self.assertIn("branch not merged", out)
        self.assertEqual(repo.removed(), [])
        self.assertNotIn(("git", "branch", "-D", "feat/old"), repo.calls)

    def test_a_squashed_agent_worktree_whose_changes_are_on_the_base_goes(self):
        # #3290: the main session squashed the subagent's work into another PR.
        repo = self.make(
            dirty={"agent-dirty"},
            on_base={
                "t3/agent-squashed",
                "t3/agent-open",
                "t3/agent-dirty",
                "t3/lane-squashed",
            },
            open_prs={"t3/agent-open"},
        )
        _code, out, _ = self.run_main(repo, [])
        lines = {
            line.split("[")[1].split("]")[0]: line
            for line in out.splitlines()
            if "[" in line
        }
        self.assertTrue(lines["t3/agent-squashed"].startswith("prune  "))
        self.assertIn("content on base", lines["t3/agent-squashed"])
        # A change the base lacks, an open PR or local edits keep the worktree.
        self.assertIn("branch not merged", lines["t3/agent-missing"])
        self.assertIn("branch not merged", lines["t3/agent-open"])
        self.assertIn("uncommitted or untracked changes", lines["t3/agent-dirty"])
        # The rule covers subagent worktrees only, not a lane's own worktree.
        self.assertIn("branch not merged", lines["t3/lane-squashed"])

        # #3294 review: the paths reach git whole and literal, even with a space.
        quiet = [c for c in repo.calls if "--quiet" in c]
        self.assertTrue(quiet)
        self.assertTrue(all(c[1] == "--literal-pathspecs" for c in quiet))

        # Applied, the squashed branch is not an ancestor, so it goes with -D.
        _code, _out, _ = self.run_main(repo, ["--apply"])
        self.assertIn(("git", "branch", "-D", "t3/agent-squashed"), repo.calls)
        removed = [c[3] for c in repo.removed()]
        self.assertEqual(
            [Path(path).name for path in removed if "agent-" in path],
            ["agent-squashed"],
        )

    def test_an_agent_branch_whose_commits_all_landed_as_patches_goes(self):
        # Develop changed the files since, so they differ, but every commit
        # has a patch-equivalent one there (git cherry: only "-").
        repo = self.make(patches={"t3/agent-missing"})
        _code, out, _ = self.run_main(repo, [])
        line = next(row for row in out.splitlines() if "[t3/agent-missing]" in row)
        self.assertTrue(line.startswith("prune  "))
        self.assertIn("content on base", line)

    def test_recently_modified_worktree_is_kept(self):
        repo = self.make(merged={"feat/old"})
        _, out, _ = self.run_main(repo, ["--apply"], age_days=1)
        self.assertIn("modified 1.0 days ago", out)
        self.assertEqual(repo.removed(), [])
        _, out, _ = self.run_main(repo, ["--idle-days", "0.5"], age_days=1)
        self.assertIn("prune  ", out)

    def test_failed_remove_keeps_branch_and_exits_nonzero(self):
        repo = self.make(merged={"feat/old"}, remove_fails={"old"})
        code, out, _ = self.run_main(repo, ["--apply"])
        self.assertEqual(code, 1)
        self.assertIn("failed ", out)
        self.assertIn("contains changes", out)
        self.assertNotIn(("git", "branch", "-d", "feat/old"), repo.calls)

    def test_low_disk_space_warns_and_threshold_is_configurable(self):
        repo = self.make()
        _, out, err = self.run_main(repo, [], free_gb=2)
        self.assertIn("Free disk space: 2.0 GB", out)
        self.assertIn("WARNING: below 10 GB", err)
        _, _, err = self.run_main(repo, ["--min-free-gb", "1"], free_gb=2)
        self.assertEqual(err, "")

    def test_schedule_registers_a_daily_apply_from_the_primary_checkout(self):
        # #3925: nothing ran the prune, so worktrees piled up.
        calls = []

        def run(args, **_kwargs):
            calls.append(args)
            return done()

        out = io.StringIO()
        with mock.patch.object(prune, "run", run), redirect_stdout(out):
            self.assertEqual(prune.schedule("C:/repo", windows=True), 0)
        (args,) = calls
        self.assertEqual(args[:9], ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "04:00", "/TN", prune.TASK])  # fmt: skip
        self.assertTrue(args[10].startswith('cmd /c cd /d "C:/repo" && "'))
        self.assertTrue(args[10].endswith("scripts/prune_worktrees.py --apply"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(prune.schedule("/srv/repo", windows=False), 0)
        self.assertIn('0 4 * * * cd "/srv/repo" && "', out.getvalue())
        repo = self.make()
        with mock.patch.object(prune, "schedule", return_value=0) as scheduled:
            self.assertEqual(self.run_main(repo, ["--schedule"])[0], 0)
        scheduled.assert_called_once_with(str(repo.tmp / "primary"))

    def test_outside_a_repository_exits_nonzero(self):
        err = io.StringIO()
        with (
            mock.patch.object(prune, "git", lambda *a, **k: done(code=128)),
            redirect_stderr(err),
        ):
            self.assertEqual(prune.main([]), 1)
        self.assertIn("not inside a Git repository", err.getvalue())


if __name__ == "__main__":
    unittest.main()
