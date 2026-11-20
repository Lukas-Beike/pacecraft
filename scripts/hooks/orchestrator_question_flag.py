"""Claude Code PostToolUse hook: flag a lane's open ORCHESTRATOR: line (#3753).

After a `t3_thread_read`, it adds a note to the reader's context when the page
shows a lane assistant item whose text was cut off (the question may sit past
the cut) or whose `ORCHESTRATOR:` line no later user message answers while the
lane is idle or the line asks a question. It only advises: it never blocks and
stays silent on any input it cannot read.
"""

import json
import sys

MARK = "ORCHESTRATOR:"


def _pages(value):
    """Yield every thread-read result object inside the hook's tool_response."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return
    if isinstance(value, dict):
        if isinstance(value.get("items"), list):
            yield value
            return
        value = list(value.values())
    if isinstance(value, list):
        for part in value:
            if isinstance(part, (dict, list)) or (
                isinstance(part, str) and part.lstrip().startswith("{")
            ):
                yield from _pages(part)


def notes(page):
    thread = page.get("thread") or {}
    name = thread.get("title") or thread.get("threadId") or "thread"
    idle = thread.get("status") != "running"
    items = sorted(
        (item for item in page["items"] if isinstance(item, dict)),
        key=lambda item: item.get("position") or 0,
    )
    found = []
    for index, item in enumerate(items):
        if item.get("type") != "assistant_message":
            continue
        text = str(item.get("text") or "")
        if item.get("textTruncated"):
            found.append(
                f"{name}: item {item.get('itemId')} is cut off; read the rest with "
                f"itemId and textOffset={item.get('nextTextOffset')} before judging it."
            )
        lines = [line.strip() for line in text.splitlines() if MARK in line]
        answered = any(
            later.get("type") == "user_message" for later in items[index + 1 :]
        )
        if lines and not answered and (idle or any("?" in line for line in lines)):
            found.append(f"{name}: unanswered {lines[-1][:300]}")
    return found


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        found = [
            note
            for page in _pages(payload.get("tool_response"))
            for note in notes(page)
        ]
    except ValueError, AttributeError, TypeError:
        return 0
    if found:
        context = "Lane report check (#3753):\n- " + "\n- ".join(dict.fromkeys(found))
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": context,
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
