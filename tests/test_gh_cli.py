"""scripts/gh_cli.py's shared command runner with a fake subprocess (#3393)."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gh_cli

# GitHub's gateway page as `gh` prints it: the whole HTML body on one line.
GATEWAY_PAGE = (
    "gh: <!DOCTYPE html><html><head><title>504 Gateway Time-out</title>"
    + "<style>body{font-family:sans-serif}</style>" * 400
    + "</head><body><h1>We couldn't respond in time.</h1></body></html> (HTTP 504)"
)


def outcomes(*results):
    """A fake subprocess.run answering each call with the next (code, stderr)."""
    calls = []
    pending = list(results)

    def run(argv, **_kwargs):
        calls.append(argv)
        code, stderr = pending.pop(0)
        return subprocess.CompletedProcess(
            argv, code, "ok" if code == 0 else "", stderr
        )

    return run, calls


class GhCliRunTests(unittest.TestCase):
    def setUp(self):
        sleep = mock.patch.object(gh_cli.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def run_gh(self, fake, *args):
        with mock.patch.object(gh_cli.subprocess, "run", fake):
            return gh_cli.gh(*args)

    def test_an_html_error_page_is_cut_to_its_status_and_first_words(self):
        fake, calls = outcomes((1, GATEWAY_PAGE), (1, GATEWAY_PAGE))
        with self.assertRaises(gh_cli.CliError) as caught:
            self.run_gh(fake, "issue", "view", "7")
        message = str(caught.exception)
        self.assertLess(len(message), 400)
        self.assertIn("504", message)
        self.assertNotIn("<", message)
        self.assertTrue(message.startswith("gh issue view failed: "))
        # A read hit a transient 5xx: tried once more, then reported.
        self.assertEqual(len(calls), 2)

    def test_a_transient_5xx_on_a_read_is_retried_once(self):
        fake, calls = outcomes((1, "HTTP 503: Service Unavailable"), (0, ""))
        self.assertEqual(self.run_gh(fake, "pr", "view", "3"), "ok")
        self.assertEqual(len(calls), 2)
        self.sleep.assert_called_once()
        for error in (
            "error connecting: operation timed out",
            "error connecting to api.github.com",  # #4059
        ):
            fake, calls = outcomes((1, error), (0, ""))
            self.assertEqual(self.run_gh(fake, "api", "repos/o/r/pulls/3"), "ok")
            self.assertEqual(len(calls), 2)

    def test_fetch_retries_an_incomplete_or_silent_transfer_from_git_output(self):
        # #3798: the cause may follow another error line, or git says nothing.
        incomplete = (
            "fatal: bad object 1111\n"
            "error: https://x did not send all necessary objects"
        )
        for stderr in (incomplete, "", "  \n"):
            with self.subTest(stderr=stderr):
                fake, calls = outcomes((1, stderr), (0, ""))
                with mock.patch.object(gh_cli.subprocess, "run", fake):
                    gh_cli.fetch("develop")
                self.assertEqual(len(calls), 2)
        # A permanent message still fails at once.
        fake, calls = outcomes(
            (1, "fatal: 'origin' does not appear to be a git repository")
        )
        with (
            mock.patch.object(gh_cli.subprocess, "run", fake),
            self.assertRaises(gh_cli.CliError),
        ):
            gh_cli.fetch("develop")
        self.assertEqual(len(calls), 1)

    def test_a_4xx_and_a_failed_write_are_not_retried(self):
        for args, stderr in (
            (("issue", "view", "9"), "HTTP 404: Not Found"),
            (("issue", "comment", "9", "--body-file", "x"), "HTTP 502: Bad Gateway"),
            (("api", "-X", "PATCH", "repos/o/r/issues/9"), "HTTP 503"),
            (("api", "graphql", "-f", "query=mutation{x}"), "HTTP 504"),
            (("pr", "merge", "4", "--auto", "--squash"), "HTTP 500"),
        ):
            with self.subTest(args=args):
                fake, calls = outcomes((1, stderr))
                with self.assertRaises(gh_cli.CliError):
                    self.run_gh(fake, *args)
                # A write that hit a 5xx may have been applied: never sent twice.
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
