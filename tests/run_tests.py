"""Run the standard-library test suite, optionally split into stable shards.

Shards keep every TestCase class whole, so class-level fixtures such as
ServerTestCase.setUpClass run once per class. Classes are assigned heaviest-first
to the currently least-loaded shard. A class weighs its recorded wall time from
DURATIONS_FILE; a class without a recording weighs its test count times the
median recorded seconds per test, so new or renamed classes still balance.
Refresh the recording with ``--record-durations tests/test_durations.json`` on a
full run, for example inside the image used by run_sqlcipher_tests.ps1.

``--suite container`` runs only the container-sensitive modules named in
CONTAINER_SUITE_PATTERNS. The default ``--suite all`` runs everything.
``--changed`` keeps only the modules changed_tests.py finds affected by the
diff against ``--base``; it falls back to the whole suite where that selector
says so; CI uses it, with ``--with-heavy``, only on a documentation-only pull
request. For a changed ``public/*.js`` file it also prints the ``node --test``
command of the harness tests that load it (#3496). ``--module`` (repeatable) runs only the named
test modules, for focused runs in the SQLCipher container.
"""

from __future__ import annotations

import argparse
import faulthandler
import fnmatch
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import changed_tests

DURATIONS_FILE = Path(__file__).resolve().parent / "test_durations.json"

# Module-name patterns for the container-sensitive subset: image dependencies,
# SQLCipher, the read-only non-root runtime, configuration, startup, security
# limits, privacy/redaction, and authentication sessions. Every pattern must
# match at least one test module; test_ci_contracts.py enforces this.
CONTAINER_SUITE_PATTERNS = (
    "test_container_delivery",
    "test_db_*",
    "test_database_*",
    "test_server_database*",
    "test_server_runtime*",
    "test_runtime_*",
    "test_security_resource_limits",
    "test_config*",
    "test_diagnostic_logs*",
    "test_privacy_*",
    "test_http_privacy_*",
    "test_garmin_login*",
    "test_session_auth_*",
    "test_http_auth_post*",
    "test_startup_sync_scheduler*",
)
SUITES = ("all", "container")


def write_result(path: Path, value: dict) -> None:
    """Replace evidence atomically; a killed runner leaves its last known state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def iter_tests(suite: unittest.TestSuite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from iter_tests(test)
        else:
            yield test


def discover_tests(directory: Path | None = None):
    loader = unittest.TestLoader()
    loaded = loader.discover(
        str(directory or Path(__file__).resolve().parent), pattern="test_*.py"
    )
    if loader.errors:
        raise RuntimeError(
            "Test discovery failed:\n" + "\n".join(map(str, loader.errors))
        )
    return sorted(iter_tests(loaded), key=lambda test: test.id())


def load_modules(names) -> list:
    """Load only the named test modules, failing like discovery on import errors."""
    loader = unittest.TestLoader()
    loaded = unittest.TestSuite(
        loader.loadTestsFromName(name) for name in sorted(names)
    )
    if loader.errors:
        raise RuntimeError(
            "Test loading failed:\n" + "\n".join(map(str, loader.errors))
        )
    return sorted(iter_tests(loaded), key=lambda test: test.id())


def module_name(test) -> str:
    return type(test).__module__


def class_key(test) -> str:
    cls = type(test)
    return f"{cls.__module__}.{cls.__qualname__}"


def load_durations(path: Path = DURATIONS_FILE) -> dict[str, float]:
    """Return recorded seconds per TestCase class, or nothing without a recording."""
    if not path.exists():
        return {}
    return {
        str(key): float(seconds)
        for key, seconds in json.loads(path.read_text(encoding="utf-8")).items()
    }


def class_weights(tests, durations: dict[str, float]) -> dict[str, float]:
    """Weigh each class by recorded seconds, else by its estimated test time."""
    sizes: dict[str, int] = {}
    for test in tests:
        key = class_key(test)
        sizes[key] = sizes.get(key, 0) + 1
    recorded = [
        durations[key] / size for key, size in sizes.items() if key in durations
    ]
    per_test = statistics.median(recorded) if recorded else 1.0
    return {key: durations.get(key, size * per_test) for key, size in sizes.items()}


def assign_classes(
    tests, total: int, durations: dict[str, float] | None = None
) -> dict[str, int]:
    """Map each TestCase class to a 0-based shard index, deterministically."""
    weights = class_weights(tests, load_durations() if durations is None else durations)
    loads = [0.0] * total
    assignment: dict[str, int] = {}
    for key, weight in sorted(weights.items(), key=lambda item: (-item[1], item[0])):
        shard_index = min(range(total), key=lambda index: (loads[index], index))
        assignment[key] = shard_index
        loads[shard_index] += weight
    return assignment


def select_shard(
    tests, shard: int, total: int, durations: dict[str, float] | None = None
):
    tests = list(tests)
    assignment = assign_classes(tests, total, durations)
    return [test for test in tests if assignment[class_key(test)] == shard - 1]


# The schema evidence modules (#3542): their tests and subtests are listed after
# the run, and one skipped for SQLCipher fails where SQLCipher is installed.
EVIDENCE_MODULES = frozenset(
    {
        "test_db_migrations",
        "test_db_encrypted_upgrade_matrix",
        "test_database_restore_validation",
    }
)


def sqlcipher_installed() -> bool:
    return importlib.util.find_spec("sqlcipher3") is not None


class ClassTimingResult(unittest.TextTestResult):
    """Attribute the wall time since the previous finished test to each test's
    class, so setUpClass counts towards the class that needs it; also note the
    outcome of each schema evidence test and subtest (#3542)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_seconds: dict[str, float] = {}
        self._mark = time.perf_counter()
        self.executed_ids: list[str] = []
        # (test id, subtest parameters or "", outcome), in run order.
        self.evidence: list[tuple[str, str, str]] = []

    def _note(self, test, outcome: str, subtest=None) -> None:
        if type(test).__module__.rsplit(".", 1)[-1] not in EVIDENCE_MODULES:
            return
        params = getattr(subtest, "params", None) or {}
        label = ", ".join(f"{key}={value}" for key, value in params.items())
        if subtest is not None and not label:
            label = str(getattr(subtest, "_message", "") or "subtest")
        self.evidence.append((test.id(), label, outcome))

    def addSuccess(self, test):
        super().addSuccess(test)
        self._note(test, "passed")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._note(test, f"skipped: {reason}")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._note(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self._note(test, "error")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        self._note(test, "passed" if err is None else "failed", subtest)

    def startTest(self, test):
        self.executed_ids.append(test.id())
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        now = time.perf_counter()
        key = class_key(test)
        self.class_seconds[key] = self.class_seconds.get(key, 0.0) + now - self._mark
        self._mark = now


def _evidence_rows(entries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Each test's own result, then its subtests, grouped by test in run order;
    a test without a result of its own (a failed subtest) reads failed."""
    order: dict[str, list[tuple[str, str]]] = {}
    outcomes: dict[str, str] = {}
    for test_id, label, outcome in entries:
        subtests = order.setdefault(test_id, [])
        if label:
            subtests.append((label, outcome))
        else:
            outcomes[test_id] = outcome
    rows = []
    for test_id, subtests in order.items():
        rows.append((test_id, "", outcomes.get(test_id, "failed")))
        rows.extend((test_id, label, outcome) for label, outcome in subtests)
    return rows


def report_evidence(entries: list[tuple[str, str, str]]) -> bool:
    """Print the schema evidence tests with their subtests, also into the job
    summary on CI, and return False when one skipped for SQLCipher although
    SQLCipher is installed (#3542)."""
    if not entries:
        return True
    rows = _evidence_rows(entries)
    print(f"Schema evidence ({len(entries)} results):")
    for test_id, label, outcome in rows:
        print(f"    {outcome} {label}" if label else f"  {outcome} {test_id}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        table = [
            f"| {test_id} | {label or '—'} | {outcome} |"
            for test_id, label, outcome in rows
        ]
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                "### Schema evidence\n\n| Test | Subtest | Result |\n| --- | --- | --- |\n"
                + "\n".join(table)
                + "\n\n"
            )
    skipped = [
        test_id
        for test_id, label, outcome in rows
        if not label
        and outcome.startswith("skipped")
        and "sqlcipher" in outcome.lower()
    ]
    if skipped and sqlcipher_installed():
        for test_id in skipped:
            print(
                f"::error::encrypted evidence test skipped although SQLCipher is installed: {test_id}"
            )
        return False
    return True


def select_suite(tests, suite: str):
    tests = list(tests)
    if suite == "all":
        return tests
    if suite == "container":
        return [
            test
            for test in tests
            if any(
                fnmatch.fnmatchcase(module_name(test), pattern)
                for pattern in CONTAINER_SUITE_PATTERNS
            )
        ]
    raise ValueError(f"unknown suite: {suite}")


def arm_hang_dump(seconds: float) -> bool:
    """After `seconds`, print every thread's stack (naming the hanging test's
    frames) and exit 1, instead of a shard holding its runner for hours (#2900).

    The result file then still says "running". Returns whether it armed, so a
    nested in-process main() never cancels the shard's own timer.
    """
    if seconds > 0:
        faulthandler.dump_traceback_later(seconds, exit=True)
    return seconds > 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, default=1, help="1-based shard number")
    parser.add_argument("--total", type=int, default=1, help="number of total shards")
    parser.add_argument(
        "--result-json", type=Path, help="atomic selection/result evidence"
    )
    parser.add_argument(
        "--suite",
        choices=SUITES,
        default="all",
        help="'container' runs only the container-sensitive test modules",
    )
    parser.add_argument(
        "--changed",
        action="store_true",
        help="run only modules affected by the diff against --base (local use)",
    )
    parser.add_argument(
        "--base",
        default="origin/develop",
        help="ref whose merge base --changed compares against",
    )
    parser.add_argument(
        "--with-heavy",
        action="store_true",
        help="keep the slow tooling modules --changed selects (CI docs-only runs)",
    )
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        metavar="NAME",
        help="run only this test module, such as test_db_manager (repeatable)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list selected test IDs without executing them",
    )
    parser.add_argument(
        "--hang-timeout",
        type=float,
        default=0,
        metavar="SECONDS",
        help="after this long, dump every thread's stack and exit 1 (CI shards)",
    )
    parser.add_argument(
        "--record-durations",
        type=Path,
        metavar="PATH",
        help="write per-class wall time as JSON; requires a full unsharded run",
    )
    args = parser.parse_args()
    if args.total < 1 or not 1 <= args.shard <= args.total:
        parser.error(
            "--shard must be between 1 and --total, and --total must be positive"
        )
    if args.record_durations and (
        args.total != 1
        or args.suite != "all"
        or args.list
        or args.changed
        or args.module
    ):
        parser.error("--record-durations needs the full suite in a single shard")
    if args.module and args.changed:
        parser.error("--module and --changed select modules in different ways")
    if not 0 <= args.hang_timeout < 86400:  # also refuses nan
        parser.error("--hang-timeout must be 0 (off) or seconds below one day")
    return args


def main(args: argparse.Namespace | None = None) -> int:
    if args is None:
        args = parse_args()
        # Armed before discovery, so a hang while importing a module dumps too.
        armed = arm_hang_dump(args.hang_timeout)
        try:
            return main(args)
        finally:
            if armed:
                faulthandler.cancel_dump_traceback_later()
    evidence = {
        "version": 1,
        "status": "discovering",
        "suite": args.suite,
        "python_version": platform.python_version(),
        "shard": args.shard,
        "total": args.total,
    }

    def save():
        if args.result_json:
            write_result(args.result_json, evidence)

    save()
    modules: dict[str, str] | None = (
        dict.fromkeys(args.module, "named") if args.module else None
    )
    if modules:
        evidence["modules"] = sorted(modules)
    skipped: dict[str, str] = {}
    if args.changed:
        try:
            paths = changed_tests.changed_paths(args.base)
        except (OSError, subprocess.CalledProcessError) as error:
            evidence["status"] = "selection_failed"
            save()
            raise SystemExit(f"--changed cannot diff against {args.base}: {error}")
        modules, reason = changed_tests.select_modules(paths)
        evidence.update(changed_paths=paths, changed_selection=reason)
        print(f"Changed-test selection against {args.base}: {reason}", flush=True)
        skipped = {} if args.with_heavy else changed_tests.heavy_skips(paths)
        for module in skipped:
            (modules or {}).pop(module, None)
        for module, why in sorted((modules or {}).items()):
            print(f"  {module}: {why}", flush=True)
        for module, why in sorted(skipped.items()):
            print(f"  skipped {module}: {why}", flush=True)
        if node := changed_tests.node_tests(paths):
            print(f"Also run the node tests: node --test {' '.join(node)}", flush=True)
        if modules == {}:
            evidence["status"] = "empty"
            save()
            print("No Python tests are affected.")
            return 0
    try:
        discovered = discover_tests() if modules is None else load_modules(modules)
    except Exception:
        evidence["status"] = "discovery_failed"
        save()
        raise
    tests = [
        test
        for test in select_suite(discovered, args.suite)
        if module_name(test) not in skipped
    ]
    if args.changed and not tests:
        evidence.update(
            status="empty", discovered_ids=[test.id() for test in discovered]
        )
        save()
        print(f"No affected tests in suite {args.suite}.")
        return 0
    selected = select_shard(tests, args.shard, args.total)
    evidence.update(
        discovered_ids=[test.id() for test in discovered],
        suite_ids=[test.id() for test in tests],
        selected_ids=[test.id() for test in selected],
        selected_classes=sorted({class_key(test) for test in selected}),
        status="selected" if selected else "empty",
    )
    save()
    if not selected:
        raise SystemExit(f"shard {args.shard}/{args.total} contains no tests")
    if args.list:
        print("\n".join(test.id() for test in selected))
        return 0

    started = time.perf_counter()
    evidence["status"] = "running"
    save()
    runner = unittest.TextTestRunner(verbosity=1, resultclass=ClassTimingResult)
    try:
        result = runner.run(unittest.TestSuite(selected))
    except BaseException:
        evidence["status"] = "interrupted"
        save()
        raise
    assert isinstance(result, ClassTimingResult)
    elapsed = time.perf_counter() - started
    # Environment skips (#2553) stay visible by reason and by test (#3163).
    skipped_ids: dict[str, list[str]] = {}
    for test, reason in result.skipped:
        skipped_ids.setdefault(reason, []).append(test.id())
    # The schema evidence listing comes first, so the saved status includes its guard.
    passed = report_evidence(result.evidence) and result.wasSuccessful()
    evidence.update(
        status="passed" if passed else "failed",
        tests_run=result.testsRun,
        skips=len(result.skipped),
        skip_reasons={reason: len(ids) for reason, ids in sorted(skipped_ids.items())},
        failures=len(result.failures),
        errors=len(result.errors),
        error_ids=[test.id() for test, _ in result.errors],
        failure_ids=[test.id() for test, _ in result.failures],
        executed_ids=result.executed_ids,
        elapsed_seconds=elapsed,
    )
    save()
    print(
        f"Discovery: {len(tests)} tests; shard {args.shard}/{args.total}: "
        f"{result.testsRun} run, {len(result.skipped)} skipped in {elapsed:.2f}s"
    )
    for reason, ids in sorted(skipped_ids.items()):
        print(f"  skip reason ({len(ids)}): {reason}")
        for test_id in sorted(ids):
            print(f"    {test_id}")
    # Last, so a reader of the output's tail still sees which tests failed.
    for kind, entries in (("FAILED", result.failures), ("ERROR", result.errors)):
        for test, _trace in entries:
            print(f"{kind}: {test.id()}")
    if args.record_durations:
        assert isinstance(result, ClassTimingResult)
        recorded = {
            key: round(seconds, 3)
            for key, seconds in sorted(result.class_seconds.items())
        }
        if result.wasSuccessful():
            write_result(args.record_durations, recorded)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
