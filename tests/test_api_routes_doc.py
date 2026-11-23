"""Keep the generated route table in docs/api-routes.md current.

Run ``python tests/test_api_routes_doc.py --write`` to regenerate it. Every route
handler's docstring is a purpose line followed by its ``Request:``, ``Response:``
and ``Errors:`` paragraphs.
"""

import inspect
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The repository root, for running this file as a script with --write.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server_test_support import server

from backend.http_api.route_table import CONTRACT_LABELS, route_reference

DOC = Path(__file__).resolve().parents[1] / "docs" / "api-routes.md"
BEGIN = "<!-- BEGIN GENERATED ROUTES -->\n"
END = "<!-- END GENERATED ROUTES -->\n"
PARITY_DOC = DOC.with_name("ui-coach-parity.md")
PARITY_VALUES = ("both", "UI only", "Coach only", "neither", "UI by rule")
GAPS = {"UI only", "Coach only", "neither"}
COACH_TOOL_SOURCES = ("backend/coach/tool_schemas.py", "backend/coach/tool_specs.py")
PROSE_ROUTE = re.compile(r"`(GET|POST|PUT) (/api/[^`\s]+)`")
# Former aliases the document names as no longer registered.
RETIRED_ALIASES = {"/api/logs/clear", "/api/diagnostics/clear"}


def _parts(document: str) -> tuple[str, str, str]:
    """The text before the generated section, the section and the text after it."""
    before, rest = document.split(BEGIN, 1)
    generated, after = rest.split(END, 1)
    return before, generated, after


def _generated(document: str) -> str:
    return _parts(document)[1]


def _rendered() -> str:
    return route_reference(server.HTTP_API.route_table.routes)


class ApiRoutesDocTests(unittest.TestCase):
    def test_generated_route_table_is_current(self):
        self.assertEqual(
            _generated(DOC.read_text(encoding="utf-8")),
            _rendered(),
            "Regenerate with: python tests/test_api_routes_doc.py --write",
        )

    def test_writer_keeps_lf_so_a_current_document_shows_no_change(self):
        # #3008: Windows wrote CRLF, and git listed an unchanged document as modified.
        with tempfile.TemporaryDirectory() as directory:
            doc = Path(directory) / "api-routes.md"
            doc.write_bytes(f"# Intro\n\n{BEGIN}stale\n{END}\nTail\n".encode())
            write_document(doc)
            written = doc.read_bytes()
            self.assertNotIn(b"\r", written)
            text = written.decode("utf-8")
            self.assertEqual(
                (_parts(text)[0], _parts(text)[2]), ("# Intro\n\n", "\nTail\n")
            )
            self.assertEqual(_generated(text), _rendered())
            # Linux CI writes LF anyway: pin the argument that keeps Windows on LF.
            with mock.patch.object(Path, "write_text", autospec=True) as write:
                write_document(doc)
            self.assertEqual(write.call_args.kwargs["newline"], "\n")

    def test_every_route_handler_states_its_purpose_and_contract(self):
        def contract(route):
            paragraphs = (inspect.getdoc(route.handle) or "").split("\n\n")
            return [p.split(" ", 1)[0] for p in paragraphs[1:4]]

        self.assertEqual(
            [
                f"{route.method} {route.path}"
                for route in server.HTTP_API.route_table.routes
                if not inspect.getdoc(route.handle)
                or contract(route) != list(CONTRACT_LABELS)
            ],
            [],
        )

    def test_routes_named_in_the_prose_are_registered(self):
        document = DOC.read_text(encoding="utf-8")
        registered = {
            (route.method, re.sub(r"\{(\w+)(?::[^}]+)?\}", "{id}", route.path))
            for route in server.HTTP_API.route_table.routes
        }
        named = {
            (method, re.sub(r"\{\w+\}", "{id}", path))
            for method, path in PROSE_ROUTE.findall(document)
        }
        self.assertTrue(named)
        self.assertEqual(sorted(named - registered), [])
        for alias in RETIRED_ALIASES:
            self.assertIn(f"`{alias}`", document)
            self.assertNotIn(alias, {path for _, path in registered})

    def test_ui_coach_parity_matrix_names_every_tool_and_write_route(self):
        # #3949: every Coach tool and athlete write route has its row, a gap its issue.
        for source in COACH_TOOL_SOURCES:  # named so the test selector picks this up
            self.assertTrue((DOC.parents[1] / source).is_file(), source)
        document = PARITY_DOC.read_text(encoding="utf-8")
        tools = {tool["name"] for tool in server.COACH_DIALOGUE_TOOLS}
        routes = {
            (route.method, re.sub(r"\{(\w+)(?::[^}]+)?\}", r"{\1}", route.path)): route
            for route in server.HTTP_API.route_table.routes
        }
        named_routes = set(PROSE_ROUTE.findall(document))
        self.assertEqual(sorted(named_routes - set(routes)), [])
        writes = {
            key for key, route in routes.items() if route.access == "session+csrf"
        }
        self.assertEqual(sorted(writes - named_routes), [])
        rows = [
            [cell.strip() for cell in line.strip("|").split("|")]
            for line in document.splitlines()
            if line.startswith("| ") and not line.startswith(("| Domain", "| ---"))
        ]
        named_tools = set()
        for _, action, ui, coach, _, parity, gap in rows:
            # #4092: what the hand-kept counts line held, without one shared line.
            self.assertIn(parity, PARITY_VALUES, action)
            # A parenthesis names the tool's action or field, not a tool.
            outside = " ".join(part.split(")")[-1] for part in coach.split("("))
            row_tools = set(re.findall(r"`([a-z_]+)`", outside))
            self.assertEqual(sorted(row_tools - tools), [], action)
            named_tools |= row_tools
            sides = (ui != "—", bool(row_tools))
            expected = {(True, True): "both", (True, False): "UI only"}
            expected |= {(False, True): "Coach only", (False, False): "neither"}
            if parity != "UI by rule" or sides != (True, False):
                self.assertEqual(parity, expected[sides], action)
            issues = re.fullmatch(r"#\d+(, #\d+)*", gap)
            self.assertEqual(bool(issues), parity in GAPS, action)
        outside = document.split("## Outside the matrix\n\n")[1].split("\n\n")[0]
        mechanics = set(re.findall(r"`([a-z_]+)`", outside))
        self.assertLessEqual(mechanics, tools)
        self.assertEqual(sorted(tools - named_tools - mechanics), [])
        self.assertNotIn("\nCounts:", document)


def write_document(path: Path = DOC) -> None:
    """Regenerate the routes section; LF on every platform, as git stores it (#3008)."""
    before, _, after = _parts(path.read_text(encoding="utf-8"))
    path.write_text(
        before + BEGIN + _rendered() + END + after, encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_document()
    else:
        unittest.main()
