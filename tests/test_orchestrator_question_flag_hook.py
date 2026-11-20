"""The thread-read hook flags a lane's cut-off item or unanswered ORCHESTRATOR: question."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = (
    Path(__file__).resolve().parents[1] / "scripts/hooks/orchestrator_question_flag.py"
)


def item(position, kind, text, cut_at=None):
    return {
        "position": position,
        "itemId": f"item-{position}",
        "type": kind,
        "text": text,
        "textTruncated": cut_at is not None,
        "nextTextOffset": cut_at,
    }


def run(raw):
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=raw,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def read(items, wrap=True, tool_input=None):
    page = {"thread": {"title": "Lane L1"}, "items": items}
    response = [{"type": "text", "text": json.dumps(page)}] if wrap else page
    result = run(
        json.dumps(
            {
                "tool_name": "mcp__t3-code__t3_thread_read",
                "tool_input": tool_input or {"threadId": "t"},
                "tool_response": response,
            }
        )
    )
    if not result.stdout.strip():
        return result.returncode, ""
    output = json.loads(result.stdout)["hookSpecificOutput"]
    return result.returncode, output["additionalContext"]


class OrchestratorQuestionFlagTests(unittest.TestCase):
    def test_unanswered_question_is_flagged_with_its_own_line(self):
        code, context = read(
            [
                item(1, "user_message", "start"),
                item(
                    2,
                    "assistant_message",
                    "Done.\n**ORCHESTRATOR: keep 7 copies?**\nORCHESTRATOR: on #9",
                ),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("Lane L1: unanswered ORCHESTRATOR: keep 7 copies?", context)
        self.assertNotIn("on #9", context)

    def test_reports_and_mentions_are_not_questions(self):
        for text in (
            "ORCHESTRATOR: claimed #1",
            "I report with an ORCHESTRATOR: prefix, right?",
        ):
            with self.subTest(text=text):
                items = [item(1, "assistant_message", text)]
                self.assertEqual(read(items, wrap=False), (0, ""))

    def test_a_later_user_message_answers_the_question(self):
        items = [
            item(1, "assistant_message", "ORCHESTRATOR: split it?"),
            item(2, "user_message", "Yes, split it."),
            item(3, "assistant_message", "Splitting."),
        ]
        self.assertEqual(read(items), (0, ""))

    def test_only_the_cut_off_newest_item_is_named(self):
        _code, context = read(
            [
                item(1, "assistant_message", "Old", 200),
                item(2, "user_message", "go"),
                item(3, "assistant_message", "Long", 20000),
            ]
        )
        self.assertIn("item-3 is cut off", context)
        self.assertIn("textOffset=20000", context)
        self.assertNotIn("item-1", context)

    def test_older_pages_and_item_continuations_are_skipped(self):
        items = [item(1, "assistant_message", "ORCHESTRATOR: split it?", 500)]
        for tool_input in (
            {"threadId": "t", "itemId": "item-1", "textOffset": 200},
            {"threadId": "t", "beforePosition": 40},
        ):
            with self.subTest(tool_input=tool_input):
                self.assertEqual(read(items, tool_input=tool_input), (0, ""))

    def test_unreadable_input_stays_silent(self):
        for raw in (
            "not json",
            json.dumps({"tool_response": "plain text"}),
            json.dumps(
                {"tool_response": {"thread": "x", "items": [{"position": "a"}]}}
            ),
        ):
            with self.subTest(raw=raw):
                result = run(raw)
                self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
