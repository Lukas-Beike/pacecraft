"""Exercise the diagnostic ratchet without running providers or changing sources."""

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import quality_baseline


class QualityBaselineTests(unittest.TestCase):
    def setUp(self):
        self.versions = {"ruff": "0.16.10", "mypy": "2.4.0"}
        self.existing = {
            "tool": "ruff",
            "path": "backend/example.py",
            "line": 4,
            "column": 2,
            "code": "F401",
            "message_sha256": "a" * 64,
        }
        self.baseline = {
            "schema": 1,
            "versions": self.versions,
            "diagnostics": [self.existing],
        }

    def check(self, diagnostics):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = quality_baseline.check(self.baseline, self.versions, diagnostics)
        return result, output.getvalue()

    def test_existing_diagnostic_is_accepted(self):
        self.assertEqual(self.check([self.existing])[0], 0)

    def test_the_executable_bit_rules_follow_the_git_modes_on_windows(self):
        # #3091: Ruff cannot read the bit on Windows, and Linux CI failed a
        # shebang file whose mode was 100644 after every local check passed.
        files = {"shebang.py": "#!/usr/bin/env python3\n", "plain.py": "x = 1\n",
                 "script.py": "#!/usr/bin/env python3\n", "bare.py": "y = 2\n",
                 "skript ä.py": "#!/usr/bin/env python3\n"}  # fmt: skip
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid"}  # fmt: skip
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")

            def git(*args):
                subprocess.run(["git", "-C", directory, *args], check=True,
                               capture_output=True, env=env)  # fmt: skip

            git("init", "-q")
            git("add", ".")
            # A path git quotes (space, non-ASCII) keeps its mode too (Gitar P3).
            git("update-index", "--chmod=+x", "script.py", "bare.py", "skript ä.py")

            def found(**kwargs):
                return sorted(
                    (item["path"], item["code"], item["line"])
                    for item in quality_baseline.executable_bit_diagnostics(
                        root, sorted(files), **kwargs
                    )
                )

            expected = [("bare.py", "EXE002", 1), ("shebang.py", "EXE001", 1)]
            # A working tree reads its index.
            with patch.dict(os.environ, {}, clear=False) as environ:
                environ.pop("PACECRAFT_GIT_ROOT", None)
                self.assertEqual(found(windows=True), expected)
            # Elsewhere Ruff reports them itself, so nothing is added twice.
            self.assertEqual(found(windows=False), [])
            # The pre-push export has no .git: the clone's HEAD gives the modes.
            git("commit", "-q", "-m", "fixture")
            with tempfile.TemporaryDirectory() as exported:
                for name, text in files.items():
                    (Path(exported) / name).write_text(text, encoding="utf-8")
                with patch.dict(os.environ, {"PACECRAFT_GIT_ROOT": directory}):
                    diagnostics = quality_baseline.executable_bit_diagnostics(
                        Path(exported), sorted(files), windows=True
                    )
            self.assertEqual(
                sorted((item["path"], item["code"]) for item in diagnostics),
                [("bare.py", "EXE002"), ("shebang.py", "EXE001")],
            )
        # The identity matches Ruff's own finding on Linux: its message, hashed.
        self.assertEqual(
            diagnostics[0]["message_sha256"],
            quality_baseline.hashlib.sha256(
                quality_baseline.EXECUTABLE_RULES[diagnostics[0]["code"]].encode()
            ).hexdigest(),
        )

    def test_a_push_that_unformats_a_formatted_file_is_new_debt(self):
        # #3061: a change left four formatted files unformatted and nothing noticed.
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            (tree / "pyproject.toml").write_text(
                (root / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
            )
            (tree / "neat.py").write_text("x = [1, 2]\n", encoding="utf-8")
            (tree / "known.py").write_text("y  =  1\n", encoding="utf-8")
            (tree / "touched.py").write_text("z = [1,2]\n", encoding="utf-8")
            found = quality_baseline.format_diagnostics(
                tree, ["known.py", "neat.py", "touched.py"]
            )
        self.assertEqual(
            [(item["tool"], item["path"], item["code"]) for item in found],
            [("format", "known.py", "format"), ("format", "touched.py", "format")],
        )
        # A file unformatted when the check arrived is baseline debt; touched.py,
        # formatted before, is new and blocks.
        self.baseline["diagnostics"] = [self.existing, found[0]]
        result, output = self.check([self.existing, *found])
        self.assertEqual(result, 1)
        self.assertIn("NEW format touched.py format", output)
        self.assertNotIn("NEW format known.py", output)
        self.assertEqual(self.check([self.existing, found[0]])[0], 0)

    def test_a_new_css_duplicate_is_new_debt_not_an_unknown_tool(self):
        # #3077 added the CSS check, but the baseline refused its tool name, so a
        # real duplicate stopped the ratchet as "could not run" instead of NEW.
        css = {"tool": "css", "path": "public/styles.css", "line": 6,
               "code": "S4666", "message_sha256": "c" * 64}  # fmt: skip
        result, output = self.check([self.existing, css])
        self.assertEqual(result, 1)
        self.assertIn("NEW css public/styles.css:6 S4666", output)

    def test_line_and_column_shifts_do_not_change_debt(self):
        moved = {**self.existing, "line": 9, "column": 1}
        result, output = self.check([moved])
        self.assertEqual(result, 0)
        self.assertIn("0 new, 0 obsolete", output)

    def test_new_violation_cannot_replace_one_fixed_violation(self):
        changed = {**self.existing, "code": "F811"}
        result, output = self.check([changed])
        self.assertEqual(result, 1)
        self.assertIn("NEW ruff backend/example.py:4 F811", output)
        self.assertIn("OBSOLETE ruff backend/example.py F401", output)

    def test_committed_baseline_stores_line_independent_entries(self):
        root = Path(__file__).resolve().parents[1]
        baseline = json.loads(
            (root / "tests/fixtures/quality_baseline.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {tuple(sorted(item)) for item in baseline["diagnostics"]},
            {("code", "message_sha256", "path", "tool")},
        )

    def test_snapshot_writes_line_independent_entries(self):
        output = io.StringIO()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        target = Path(temp.name) / "tests/fixtures/quality_baseline.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(self.baseline), encoding="utf-8")
        with (
            patch("tools.quality_baseline.ROOT", Path(temp.name)),
            patch("tools.quality_baseline.BASELINE", target),
            patch(
                "tools.quality_baseline.collect",
                return_value=(self.versions, [self.existing]),
            ),
            patch.object(
                sys, "argv", ["quality_baseline.py", "--snapshot", "--base", "a" * 40]
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(quality_baseline.main(), 0)
        self.assertIn("wrote tests/fixtures/quality_baseline.json", output.getvalue())
        snapshot = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(
            snapshot["diagnostics"],
            [
                {
                    "tool": "ruff",
                    "path": "backend/example.py",
                    "code": "F401",
                    "message_sha256": "a" * 64,
                }
            ],
        )

    def test_obsolete_diagnostic_requires_baseline_cleanup(self):
        result, output = self.check([])
        self.assertEqual(result, 1)
        self.assertIn("1 obsolete", output)
        self.assertIn("Regenerate with", output)

    def test_removed_baseline_entry_cannot_be_reintroduced(self):
        self.baseline["diagnostics"] = []
        result, output = self.check([self.existing])
        self.assertEqual(result, 1)
        self.assertIn("NEW ruff backend/example.py:4", output)

    def test_duplicate_diagnostics_are_not_collapsed(self):
        self.assertEqual(self.check([self.existing, self.existing])[0], 1)

    def test_an_unknown_tool_is_rejected_from_the_quality_baseline(self):
        self.baseline["diagnostics"] = [{"tool": "black"}]
        with self.assertRaisesRegex(
            RuntimeError,
            "Ruff, Ruff format, mypy, cognitive complexity, CSS duplicate selector "
            "and ESLint",
        ):
            self.check([])

    def test_the_gate_runs_ruff_check_mypy_and_a_read_only_format_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text("value = 1\n", encoding="utf-8")
            with (
                patch("tools.quality_baseline.run") as mocked_run,
                patch(
                    "tools.quality_baseline.executable_bit_diagnostics",
                    return_value=[],
                ),
            ):
                mocked_run.side_effect = [
                    "ruff 0.16.10\n",
                    "mypy 2.4.0\n",
                    "[]",
                    "\n".join(
                        json.dumps({
                            "file": "sample.py", "line": 1, "column": 0,
                            "code": "misc", "message": f"Synthetic {word}",
                            "severity": "error",
                        })
                        for word in ("one", "two")
                    ),
                    "[]",
                ]  # fmt: skip
                notes: dict[str, str] = {}
                _, diagnostics = quality_baseline.collect(root, notes)
            commands = [call.args[0] for call in mocked_run.call_args_list]
        # #2944: the record stays hashed; the message prints beside NEW only,
        # each finding its own even at the same position.
        self.assertNotIn("Synthetic", str(diagnostics))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            quality_baseline.check(self.baseline, self.versions, diagnostics, (), notes)
        for word in ("one", "two"):
            self.assertIn(
                f"NEW mypy sample.py:1 misc: Synthetic {word}", output.getvalue()
            )
        ruff = quality_baseline.tool_command("ruff")
        mypy = quality_baseline.tool_command("mypy")
        self.assertTrue(
            any(command[: len(ruff) + 1] == [*ruff, "check"] for command in commands)
        )
        # #3217: a prepared worktree runs its own tools, else this interpreter's.
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            (tools / "ruff").mkdir()
            (tools / "mypy").mkdir()
            with patch.object(quality_baseline, "TOOLS_DIR", tools):
                own_ruff = quality_baseline.tool_command("ruff")
                own_mypy = quality_baseline.tool_command("mypy")
            with patch.object(quality_baseline, "TOOLS_DIR", tools / "absent"):
                self.assertEqual(
                    quality_baseline.tool_command("ruff"),
                    [sys.executable, "-m", "ruff"],
                )
        self.assertEqual(Path(own_ruff[0]).parent, tools / "bin")
        self.assertEqual(quality_baseline.tool_name(own_ruff), "ruff")
        self.assertEqual(own_mypy[:2], [sys.executable, "-c"])
        self.assertIn(repr(str(tools)), own_mypy[2])
        self.assertEqual(quality_baseline.tool_name(own_mypy), "mypy")
        self.assertTrue(any(command[: len(mypy)] == mypy for command in commands))
        # #3061: the format check reports only; the gate never rewrites a file.
        formats = [command for command in commands if "format" in command]
        self.assertEqual(len(formats), 1)
        self.assertIn("--check", formats[0])

    def test_commands_stay_under_the_windows_command_line_limit(self):
        # Long synthetic paths force several Ruff batches.
        files = [f"tests/{'nested/' * 20}module_{index}.py" for index in range(450)]
        ruff_commands = []
        format_commands = []
        mypy_files = []

        def fake_run(command, root, allowed=(0, 1)):
            self.assertLess(len(subprocess.list2cmdline(command)), 32767)
            if "--version" in command:
                return f"{command[0]} 2.4.0\n"
            ruff = quality_baseline.tool_command("ruff")
            if command[: len(ruff) + 1] == [*ruff, "check"]:
                ruff_commands.append(command)
                return "[]"
            if command[: len(ruff) + 1] == [*ruff, "format"]:
                format_commands.append(command)
                return "[]"
            mypy = quality_baseline.tool_command("mypy")
            self.assertEqual(command[: len(mypy)], mypy)
            self.assertFalse(set(files) & set(command))
            [list_arg] = [arg for arg in command if arg.startswith("@")]
            mypy_files.extend(
                Path(list_arg[1:]).read_text(encoding="utf-8").splitlines()
            )
            return ""

        with (
            patch("tools.quality_baseline.source_files", return_value=files),
            patch("tools.quality_baseline.cognitive_diagnostics", return_value=[]),
            patch("tools.quality_baseline.executable_bit_diagnostics", return_value=[]),
            patch("tools.quality_baseline.run", side_effect=fake_run),
            patch("tools.quality_baseline.javascript_diagnostics", return_value=[]),
        ):
            quality_baseline.collect(Path.cwd(), {})
        self.assertGreater(len(ruff_commands), 1)
        self.assertEqual(
            [arg for command in ruff_commands for arg in command if arg in files],
            files,
        )
        self.assertEqual(mypy_files, files)
        self.assertEqual(
            [arg for command in format_commands for arg in command if arg in files],
            files,
        )

    def test_tool_version_drift_is_fatal(self):
        with self.assertRaisesRegex(RuntimeError, "versions.*--align-versions"):
            quality_baseline.check(self.baseline, {"ruff": "9.0.0"}, [])

    def test_alignment_records_only_the_pinned_tool_versions(self):
        requirements = (
            "mypy==2.5.0 \\\n    --hash=sha256:00\n"
            "mypy-extensions==1.1.0 \\\n    --hash=sha256:11\n"
            "ruff==0.17.1 \\\n    --hash=sha256:22\n"
        )
        baseline = {**self.baseline, "base": "b" * 40, "coverage_evidence": None}
        aligned = quality_baseline.aligned_baseline(
            json.dumps(baseline, indent=2) + "\n", requirements
        )
        self.assertEqual(
            json.loads(aligned),
            {**baseline, "versions": {"ruff": "0.17.1", "mypy": "2.5.0"}},
        )
        self.assertIsNone(quality_baseline.aligned_baseline(aligned, requirements))

    def test_alignment_requires_both_pins_and_a_supported_baseline(self):
        text = json.dumps(self.baseline)
        with self.assertRaisesRegex(RuntimeError, "pinned mypy"):
            quality_baseline.aligned_baseline(text, "ruff==0.17.1\nmypy>=2\n")
        with self.assertRaisesRegex(RuntimeError, "not supported"):
            quality_baseline.aligned_baseline(
                json.dumps({**self.baseline, "schema": 2}), "ruff==1.0.0\nmypy==1.0.0\n"
            )

    def test_align_versions_cli_rewrites_only_the_repository_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline = Path(temp) / "baseline.json"
            requirements = Path(temp) / "requirements-dev.txt"
            baseline.write_text(json.dumps(self.baseline, indent=2) + "\n")
            requirements.write_text("ruff==0.17.1\nmypy==2.4.0\n")
            output = io.StringIO()
            with (
                patch.object(sys, "argv", ["quality_baseline.py", "--align-versions"]),
                patch.object(quality_baseline, "BASELINE", baseline),
                patch.object(quality_baseline, "REQUIREMENTS", requirements),
                patch("tools.quality_baseline.collect") as collect,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(quality_baseline.main(), 0)
            collect.assert_not_called()
            self.assertEqual(output.getvalue().strip(), "aligned")
            self.assertEqual(
                json.loads(baseline.read_text())["versions"],
                {"ruff": "0.17.1", "mypy": "2.4.0"},
            )

    def test_all_source_boundaries_and_new_files_are_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = [
                ".github/scripts/new.py",
                "backend/new.py",
                "backend/contract.pyi",
                "backend/data/new.py",
                "e2e/new.py",
                "garmin-login.py",
                "server.py",
                "tests/new.py",
                "tools/new.py",
            ]
            for filename in [
                *expected,
                ".venv/generated.py",
                "data/private.py",
                ".graphify-local/inputs/corpus/backend/generated.py",
            ]:
                path = root / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self.assertEqual(quality_baseline.source_files(root), sorted(expected))

    def test_messages_are_hashed_without_source_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = {
                "filename": str(root / "server.py"),
                "location": {"row": 1, "column": 1},
                "code": "F401",
                "message": "synthetic diagnostic",
            }
            result = quality_baseline.diagnostic("ruff", item, root)
            self.assertNotIn("message", result)
            self.assertNotIn("synthetic", str(result))
            self.assertEqual(result["path"], "server.py")
            self.assertEqual(len(result["message_sha256"]), 64)

    def javascript_findings(self, *source, helpers="function work() {}", worker=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public").mkdir()
            (root / "public/sample.js").write_text(
                chr(10).join(source), encoding="utf-8"
            )
            # Another page script: its top-level names are globals of sample.js (#3016).
            (root / "public/helpers.js").write_text(helpers, encoding="utf-8")
            if worker is not None:
                (root / "public/service-worker.js").write_text(worker, encoding="utf-8")
            return quality_baseline.javascript_diagnostics(root)

    @unittest.skipUnless(quality_baseline.eslint_script(), "needs npm ci for ESLint")
    def test_the_other_javascript_sonar_analyzes_is_checked_too(self):
        # #3058: Sonar flagged scripts/ui_walk.mjs, which the ratchet did not lint.
        nested = "(a, b) => (a ? (b ? 1 : 2) : 3)"
        files = {
            "public/page.js": "const page = 1;",
            "scripts/walk.mjs": f"export const pick = {nested}; process.exit(0);",
            "tools/tool.mjs": "export const ok = 1;",
            "e2e/flow.spec.js": f"const {{ test }} = require('t'); test('x', {nested});",
            "tests/unit.test.cjs": f"module.exports = {nested};",
            "playwright.config.cjs": f"module.exports = {{ pick: {nested} }};",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            found = quality_baseline.javascript_diagnostics(root)
        # Sonar's rules apply; Node's globals (process, require, module) are no finding.
        self.assertEqual(
            sorted((item["path"], item["code"]) for item in found or []),
            [
                (name, "sonarjs/no-nested-conditional")
                for name in (
                    "e2e/flow.spec.js",
                    "playwright.config.cjs",
                    "scripts/walk.mjs",
                    "tests/unit.test.cjs",
                )
            ],
        )

    @unittest.skipUnless(quality_baseline.eslint_script(), "needs npm ci for ESLint")
    def test_a_call_to_a_helper_declared_nowhere_is_found(self):
        # #3016: numberText() existed in no public/ file and only Playwright caught it.
        call = "function card(value) { return numberText(value) + format(value); }"
        found = self.javascript_findings(call, helpers="const format = String;")
        self.assertEqual(
            [(item["path"], item["line"], item["code"]) for item in found],
            [("public/sample.js", 1, "no-undef")],
        )
        # Declared by another page script, at its top level, in a top-level block
        # (var) or on globalThis: fine.
        for helpers in (
            "const format = String; function numberText(v) { return v; }",
            "const format = String; if (true) { var numberText = String; }",
            "const format = String; (() => { globalThis.numberText = String; })();",
        ):
            with self.subTest(helpers=helpers):
                self.assertEqual(self.javascript_findings(call, helpers=helpers), [])
        # The service worker has its own globals, and its names are not the page's;
        # an unguarded module.exports is a ReferenceError in the browser.
        found = self.javascript_findings(
            "swOnly(); module.exports = {};",
            worker="self.addEventListener('activate', () => clients.claim()); function swOnly() {}",
        )
        self.assertEqual(
            [(item["path"], item["code"]) for item in found],
            [("public/sample.js", "no-undef")] * 2,
        )
        # The Node tests' guarded export is fine; it never runs in the browser.
        guarded = 'if (typeof module !== "undefined") module.exports = { work };'
        self.assertEqual(self.javascript_findings(guarded), [])

    @staticmethod
    def tangled_function(depth, marker=""):
        body = "if (a) {" * depth + "work();" + "}" * depth
        return [
            f"function tangled(a) {{{marker}",
            f"  for (const x of a) {{ {body} }}",
            "}",
        ]

    @unittest.skipUnless(quality_baseline.eslint_script(), "needs npm ci for ESLint")
    def test_complex_function_and_nested_ternary_in_public_js_are_found(self):
        found = self.javascript_findings(
            *self.tangled_function(6),
            "const pick = (a, b) => (a ? 1 : b ? 2 : 3);",
        )
        self.assertEqual(
            [(item["path"], item["line"], item["code"]) for item in found],
            [
                ("public/sample.js", 1, "sonarjs/cognitive-complexity"),
                ("public/sample.js", 4, "sonarjs/no-nested-conditional"),
            ],
        )
        self.assertEqual(self.check(found)[0], 1)

    @unittest.skipUnless(quality_baseline.eslint_script(), "needs npm ci for ESLint")
    def test_complexity_score_is_not_part_of_the_identity_and_nosonar_suppresses(self):
        short = self.javascript_findings(*self.tangled_function(6))
        long = self.javascript_findings(*self.tangled_function(8))
        self.assertEqual(short[0]["message_sha256"], long[0]["message_sha256"])
        self.assertEqual(
            self.javascript_findings(*self.tangled_function(8, " // NOSONAR")), []
        )

    def test_skipped_eslint_ignores_its_baseline_entries(self):
        self.baseline["diagnostics"].append(
            {**self.existing, "tool": "eslint", "code": "sonarjs/no-nested-conditional"}
        )
        self.assertEqual(self.check([self.existing])[0], 1)
        result = quality_baseline.check(
            self.baseline, self.versions, [self.existing], ("eslint",)
        )
        self.assertEqual(result, 0)
        # Without a named node_modules (the caller may have set one, #3170).
        environment = {
            key: value
            for key, value in os.environ.items()
            if key != "PACECRAFT_NODE_MODULES"
        }
        with (
            patch("tools.quality_baseline.eslint_script", return_value=None),
            patch.dict("os.environ", environment, clear=True),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertIsNone(quality_baseline.javascript_diagnostics(Path.cwd()))
        self.assertIn("skipped JavaScript", output.getvalue())
        # The pre-push hook names the clone's node_modules: a clone installed
        # before the ESLint pin must block the push, not skip the check.
        with (
            patch("tools.quality_baseline.eslint_script", return_value=None),
            patch.dict("os.environ", {"PACECRAFT_NODE_MODULES": "node_modules"}),
            self.assertRaisesRegex(RuntimeError, "npm ci"),
        ):
            quality_baseline.javascript_diagnostics(Path.cwd())

    @patch("tools.quality_baseline.subprocess.run")
    def test_tool_failure_cannot_become_baseline_debt(self, mocked_run):
        mocked_run.return_value.returncode = 2
        with self.assertRaisesRegex(RuntimeError, "exit 2"):
            quality_baseline.run(["mypy"], Path.cwd())

    def test_findings_text_outside_the_windows_code_page_is_returned(self):
        # #2889: "Ё" encodes to D0 81, undefined in cp1252 and ASCII, so a
        # locale decoder left stdout None. The check runs without UTF-8 mode
        # (the Windows default before Python 3.15) and with an ASCII locale.
        text = "Ё − ±"
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHONUTF8")}
        env = {**env, "LC_ALL": "C", "PYTHONIOENCODING": ""}

        def child(code):
            return subprocess.run(
                [sys.executable, "-X", "utf8=0", "-c", code],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                check=False,
            )

        probe = "import locale; print(locale.getencoding())"
        if "utf" in child(probe).stdout.decode().lower():
            self.skipTest("the locale decoder is UTF-8 here (macOS)")
        # A non-Python tool writing UTF-8 bytes, and a Python tool (like mypy)
        # printing text, which fails on a cp1252 or ASCII stdout unless run()
        # makes it write UTF-8.
        for tool in (
            f"import sys; sys.stdout.buffer.write({text.encode()!r}); sys.exit(1)",
            # !a: an ASCII locale cannot carry the text on the command line.
            f"import sys; print({text!a}, end=''); sys.exit(1)",
        ):
            with self.subTest(tool=tool):
                result = child(
                    "import sys; from pathlib import Path;"
                    "from tools import quality_baseline;"
                    f"out = quality_baseline.run([sys.executable, '-c', {tool!r}],"
                    " Path.cwd()); sys.stdout.buffer.write(out.encode())"
                )
                self.assertEqual(
                    result.stdout.decode(), text, result.stderr.decode()[-300:]
                )

    @patch("tools.quality_baseline.subprocess.run")
    def test_failure_without_diagnostics_is_fatal(self, mocked_run):
        mocked_run.return_value.returncode = 1
        mocked_run.return_value.stdout = ""
        with self.assertRaisesRegex(RuntimeError, "without diagnostic"):
            quality_baseline.run(["ruff"], Path.cwd())


def nested_chain(extra=""):
    """A function whose five nested structures score 1 + 2 + 3 + 4 + 5 = 15."""
    return (
        "def chain(a, b):\n"
        "    if a:\n"
        "        for x in b:\n"
        "            if x:\n"
        "                while x:\n"
        "                    if b:\n"
        "                        pass\n" + extra
    )


class CognitiveComplexityTests(unittest.TestCase):
    def findings(self, source, name="sample.py"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / name).write_text(source, encoding="utf-8")
            return quality_baseline.cognitive_diagnostics(root, [name])

    def score(self, source):
        return quality_baseline.cognitive_complexity(ast.parse(source).body[0])

    def test_limit_is_exclusive_at_fifteen(self):
        self.assertEqual(self.score(nested_chain()), 15)
        self.assertEqual(self.findings(nested_chain()), [])
        over = nested_chain("    c = a or b\n")
        self.assertEqual(self.score(over), 16)
        [finding] = self.findings(over)
        self.assertEqual(
            (finding["tool"], finding["code"], finding["path"], finding["line"]),
            ("cognitive", "S3776", "sample.py", 1),
        )

    def test_scores_follow_the_sonar_increments(self):
        source = (
            "def f(a, b, c):\n"
            "    if a and b or c:\n"  # if 1, two operator chains 2
            "        pass\n"
            "    elif b:\n"  # 1
            "        pass\n"
            "    else:\n"  # 1
            "        pass\n"
            "    try:\n"
            "        pass\n"
            "    except ValueError:\n"  # 1
            "        pass\n"
            "    for i in a:\n"  # 1
            "        x = 1 if i else 2\n"  # 1 + nesting 1
            "    g = lambda: 1 if a else 2\n"  # lambda nests the ternary: 2
            "    def inner():\n"
            "        if a:\n"  # 1 + nesting 1
            "            pass\n"
            "    match a:\n"  # 1
            "        case 1:\n"
            "            pass\n"
            "    return a and (b and c)\n"  # one chain: 1
        )
        self.assertEqual(self.score(source), 15)

    def test_else_if_nests_but_elif_does_not(self):
        nested = "def f(a, b):\n  if a:\n    pass\n  else:\n    if b:\n      pass\n"
        flat = "def f(a, b):\n  if a:\n    pass\n  elif b:\n    pass\n"
        self.assertEqual(self.score(nested), 4)
        self.assertEqual(self.score(flat), 2)

    def test_methods_are_named_and_nested_functions_belong_to_the_outer_one(self):
        body = nested_chain("    c = a or b\n").splitlines()
        indented = "".join(f"    {line}\n" for line in body)
        [method] = self.findings("class Box:\n" + indented)
        self.assertEqual(method["line"], 2)
        [outer] = self.findings("def outer():\n" + indented)
        self.assertEqual(outer["line"], 1)

    def test_identity_ignores_the_score_and_names_the_function(self):
        first = self.findings(nested_chain("    c = a or b\n"))
        more = self.findings(nested_chain("    c = a or b\n    d = a or b\n"))
        renamed = self.findings(
            nested_chain("    c = a or b\n").replace("chain", "other")
        )
        self.assertEqual(
            quality_baseline.identity(first[0]), quality_baseline.identity(more[0])
        )
        self.assertNotEqual(
            quality_baseline.identity(first[0]), quality_baseline.identity(renamed[0])
        )

    def test_existing_over_limit_function_is_baseline_debt_and_new_ones_fail(self):
        known = self.findings(nested_chain("    c = a or b\n"))[0]
        versions = {"ruff": "0.16.10", "mypy": "2.4.0"}
        baseline = {"schema": 1, "versions": versions, "diagnostics": [known]}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(quality_baseline.check(baseline, versions, [known]), 0)
            added = {**known, "line": 40, "message_sha256": "b" * 64}
            result = quality_baseline.check(baseline, versions, [known, added])
        self.assertEqual(result, 1)
        self.assertIn("NEW cognitive sample.py:40 S3776", output.getvalue())

    def test_unparsable_source_is_fatal(self):
        with self.assertRaisesRegex(RuntimeError, "Cannot parse"):
            self.findings("def broken(:\n")


class CssDuplicateSelectorTests(unittest.TestCase):
    """Sonar's css:S4666 checked locally, as CI's gate failed on it (#3077)."""

    def findings(self, css):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public").mkdir()
            (root / "public/styles.css").write_text(css, encoding="utf-8")
            return quality_baseline.css_duplicate_diagnostics(root)

    def test_a_selector_repeated_in_the_same_context_is_found_at_its_second_rule(self):
        css = (
            ".composer .attachment-button { color: red; }\n"
            "/* a comment { with braces } */\n"
            ".other { margin: 0; }\n"
            "\n"
            ".composer  >  .x, .composer .attachment-button { padding: 0; }\n"
            ".composer .attachment-button { border: 0; }\n"
        )
        [finding] = self.findings(css)
        self.assertEqual(
            (finding["tool"], finding["code"], finding["path"], finding["line"]),
            ("css", "S4666", "public/styles.css", 6),
        )

    def test_selector_lists_compare_as_sets_with_whitespace_ignored(self):
        self.assertEqual(
            len(self.findings(".a > .b, .c { x: 1; }\n.c,.a>.b { y: 2; }\n")), 1
        )

    def test_other_contexts_and_keyframe_steps_are_not_duplicates(self):
        css = (
            ".a { x: 1; }\n"
            "@media (max-width: 600px) { .a { x: 2; } }\n"
            "@media (min-width: 900px) { .a { x: 3; } }\n"
            '@supports (content: "a" / "") { .a { x: 4; } }\n'
            "@keyframes pulse { 0% { opacity: 0; } 0% { opacity: 1; } }\n"
            ".b .a { x: 5; }\n"
        )
        self.assertEqual(self.findings(css), [])

    def test_separate_blocks_with_the_same_query_are_different_contexts(self):
        css = (
            "@media (max-width: 600px) { .a { x: 1; } }\n"
            ".b { y: 1; }\n"
            "@media (max-width: 600px) { .a { x: 2; } }\n"
        )
        self.assertEqual(self.findings(css), [])

    def test_a_repeat_inside_one_media_query_is_found(self):
        css = "@media print { .a { x: 1; }\n .a { x: 2; } }\n"
        [finding] = self.findings(css)
        self.assertEqual(finding["line"], 2)

    def test_the_repository_stylesheets_carry_no_duplicate(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(quality_baseline.css_duplicate_diagnostics(root), [])
