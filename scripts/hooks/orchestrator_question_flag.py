"""Claude Code PostToolUse hook: flag a lane's open ORCHESTRATOR: question (#3753).

After a `t3_thread_read` of a thread's newest page, it adds a note to the
reader's context when the newest assistant item was cut off (a question may
sit past the cut) or when an assistant line starting with `ORCHESTRATOR:` asks
a question that no later user message on the page answers. Reads of older
pages or of the rest of one item are skipped: their later answers are not on
the page. It only advises: it never blocks and stays silent on input it
cannot read.
"""

import json
import sys

MARK = "ORCHESTRATOR:"
PAGED = ("itemId", "textOffset", "beforePosition", "afterPosition")


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
    items = sorted(
        (item for item in page["items"] if isinstance(item, dict)),
        key=lambda item: int(item.get("position") or 0),
    )
    replies = [i for i, item in enumerate(items) if item.get("type") == "user_message"]
    last_reply = replies[-1] if replies else -1
    mine = [
        i for i, item in enumerate(items) if item.get("type") == "assistant_message"
    ]
    found = []
    if mine and items[mine[-1]].get("textTruncated"):
        newest = items[mine[-1]]
        found.append(
            f"{name}: newest item {newest.get('itemId')} is cut off; read the rest "
            f"with itemId and textOffset={newest.get('nextTextOffset')}."
        )
    open_text = "\n".join(
        str(items[index].get("text") or "") for index in mine if index > last_reply
    )
    lines = (line.strip().lstrip("-*>` ").strip() for line in open_text.splitlines())
    found.extend(
        f"{name}: unanswered {line[:300]}"
        for line in lines
        if line.startswith(MARK) and "?" in line
    )
    return found


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        if any(key in (payload.get("tool_input") or {}) for key in PAGED):
            return
        pages = list(_pages(payload.get("tool_response")))
    except ValueError, AttributeError, TypeError, RecursionError:
        return
    found = []
    for page in pages:
        try:
            found.extend(notes(page))
        except ValueError, AttributeError, TypeError:
            continue
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


if __name__ == "__main__":
    main()
