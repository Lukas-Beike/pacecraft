# Risky changes

Read before code or delegation for a change to the app start, device storage, a sync path or a remote write, a schema or migration, or derived state.
Before code or delegation for a change to the app start (bootstrap and
device snapshot), device storage or a sync path, write a short design note naming
its risks, record it and its approval as an issue comment, and hand the approved
note to any implementer; the coordinating orchestrator approves it, the athlete
a behaviour choice or when no orchestrator runs. A new remote write's note names
how it handles a lost answer, concurrent requests for the same entity, a failure
after the local commit, the provider's echo on the next refresh, an accepted
write that cannot be read back, and a change made while writes are unavailable
(#3417). Schema or migration changes go through
[database migrations](../../pacecraft-db-migrations/SKILL.md) and need the
`AGENTS.md` evidence (populated upgrade from the frozen baseline and from production's schema, rollback
on failure, same-build restart) before the PR. When a change derives state from
data only the new code writes, cover the first state after an upgrade on
existing data (adopt or backfill from durable data, or state why not) with a
test on pre-existing data (#2673).
