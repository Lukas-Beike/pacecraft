"""scripts/release_changelog.py on a synthetic squash-promote history; no network (#2899)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_changelog
from test_release_contains import commit, git


def change(repo, day, subject, trailer=True):
    """A commit whose Changelog trailer is its subject's text, as the German line would be."""
    message = (
        f"{subject}\n\nChangelog: {subject.partition(': ')[2]}" if trailer else subject
    )
    return commit(repo, day, f"f{day}.txt", subject, subject=message)


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
        change(
            repo, 7, "chore(release): set application version to 1.0.1", trailer=False
        )
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
                (None, "1.0.1", ["docs: after the release"]),
                ("1.0.1", None, ["feat: from a merged branch", "fix: second"]),
                ("1.0.0", None, ["feat: first"]),
            ],
        )
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("0.9.9", self.warnings[0])
        self.assertTrue(self.warnings[0].startswith("::warning::"))

    def test_a_later_revert_to_a_release_tree_stays_in_the_build_section(self):
        git(self.repo, "revert", "--no-edit", "HEAD", day=10)
        releases = self.run_changelog("1.0.1")
        self.assertEqual(len(releases[0]["commits"]), 1)
        self.assertEqual(
            sorted(releases[1]["commits"]),
            ["feat: from a merged branch", "fix: second"],
        )

    def test_a_release_build_names_its_new_version_and_a_release_tree_adds_nothing(
        self,
    ):
        self.assertEqual(self.run_changelog("1.0.2")[0]["version"], "1.0.2")
        git(self.repo, "switch", "-q", "--detach", self.release)
        self.assertEqual(
            [r["version"] for r in self.run_changelog("1.0.1")], ["1.0.1", "1.0.0"]
        )

    def test_a_build_of_main_lists_the_promoted_develop_commits(self):
        """main holds one squashed promotion per release; its changes are on develop (#2960)."""
        main = None
        for tag, day in (("1.0.0", 3), ("1.0.1", 8)):
            tree = git(self.repo, "rev-parse", f"{tag}^{{tree}}")
            main = git(
                self.repo,
                "commit-tree",
                tree,
                *(["-p", main] if main else []),
                "-m",
                f"chore(release): promote {tag} to main (#9)",
                day=day,
            )
            git(self.repo, "tag", "-f", tag, main)
        git(self.repo, "switch", "-q", "--detach", main)
        self.assertEqual(
            [(r["version"], sorted(r["commits"])) for r in self.run_changelog("1.0.1")],
            [
                ("1.0.1", ["feat: from a merged branch", "fix: second"]),
                ("1.0.0", ["feat: first"]),
            ],
        )

    def test_a_missing_develop_ref_is_named(self):
        self.run_changelog("1.0.1")
        self.assertNotIn("develop", " ".join(self.warnings))
        git(self.repo, "branch", "-q", "-m", "develop", "trunk")
        self.warnings.clear()
        self.run_changelog("1.0.1")
        self.assertIn(
            "::warning::No develop ref; the changelog follows HEAD's history.",
            self.warnings,
        )

    def test_only_a_commit_with_a_changelog_trailer_is_shown_as_its_german_line(self):
        commit(self.repo, 10, "g.txt", "g", subject="fix(x): without a trailer")
        commit(
            self.repo,
            11,
            "h.txt",
            "h",
            subject="feat(y)!: with one\n\nBody.\n\nChangelog: Die Woche zeigt die Belastung.",
        )
        self.assertEqual(
            sorted(self.run_changelog("1.0.1")[0]["commits"]),
            ["docs: after the release", "feat!: Die Woche zeigt die Belastung."],
        )

    def test_both_release_chores_are_left_out(self):
        for subject in (
            "chore(release): set application version to 2.1.15 (#2945)",
            "chore(release): promote 2.1.15 to main (#2951)",
            "chore(release): promote 2.1.15 to main",
        ):
            self.assertTrue(release_changelog.RELEASE_CHORE.match(subject), subject)
        self.assertFalse(
            release_changelog.RELEASE_CHORE.match("chore(release): pin the runner")
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
