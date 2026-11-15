"""Claude Code PreToolUse hook: run every shell guard in one Python process (#2982).

Each Bash/PowerShell call started one interpreter per guard. Under local load
those starts timed out and blocked the command. This entry runs each guard's
own ``main()`` in turn on the same hook input and stops at the first block. The
guards are unchanged and still run on their own. Exit code 2 blocks the call;
a guard that fails, or cannot even be imported, blocks it too.
"""

import importlib
import io
import sys

GUARDS = (
    "guard_private_paths",
    "guard_destructive_git",
    "guard_shell_backslashes",
    "guard_resolve_thread",
    "guard_empty_body",
)


def main() -> int:
    data = sys.stdin.buffer.read()
    for name in GUARDS:
        sys.stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
        try:
            code = importlib.import_module(name).main()
        except Exception as error:  # noqa: BLE001 - any failure blocks, as a crashed hook did
            print(
                f"Blocked: the shell guard {name} failed ({type(error).__name__}).",
                file=sys.stderr,
            )
            return 2
        if code:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
