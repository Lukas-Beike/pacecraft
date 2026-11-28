"""Exercise source selection and shard discovery without remote CI side effects."""

import fnmatch
import functools
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from unittest import mock

from tests import run_tests

SPEC = importlib.util.spec_from_file_location(
    "release_source",
    Path(__file__).resolve().parents[1] / ".github/scripts/release_source.py",
)
release_source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_source)
CHANGES_SPEC = importlib.util.spec_from_file_location(
    "ci_changes",
    Path(__file__).resolve().parents[1] / ".github/scripts/ci_changes.py",
)
assert CHANGES_SPEC is not None and CHANGES_SPEC.loader is not None
ci_changes = importlib.util.module_from_spec(CHANGES_SPEC)
CHANGES_SPEC.loader.exec_module(ci_changes)
REUSE_SPEC = importlib.util.spec_from_file_location(
    "reuse_green_tree",
    Path(__file__).resolve().parents[1] / ".github/scripts/reuse_green_tree.py",
)
assert REUSE_SPEC is not None and REUSE_SPEC.loader is not None
reuse_green_tree = importlib.util.module_from_spec(REUSE_SPEC)
REUSE_SPEC.loader.exec_module(reuse_green_tree)
WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/publish-container.yml"
)
# On Windows the bash on PATH can be WSL's, which cannot run a script by its
# Windows path; use Git for Windows' bash there, as CI's Git Bash does (#3991).
BASH = shutil.which("bash")
if os.name == "nt":
    _GIT = shutil.which("git")
    _GIT_BASH = _GIT and Path(_GIT).parents[1] / "bin" / "bash.exe"
    BASH = str(_GIT_BASH) if _GIT_BASH and _GIT_BASH.is_file() else None


def _job(workflow, name, following):
    return workflow.split(f"  {name}:\n", 1)[1].split(f"  {following}:\n", 1)[0]


class WorkflowSourceTests(unittest.TestCase):
    def test_ai_docs_requires_guidance_before_discovery(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/validate-ai-docs.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("  pull_request:\n", workflow)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("AI_DOCS_REQUIRED: '1'", workflow)
        validator = workflow.index(
            "python -m unittest discover -s tests -p test_ai_docs_contract.py -v"
        )
        for relative in (
            "AGENTS.md",
            "REVIEW.md",
            ".agents/skills/README.md",
            "tests/test_ai_docs_contract.py",
        ):
            self.assertLess(workflow.index(f"test -f {relative}"), validator)
        # #3173: every PR runs the strict OpenSpec validation with the pinned CLI.
        install = workflow.index("run: bash .github/scripts/npm_ci_retry.sh")
        self.assertLess(install, workflow.index("run: python scripts/spec_validate.py"))

    def test_dependabot_covers_all_manifests_on_develop(self):
        root = Path(__file__).resolve().parents[1]
        configuration = (root / ".github/dependabot.yml").read_text(encoding="utf-8")
        updates = re.split(r"  - package-ecosystem: ", configuration)[1:]
        self.assertEqual(
            {update.splitlines()[0] for update in updates},
            {"pip", "npm", "docker", "github-actions"},
        )
        # The only ignores (athlete decisions): the direct Graphify requirements,
        # whose Linux lock drops Windows-only pins (#2554), and modern-web-guidance,
        # updated by hand only (#2633); any other fails.
        allowed = {
            "pip": "    ignore:\n"
            "      - dependency-name: graphifyy\n"
            "      - dependency-name: pywin32\n",
            "npm": "    ignore:\n      - dependency-name: modern-web-guidance\n",
        }
        for update in updates:
            self.assertIn("target-branch: develop", update)
            self.assertIn("commit-message:", update)
            ecosystem = update.splitlines()[0]
            if ecosystem in allowed:
                self.assertEqual(update.count(allowed[ecosystem]), 1)
                update = update.replace(allowed[ecosystem], "")
            self.assertNotIn("ignore:", update)
            self.assertNotIn("dependency-name:", update)

    def test_workflow_actions_are_immutable_and_ci_does_not_persist_credentials(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / ".github/workflows").glob("*.yml"):
            workflow = path.read_text(encoding="utf-8")
            for action in re.findall(r"uses: ([^\s#]+)", workflow):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", str(path))
        container = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            container.count("uses: actions/checkout@"),
            container.count("persist-credentials: false"),
        )

    def test_quality_checks_all_sources_and_merges_every_shard(self):
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(config["tool"]["ruff"]["target-version"], "py314")
        self.assertIn("DTZ", config["tool"]["ruff"]["lint"]["extend-select"])
        self.assertEqual(config["tool"]["mypy"]["python_version"], "3.14")
        self.assertEqual(config["tool"]["mypy"]["files"], ["."])
        self.assertNotIn("exclude", config["tool"]["mypy"])
        self.assertEqual(config["tool"]["mypy"]["platform"], "linux")
        coverage = config["tool"]["coverage"]["run"]
        self.assertEqual(coverage["source"], ["server", "backend"])
        self.assertTrue(coverage["branch"])
        self.assertTrue(coverage["relative_files"])
        self.assertTrue(coverage["parallel"])
        evidence = json.loads(
            (root / "tests/fixtures/quality_baseline.json").read_text(encoding="utf-8")
        )
        diagnostics = evidence["diagnostics"]
        self.assertTrue(diagnostics)
        self.assertLessEqual(
            {item["tool"] for item in diagnostics},
            # ESLint: Sonar's JavaScript rules beyond public/ (#3058).
            {"ruff", "mypy", "cognitive", "eslint", "format", "css"},
        )
        evidence = evidence["coverage_evidence"]
        totals = evidence["totals"]
        measured = (
            Decimal(totals["covered_lines"] + totals["covered_branches"])
            * 100
            / Decimal(totals["num_statements"] + totals["num_branches"])
        )
        threshold = config["tool"]["coverage"]["report"]["fail_under"]
        self.assertGreater(threshold, 0)
        self.assertEqual(
            Decimal(str(threshold)),
            measured.quantize(Decimal("0.01"), rounding=ROUND_DOWN),
        )
        self.assertEqual(config["tool"]["coverage"]["report"]["precision"], 2)
        self.assertEqual(evidence["shards"], 4)

        shards = workflow.split("  test_shards:\n", 1)[1].split(
            "  container_tests:\n", 1
        )[0]
        self.assertIn("coverage run tests/run_tests.py --shard", shards)
        self.assertIn('--total "$PYTHON_TEST_SHARDS"', shards)
        self.assertIn("python-coverage-${{ matrix.shard }}", shards)
        self.assertIn("include-hidden-files: true", shards)
        self.assertIn("if-no-files-found: error", shards)
        quality = workflow.split("  quality:\n", 1)[1].split("  e2e:\n", 1)[0]
        self.assertIn("pattern: python-coverage-*", quality)
        self.assertNotIn("merge-multiple", quality)
        self.assertIn('for shard in $(seq 1 "$PYTHON_TEST_SHARDS")', quality)
        self.assertIn("::error::Test shard ${shard}", quality)
        self.assertIn("coverage combine", quality)
        self.assertIn("coverage report", quality)
        self.assertNotIn("-ne 4", quality)
        self.assertIn("exit 1", quality)
        self.assertNotIn("coverage run", quality)
        self.assertNotIn("--fail-under", quality)
        self.assertNotIn("ruff format", quality)
        self.assertIn("python tools/quality_baseline.py", quality)
        self.assertNotIn("Run lint baseline", quality)
        self.assertIn("/review/pyproject.toml:ro", workflow)
        self.assertIn("/app/tools:ro", workflow)

    def test_dependabot_pip_compile_updates_the_hash_locked_docker_inputs(self):
        root = Path(__file__).resolve().parents[1]
        dependabot = (root / ".github/dependabot.yml").read_text(encoding="utf-8")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("package-ecosystem: pip", dependabot)
        self.assertIn('directory: "/"', dependabot)

        def pinned_requirements(path):
            content = (root / path).read_text(encoding="utf-8")
            return {
                match.group(1).lower().replace("_", "-"): match.group(2)
                for match in re.finditer(
                    r"(?m)^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^\\\s;]+)", content
                )
            }

        def compile_command(lock, lock_content):
            line = next(
                (
                    line
                    for line in lock_content.splitlines()[:8]
                    if line.lstrip("# ").startswith("pip-compile ")
                ),
                None,
            )
            self.assertIsNotNone(line, f"{lock} has no pip-compile header")
            return line.lstrip("# ").split()

        # pip-tools releases order the options differently; the set must hold.
        options = {"--allow-unsafe", "--generate-hashes"}
        for manifest, lock, extra in (
            ("requirements.in", "requirements.txt", set()),
            ("requirements-dev.in", "requirements-dev.txt", set()),
            (
                "requirements-graphify.in",
                "requirements-graphify.txt",
                {"--strip-extras"},
            ),
        ):
            lock_content = (root / lock).read_text(encoding="utf-8")
            command = compile_command(lock, lock_content)
            self.assertEqual(command[0], "pip-compile")
            self.assertEqual(command[-1], manifest)
            self.assertEqual(
                sorted(command[1:-1]),
                sorted(options | extra | {f"--output-file={lock}"}),
            )
            lock_pins = pinned_requirements(lock)
            manifests = pinned_requirements(manifest)
            if manifest == "requirements-dev.in":
                manifests.update(pinned_requirements("requirements.in"))
            for name, version in manifests.items():
                self.assertEqual(
                    lock_pins.get(name), version, f"{name} differs in {lock}"
                )

        self.assertIn("COPY requirements.txt /app/requirements.txt", dockerfile)
        self.assertIn("--require-hashes -r /app/requirements.txt", dockerfile)
        self.assertIn("--require-hashes -r requirements.txt", workflow)
        self.assertIn("--require-hashes -r requirements-dev.txt", workflow)
        self.assertNotIn("requirements.lock", dockerfile + workflow)
        self.assertNotIn("requirements-graphify", dockerfile)
        for path in (root / ".github/workflows").glob("*.yml"):
            self.assertNotIn("requirements-graphify", path.read_text(encoding="utf-8"))
        graphify = (root / "requirements-graphify.txt").read_text(encoding="utf-8")
        # pywin32 has no Linux wheels; keep its transitive pin Windows-only.
        windows_pins = re.findall(r"(?m)^pywin32==.*$", graphify)
        self.assertEqual(len(windows_pins), 1)
        self.assertRegex(
            windows_pins[0], r'^pywin32==[^;\s]+\s*; sys_platform == "win32" '
        )

    def test_daily_release_limits_default_token_permissions(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/daily-release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", workflow)

    def test_executed_source_is_bound_to_the_workflow_event(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        checkout_refs = re.findall(r"^          ref: (.+)$", workflow, re.MULTILINE)
        # The syntax and SBOM jobs were merged into others (#2750); the pinned
        # weekdays have their own job (#4210).
        self.assertEqual(len(checkout_refs), 9)
        self.assertTrue(all(ref == "${{ github.sha }}" for ref in checkout_refs[1:]))
        self.assertIn("'refs/heads/main' || github.sha", checkout_refs[0])
        self.assertNotIn("source_ref", workflow)
        self.assertIn("SOURCE_REF: ${{ github.sha }}", workflow)
        self.assertIn("TESTED_SHA: ${{ needs.source.outputs.source_sha }}", workflow)
        self.assertIn('release_source.py --verify "$TESTED_SHA"', workflow)
        self.assertIn(
            "github.ref == 'refs/heads/main' && inputs.publish_container == true",
            workflow,
        )

    def test_ci_image_is_built_once_and_shared_by_consumers(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(len(re.findall(r"docker build\s", workflow)), 1)
        image = workflow.split("  image:\n", 1)[1].split("  test_shards:\n", 1)[0]
        self.assertIn("docker build --tag pacecraft:ci\n", image)
        self.assertIn("needs: source\n", image)
        # The image carries the release changelog written from the full local history (#2899).
        self.assertIn("fetch-depth: 0\n", image)
        self.assertIn("fetch-tags: true\n", image)
        self.assertIn("filter: blob:none\n", image)  # metadata only
        self.assertLess(
            image.index("python-version: '3.14'"), image.index("release_changelog")
        )
        self.assertLess(
            image.index("run: python3 scripts/release_changelog.py\n"),
            image.index("docker build --tag pacecraft:ci\n"),
        )
        upload = image.split("actions/upload-artifact@", 1)[1].split("- name:", 1)[0]
        self.assertIn("name: pacecraft-ci-image", upload)
        self.assertIn("retention-days: 1", upload)
        consumers = {
            "container_tests": (
                workflow.split("  container_tests:\n", 1)[1].split("  test:\n", 1)[0],
                "needs: [source, image]",
            ),
            "e2e": (
                workflow.split("  e2e:\n", 1)[1].split("  browser:\n", 1)[0],
                "needs: [source, image]",
            ),
        }
        for job, (text, needs) in consumers.items():
            with self.subTest(job=job):
                self.assertIn(needs, text)
                self.assertIn("name: pacecraft-ci-image", text)
                self.assertIn("gunzip -c pacecraft-ci-image.tar.gz | docker load", text)
                self.assertNotIn("docker build", text)
        # The SBOM describes the image the same job built (#2750), after its upload.
        self.assertLess(
            image.index("- name: Upload shared CI image\n"),
            image.index("- name: Generate SPDX SBOM\n"),
        )
        sbom = image.split("- name: Generate SPDX SBOM\n", 1)[1]
        self.assertIn("image: pacecraft:ci", sbom)
        self.assertIn("name: pacecraft-sbom", sbom)
        self.assertIn("- name: Retry SPDX SBOM generation\n", sbom)
        # #4359: Syft comes from a cache keyed by its one version, so a release outage
        # does not fail the SBOM; both SBOM steps use that version and stay blocking.
        self.assertLess(
            image.index("- name: Restore the Syft binary\n"),
            image.index("- name: Generate SPDX SBOM\n"),
        )
        key = "key: syft-${{ runner.os }}-${{ runner.arch }}-${{ env.SYFT_VERSION }}\n"
        for step, use in (
            ("Restore the Syft binary", "actions/cache/restore@"),
            ("Save the Syft binary", "actions/cache/save@"),
        ):
            text = image.split(f"- name: {step}\n", 1)[1].split("      - name:", 1)[0]
            self.assertIn(use, text)
            self.assertIn("path: ${{ runner.tool_cache }}/syft\n", text)
            self.assertIn(key, text)
        # Saved only once the SBOM exists, whatever a later step does.
        self.assertLess(
            image.index("- name: Retry SPDX SBOM generation\n"),
            image.index("- name: Save the Syft binary\n"),
        )
        self.assertLess(
            image.index("- name: Save the Syft binary\n"),
            image.index("- name: Upload image SBOM\n"),
        )
        self.assertEqual(2, sbom.count("syft-version: ${{ env.SYFT_VERSION }}"))
        self.assertRegex(image, r"SYFT_VERSION: v\d+\.\d+\.\d+\n")
        retry = sbom.split("- name: Retry SPDX SBOM generation\n", 1)[1]
        self.assertNotIn(
            "continue-on-error", retry.split("- name: Upload image SBOM")[0]
        )
        self.assertNotIn("  image_sbom:\n", workflow)
        self.assertIn("--workdir /app pacecraft:ci", consumers["container_tests"][0])

    def test_e2e_selects_specs_with_playwright_list_not_file_grep(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        e2e = workflow.split("  e2e:\n", 1)[1].split("  browser:\n", 1)[0]
        self.assertNotIn("grep -q '@responsive'", e2e)
        self.assertIn("python scripts/run_browser_tests.py --project", e2e)
        self.assertIn('--shard "$SPEC_SHARD" --total "$SPEC_SHARDS"', e2e)
        self.assertNotIn("mapfile -t specs", e2e)
        # #3001: a red shard names every failed spec, not only its first one.
        self.assertIn("--keep-going", e2e)

    def test_e2e_runs_beside_unit_tests_and_shards_desktop_completely(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        e2e = workflow.split("  e2e:\n", 1)[1].split("  browser:\n", 1)[0]
        # Browser checks no longer wait for the unit tests; publishing still needs both.
        self.assertNotIn("needs.test", e2e)
        self.assertIn("needs: [source, image, test, e2e, browser, quality]", workflow)
        # Every project runs unless the classifier answered ui=false (#2748).
        choice = re.search(
            r"project: \$\{\{ fromJSON\(needs\.source\.outputs\.ui == 'false' "
            r"&& '(\[[^']*\])' \|\| '(\[[^']*\])'\) \}\}",
            e2e,
        )
        assert choice is not None
        no_ui, every = (json.loads(choice.group(i)) for i in (1, 2))
        self.assertEqual(no_ui, ["desktop"])
        self.assertEqual(set(every), {"mobile", "tablet-landscape", "desktop"})
        shards = re.search(r"\n        shards: \[(\d+)\]\n", e2e)
        assert shards is not None
        count = int(shards.group(1))
        self.assertIn(
            f"\n        shard: [{', '.join(str(i) for i in range(1, count + 1))}]\n",
            e2e,
        )
        aggregate = workflow.split("  browser:\n", 1)[1].split(
            "  build-and-push:\n", 1
        )[0]
        expected = {
            name: int(count)
            for name, count in re.findall(r"--expected ([a-z-]+):(\d+)", aggregate)
        }
        self.assertEqual(expected, dict.fromkeys(every, count))
        self.assertIn(f"expected=(--expected desktop:{count})\n", aggregate)
        self.assertIn(
            'if [[ "$UI" != "false" ]]; then\n            expected+=', aggregate
        )
        self.assertIn("--jobs 1", e2e)
        self.assertIn(
            "browser-test-artifacts-${{ matrix.project }}-${{ matrix.shard }}", e2e
        )
        self.assertIn("format(' {0}/{1}', matrix.shard, matrix.shards)", e2e)
        # #3200, #4210: every clock-pinned spec (found by its helper import) reruns on the
        # week's other six days beside the shards, outside the aggregated evidence;
        # "Browser checks" fails when one does. #4249: one job per day, so none sets the
        # critical path; today's day skips, as the shards run the real day.
        weekdays = workflow.split("  e2e_weekdays:\n", 1)[1].split("  browser:\n", 1)[0]
        self.assertIn("needs: [source, image]\n", weekdays)
        self.assertIn("name: Browser checks (pinned ${{ matrix.weekday }})\n", weekdays)
        offsets = re.findall(r"- \{offset: (\d), weekday: \w{3}\}", weekdays)
        self.assertEqual(offsets, [str(n) for n in range(7)])
        self.assertIn("fail-fast: false\n", weekdays)
        self.assertIn('--project desktop --today "$DAY" --clock-pinned', weekdays)
        self.assertIn("timeout-minutes:", weekdays)
        self.assertNotIn("mapfile -t specs", weekdays)
        self.assertIn('echo "skip=true" >> "$GITHUB_OUTPUT"', weekdays)
        self.assertIn("if: steps.day.outputs.skip != 'true'", weekdays)
        self.assertIn("--results-dir test-results/pinned-day-runs", weekdays)
        self.assertIn("browser-test-artifacts-pinned-${{ matrix.weekday }}", weekdays)
        self.assertNotIn("pinned-day-runs", e2e.split("  e2e_weekdays:\n", 1)[0])
        self.assertIn("needs: [source, e2e, e2e_weekdays]\n", aggregate)
        self.assertIn('if [[ "$WEEKDAYS_RESULT" != "success" ]]; then', aggregate)

    def test_e2e_fixtures_reuse_slot_networks_created_before_the_browser(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        e2e = workflow.split("  e2e:\n", 1)[1].split("  browser:\n", 1)[0]
        runner = (root / "scripts/run_browser_tests.py").read_text(encoding="utf-8")
        fixture = runner.split("    def fixture(", 1)[1].split(
            "    def browser_command(", 1
        )[0]
        self.assertIn('"container:" + network', fixture)
        self.assertNotIn('"--publish"', fixture)
        prepare = runner.split("    def prepare_slots(", 1)[1].split(
            "    def fixture(", 1
        )[0]
        self.assertIn("reserve_ports(count * 2)", prepare)
        execute = runner.split("    def execute(self, children):", 1)[1]
        self.assertLess(
            execute.index("self.prepare_slots(jobs)"),
            execute.index("pool.submit(self.lane"),
        )
        lane = runner.split("    def lane(", 1)[1].split("    def execute(", 1)[0]
        self.assertNotIn("prepare_slots", lane)
        self.assertIn("python scripts/run_browser_tests.py", e2e)

    def test_ci_failure_artifacts_exclude_traces_and_auth_state(self):
        root = Path(__file__).resolve().parents[1]
        config = (root / "playwright.config.cjs").read_text(encoding="utf-8")
        self.assertIn('trace: process.env.CI ? "off" : "retain-on-failure"', config)
        self.assertNotIn('trace: "retain-on-failure"', config)
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        upload = workflow.split("name: Upload browser failure artifacts\n", 1)[1]
        upload = upload.split("      - name:", 1)[0]
        paths = upload.split("path: |\n", 1)[1].split("if-no-files-found:", 1)[0]
        self.assertEqual(
            paths.split(),
            [
                "test-results/browser-runs/*/*/*/artifacts/",
                "test-results/browser-runs/*/*/*/report/",
                "!**/trace.zip",
                "!**/.auth/**",
            ],
        )
        self.assertNotIn("manifest.json", paths)
        # The pinned weekdays' own upload excludes traces and auth state too (#4210).
        pinned = workflow.split("name: Upload pinned-day failure artifacts\n", 1)[1]
        pinned = pinned.split("path: |\n", 1)[1].split("if-no-files-found:", 1)[0]
        self.assertEqual(
            pinned.split(),
            [
                "test-results/pinned-day-runs/*/*/*/artifacts/",
                "test-results/pinned-day-runs/*/*/*/report/",
                "!**/trace.zip",
                "!**/.auth/**",
            ],
        )
        runner = (root / "scripts/run_browser_tests.py").read_text(encoding="utf-8")
        self.assertIn('prefix="ai-coach-browser-private-"', runner)
        self.assertIn('"--trace=off"', runner)
        # The login state lives outside both uploaded directories.
        self.assertIn(
            'path.join(__dirname, "playwright", ".auth", "user.json")', config
        )

    def test_browser_results_are_aggregated_before_publishing(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/publish-container.yml"
        ).read_text(encoding="utf-8")
        browser = workflow.split("  browser:\n", 1)[1].split("  build-and-push:\n", 1)[
            0
        ]
        self.assertIn("needs: [source, e2e, e2e_weekdays]", browser)
        self.assertIn(
            "name: browser-run-summary-${{ matrix.project }}-${{ matrix.shard }}-attempt-${{ github.run_attempt }}",
            workflow,
        )
        self.assertIn("pattern: browser-run-summary-*\n", browser)
        self.assertNotIn("attempt-${{ github.run_attempt }}", browser)
        self.assertIn("python .github/scripts/aggregate_browser_results.py", browser)
        self.assertIn("E2E_RESULT: ${{ needs.e2e.result }}", browser)
        self.assertIn("needs: [source, image, test, e2e, browser, quality]", workflow)

    def test_release_pushes_the_tested_image_instead_of_rebuilding(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/publish-container.yml"
        ).read_text(encoding="utf-8")
        publish = workflow.split("  build-and-push:\n", 1)[1]
        self.assertNotIn("docker/build-push-action", workflow)
        self.assertIn("needs: [source, image, test, e2e, browser, quality]", publish)
        self.assertIn("name: pacecraft-ci-image", publish)
        self.assertIn("docker tag pacecraft:ci", publish)
        self.assertIn("cosign attest --type spdxjson", publish)
        self.assertIn("org.opencontainers.image.source=", workflow)

    def test_jobs_set_up_python_314_before_running_repository_python(self):
        # #2796: ruff targets py314 syntax; the runner's system Python is older.
        # Repository code in the CI image runs from /review or /app on 3.14.
        root = Path(__file__).resolve().parents[1]
        script = re.compile(r"\bpython3? (?:\.github/scripts|scripts|tools|tests)/")
        setup = re.compile(r"\n +python-version: (['\"]?)3\.14\1\n")
        for path in sorted((root / ".github/workflows").glob("*.yml")):
            jobs = path.read_text(encoding="utf-8").split("\njobs:\n", 1)[-1]
            for job in re.split(r"\n(?=  [\w-]+:[ \t]*(?:#.*)?\n)", jobs):
                if not (first := script.search(job)):
                    continue
                with self.subTest(workflow=path.name, job=job.split(":", 1)[0]):
                    found = setup.search(job)
                    self.assertTrue(found and found.start() < first.start(), first[0])

    def test_newer_pr_and_develop_pushes_cancel_only_their_superseded_run(self):
        # #3353: about 20 superseded develop runs were cancelled by hand. A
        # newer develop push now replaces them; the release version commit's
        # run, which the release chain reuses, keeps its own group, as do main
        # pushes, releases and dispatches.
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        head = workflow.split("\njobs:\n", 1)[0]
        develop_push = (
            "github.event_name == 'push' && github.ref == 'refs/heads/develop' && "
            "!startsWith(github.event.head_commit.message, "
            "'chore(release): set application version to ')"
        )
        # Quoted: the prefix holds ": ", which a plain YAML scalar refuses.
        self.assertIn(
            "  group: \"publish-${{ github.event_name == 'pull_request' && "
            "format('pr-{0}', github.event.pull_request.number) || "
            f"{develop_push} && 'develop-push' || github.run_id }}}}\"\n",
            head,
        )
        # Every other run has its own run_id group, so nothing can cancel it.
        self.assertIn("  cancel-in-progress: true\n", head)
        self.assertEqual(head.count("concurrency:"), 1)
        # The excluded prefix is the one the release chain gives its version
        # commit: the repository's squash subject is the PR title, which the
        # changelog and release-dispatch filters rely on as well.
        release = (root / ".github/workflows/daily-release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '--title "chore(release): set application version to $next_tag"',
            release,
        )

    def test_pages_deploy_only_from_main(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/pages.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("    branches: [main]\n", workflow)
        self.assertIn("  workflow_dispatch:\n", workflow)

    def test_the_browser_image_pin_moves_with_the_locked_playwright(self):
        # #3334: a Dependabot @playwright/test bump moved only the package and the
        # lock, and every browser shard stopped in verify_browser (#3332).
        root = Path(__file__).resolve().parents[1]
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        pin = json.loads(
            (root / "scripts/browser_environment.json").read_text(encoding="utf-8")
        )
        locked = {
            name: lock["packages"][f"node_modules/{name}"]["version"]
            for name in ("@playwright/test", "playwright", "playwright-core")
        }
        # An image in another form is compared whole, so it fails with the recipe.
        image = re.fullmatch(
            r"mcr\.microsoft\.com/playwright:v([0-9.]+)-noble@sha256:[a-f0-9]{64}",
            pin["image"],
        )
        declared = {
            "package.json": package["devDependencies"]["@playwright/test"],
            **locked,
            "browser_environment.json version": pin["version"],
            "browser_environment.json image tag": image.group(1)
            if image
            else pin["image"],
        }
        self.assertEqual(
            len(set(declared.values())),
            1,
            f"Playwright versions differ: {declared}. Bump scripts/browser_environment"
            ".json (version, image tag and index digest) with the package: "
            "docs/dependencies.md",
        )

    def test_dependabot_updates_wait_three_days(self):
        config = (
            Path(__file__).resolve().parents[1] / ".github/dependabot.yml"
        ).read_text(encoding="utf-8")
        ecosystems = config.count("package-ecosystem:")
        self.assertGreater(ecosystems, 0)
        self.assertEqual(config.count("cooldown:\n      default-days: 3\n"), ecosystems)

    def test_published_tags_are_verified_against_the_signed_digest(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/publish-container.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("verify published tag digest parity", workflow)
        self.assertIn("docker buildx imagetools inspect", workflow)
        # The tag digest is read without a pipe: an early-exiting reader made
        # inspect fail with SIGPIPE under pipefail (#2600); a mismatch still fails.
        self.assertIn(
            """imagetools inspect "$image:$tag" --format '{{.Manifest.Digest}}')\"""",
            workflow,
        )
        self.assertIn('"$actual_digest" != "$EXPECTED_DIGEST" ]]; then', workflow)
        self.assertIsNone(re.search(r"\| awk [^\n]*'[^'\n]*\bexit\b", workflow))
        self.assertIn("Verify image signature", workflow)
        self.assertIn("cosign verify", workflow)

    def test_every_npm_install_retries_a_transient_failure(self):
        # #3439: a registry or network blip in `npm ci` failed a browser shard of
        # a release PR; every install runs through the bounded retry instead.
        root = Path(__file__).resolve().parents[1]
        installs = 0
        for path in sorted((root / ".github/workflows").glob("*.yml")):
            workflow = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                # Also a line of a multi-line `run: |` block.
                self.assertNotRegex(
                    workflow,
                    re.compile(r"^\s*(run: +)?npm (ci|install)\b", re.MULTILINE),
                )
                installs += workflow.count("run: bash .github/scripts/npm_ci_retry.sh")
        self.assertEqual(installs, 5)  # the pinned weekdays' own job (#4210)
        script = (root / ".github/scripts/npm_ci_retry.sh").read_text(encoding="utf-8")
        self.assertIn("ci --ignore-scripts", script)
        self.assertIn('attempts="${NPM_CI_ATTEMPTS:-3}"', script)
        self.assertIn('delay="${NPM_CI_DELAY_S:-15}"', script)

    @unittest.skipUnless(BASH, "bash is not available")
    def test_npm_retry_passes_on_a_later_attempt_and_fails_after_the_last(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            calls = Path(folder, "calls").as_posix()
            fake = Path(folder, "npm")
            # Fails until its failure budget is used up, counting every call.
            fake.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "$*" >> "{calls}"\n'
                f'[ "$(wc -l < "{calls}")" -gt "$FAKE_FAILURES" ]\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            for failures, code, runs in ((1, 0, 2), (5, 1, 3)):
                with self.subTest(failures=failures):
                    Path(calls).write_text("", encoding="utf-8")
                    done = subprocess.run(
                        [str(BASH), ".github/scripts/npm_ci_retry.sh"],
                        cwd=root,
                        env={**os.environ, "NPM": fake.as_posix(),
                             "NPM_CI_DELAY_S": "0", "FAKE_FAILURES": str(failures)},
                        capture_output=True, text=True, check=False,
                    )  # fmt: skip
                    lines = Path(calls).read_text(encoding="utf-8").splitlines()
                    self.assertEqual((done.returncode, len(lines)), (code, runs))
                    self.assertEqual(set(lines), {"ci --ignore-scripts"})
            self.assertIn("failed after 3 attempts", done.stdout)

    def test_openspec_update_runs_pinned_cli_before_any_write_token(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/openspec-update.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("schedule:", workflow)
        regenerate, publish = workflow.split("\n  publish:\n")
        self.assertIn("run: bash .github/scripts/npm_ci_retry.sh", regenerate)
        self.assertIn("npm run openspec -- update", regenerate)
        self.assertNotIn("secrets.", regenerate)
        self.assertNotIn("npm ", publish)
        self.assertEqual(
            workflow.count("uses: actions/checkout@"),
            workflow.count("persist-credentials: false"),
        )
        self.assertLess(
            publish.index("git apply --numstat"),
            publish.index("create-github-app-token"),
        )
        self.assertIn(
            r"allowed='^(\.agents/skills/(\.openspec-target|openspec-[a-z0-9-]+/SKILL\.md)"
            r"|tests/fixtures/quality_baseline\.json)$'",
            publish,
        )
        # #4107: the regenerated skills' sizes go into the same PR, and the
        # baseline may change in nothing else.
        self.assertIn(
            "python tools/quality_baseline.py --align-openspec-sizes", regenerate
        )
        self.assertLess(
            regenerate.index("npm run openspec -- update"),
            regenerate.index("--align-openspec-sizes"),
        )
        self.assertIn("beyond the generated skills' sizes", publish)
        self.assertLess(
            publish.index("beyond the generated skills' sizes"),
            publish.index("create-github-app-token"),
        )
        check = textwrap.dedent(
            publish.split("<<'PY'\n", 1)[1].split("\n          PY\n")[0]
        )
        skill = ".agents/skills/openspec-archive-change/SKILL.md"
        before = {
            "schema": 1,
            "versions": {"ruff": "1"},
            "instruction_lines": {"AGENTS.md": 9, skill: 221},
        }
        for changed, code in (
            ({skill: 227}, 0),
            ({"AGENTS.md": 10}, 1),
            ({".agents/skills/pr/SKILL.md": 5}, 1),
        ):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder:
                path = Path(folder, "tests/fixtures/quality_baseline.json")
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(before), encoding="utf-8")
                git = ["git", "-C", folder, "-c", "user.name=t", "-c", "user.email=t@t"]
                subprocess.run([*git, "init", "-q"], check=True)
                subprocess.run([*git, "add", "."], check=True)
                subprocess.run([*git, "commit", "-qm", "base"], check=True)
                after = {
                    **before,
                    "instruction_lines": {**before["instruction_lines"], **changed},
                }
                path.write_text(json.dumps(after), encoding="utf-8")
                subprocess.run([*git, "add", "."], check=True)
                done = subprocess.run(
                    [sys.executable, "-c", check],
                    cwd=folder,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(done.returncode, code, done.stderr)
        self.assertIn("core.hooksPath=/dev/null push", publish)
        self.assertIn("--base develop", workflow)
        self.assertIn("--auto --squash", workflow)

    def test_release_pr_dispatch_selects_its_own_branch_without_source_override(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/daily-release.yml"
        ).read_text(encoding="utf-8")
        dispatch = workflow.split("trigger_release_test() {", 1)[1].split(
            "ensure_release_test() {", 1
        )[0]
        self.assertIn('--ref "$source_ref"', dispatch)
        self.assertIn('--field "publish_container=false"', dispatch)
        self.assertNotIn('--field "source_ref=', dispatch)

    def test_main_push_test_can_create_the_release_after_promotion_merge(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/daily-release.yml"
        ).read_text(encoding="utf-8")
        create_release = workflow.split("  create-release:", 1)[1].split(
            "    runs-on:", 1
        )[0]
        self.assertNotIn("workflow_dispatch", create_release)
        self.assertNotIn("chore/release-promotion-", create_release)
        self.assertIn("github.event.workflow_run.event == 'push'", create_release)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", create_release)
        self.assertIn("TESTED_SHA: ${{ github.event.workflow_run.head_sha }}", workflow)
        self.assertIn(
            'tested_tree="$(git show -s --format=\'%T\' "$TESTED_SHA")"', workflow
        )
        self.assertIn(
            'current_main_tree="$(git rev-parse refs/remotes/origin/main^{tree})"',
            workflow,
        )
        self.assertIn('if [[ "$current_main_tree" != "$tested_tree" ]]', workflow)
        self.assertNotIn(
            'git merge-base --is-ancestor "$TESTED_SHA" refs/remotes/origin/main',
            workflow,
        )
        self.assertNotIn("PROMOTION_BRANCH", workflow)
        self.assertNotIn("sleep 10", workflow)
        self.assertIn("queue: max", workflow)
        self.assertIn("branches: [main, 'chore/release-version-*']", workflow)


class CodexReviewWorkflowTests(unittest.TestCase):
    def test_dependabot_automerge_is_limited_to_develop(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/dependabot-automerge.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "github.event.pull_request.user.login == 'dependabot[bot]'", workflow
        )
        self.assertIn("github.event.pull_request.base.ref == 'develop'", workflow)
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            workflow,
        )

    def test_dependabot_automerge_security_and_major_updates(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/dependabot-automerge.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("permissions: {}", workflow)
        # Stays on pull_request_target (#3277): under pull_request a Dependabot run
        # gets no Actions secrets, so the release app token could not be created;
        # the job checks out nothing and runs no pull-request code.
        self.assertIn("pull_request_target:", workflow)
        self.assertIn("--auto --squash", workflow)
        self.assertNotIn("actions/checkout", workflow)
        self.assertNotIn("--admin", workflow)
        self.assertNotIn("update-type", workflow)
        self.assertIn("steps.release-app-token.outputs.token", workflow)
        self.assertNotIn("secrets.GITHUB_TOKEN", workflow)

    def test_dependabot_tool_bumps_align_the_baseline_without_running_pr_code(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/auto-rebase-squash.yml").read_text(
            encoding="utf-8"
        )
        align = workflow.split("- name: Align quality baseline", 1)[1].split(
            "- name: Rebase eligible pull requests", 1
        )[0]
        self.assertIn("branches:\n      - develop", workflow)
        self.assertIn("schedule:\n    - cron: '*/10 * * * *'", workflow)
        self.assertNotIn("workflow_dispatch", workflow)
        self.assertNotIn("pull_request", workflow.split("jobs:", 1)[0])
        # The checkout is the default, the pushed or latest develop commit; PR
        # files are only data. A finished CI run checks out nothing (#3979).
        self.assertIn(
            "- name: Check out trusted develop\n"
            "        if: ${{ github.event_name != 'workflow_run' }}\n"
            "        uses: actions/checkout@",
            workflow,
        )
        self.assertIn(
            "persist-credentials: false\n      - name: Set up Python", workflow
        )
        self.assertEqual(
            3, workflow.count("if: ${{ github.event_name != 'workflow_run' }}")
        )
        self.assertIn("--author app/dependabot", align)
        self.assertIn('startswith("dependabot/")', align)
        self.assertIn("select(.isCrossRepository == false)", align)
        self.assertIn("ref=$head_sha", align)
        self.assertIn("env -u GH_TOKEN python tools/quality_baseline.py", align)
        self.assertIn("--align-versions", align)
        # The commit is rejected when the PR head moved after the reads.
        self.assertIn("createCommitOnBranch", align)
        self.assertIn("expectedHeadOid: $head", align)
        self.assertIn('-f head="$head_sha"', align)
        self.assertIn(
            'headline: "build(deps): align quality baseline tool versions"', align
        )
        self.assertNotIn("--method PUT", align)
        self.assertNotIn("pip install", align)
        self.assertNotIn("git push", workflow)
        rebase = workflow.split("- name: Rebase eligible pull requests", 1)[1]
        self.assertIn(
            "if: ${{ !cancelled() && steps.branch-app-token.outcome == 'success' }}",
            rebase,
        )
        # Push and schedule rebase one armed PR at a time (#3854, #3979); the
        # queue itself is run by the test below.
        self.assertIn("SCHEDULED: ${{ github.event_name != 'push' }}", rebase)
        self.assertIn(
            "workflow_run:\n    workflows: ['Test and publish container image']\n"
            "    types: [completed]",
            workflow,
        )
        self.assertIn("sort_by(.autoMergeRequest.enabledAt)", rebase)
        # Rebase only, never a merge-style update; same-repository squash PRs only.
        self.assertIn(
            'gh pr update-branch "$pull_request_number" \\\n'
            '              --repo "$REPOSITORY" \\\n'
            "              --rebase 2>&1",
            rebase,
        )
        self.assertEqual(1, rebase.count("gh pr update-branch"))
        # A scheduled run warns on a failed rebase; a push run fails the job.
        self.assertIn('if [[ "$SCHEDULED" != "true" ]]; then\n', rebase)
        self.assertIn("select(.isCrossRepository == false)", rebase)
        self.assertIn('select(.autoMergeRequest.mergeMethod == "SQUASH")', rebase)
        self.assertNotIn("gh pr merge", rebase)

    @unittest.skipUnless(BASH, "bash is not available")
    def test_auto_rebase_rebases_one_armed_pr_once_its_checks_are_green(self):
        # #3979: every develop push restarted the browser shards of every armed PR.
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/auto-rebase-squash.yml").read_text(
            encoding="utf-8"
        )
        step = workflow.split("- name: Rebase eligible pull requests", 1)[1]
        body = step.split("        run: |\n", 1)[1]
        # The fake gh prints the listed rows (the jq filter is GitHub's part); a
        # head named b... is one commit behind develop, c... is current, and
        # comparing e... fails.
        fake = (
            "gh() {\n"
            '  case "$1 $2" in\n'
            '    "pr list") printf "%b" "$ROWS" ;;\n'
            '    "pr update-branch") echo "$3" >> "$CALLS"\n'
            '      [[ " $CONFLICTS " != *" $3 "* ]] || { echo RebaseConflictError; return 1; } ;;\n'
            '    *) [[ "$2" != *...e* ]] || return 1\n'
            '      [[ "$2" == *...b* ]] && echo 1 || echo 0 ;;\n'
            "  esac\n"
            "}\n"
        )
        cases = (
            # Arming order; only the first green behind PR is rebased.
            ("1\tx\tb1\tgreen\n2\tx\tb2\tgreen", "", ["1"], 0),
            # A current PR still running its checks merges first.
            ("1\tx\tc1\tpending\n2\tx\tb2\tgreen", "", [], 0),
            # A stuck one does not hold the queue; a behind PR whose checks
            # still run, stalled or failed waits.
            (
                "1\tx\tc1\tstale\n2\tx\tb2\tpending\n3\tx\tb3\tfailing\n4\tx\tb4\tstale\n5\tx\tb5\tgreen",
                "",
                ["5"],
                0,
            ),
            # A conflict passes the turn to the next one.
            ("1\tx\tb1\tgreen\n2\tx\tb2\tgreen", "1", ["1", "2"], 0),
            ("1\tx\tc1\tgreen\n2\tx\tc2\tfailing", "", [], 0),
            # A failed compare skips that PR and fails a push run at the end.
            ("1\tx\te1\tgreen\n2\tx\tb2\tgreen", "", ["2"], 1),
        )
        with tempfile.TemporaryDirectory() as folder:
            script, calls = Path(folder, "step.sh"), Path(folder, "calls")
            script.write_text(fake + textwrap.dedent(body), encoding="utf-8")
            for rows, conflicts, rebased, code in cases:
                with self.subTest(rows=rows, conflicts=conflicts):
                    calls.write_text("", encoding="utf-8")
                    done = subprocess.run(
                        [str(BASH), "--noprofile", "--norc", script.as_posix()],
                        env={**os.environ, "ROWS": rows, "CONFLICTS": conflicts,
                             "CALLS": calls.as_posix(), "REPOSITORY": "o/r",
                             "SCHEDULED": "false"},
                        capture_output=True, text=True, check=False,
                    )  # fmt: skip
                    self.assertEqual(done.returncode, code, done.stdout + done.stderr)
                    self.assertEqual(calls.read_text(encoding="utf-8").split(), rebased)

    def test_native_review_removes_legacy_release_dispatch(self):
        root = Path(__file__).resolve().parents[1]
        release = (root / ".github/workflows/daily-release.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("codex-code-review.yml", release)
        self.assertFalse((root / ".github/workflows/codex-code-review.yml").exists())


class DiscoveryTests(unittest.TestCase):
    def test_direct_cli_discovers_backend_modules_from_any_working_directory(self):
        runner = Path(run_tests.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            output = subprocess.check_output(
                [sys.executable, str(runner), "--list"],
                cwd=directory,
                text=True,
                stderr=subprocess.PIPE,
            )
        ids = output.splitlines()
        self.assertTrue(
            any(test_id.startswith("test_coach_dialogue.") for test_id in ids)
        )
        self.assertTrue(any(test_id.startswith("test_db_manager.") for test_id in ids))
        self.assertEqual(ids, sorted(set(ids)))

    def test_every_discovered_module_runs_once_across_shards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("intent", "db", "new_module"):
                (root / f"test_{name}.py").write_text(
                    "import unittest\nclass Case(unittest.TestCase):\n def test_example(self): pass\n",
                    encoding="utf-8",
                )
            tests = run_tests.discover_tests(root)
            expected = [test.id() for test in tests]
            actual = [
                test.id()
                for shard in range(1, 5)
                for test in run_tests.select_shard(tests, shard, 4)
            ]
            self.assertEqual(len(expected), 3)
            self.assertCountEqual(actual, expected)
            self.assertEqual(len(actual), len(set(actual)))

    def test_discovery_import_failure_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_broken_fixture.py").write_text(
                "raise RuntimeError('synthetic import failure')", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "synthetic import failure"):
                run_tests.discover_tests(root)


@functools.cache
def _discovered_suite():
    """Discover the real suite once per process; imports are cached by name."""
    return run_tests.discover_tests()


def _native_shard_count():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/publish-container.yml").read_text(
        encoding="utf-8"
    )
    match = re.search(r"(?m)^  PYTHON_TEST_SHARDS: '(\d+)'$", workflow)
    if match is None:
        raise AssertionError("PYTHON_TEST_SHARDS is not declared at workflow level")
    return int(match.group(1))


def _write_synthetic_module(root, name, class_sizes):
    lines = ["import unittest", ""]
    for class_name, count in class_sizes.items():
        lines.append(f"class {class_name}(unittest.TestCase):")
        lines.extend(f"    def test_{index:02d}(self): pass" for index in range(count))
        lines.append("")
    (root / f"test_{name}.py").write_text("\n".join(lines), encoding="utf-8")


class ShardingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Module names are unique so they cannot collide with the real suite or
        # with modules imported earlier from another temporary directory.
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        _write_synthetic_module(root, "shardfixture_alpha", {"A1": 3, "A2": 1})
        _write_synthetic_module(root, "shardfixture_beta", {"B1": 5})
        _write_synthetic_module(root, "shardfixture_gamma", {"C1": 2, "C2": 2, "C3": 1})
        _write_synthetic_module(root, "shardfixture_delta", {"D1": 4, "D2": 1, "D3": 1})
        cls.synthetic = run_tests.discover_tests(root)

    def test_every_test_runs_exactly_once_across_shards(self):
        for suite in (self.synthetic, _discovered_suite()):
            expected = sorted(test.id() for test in suite)
            for total in (1, 2, 3, 4, 5):
                with self.subTest(total=total, tests=len(suite)):
                    ids = [
                        test.id()
                        for shard in range(1, total + 1)
                        for test in run_tests.select_shard(suite, shard, total)
                    ]
                    self.assertEqual(sorted(ids), expected)

    def test_no_test_class_is_split_across_shards(self):
        for suite in (self.synthetic, _discovered_suite()):
            for total in (2, 3, 4):
                owners = {}
                for shard in range(1, total + 1):
                    for test in run_tests.select_shard(suite, shard, total):
                        owners.setdefault(run_tests.class_key(test), set()).add(shard)
                with self.subTest(total=total):
                    self.assertEqual(
                        [key for key, shards in owners.items() if len(shards) > 1],
                        [],
                    )

    def test_shards_are_deterministic_and_independent_of_input_order(self):
        suite = self.synthetic
        for shard in range(1, 4):
            first = [test.id() for test in run_tests.select_shard(suite, shard, 3)]
            again = [test.id() for test in run_tests.select_shard(suite, shard, 3)]
            reversed_input = [
                test.id()
                for test in run_tests.select_shard(list(reversed(suite)), shard, 3)
            ]
            with self.subTest(shard=shard):
                self.assertEqual(first, again)
                self.assertEqual(sorted(first), sorted(reversed_input))

    def test_largest_shard_is_within_the_greedy_balance_bound(self):
        # Heaviest-first assignment guarantees max(load) <= mean + heaviest class.
        suite = _discovered_suite()
        weights = run_tests.class_weights(suite, run_tests.load_durations())
        total = _native_shard_count()
        loads = [
            sum(
                weights[key]
                for key in {
                    run_tests.class_key(test)
                    for test in run_tests.select_shard(suite, shard, total)
                }
            )
            for shard in range(1, total + 1)
        ]
        mean = sum(weights.values()) / total
        self.assertLessEqual(max(loads) - mean, max(weights.values()) + 1e-9)
        self.assertLessEqual(max(loads) - min(loads), max(weights.values()) + 1e-9)

    def test_recorded_durations_outweigh_test_counts(self):
        # B1 has the most tests, but A1 is recorded as the slowest class by far.
        suite = self.synthetic
        durations = {
            f"test_shardfixture_{module}.{name}": 1.0
            for module, names in {
                "alpha": ("A2",),
                "beta": ("B1",),
                "gamma": ("C1", "C2", "C3"),
                "delta": ("D1", "D2"),
            }.items()
            for name in names
        }
        durations["test_shardfixture_alpha.A1"] = 100.0
        weights = run_tests.class_weights(suite, durations)
        # Unrecorded D3 (one test) weighs the median recorded seconds per test:
        # median(0.2, 0.25, 0.5, 0.5, 1, 1, 1, 33.3) = 0.75.
        self.assertAlmostEqual(weights["test_shardfixture_delta.D3"], 0.75)
        first = run_tests.select_shard(suite, 1, 2, durations)
        self.assertEqual(
            {run_tests.class_key(test) for test in first},
            {"test_shardfixture_alpha.A1"},
        )
        without = run_tests.class_weights(suite, {})
        self.assertEqual(without["test_shardfixture_beta.B1"], 5.0)

    def test_recorded_durations_file_is_valid(self):
        durations = run_tests.load_durations()
        self.assertTrue(durations, "tests/test_durations.json is missing or empty")
        self.assertTrue(all(seconds >= 0 for seconds in durations.values()))
        self.assertEqual(list(durations), sorted(durations))
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                run_tests.load_durations(Path(directory) / "missing.json"), {}
            )

    def test_native_shard_count_is_declared_once_and_matched_by_the_matrix(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        shards = workflow.split("  test_shards:\n", 1)[1].split(
            "  container_tests:\n", 1
        )[0]
        matrix = re.search(r"shard: \[([0-9, ]+)\]", shards)
        self.assertIsNotNone(matrix)
        self.assertEqual(
            [int(value) for value in matrix.group(1).split(",")],
            list(range(1, _native_shard_count() + 1)),
        )
        self.assertNotIn("--total 4", workflow)
        self.assertIn("AI_DOCS_REQUIRED: '1'", shards)
        # Rulesets require the "Test shard N/<total>" check names.
        self.assertIn(
            f"name: Test shard ${{{{ matrix.shard }}}}/{_native_shard_count()}\n",
            shards,
        )


class ContainerSuiteContractTests(unittest.TestCase):
    def test_every_container_pattern_matches_a_discovered_module(self):
        modules = {run_tests.module_name(test) for test in _discovered_suite()}
        for pattern in run_tests.CONTAINER_SUITE_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertTrue(
                    any(fnmatch.fnmatchcase(module, pattern) for module in modules),
                    f"container pattern {pattern!r} matches no test module",
                )

    def test_container_suite_is_non_empty_and_narrower_than_the_full_suite(self):
        suite = _discovered_suite()
        container = run_tests.select_suite(suite, "container")
        self.assertTrue(container)
        self.assertLess(len(container), len(suite))
        modules = {run_tests.module_name(test) for test in container}
        for required in (
            "test_container_delivery",
            "test_db_manager",
            "test_security_resource_limits",
            "test_session_auth_cache",
        ):
            with self.subTest(module=required):
                self.assertIn(required, modules)

    def test_container_suite_is_selectable_from_the_command_line(self):
        runner = Path(run_tests.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            output = subprocess.check_output(
                [sys.executable, str(runner), "--list", "--suite", "container"],
                cwd=directory,
                text=True,
                stderr=subprocess.PIPE,
            )
        ids = output.splitlines()
        self.assertTrue(ids)
        modules = {test_id.split(".", 1)[0] for test_id in ids}
        self.assertTrue(
            all(
                any(
                    fnmatch.fnmatchcase(module, p)
                    for p in run_tests.CONTAINER_SUITE_PATTERNS
                )
                for module in modules
            )
        )
        self.assertEqual(
            len(ids), len(run_tests.select_suite(_discovered_suite(), "container"))
        )

    def test_container_job_runs_the_container_suite_with_matching_mounts(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/publish-container.yml").read_text(
            encoding="utf-8"
        )
        container = workflow.split("  container_tests:\n", 1)[1].split("  test:\n", 1)[
            0
        ]
        self.assertIn("python /review/tests/run_tests.py --suite container", container)
        local = (root / "tests/run_sqlcipher_tests.ps1").read_text(encoding="utf-8")
        from scripts import run_sqlcipher_tests as supervisor

        # The wrapper delegates to the supervisor, retaining the CI inputs, and
        # runs CI's container suite by default; `-Suite all` runs everything.
        self.assertIn("scripts/run_sqlcipher_tests.py", local)
        self.assertIn("& python @runnerArguments", local)
        self.assertIn('[string]$Suite = "container"', local)
        self.assertIn('"--suite", $Suite', local)
        self.assertEqual(supervisor.parse_args([]).suite, "container")
        source = (root / "scripts/run_sqlcipher_tests.py").read_text(encoding="utf-8")
        self.assertIn('"/app/tests/run_tests.py"', source)
        mount_pattern = r':(/[^":\s]+):ro"'
        ci_targets = set(re.findall(mount_pattern, container))
        local_targets = {f"/review/{relative}" for relative in supervisor.INPUTS}
        local_targets.update(target for _, target in supervisor.EXTRA_MOUNTS)
        self.assertIn("/review/scripts", ci_targets)
        self.assertEqual(ci_targets - local_targets, set())


@unittest.skipUnless(
    shutil.which("git"),
    "Git fixture contracts run in native CI; Git is not an application runtime dependency",
)
class ReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "--initial-branch=develop")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.commit("1.7.2")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("tag", "1.7.2")
        self.git("branch", "main")
        self.git("update-ref", "refs/remotes/origin/main", self.sha)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self, version):
        (self.root / "server.py").write_text(
            f'APP_VERSION = "{version}"\n', encoding="utf-8"
        )
        self.git("add", "server.py")
        self.git("commit", "-m", "test: fixture")

    def test_source_and_tag_must_identify_same_commit(self):
        self.assertEqual(release_source.resolve(self.root, self.sha, "1.7.2"), self.sha)
        self.commit("1.7.3")
        with self.assertRaisesRegex(ValueError, "different commits"):
            release_source.resolve(self.root, self.git("rev-parse", "HEAD"), "1.7.2")

    def test_release_rejects_mutable_branch_source(self):
        with self.assertRaisesRegex(ValueError, "immutable commit SHA"):
            release_source.resolve(self.root, "develop", "1.7.2")

    def test_read_only_release_pr_can_resolve_before_its_tag_exists(self):
        self.commit("1.7.3")
        self.assertEqual(
            release_source.resolve(self.root, "develop"), self.git("rev-parse", "HEAD")
        )

    def test_read_only_release_pr_version_must_match_application(self):
        self.commit("1.7.3")
        script = (
            Path(__file__).resolve().parents[1] / ".github/scripts/release_source.py"
        )
        # #2814: a release PR's own run takes the version from its head branch.
        for extra, head_ref, passes in (
            (["--expected-version", "1.7.3"], "", True),
            (["--expected-version", "1.7.4"], "", False),
            ([], "chore/release-version-1.7.3", True),
            ([], "chore/release-version-1.7.4", False),
            ([], "chore/release-promotion-1.7.4", False),
            ([], "feat/release-version-1.7.4", True),  # not a release branch
        ):
            with self.subTest(extra=extra, head_ref=head_ref):
                result = subprocess.run(
                    [sys.executable, str(script), "--source", "develop", *extra],
                    cwd=self.root,
                    env={**os.environ, "GITHUB_HEAD_REF": head_ref},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, passes, result.stderr)

    def test_release_rejects_commit_outside_main(self):
        self.commit("1.7.3")
        self.git("tag", "1.7.3")
        with self.assertRaisesRegex(ValueError, "protected main history"):
            release_source.resolve(self.root, "1.7.3", "1.7.3")

    def test_moving_branch_cannot_change_resolved_checkout(self):
        source = release_source.resolve(self.root, "1.7.2", "1.7.2")
        self.commit("1.7.3")
        with self.assertRaisesRegex(ValueError, "differs"):
            release_source.verify(self.root, source, "1.7.2")
        self.git("checkout", "--detach", source)
        release_source.verify(self.root, source, "1.7.2")

    def test_resolve_rejects_invalid_source_reference(self):
        for invalid in ["-o", "--output", "develop;rm", "foo..bar", "refs/heads/.."]:
            with self.assertRaisesRegex(ValueError, "valid source reference"):
                release_source.resolve(self.root, invalid)

    def test_verify_rejects_invalid_sha(self):
        with self.assertRaisesRegex(ValueError, "differs"):
            release_source.verify(self.root, "invalid-sha", "1.7.2")

    def test_tag_version_must_match_application(self):
        self.git("tag", "9.0.0")
        with self.assertRaisesRegex(ValueError, "APP_VERSION"):
            release_source.resolve(self.root, "9.0.0", "9.0.0")

    def test_build_verification_requires_fetching_the_release_tag(self):
        clone = self.root / "checkout"
        subprocess.check_output(
            ["git", "clone", "--depth=1", "--no-tags", self.root.as_uri(), str(clone)],
            stderr=subprocess.PIPE,
        )
        with self.assertRaises(subprocess.CalledProcessError):
            release_source.verify(clone, self.sha, "1.7.2")
        release_source.git(clone, "fetch", "--unshallow", "--tags", "origin")
        release_source.git(clone, "fetch", "origin", "main:refs/remotes/origin/main")
        release_source.verify(clone, self.sha, "1.7.2")


class ChangedFileClassifierTests(unittest.TestCase):
    """Only a classified pull request skips suites (#2747) or browser projects (#2748)."""

    def test_only_documentation_paths_are_docs(self):
        docs = (
            "docs/testing.md",
            "docs/notes.txt",
            ".agents/skills/pr/SKILL.md",
            ".agents/skills/pr/references/review.md",
            "README.md",
            "AGENTS.md",
            "backend/AGENTS.md",
            ".github/AGENTS.md",
            "REVIEW.md",
            "openspec/specs/coach/spec.md",
        )
        not_docs = (
            ".agents/skills/pr/scripts/pr.py",
            ".agents/skills/pr/scripts/notes.md",
            ".agents/skills/pr/evals/evals.json",
            "docs/diagram.png",
            "openspec/config.yaml",
            "public/AGENTS.md",
            "public/InterVariable-LICENSE.md",
            "backend/sync/AGENTS.md",
            "tests/fixtures/providers/README.md",
            ".github/workflows/publish-container.yml",
            "tests/test_ci_contracts.py",
            "scripts/release_dispatch.py",
            "server.py",
            "CHANGELOG.md",
            "package.json",
        )
        for path in docs:
            with self.subTest(path=path):
                self.assertTrue(ci_changes.is_doc(path))
                self.assertTrue(ci_changes.docs_only([path]))
        for path in not_docs:
            with self.subTest(path=path):
                self.assertFalse(ci_changes.is_doc(path))
                self.assertFalse(ci_changes.docs_only(["docs/testing.md", path]))
        self.assertFalse(ci_changes.docs_only([]))

    def test_ui_paths_run_every_browser_project(self):
        ui_paths = (
            "public/app.js",
            "public/AGENTS.md",
            "e2e/calendar.spec.cjs",
            "e2e/browser_durations.json",
            "playwright.config.cjs",
            "package.json",
            "package-lock.json",
            "backend/http_api/static_assets.py",
            "backend/http_api/response_transport.py",
            "backend/http_api/responses.py",
            "scripts/run_browser_tests.py",
            "scripts/browser_environment.json",
            ".github/scripts/aggregate_browser_results.py",
            ".github/scripts/ci_changes.py",
            ".github/workflows/publish-container.yml",
        )
        backend_only = (
            "server.py",
            "backend/sync/garmin.py",
            "backend/http_api/handler.py",
            "tests/test_ci_contracts.py",
            "tests/navigation-state.test.cjs",
            "docs/testing.md",
            "Dockerfile",
            "publicity.md",
        )
        for path in ui_paths:
            with self.subTest(path=path):
                self.assertTrue(ci_changes.ui(["server.py", path]))
        for path in backend_only:
            with self.subTest(path=path):
                self.assertFalse(ci_changes.ui([path]))
        # An unknown change runs every project.
        self.assertTrue(ci_changes.ui(None))
        self.assertTrue(ci_changes.ui([]))

    @unittest.skipUnless(shutil.which("git"), "needs Git for a merge-commit fixture")
    def test_pull_request_merge_commit_is_classified_and_every_other_event_is_not(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
            ).strip()

        def commit(path, message):
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(message, encoding="utf-8")
            git("add", path)
            git("commit", "-m", message)

        git("init", "--initial-branch=develop")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        commit("server.py", "base")
        every_suite = ("false", "true")
        for branch, path, expected in (
            ("docs", "docs/testing.md", ("true", "false")),
            ("code", "server.py", ("false", "false")),
            ("ui", "public/app.js", every_suite),
        ):
            with self.subTest(branch=branch):
                git("switch", "-c", branch, "develop")
                commit(path, f"change {branch}")
                git("switch", "--detach", "develop")
                git("merge", "--no-ff", "-m", "merge", branch)
                for event, want in (
                    ("pull_request", expected),
                    ("push", every_suite),
                    ("release", every_suite),
                    ("workflow_dispatch", every_suite),
                ):
                    self.assertEqual(self._classify(root, event), want, event)
                # A single-parent checkout is no pull request merge commit.
                git("switch", "--detach", branch)
                self.assertEqual(self._classify(root, "pull_request"), every_suite)

    def _classify(self, root, event):
        output = root.parent / f"{root.name}-output"
        output.write_text("", encoding="utf-8")
        self.addCleanup(output.unlink, missing_ok=True)
        subprocess.run(
            [sys.executable, Path(ci_changes.__file__).resolve()],
            cwd=root,
            env={**os.environ, "EVENT_NAME": event, "GITHUB_OUTPUT": str(output)},
            check=True,
            capture_output=True,
        )
        values = dict(
            line.split("=", 1) for line in output.read_text(encoding="utf-8").split()
        )
        return values["docs_only"], values["ui"]

    def test_workflow_skips_suites_only_on_classified_or_reused_runs(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        source = _job(workflow, "source", "image")
        self.assertIn("docs_only: ${{ steps.changes.outputs.docs_only }}", source)
        self.assertIn("ui: ${{ steps.changes.outputs.ui }}", source)
        classify = source.split("- name: Classify changed files\n", 1)[1]
        self.assertIn("if: github.event_name == 'pull_request'\n", classify)
        self.assertIn("python .github/scripts/ci_changes.py", classify)
        # Container tests skip only on docs-only runs, never on a reuse (#2749).
        skip = "if: needs.source.outputs.docs_only != 'true'\n"
        self.assertIn(skip, _job(workflow, "container_tests", "test"))
        reused = " && needs.source.outputs.reuse_run == ''\n"
        self.assertIn(
            "if: needs.source.outputs.docs_only != 'true'" + reused,
            _job(workflow, "e2e", "browser"),
        )
        self.assertIn(
            "if: needs.test.result == 'success' && "
            "needs.source.outputs.docs_only != 'true'" + reused,
            _job(workflow, "quality", "e2e"),
        )
        # Required "Test shard N/4" names need the matrix job itself to run.
        shards = _job(workflow, "test_shards", "container_tests")
        self.assertNotRegex(shards.split("steps:", 1)[0], r"\n    if:")
        self.assertIn(
            "MODE: ${{ needs.source.outputs.reuse_run != '' && 'reused' "
            "|| needs.source.outputs.docs_only != 'true' && 'full' "
            "|| matrix.shard == 1 && 'docs' || 'idle' }}",
            shards,
        )
        for mode, command in (
            ("full", "coverage run tests/run_tests.py --shard"),
            ("docs", "python tests/run_tests.py --changed --base HEAD^1 --with-heavy"),
            # The skipped e2e job runs them otherwise; docs-only runs need them.
            ("docs", "node --test tests/*.cjs"),
        ):
            self.assertIn(f"if: env.MODE == '{mode}'\n        run: {command}", shards)
        # #2900: a hung shard dumps its stacks before the job frees the runner.
        container = _job(workflow, "container_tests", "test")
        for job, limit, commands in (
            (shards, 40, ('--total "$PYTHON_TEST_SHARDS" --hang-timeout 600',
                          "--with-heavy --hang-timeout 1800")),
            (container, 20, ("--suite container --hang-timeout 600",)),
        ):  # fmt: skip
            self.assertIn(
                f"\n    timeout-minutes: {limit}\n", job.split("steps:", 1)[0]
            )
            for command in commands:
                self.assertIn(f"{command}\n", job)
        # Aggregators accept a skip only when the run is docs-only.
        test = _job(workflow, "test", "quality")
        self.assertIn(
            'if [[ "$DOCS_ONLY" == "true" ]]; then\n            container_expected=skipped',
            test,
        )
        self.assertIn('"$CONTAINER_TESTS_RESULT" != "$container_expected"', test)
        self.assertIn("docs-only: suites skipped", test)
        browser = _job(workflow, "browser", "build-and-push")
        self.assertIn(
            'if [[ "$DOCS_ONLY" == "true" && "$E2E_RESULT" == "skipped" ]]', browser
        )
        # Image with its SBOM, the syntax check and publishing never skip.
        self.assertIn(
            "- name: Run syntax checks\n"
            "        if: (env.MODE == 'full' || env.MODE == 'docs') && matrix.shard == 1\n",
            shards,
        )
        never_skip = (
            _job(workflow, "image", "test_shards"),
            workflow.split("  build-and-push:\n", 1)[1],
        )
        for job in never_skip:
            self.assertNotIn("docs_only", job.lower())


# Commit -> tree of the fixture runs; the own tree is tree-a.
_REUSE_TREES = {"same": "tree-a", "same-main": "tree-a", "other": "tree-b"}


def _reuse_run(
    run_id,
    sha,
    *,
    event="push",
    branch="develop",
    status="completed",
    conclusion="success",
    repository="owner/repo",
):
    return {
        "id": run_id,
        "head_sha": sha,
        "head_commit": {"tree_id": _REUSE_TREES[sha]},
        "event": event,
        "head_branch": branch,
        "status": status,
        "conclusion": conclusion,
        "head_repository": {"full_name": repository},
    }


def _complete_jobs(**overrides):
    jobs = [
        {
            "name": name,
            "conclusion": "success",
            "steps": [{"name": step, "conclusion": "success"}] if step else [],
        }
        for name, step in reuse_green_tree.required_jobs(4).items()
    ]
    for job in jobs:
        job.update(overrides.get(job["name"], {}))
    return jobs


class ReuseGreenTreeTests(unittest.TestCase):
    """Only a complete green push run of the identical tree is reused (#2749)."""

    def choose(self, runs, jobs=None, own_run_id=100):
        jobs = jobs or {}
        return reuse_green_tree.choose(
            "tree-a",
            own_run_id,
            runs,
            lambda run_id: jobs.get(run_id, _complete_jobs()),
            "owner/repo",
            4,
        )

    def test_a_complete_green_push_run_of_the_same_tree_is_reused(self):
        for branch, sha in (("develop", "same"), ("main", "same-main")):
            with self.subTest(branch=branch):
                run_id, reason, _ = self.choose([_reuse_run(7, sha, branch=branch)])
                self.assertEqual(run_id, 7)
                self.assertIn("run 7", reason)
        # The newest qualifying run wins.
        run_id, _, _ = self.choose([_reuse_run(7, "same"), _reuse_run(9, "same")])
        self.assertEqual(run_id, 9)

    def test_anything_else_runs_the_full_suite(self):
        incomplete = {
            "a skipped unit-test step (a run that reused another)": {
                "Test shard 2/4": {
                    "steps": [{"name": "Run unit tests", "conclusion": "skipped"}]
                }
            },
            "a desktop-only browser matrix": {
                "Browser checks (mobile 1/2)": {"conclusion": "skipped"}
            },
            "skipped quality": {
                "Incremental quality checks": {"conclusion": "skipped"}
            },
            "failed container tests": {
                "Container unit tests": {"conclusion": "failure"}
            },
        }
        for name, overrides in incomplete.items():
            with self.subTest(case=name):
                run_id, _, _ = self.choose(
                    [_reuse_run(7, "same")], {7: _complete_jobs(**overrides)}
                )
                self.assertIsNone(run_id)
        for name, run in {
            "changed tree": _reuse_run(7, "other"),
            "failed run": _reuse_run(7, "same", conclusion="failure"),
            "cancelled run": _reuse_run(7, "same", conclusion="cancelled"),
            "pull request event": _reuse_run(7, "same", event="pull_request"),
            "dispatch event": _reuse_run(7, "same", event="workflow_dispatch"),
            "release event": _reuse_run(7, "same", event="release"),
            "feature branch push": _reuse_run(7, "same", branch="feature/x"),
            "fork repository": _reuse_run(7, "same", repository="fork/repo"),
            "later run": _reuse_run(101, "same"),
            "the run itself": _reuse_run(100, "same"),
        }.items():
            with self.subTest(case=name):
                run_id, _, running = self.choose([run])
                self.assertIsNone(run_id)
                self.assertEqual(running, [])

    def test_an_earlier_running_push_of_the_same_tree_is_awaited(self):
        states = iter(
            [
                [_reuse_run(7, "same", status="in_progress", conclusion=None)],
                [_reuse_run(7, "same")],
            ]
        )
        github = mock.Mock(repository="owner/repo")
        github.runs.side_effect = lambda: next(states)
        github.jobs_of.side_effect = lambda run_id: _complete_jobs()
        sleeps = []
        run_id, _ = reuse_green_tree.decide(
            github, "tree-a", 100, 4, wait=60, sleep=sleeps.append, clock=lambda: 0
        )
        self.assertEqual((run_id, sleeps), (7, [reuse_green_tree.POLL_SECONDS]))
        # A run that never finishes is waited for only until the limit.
        github.runs.side_effect = lambda: [
            _reuse_run(7, "same", status="queued", conclusion=None)
        ]
        # The deadline counts lookup time too, not only the sleeps.
        times = iter([0, 30, 61])
        run_id, reason = reuse_green_tree.decide(
            github,
            "tree-a",
            100,
            4,
            wait=60,
            sleep=lambda _: None,
            clock=lambda: next(times),
        )
        self.assertIsNone(run_id)
        self.assertIn("still running", reason)

    def test_a_failed_lookup_runs_the_full_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            result = subprocess.run(
                [sys.executable, Path(reuse_green_tree.__file__).resolve()],
                env={
                    **os.environ,
                    "REPOSITORY": "owner/repo",
                    "SOURCE_SHA": "0" * 40,
                    "RUN_ID": "100",
                    "PYTHON_TEST_SHARDS": "4",
                    "GITHUB_OUTPUT": str(output),
                },
                cwd=directory,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("Full suite: lookup failed", result.stdout)
            self.assertEqual(output.read_text(encoding="utf-8"), "reuse_run=\n")

    def test_required_jobs_match_the_workflow(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        e2e = _job(workflow, "e2e", "browser")
        self.assertIn(
            json.dumps(list(reuse_green_tree.BROWSER_PROJECTS)),
            e2e,
        )
        self.assertIn(f"shards: [{reuse_green_tree.BROWSER_SHARDS}]\n", e2e)
        for name in reuse_green_tree.required_jobs(_native_shard_count()):
            if name == "test" or name.startswith(("Test shard", "Browser checks (")):
                continue
            self.assertIn(f"name: {name}\n", workflow)
        self.assertIn("  test:\n", workflow)

    def test_workflow_reuses_only_through_the_read_only_lookup(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        source = _job(workflow, "source", "image")
        self.assertIn("reuse_run: ${{ steps.reuse.outputs.reuse_run }}", source)
        self.assertIn(
            "permissions:\n      contents: read\n"
            "      # Read-only: list earlier runs of this workflow to reuse (#2749).\n"
            "      actions: read\n",
            source,
        )
        self.assertNotIn("write", source)
        lookup = source.split(
            "- name: Find an earlier complete green run of this tree\n", 1
        )[1]
        self.assertIn("GH_TOKEN: ${{ github.token }}", lookup)
        self.assertIn("run: python .github/scripts/reuse_green_tree.py", lookup)
        # The aggregates accept the skipped matrix only with a reused run id.
        browser = _job(workflow, "browser", "build-and-push")
        self.assertIn('if [[ -n "$REUSE_RUN" && "$E2E_RESULT" == "skipped" ]]', browser)
        # Publishing still needs source, image, container-tested aggregates.
        publish = workflow.split("  build-and-push:\n", 1)[1]
        for condition in (
            "!cancelled() &&",
            "needs.source.result == 'success' && needs.image.result == 'success' &&",
            "needs.test.result == 'success' && needs.browser.result == 'success' &&",
            "(needs.e2e.result == 'success' || (needs.source.outputs.reuse_run != '' && needs.e2e.result == 'skipped')) &&",
            "(needs.quality.result == 'success' || (needs.source.outputs.reuse_run != '' && needs.quality.result == 'skipped'))",
        ):
            self.assertIn(condition, publish)
        # Image, SBOM and container tests never consult the reuse.
        for job in (
            _job(workflow, "image", "test_shards"),
            _job(workflow, "container_tests", "test"),
        ):
            self.assertNotIn("reuse_run", job)


class ReleaseWorkflowTests(unittest.TestCase):
    def test_daily_release_uses_release_tree_to_count_new_commits(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "daily-release.yml"
        ).read_text(encoding="utf-8")
        release_counting = workflow.split('if [[ -n "$latest_tag" ]]', 1)[1].split(
            'echo "Commits since latest release:', 1
        )[0]

        self.assertIn(
            'release_tree="$(git show -s --format=\'%T\' "$latest_tag")"',
            release_counting,
        )
        self.assertIn("git log HEAD --format='%H %T'", release_counting)
        self.assertNotIn('git merge-base "$latest_tag" HEAD', release_counting)
        self.assertIn("count_releaseable_commits()", workflow)
        self.assertIn(
            r"!/^chore\(release\): set application version to [0-9]+\.[0-9]+\.[0-9]+( \(#[0-9]+\))?$/",
            workflow,
        )
        self.assertEqual(
            workflow.count('commit_count="$(count_releaseable_commits '), 2
        )
