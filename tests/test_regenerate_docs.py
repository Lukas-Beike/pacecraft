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
            mock.patch.object(regenerate_docs, "GENERATORS", (writer(text),)),
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

    def test_without_check_it_rewrites_the_checkout(self):
        with mock.patch.object(regenerate_docs, "ROOT", self.tree):
            code, out = self.main("new\n")
        self.assertEqual((code, out.strip()), (0, "rewrote docs/x.md"))
        self.assertEqual((self.tree / "docs/x.md").read_text(), "new\n")


if __name__ == "__main__":
    unittest.main()
