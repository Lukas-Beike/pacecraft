"""scripts/check_changed.py with a fake subprocess; nothing real runs."""

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_changed
import gh_cli


class FakeRun:
    """Stands in for subprocess.run; `failing` names a word in the argv to fail."""

    def __init__(self, failing=(), missing=(), extra=""):
        self.failing = failing
        self.missing = missing
        self.extra = extra
        self.calls, self.envs = [], []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.envs.append(kwargs.get("env"))
        if any(word in argv[0] for word in self.missing):
            raise FileNotFoundError(argv[0])
        failed = any(word in argv for word in self.failing)
        output = (
            "FAIL: test_x (mod.Case.test_x)\n"
            + "\n".join(f"line {n}" for n in range(30))
            + self.extra
            if failed
            else "fine"
        )
        return subprocess.CompletedProcess(argv, 1 if failed else 0, output, None)


def fake_git(changed, untracked=()):
    def git(*args, **_kwargs):
        if args[0] == "merge-base":
            return "abc123\n"
        if args[0] == "diff":
            return "\n".join(changed)
        return "\n".join(untracked)

    return git


def run_main(module, argv, runner, git=None):
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.object(check_changed.subprocess, "run", runner),
        mock.patch.object(check_changed, "git", git or fake_git(["backend/x.py"])),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


class CheckChangedTests(unittest.TestCase):
    def test_runs_the_steps_in_order_with_the_base(self):
        runner = FakeRun()
        code, out, _ = run_main(check_changed, ["--base", "origin/main"], runner)
        self.assertEqual(code, 0)
        steps = [line.split()[1] for line in out.splitlines()]
        self.assertEqual(steps, ["tests", "compile", "quality"])
        self.assertTrue(all(line.startswith("ok ") for line in out.splitlines()))
        first = runner.calls[0]
        self.assertEqual(first[0], sys.executable)
        self.assertEqual(first[-3:], ["--changed", "--base", "origin/main"])
        self.assertEqual(runner.calls[1][1:3], ["-m", "compileall"])
        # #2832: only the ratchet reads this checkout's node_modules, as the
        # pre-push hook does, so a missing ESLint fails here, not at the push.
        envs = dict(zip(steps, runner.envs, strict=True))
        modules = str(check_changed.ROOT / "node_modules")
        with mock.patch.dict("os.environ", {"PACECRAFT_NODE_MODULES": ""}):
            quality = check_changed.quality_env()
        self.assertEqual(quality["PACECRAFT_NODE_MODULES"], modules)
        self.assertEqual(quality.get("PATH"), check_changed.os.environ.get("PATH"))
        self.assertIsNotNone(envs["quality"])
        self.assertEqual((envs["tests"], envs["compile"]), (None, None))
        # A shared install someone already points at is kept.
        with mock.patch.dict("os.environ", {"PACECRAFT_NODE_MODULES": "shared"}):
            self.assertEqual(
                check_changed.quality_env()["PACECRAFT_NODE_MODULES"], "shared"
            )

    def test_a_failure_does_not_stop_later_steps_and_shows_a_tail(self):
        runner = FakeRun(failing=("compileall",))
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        with mock.patch.object(check_changed.tempfile, "tempdir", logs.name):
            code, out, _ = run_main(check_changed, [], runner)
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 3)
        self.assertIn("FAILED compile", out)
        self.assertIn("ok quality", out)
        self.assertIn("line 29", out)
        self.assertIn("line 10", out)
        self.assertNotIn("line 9\n", out)
        # The failed test's name sits far above the tail but is still shown,
        # and the whole output is kept in a temporary file.
        self.assertIn("FAIL: test_x (mod.Case.test_x)", out)
        log = Path(out.split("full output: ", 1)[1].splitlines()[0])
        self.assertEqual(log.parent, Path(logs.name))
        self.assertIn("line 5\n", log.read_text(encoding="utf-8"))
        # An unwritable temp directory drops only the log line.
        with mock.patch.object(
            check_changed.tempfile, "NamedTemporaryFile", side_effect=OSError
        ):
            code, out, _ = run_main(check_changed, [], runner)
        self.assertEqual(code, 1)
        self.assertIn("ok quality", out)
        self.assertNotIn("full output:", out)
        # A cp1252 console cannot encode the Node reporter's mark (#2637): the
        # report writes UTF-8 instead of ending in a traceback.
        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        errors = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        runner = FakeRun(failing=("compileall",), extra="\n✖ node �")
        with (
            mock.patch.object(check_changed.subprocess, "run", runner),
            mock.patch.object(check_changed, "git", fake_git(["backend/x.py"])),
            mock.patch.object(check_changed.tempfile, "tempdir", logs.name),
            redirect_stdout(console),
            redirect_stderr(errors),
        ):
            code = check_changed.main([])
        console.flush()
        self.assertEqual(code, 1)
        self.assertIn("✖ node �", console.buffer.getvalue().decode("utf-8"))
        self.assertEqual(errors.encoding, "utf-8")

    def test_node_tests_run_with_the_flag_or_when_the_diff_has_js(self):
        cases = [
            (["--js"], ["backend/x.py"], True),
            ([], ["public/app.js"], True),
            ([], ["tests/a.test.cjs"], True),
            ([], ["backend/x.py", "README.md"], False),
        ]
        for argv, changed, expected in cases:
            with self.subTest(argv=argv, changed=changed):
                runner = FakeRun()
                run_main(check_changed, argv, runner, fake_git(changed))
                node = [call for call in runner.calls if call[0] == "node"]
                self.assertEqual(len(node), int(expected))
                if expected:
                    self.assertEqual(node[0][1], "--test")
                    self.assertTrue(
                        all(path.endswith(".cjs") for path in node[0][2:]), node[0]
                    )
                    self.assertGreater(len(node[0]), 3)

    def test_a_changed_workflow_is_linted_with_the_pinned_actionlint(self):
        # #3377: the same image and flags as .github/workflows/workflow-lint.yml.
        lint = (check_changed.ROOT / ".github/workflows/workflow-lint.yml").read_text(
            encoding="utf-8"
        )
        for part in check_changed.ACTIONLINT:
            self.assertIn(part.replace('"', ""), lint.replace('"', ""))
        changed = [".github/workflows/workflow-lint.yml", ".github/workflows/gone.yml"]
        runner = FakeRun()
        code, out, _ = run_main(check_changed, [], runner, fake_git(changed))
        self.assertEqual(code, 0)
        docker = [call for call in runner.calls if call[0] == "docker"]
        self.assertEqual(len(docker), 1)
        self.assertEqual(docker[0][1:3], ["run", "--rm"])
        self.assertEqual(docker[0][-1], ".github/workflows/workflow-lint.yml")
        self.assertIn("ok actionlint", out)
        # No changed workflow: no Docker call.
        runner = FakeRun()
        run_main(check_changed, [], runner, fake_git(["backend/x.py"]))
        self.assertFalse(any(call[0] == "docker" for call in runner.calls))
        # A lint finding fails the run.
        runner = FakeRun(failing=("-oneline",))
        code, out, _ = run_main(check_changed, [], runner, fake_git(changed))
        self.assertEqual(code, 1)
        self.assertIn("FAILED actionlint", out)

    def test_actionlint_without_docker_is_reported_as_unrun(self):
        changed = fake_git([".github/workflows/workflow-lint.yml"])
        code, out, _ = run_main(
            check_changed, [], FakeRun(missing=("docker",)), changed
        )
        self.assertEqual(code, 0)
        self.assertIn("unrun actionlint: docker is not installed.", out)
        down = FakeRun(
            failing=("-oneline",), extra="\nCannot connect to the Docker daemon"
        )
        code, out, _ = run_main(check_changed, [], down, changed)
        self.assertEqual(code, 0)
        self.assertIn("unrun actionlint: the Docker daemon is not reachable.", out)
        desktop = "\nfailed to connect to the docker API at npipe:////./pipe/x"
        code, out, _ = run_main(
            check_changed, [], FakeRun(failing=("-oneline",), extra=desktop), changed
        )
        self.assertEqual(code, 0)
        self.assertIn("unrun actionlint", out)
        # The same text from a failing test step stays a test failure.
        tests = FakeRun(failing=("--changed",), extra=desktop)
        code, out, _ = run_main(check_changed, [], tests, changed)
        self.assertEqual(code, 1)
        self.assertIn("FAILED tests", out)

    def test_untracked_js_counts_as_a_change(self):
        git = fake_git(["backend/x.py"], untracked=["public/new.mjs"])
        runner = FakeRun()
        run_main(check_changed, [], runner, git)
        self.assertEqual(sum(call[0] == "node" for call in runner.calls), 1)

    def test_missing_tool_is_exit_two_and_other_steps_still_run(self):
        runner = FakeRun(missing=("node",))
        code, out, _ = run_main(check_changed, ["--js"], runner)
        self.assertEqual(code, 2)
        self.assertIn("FAILED node-tests", out)
        self.assertIn("node is not installed", out)
        self.assertIn("ok quality", out)

    def test_unknown_base_is_exit_two(self):
        def git(*_args, **_kwargs):
            raise gh_cli.CliError("git merge-base failed: bad revision")

        runner = FakeRun()
        code, _, err = run_main(check_changed, ["--base", "nope"], runner, git)
        self.assertEqual(code, 2)
        self.assertIn("bad revision", err)
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
