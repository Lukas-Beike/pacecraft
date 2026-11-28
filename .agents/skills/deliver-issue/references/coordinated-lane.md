# Coordinated lane

Read when an orchestrator coordinates the delivery. Moved unchanged from the deliver-issue skill (#4290).

When an orchestrator coordinates the run: ask it for the `AGENTS.md` turn before
touching that file, follow its hold on a red `develop`, and report each closed
issue or merged part with a one-line entry from
[ledger-line](../../pacecraft-ledger-line/SKILL.md), written after the `close` or
`tick`, because it reads the issue's live state. Ask the athlete directly only
in a turn the athlete started in this lane thread; otherwise report the question
to the orchestrator with `t3_thread_send`, prefixed `ORCHESTRATOR:`, and
write the same line as the first line of your final message; if the send was
refused, say so there (#3753). The orchestrator asks it under its
[athlete question rules](../../pacecraft-orchestrator/SKILL.md#athlete-questions)
and passes the answer back (#3376). Keep that item in the queue as parked and
continue with the next independent one; with none left, report that the lane
waits for the answer. A question that offers a default parks only its item:
keep building every item that needs no decision, and end the run only when no
ready work is left (#3669). Record the answer as an issue comment; no closing
sentence while an item is parked.
