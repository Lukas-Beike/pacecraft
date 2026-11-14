"""Regression tests for source text encoding."""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = (
    "server.py",
    "backend/**/*.py",
    "public/**/*.js",
    "public/**/*.html",
    "public/**/*.css",
    "e2e/**/*.js",
)
MOJIBAKE_MARKERS = ("Ã", "â€")
LITERAL_UNICODE_ESCAPE = "\\\\u00"


def tracked_source_files() -> list[Path]:
    return sorted(
        path
        for source_path in SOURCE_PATHS
        for path in ROOT.glob(source_path)
        if path.is_file()
    )


class SourceEncodingTests(unittest.TestCase):
    def test_tracked_source_has_no_mojibake_or_literal_unicode_escapes(self):
        failures = []
        for path in tracked_source_files():
            source = path.read_text(encoding="utf-8")
            for marker in MOJIBAKE_MARKERS:
                if marker in source:
                    failures.append(f"{path.relative_to(ROOT)} contains {marker!r}")
            if LITERAL_UNICODE_ESCAPE in source:
                failures.append(
                    f"{path.relative_to(ROOT)} contains a literal \\u00 escape"
                )
        self.assertEqual(failures, [])

    def test_local_scripts_decode_captured_text_as_utf8(self):
        # #2889, #2895: text=True without encoding decodes with the Windows
        # code page; a byte it cannot decode leaves stdout None. The scripts
        # agents run on Windows name the encoding (.github/scripts run on Linux).
        failures = []
        runners = ("tests/run_tests.py", "tests/changed_tests.py")
        for pattern in ("scripts/**/*.py", "tools/**/*.py", *runners):
            for path in sorted(ROOT.glob(pattern)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    keywords = getattr(node, "keywords", [])
                    text = any(
                        k.arg in ("text", "universal_newlines")
                        and getattr(k.value, "value", None) is True
                        for k in keywords
                    )
                    if text and "encoding" not in {k.arg for k in keywords}:
                        failures.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(failures, [])
