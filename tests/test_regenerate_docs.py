"""scripts/regenerate_docs.py with fake generators in a temporary tree."""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import regenerate_docs


def writer(text):
    """A generator that rewrites docs/x.md, and fails without node_modules."""
    code = (
        "import pathlib,sys;"
        "sys.exit(3) if not pathlib.Path('node_modules/marker').is_file() else None;"
        f"pathlib.Path('docs/x.md').write_text({text!r})"
    )
    return [sys.executable, "-c", code]


class RegenerateDocsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tree, self.modules = Path(temp.name) / "tree", Path(temp.name) / "nm"
        (self.tree / "docs").mkdir(parents=True)
        (self.tree / "docs/x.md").write_text("old\n")
        self.modules.mkdir()
        (self.modules / "marker").write_text("real")

    def main(self, text, *args):
        out, err = io.StringIO(), io.StringIO()
        env = {**os.environ, "PACECRAFT_NODE_MODULES": str(self.modules)}
        with (
            mock.patch.object(
                regenerate_docs, "GENERATORS", ((writer(text), ("docs/x.md",)),)
            ),
            mock.patch.dict(os.environ, env, clear=True),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = regenerate_docs.main([*args])
        return code, out.getvalue() + err.getvalue()

    def test_check_names_a_stale_document_and_the_fix(self):
        # #2949: a stale generated document fails the push with one command.
        code, out = self.main("new\n", "--check", str(self.tree))
        self.assertEqual(code, 1)
        self.assertIn("stale: docs/x.md. Run `python scripts/regenerate_docs.py`", out)
        self.assertEqual(self.main("new\n", "--check", str(self.tree))[0], 0)

    def test_the_node_modules_link_lives_only_for_the_run(self):
        self.assertEqual(self.main("old\n", "--check", str(self.tree))[0], 0)
        self.assertFalse(os.path.lexists(self.tree / "node_modules"))
        self.assertEqual((self.modules / "marker").read_text(), "real")

    def test_a_failing_generator_exits_2(self):
        (self.modules / "marker").unlink()
        code, out = self.main("new\n", "--check", str(self.tree))
        self.assertEqual(code, 2)
        self.assertIn("failed", out)
        self.assertNotIn("rewrote", out)  # nothing in the checkout changed

    def test_a_failed_generator_shows_why_not_its_stack_tail(self):
        # #3039: the stale-install hint sat above the stack's last lines and was cut off.
        hint = "Error: node_modules does not match package-lock.json: run npm ci"
        frames = "".join(f"    at f{n} (node:internal/x:{n}:1)\n" for n in range(9))
        thrown = (
            f"x.cjs:24\n  throw new Error(hint);\n  ^\n\n{hint}\n{frames}\nNode.js v26"
        )
        self.assertEqual(
            regenerate_docs.failure_lines(thrown),
            ["x.cjs:24", "throw new Error(hint);", hint],
        )
        chained = (
            'Traceback (most recent call last):\n  File "a.py", line 1, in <module>\n'
            "    open(p)\n    ~~~~^^^\nKeyError: 'p'\n\nDuring handling of the above"
            ' exception, another exception occurred:\n\n  File "a.py", line 3\n'
            "RuntimeError: kv catalogue unreadable"
        )
        self.assertEqual(
            regenerate_docs.failure_lines(chained)[-1],
            "RuntimeError: kv catalogue unreadable",  # Python's real cause comes last
        )
        spec = (
            "✔ a (1ms)\n✖ writes the catalogue (3ms)\n  AssertionError: 2 == 3\n"
            f"{frames}ℹ tests 2\nℹ fail 1\n\n✖ failing tests:\n\n"
            "✖ writes the catalogue (3ms)\n  AssertionError: 2 == 3\n"
        )
        self.assertEqual(
            regenerate_docs.failure_lines(spec),
            [
                "✖ writes the catalogue (3ms)",
                "✖ failing tests:",
                "AssertionError: 2 == 3",
            ],
        )
        tap = "ok 1 - a\nnot ok 2 - b\n# fail 1"
        self.assertEqual(regenerate_docs.failure_lines(tap)[0], "not ok 2 - b")
        self.assertEqual(regenerate_docs.failure_lines("a\nb\nc\nd\ne"), list("bcde"))

    def test_without_check_it_rewrites_the_checkout(self):
        with mock.patch.object(regenerate_docs, "ROOT", self.tree):
            code, out = self.main("new\n")
        self.assertEqual((code, out.strip()), (0, "rewrote docs/x.md"))
        self.assertEqual((self.tree / "docs/x.md").read_text(), "new\n")


BLOCK = "<<<<<<< HEAD\nup\n=======\nmine\n>>>>>>> abc (feat: x)\n"
BEGIN, END = "<!-- BEGIN GENERATED ROUTES -->\n", "<!-- END GENERATED ROUTES -->\n"


class ConflictResolutionTests(unittest.TestCase):
    def test_only_conflicts_inside_generated_text_are_resolvable(self):
        # #3031: taking one side and regenerating must not lose hand-written text.
        inside = f"# Routes\n{BEGIN}{BLOCK}{END}Tail\n"
        for path, text, expected in (
            ("docs/api-routes.md", inside, True),
            ("docs/api-routes.md", f"# Routes\n{BLOCK}{BEGIN}x\n{END}", False),
            (
                "docs/api-routes.md",
                f"{BEGIN}<<<<<<< HEAD\n{END}=======\nx\n>>>>>>> a\n",
                False,
            ),
            ("docs/api-routes.md", f"# Routes\n{BEGIN}x\n{END}", False),  # no conflict
            ("docs/performance-methods/code-constants.md", BLOCK, True),
            ("docs/features.md", inside, False),  # not a generated document
        ):
            with self.subTest(path=path, text=text):
                self.assertEqual(
                    regenerate_docs.conflict_resolvable(path, text), expected
                )

    def test_upstream_side_keeps_the_first_side_and_markdown_rules(self):
        text = f"Title\n=======\n{BEGIN}{BLOCK}{END}"
        self.assertEqual(
            regenerate_docs.upstream_side(text), f"Title\n=======\n{BEGIN}up\n{END}"
        )


if __name__ == "__main__":
    unittest.main()
