"""Open-checkbox parsing for scripts/issue_status.py."""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/issue_status.py"
SPEC = importlib.util.spec_from_file_location("issue_status", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
issue_status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(issue_status)

BODY = """## Required changes
- [x] Add the CI check
- [ ] Add the hook
  * [ ] nested follow-up
- [ ]missing space is not a box
Text with [ ] inside a sentence.
### Acceptance criteria
- [ ] closing summary covers this
#### Edge cases
- [ ] still a criterion
```bash
# Acceptance criteria inside a fence is no heading
```
## Parts
- [ ] Part two
```bash
# acceptance criteria smoke test
```
- [ ] Part three
"""


class IssueStatusTests(unittest.TestCase):
    def test_lists_only_open_checkboxes(self):
        self.assertEqual(
            issue_status.open_checkboxes(BODY),
            ["Add the hook", "nested follow-up", "Part two", "Part three"],
        )
        criteria = issue_status.open_boxes(BODY)[1]
        self.assertEqual(
            [m[1] for m in criteria],
            ["closing summary covers this", "still a criterion"],
        )
        # #2799: a plain "Acceptance" heading starts the criteria too, but a
        # heading of work items such as "Acceptance tests" holds parts
        body = "## Acceptance\n- [ ] a\n## Acceptance tests\n- [ ] b\n## Acceptance:\n- [ ] c"
        parts, criteria = issue_status.open_boxes(body)
        self.assertEqual(
            ([m[1] for m in parts], [m[1] for m in criteria]), (["b"], ["a", "c"])
        )

    def test_only_closing_or_part_of_references_link_a_pr(self):
        bodies = {  # #2798: a mention in prose or a longer number does not link
            1: "Closes #12",
            2: "Part of #12",
            3: "fixed: #12.",
            4: "RESOLVES #12 and more",
            5: "Builds on #12 (merge hold).",
            6: "Closes #123",
            7: "Part of #1",
            8: None,
            9: "_Part of #12_",
            10: "Suffixes #12",
        }
        pulls = [{"number": n, "body": body} for n, body in bodies.items()]
        calls = []
        linked = issue_status.linked_pulls(12, lambda *a: calls.append(a) or pulls)
        self.assertEqual([pull["number"] for pull in linked], [1, 2, 3, 4, 9])
        # the default 30 results could hide an older linked PR
        self.assertIn("1000", calls[0])

    def test_gh_json_reads_text_outside_the_windows_code_page(self):
        # #2895: "Ё" (D0 81) is undefined in cp1252 and ASCII; decoded with the
        # locale, stdout was None and gh_json silently returned None. Runs
        # without UTF-8 mode and with an ASCII locale; "gh" is a stub child.
        payload = json.dumps({"title": "Ё −"}, ensure_ascii=False).encode()
        tool = f"import sys; sys.stdout.buffer.write({payload!r})"  # UTF-8 bytes
        check = (
            "import json, subprocess, sys, importlib.util;"
            f"spec = importlib.util.spec_from_file_location('s', {str(SCRIPT)!a});"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
            "real = subprocess.run;"
            f"m.subprocess.run = lambda a, **k: real([sys.executable, '-c', {tool!a}], **k);"
            "print(ascii(m.gh_json('issue', 'view')))"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        result = subprocess.run(
            [sys.executable, "-X", "utf8=0", "-c", check],
            env={**env, "LC_ALL": "C"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(
            result.stdout.strip(), ascii({"title": "Ё −"}), result.stderr[-300:]
        )

    def test_empty_body_has_no_checkboxes(self):
        self.assertEqual(issue_status.open_checkboxes(None), [])

    def test_usage_error_without_number(self):
        self.assertEqual(issue_status.main(["issue_status.py"]), 2)


if __name__ == "__main__":
    unittest.main()
