"""A mapper's code cannot change without the version that replays it (#1672).

Stored provider records are re-projected only when the version of their mapper
changes. Sub-mappers have no version of their own: they ride on the version of
the mapper that calls them. This guard pins a fingerprint of every mapping
module's code, and of every backend/providers or backend/canonical module a
replayed mapper imports (directly or through another such module, #2501),
together with the versions that replay it. Changing the code without bumping
one of those versions fails here. Widely shared canonical modules such as
vocabulary are pinned too.

The fingerprint ignores comments, docstrings and layout, so documentation edits
pass. A code change updates only the fingerprint when replaying the stored
records gives the same output, for example a refactor, a new enum member, or a
mapping of a section or field that no stored record carries yet (#2880); say so
in the PR. Otherwise bump the version first. Run this module to print the
current table; ``--write`` rewrites ``PINNED`` from it after a bump and refuses
a fingerprint change without one, unless ``--fingerprint-only MODULE`` names a
module whose change, by the clauses here, maps nothing differently (#3743).

Third named clause, the self-versioned additive field (#3273, approved by the
athlete after a Fable review): a code change may also update only the
fingerprint when it adds a field whose presence the stored row states through
its own stored version, readers treat a row of a lower version as "not
evaluated" (never as a negative result), and the next bump of the replaying
mappers carries the backfill of the older rows. The DFA-a1 window series is
the one such field: ``ActivityDfaAlpha1.method`` 3 says the row has it, and
method 3 also stores a series-only row (null estimate and quality) for a
recording without a 0.75 crossing, which a build before it reads as no result.
Name
the clause in the PR; any other output change still needs the bump.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib
import io
import tokenize
import unittest
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ROOT / "backend" / "providers"
CANONICAL = "canonical."

# Mapping module -> the modules whose MAPPER_VERSION replays its output.
REPLAYED_BY: Final[dict[str, tuple[str, ...]]] = {
    "fit_activity_detail_mapping": (
        "fit_activity_detail_mapping",
        "garmin_activity_detail_mapping",
    ),
    "fit_rr_mapping": (
        "fit_activity_detail_mapping",
        "garmin_activity_detail_mapping",
    ),
    "fit_sensor_mapping": (
        "fit_activity_detail_mapping",
        "garmin_activity_detail_mapping",
    ),
    "garmin_activity_detail_mapping": ("garmin_activity_detail_mapping",),
    "garmin_activity_mapping": ("garmin_activity_mapping",),
    "garmin_gear_mapping": ("garmin_health_mapping",),
    "garmin_health_mapping": ("garmin_health_mapping",),
    "garmin_status_mapping": ("garmin_health_mapping",),
    "garmin_zone_mapping": ("garmin_health_mapping",),
    "garmin_training_mapping": ("garmin_health_mapping",),
    "sleep_period_mapping": ("garmin_health_mapping",),
    # garmin_activity_mapping uses wahoo_device_type for relayed Wahoo rides.
    "wahoo_activity_mapping": ("wahoo_activity_mapping", "garmin_activity_mapping"),
    "wahoo_threshold_mapping": ("wahoo_threshold_mapping",),
}
# Mapping modules that project nothing that is stored and replayed.
NOT_REPLAYED: Final[dict[str, str]] = {
    "wahoo_plan_mapping": "outbound: builds Wahoo plan payloads for export",
}
# Module -> (replaying versions, code fingerprint).
PINNED: Final[dict[str, tuple[tuple[int, ...], str]]] = {
    "canonical.activity_detail": (
        (13, 14),
        "b923a24740004724fb28c83dd54dd77ef374eb811bee98c9dbaa76ad8f667167",
    ),
    "canonical.decimal_text": (
        (13, 14, 21, 28, 8, 2),
        "fe45d029fb3c059900543f140a5715de91421a68f6226ddfb1f9d4a093edaaa6",
    ),
    "canonical.dfa_alpha1": (
        (13, 14),
        "ad5681938636614d75c3e2361700ef66b30d4aa9d9ba706342a78dd81b88dc2c",
    ),
    "canonical.domain_records": (
        (28,),
        "c6641d6b54e9d806f0dd1ff01b8974008ceaa0bc24616794bb2417371d05641b",
    ),
    "canonical.local_time": (
        (13, 14, 21, 28, 8, 2),
        "bb8d2cace6846fc1f0f57aa0cc04ee9561fb8603105cb65f337204eece611886",
    ),
    "canonical.mapping": (
        (13, 14, 21, 28, 8, 2),
        "ce31fc467c34d7276223455adccf24557ed633520a1c849051216c23d0549359",
    ),
    "canonical.records": (
        (13, 14, 21, 28, 8, 2),
        "ca06ef248ed891b0643a14c08469268a974fd84a313389e6faa48fbc59a9e26d",
    ),
    "canonical.sleep_bounds": (
        (28,),
        "a18b7a440cdfb7ba0d03909e5d28cc843a99fc962d73383d8f4905084022884c",
    ),
    "canonical.sports": (
        (14, 21),
        "223eb5ca42399100cccd4093e4ec74e042986f3cd34c71022651e794bba0df66",
    ),
    "canonical.validation": (
        (13, 14, 21, 28, 8, 2),
        "da43456050d5763e2f48e0dc28d2d0e5fb131a36a836be5441ba1ac4bc832dfa",
    ),
    "canonical.vocabulary": (
        (13, 14, 21, 28, 8, 2),
        "955f8bae501843c175fe3b4b7e8e6cb44e897c58bcdbdc0aa25e3aba8ed47749",
    ),
    "fit_activity_detail_mapping": (
        (13, 14),
        "70485533a3986d950572db5dce3675d20966ba3d78bc9583f752e6396d15cd09",
    ),
    "fit_file": (
        (13, 14),
        "220282c0c094e17db97c5694fc3efbb86035dd8efe8bb021b1cdb20407e7ca72",
    ),
    "fit_rr_mapping": (
        (13, 14),
        "4319aab924017281cffb92b24b53fa67ed8633d747b55e9f61f57b58821c6981",
    ),
    "fit_sensor_mapping": (
        (13, 14),
        "0bc6dc6a5828641940f851dc9a2c4d6362d1b53d7f0f66d412f1f0ebdcbe71f6",
    ),
    "garmin_activity_detail_mapping": (
        (14,),
        "cb122f30a2ca0b11ba43ecf8a418016859c4506792763844c565df88184a6b1c",
    ),
    "garmin_activity_mapping": (
        (21,),
        "adb059dae3b2103933de835f3cec94d2abf0a2e9d76e2898d394f1b015a27f8b",
    ),
    "garmin_device_records": (
        (28,),
        "68ecba9d45e93b4ec7d4427cca42a212b43e921f264960cdb532f67b192cce66",
    ),
    "garmin_fields": (
        (28,),
        "89d941ff3dac7ee49c8f800bf8bcbfa4da96ad5ed6fd50c4e87cd2d3a0cf664a",
    ),
    "garmin_gear_mapping": (
        (28,),
        "b53d3b960450b21475f4f86a73c6be8ecd3d182afa0b73ed3055b535482d9028",
    ),
    "garmin_health_mapping": (
        (28,),
        "dae446f9120a07d74b36fbabd42155f40f32b27a2362df2c773562387cac1680",
    ),
    "garmin_performance_fields": (
        (28,),
        "6762077b0617cbc1ead19f39a298a29458d6647c2c7d2f391b76b54f55e1eec4",
    ),
    "garmin_status_mapping": (
        (28,),
        "9620141f6c44006d8d37431ca904758d5a0d1e9662db5002698ab907316c0e44",
    ),
    "garmin_training_mapping": (
        (28,),
        "6ca30853f9f41328fa0f75db681d4195b6780414e17cd9d09018aa25e605d186",
    ),
    "garmin_weight_walk": (
        (28,),
        "5a9eeeebf0175118b4759c1784e45df6dc53035556d935a31c3fef89ee4f1bad",
    ),
    "garmin_zone_mapping": (
        (28,),
        "b06dffd17d1c09036a5ef7c1aab7695d56f0d5d8e2e4616238afc4a5f21dfeee",
    ),
    "sleep_period_mapping": (
        (28,),
        "77efab15de0cda5414ed02c0d5005a9fe6fb1a650ec103bdb40e01cef065cf0f",
    ),
    "wahoo_activity_mapping": (
        (8, 21),
        "9b74c3dca726d23d18e782b7dd8f351572a6a24c2230187db13a5cb5ba181312",
    ),
    "wahoo_threshold_mapping": (
        (2,),
        "97792201907fefa34005f62fa805c7190b09177ac8b82f56d8e40e0d12823d89",
    ),
}


def fingerprint(source: str) -> str:
    """SHA-256 of the code tokens, without comments, docstrings and layout."""
    docstrings = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            )
            and ast.get_docstring(node, clean=False) is not None
        ):
            first = node.body[0]
            docstrings.add((first.lineno, first.col_offset))
    skipped = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING}
    parts = [
        # Token names, not numbers: the numbers differ between Python versions.
        f"{tokenize.tok_name[token.type]}:{token.string}"
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in skipped
        and not (token.type == tokenize.STRING and token.start in docstrings)
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def module_path(module: str) -> Path:
    """Source of a provider module, or of a ``canonical.<name>`` module."""
    if module.startswith(CANONICAL):
        return ROOT / "backend" / "canonical" / f"{module.removeprefix(CANONICAL)}.py"
    return PROVIDERS / f"{module}.py"


@functools.cache
def backend_imports(module: str) -> set[str]:
    """The backend.providers and ``canonical.<name>`` modules ``module`` imports."""
    package = ["backend", "canonical" if module.startswith(CANONICAL) else "providers"]
    dotted: list[str] = []
    for node in ast.walk(ast.parse(module_path(module).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            # A relative import resolves from the importing module's package.
            parent = package[: len(package) + 1 - node.level] if node.level else []
            base = ".".join(parent + (node.module or "").split(".")).strip(".")
            dotted += [f"{base}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            dotted += [alias.name for alias in node.names]
    names = {
        (CANONICAL if parts[1] == "canonical" else "") + parts[2]
        for parts in (name.split(".") for name in dotted)
        if len(parts) > 2
        and parts[0] == "backend"
        and parts[1] in ("canonical", "providers")
    }
    return {name for name in names if module_path(name).is_file()}


def helper_replayed_by() -> dict[str, tuple[str, ...]]:
    """Helper module -> the versions replaying every mapper that reaches it,
    also through a sub-mapper it calls."""
    owners: dict[str, set[str]] = {}
    for mapping, replaying in REPLAYED_BY.items():
        pending, seen = [mapping], {mapping}
        while pending:
            for name in backend_imports(pending.pop()) - seen:
                seen.add(name)
                pending.append(name)
        for helper in seen:
            # Registered mappers carry their own replaying versions.
            if helper not in REPLAYED_BY:
                owners.setdefault(helper, set()).update(replaying)
    return {helper: tuple(sorted(found)) for helper, found in owners.items()}


def replayed_by() -> dict[str, tuple[str, ...]]:
    return {**REPLAYED_BY, **helper_replayed_by()}


def current_table() -> dict[str, tuple[tuple[int, ...], str]]:
    table = {}
    for module, owners in sorted(replayed_by().items()):
        versions = tuple(
            int(importlib.import_module(f"backend.providers.{owner}").MAPPER_VERSION)
            for owner in owners
        )
        source = module_path(module).read_text(encoding="utf-8")
        table[module] = (versions, fingerprint(source))
    return table


def write_refusals(
    table: dict[str, tuple[tuple[int, ...], str]],
    pinned: dict[str, tuple[tuple[int, ...], str]],
    fingerprint_only: frozenset[str] = frozenset(),
) -> list[str]:
    """The modules ``--write`` refuses: a changed fingerprint without a bump of
    every replaying version, unless named as a fingerprint-only change. A module
    new to the replayed set is pinned as it is: its owners' own rows carry the
    bump check."""
    refused = []
    for module, (versions, digest) in sorted(table.items()):
        if module not in pinned:
            continue
        pinned_versions, pinned_digest = pinned[module]
        if digest == pinned_digest or module in fingerprint_only:
            continue
        bumped = len(versions) == len(pinned_versions) and all(
            now != before for now, before in zip(versions, pinned_versions, strict=True)
        )
        if not bumped:
            refused.append(module)
    return refused


def pinned_source(source: str, table: dict[str, tuple[tuple[int, ...], str]]) -> str:
    """``source`` with its ``PINNED`` table replaced by ``table``."""
    node = next(
        item
        for item in ast.parse(source).body
        if isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and item.target.id == "PINNED"
    )
    lines = source.splitlines(keepends=True)
    body = "".join(
        f'    "{module}": (\n        {versions!r},\n        "{digest}",\n    ),\n'
        for module, (versions, digest) in sorted(table.items())
    )
    block = f"PINNED: Final[dict[str, tuple[tuple[int, ...], str]]] = {{\n{body}}}\n"
    assert node.end_lineno is not None
    return "".join(lines[: node.lineno - 1]) + block + "".join(lines[node.end_lineno :])


class MapperVersionGuardTests(unittest.TestCase):
    def test_every_mapping_module_is_registered(self) -> None:
        modules = {path.stem for path in PROVIDERS.glob("*_mapping.py")}
        self.assertEqual(modules, set(REPLAYED_BY) | set(NOT_REPLAYED))
        # Every mapper and every provider or canonical module it reaches,
        # also through another one, is pinned (#2484, #2501).
        self.assertEqual(set(PINNED), set(replayed_by()))
        helpers = helper_replayed_by()
        detail = ("fit_activity_detail_mapping", "garmin_activity_detail_mapping")
        for module, owners in (
            ("garmin_weight_walk", ("garmin_health_mapping",)),
            ("canonical.sleep_bounds", ("garmin_health_mapping",)),
            ("canonical.dfa_alpha1", detail),
            ("canonical.activity_detail", detail),
        ):
            with self.subTest(module=module):
                self.assertEqual(helpers[module], owners)

    def test_code_changes_come_with_a_replaying_version_bump(self) -> None:
        owners = replayed_by()
        for module, (versions, digest) in current_table().items():
            with self.subTest(module=module):
                pinned_versions, pinned_digest = PINNED[module]
                if digest != pinned_digest:
                    # Every replaying version, not just one: each projects it.
                    self.assertEqual(
                        len(versions),
                        len(pinned_versions),
                        f"{module} changed together with its replaying mappers; "
                        "bump every one of them, then update PINNED",
                    )
                    unbumped = [
                        owner
                        for owner, now, pinned in zip(
                            owners[module], versions, pinned_versions, strict=True
                        )
                        if now == pinned
                    ]
                    self.assertEqual(
                        unbumped,
                        [],
                        f"{module} changed without bumping the MAPPER_VERSION of "
                        f"{', '.join(unbumped)}; bump each so stored records "
                        "replay, then update PINNED with `python "
                        "tests/test_mapper_version_guard.py --write` (a change "
                        "that maps nothing differently only updates the "
                        "fingerprint: add --fingerprint-only MODULE)",
                    )
                self.assertEqual(
                    (versions, digest),
                    (pinned_versions, pinned_digest),
                    f"update PINNED[{module!r}] to the new version and fingerprint: "
                    "python tests/test_mapper_version_guard.py --write",
                )

    def test_write_refuses_a_fingerprint_change_without_a_bump(self) -> None:
        # #3743: --write keeps the "bump first" rule.
        pinned = {"a": ((1, 2), "old"), "b": ((3,), "same"), "c": ((5,), "old")}
        table = {"a": ((2, 3), "new"), "b": ((3,), "same"), "c": ((5,), "new")}
        self.assertEqual(write_refusals(table, pinned), ["c"])
        self.assertEqual(write_refusals(table, pinned, frozenset({"c"})), [])
        # A bump of only one of two replaying versions is refused too.
        partial = {"a": ((2, 2), "new"), "b": ((3,), "same"), "c": ((5,), "old")}
        self.assertEqual(write_refusals(partial, pinned), ["a"])
        # A module new to the replayed set is pinned as it is.
        added = table | {"new_helper": ((2, 3), "fresh")}
        self.assertEqual(write_refusals(added, pinned), ["c"])

    def test_write_rewrites_only_the_pinned_table(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        # The current table round-trips to this very file when nothing changed.
        self.assertEqual(pinned_source(source, PINNED), source)
        changed = dict(PINNED) | {"garmin_weight_walk": ((99,), "x" * 64)}
        rewritten = pinned_source(source, changed)
        self.assertIn('"garmin_weight_walk": (\n        (99,),', rewritten)
        self.assertEqual(
            rewritten.split("PINNED: Final")[0], source.split("PINNED: Final")[0]
        )

    def test_docstring_and_comment_edits_keep_the_fingerprint(self) -> None:
        code = 'def f(x):\n    """Doc."""\n    return x + 1  # note\n'
        edited = 'def f(x):\n    """Other doc."""\n\n    return x + 1\n'
        changed = 'def f(x):\n    """Doc."""\n    return x + 2\n'
        self.assertEqual(fingerprint(code), fingerprint(edited))
        self.assertNotEqual(fingerprint(code), fingerprint(changed))


def main(argv: list[str]) -> int:
    """Print the current table, or rewrite ``PINNED`` with ``--write`` (#3743)."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--fingerprint-only", action="append", default=[])
    args = parser.parse_args(argv)
    table = current_table()
    if not args.write:
        for name, row in table.items():
            print(f"    {name!r}: {row!r},")
        return 0
    refused = write_refusals(table, PINNED, frozenset(args.fingerprint_only))
    if refused:
        print("refused, bump the replaying MAPPER_VERSION first: " + ", ".join(refused))
        return 1
    path = Path(__file__)
    # LF as stored (.gitattributes), so nothing outside PINNED changes on Windows.
    path.write_text(
        pinned_source(path.read_text(encoding="utf-8"), table),
        encoding="utf-8",
        newline="\n",
    )
    changed = sorted(name for name, row in table.items() if PINNED.get(name) != row)
    print("PINNED updated: " + (", ".join(changed) or "nothing changed"))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
