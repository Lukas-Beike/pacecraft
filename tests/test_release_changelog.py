"""scripts/release_changelog.py on a synthetic squash-promote history; no network (#2899)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_changelog
from test_release_contains import git


def change(repo, day, subject):
    (Path(repo) / f"f{day}.txt").write_text(subject, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", subject, day=day)
    return git(repo, "rev-parse", "HEAD")


def promote(repo, tag, day):
    """Tag a main commit that carries develop's current tree, as the release chain does."""
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    git(
        repo,
        "tag",
        tag,
        git(repo, "commit-tree", tree, "-m", f"release {tag}", day=day),
    )


class ReleaseChangelogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = self.repo = Path(tmp.name)
        git(repo, "init", "-q", "-b", "develop")
        change(repo, 1, "feat(a): first")
        change(repo, 2, "chore(release): set application version to 1.0.0 (#1)")
        promote(repo, "1.0.0", 3)
        change(repo, 4, "fix(b): second")
        git(repo, "switch", "-q", "-c", "side")
        change(repo, 5, "feat(c): from a merged branch")
        git(repo, "switch", "-q", "develop")
        git(repo, "merge", "-q", "--no-ff", "side", "-m", "Merge branch 'side'", day=6)
        change(repo, 7, "chore(release): set application version to 1.0.1")
        self.release = git(repo, "rev-parse", "HEAD")
        promote(repo, "1.0.1", 8)
        # A hotfix tag whose tree never reached develop, and a non-release tag.
        git(repo, "switch", "-q", "-c", "hotfix")
        change(repo, 8, "fix(e): only on a hotfix branch")
        git(repo, "tag", "0.9.9")
        git(repo, "switch", "-q", "develop")
        git(repo, "branch", "-q", "-D", "hotfix")
        git(repo, "tag", "nightly")
        change(repo, 9, "docs(d): after the release")
        self.warnings = []

    def run_changelog(self, version):
        return release_changelog.changelog(
            version, cwd=self.repo, warn=self.warnings.append
        )["releases"]

    def test_each_release_lists_its_own_commits_without_chores_or_merges(self):
        releases = self.run_changelog("1.0.1")
        self.assertEqual(
            [(r["version"], r.get("after"), sorted(r["commits"])) for r in releases],
            [
                (None, "1.0.1", ["docs(d): after the release"]),
                ("1.0.1", None, ["feat(c): from a merged branch", "fix(b): second"]),
                ("1.0.0", None, ["feat(a): first"]),
            ],
        )
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("0.9.9", self.warnings[0])
        self.assertTrue(self.warnings[0].startswith("::warning::"))

    def test_a_release_build_names_its_new_version_and_a_release_tree_adds_nothing(
        self,
    ):
        self.assertEqual(self.run_changelog("1.0.2")[0]["version"], "1.0.2")
        git(self.repo, "switch", "-q", "--detach", self.release)
        self.assertEqual(
            [r["version"] for r in self.run_changelog("1.0.1")], ["1.0.1", "1.0.0"]
        )

    def test_the_build_fails_when_no_release_maps_to_its_history(self):
        for tag in ("1.0.0", "1.0.1"):
            git(self.repo, "tag", "-d", tag)
        with self.assertRaises(release_changelog.Refused):
            self.run_changelog("1.0.1")
        git(self.repo, "tag", "-d", "0.9.9")
        # Without any release tag the whole history is the build's own section.
        self.assertEqual(len(self.run_changelog("0.0.1")[0]["commits"]), 4)


if __name__ == "__main__":
    unittest.main()
