"""The thread-read hook flags a lane's cut-off or unanswered ORCHESTRATOR: line."""

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


def read(items, status="idle", wrap=True):
    page = {"thread": {"title": "Lane L1", "status": status}, "items": items}
    response = [{"type": "text", "text": json.dumps(page)}] if wrap else page
    payload = {"tool_name": "mcp__t3-code__t3_thread_read", "tool_response": response}
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if not result.stdout.strip():
        return result.returncode, ""
    output = json.loads(result.stdout)["hookSpecificOutput"]
    return result.returncode, output["additionalContext"]


class OrchestratorQuestionFlagTests(unittest.TestCase):
    def test_unanswered_line_of_an_idle_lane_is_flagged(self):
        code, context = read(
            [
                item(1, "user_message", "start"),
                item(2, "assistant_message", "Done.\nORCHESTRATOR: keep 7 copies?"),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("Lane L1: unanswered ORCHESTRATOR: keep 7 copies?", context)

    def test_question_of_a_running_lane_is_flagged_a_report_is_not(self):
        question = [item(1, "assistant_message", "ORCHESTRATOR: split it?")]
        report = [item(1, "assistant_message", "ORCHESTRATOR: claimed #1")]
        self.assertIn("unanswered", read(question, "running")[1])
        self.assertEqual(read(report, "running"), (0, ""))
        self.assertIn("unanswered", read(report, "idle", wrap=False)[1])

    def test_a_later_user_message_answers_the_line(self):
        items = [
            item(1, "assistant_message", "ORCHESTRATOR: split it?"),
            item(2, "user_message", "Yes, split it."),
        ]
        self.assertEqual(read(items), (0, ""))

    def test_cut_off_lane_item_names_the_offset(self):
        _code, context = read(
            [item(1, "user_message", "go"), item(2, "assistant_message", "Long", 20000)]
        )
        self.assertIn("item-2 is cut off", context)
        self.assertIn("textOffset=20000", context)

    def test_unreadable_input_stays_silent(self):
        for raw in ("not json", json.dumps({"tool_response": "plain text"})):
            with self.subTest(raw=raw):
                result = subprocess.run(
                    [sys.executable, str(HOOK)],
                    input=raw,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
