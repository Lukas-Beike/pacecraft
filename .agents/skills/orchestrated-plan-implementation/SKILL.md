---
name: orchestrated-plan-implementation
description: Decide whether repository work stays with the main model or goes to subagents or parallel workers, and which model and effort a delegate gets. Default to the main model; delegate only for a clear, cost-effective benefit; the main model alone integrates results. Use when considering a subagent, parallel worker or delegate.
license: MIT adaptations; see ../THIRD_PARTY.md.
---

# Orchestration

The main model does the work unless delegation clearly pays off: subagents give
parallelism, keep noisy output away, or give a fresh reviewer, and cost tokens.
Delegate only for:

- **Parallelism:** two or more substantial, independent tasks with no shared
  files or changing contracts.
- **Cheaper execution:** large, clearly specified mechanical work a lower tier
  does well.
- **Noisy search:** a broad read-only search where only the conclusion matters
  (callers or owners across directories) goes to `explorer`, which may use
  Graphify. Targeted lookups and reads needed for an edit stay with the main model.
- **Independent review:** a risky diff reviewed without the author's reasoning.

State the decision in one line and continue. An explicit user instruction wins.

## Tiers

The one statement of the model and effort rule: other files point here. Use it
without asking. Effort never exceeds `high`.

| Tier | Tasks | Model and effort | Agent definition |
| --- | --- | --- | --- |
| L1 | Clear target and instructions, cheap check, read-only search | Haiku (low) alternating with Gemini Flash (`-low`), starting with Gemini | `explorer` for search |
| L2 | Almost everything | Sonnet: `implementer` high, routine `reviewer` medium | `implementer`, `reviewer` |
| L3 | Hard or risky work only: migrations, auth/privacy, data integrity, hard diagnosis | Opus, at most medium | `implementer`/`reviewer` with `model: "opus"` |

T3 threads: a lane starts with Sonnet at high (tooling, CI, docs, small fixes) or
Opus at medium (method, data, schema), never Opus at high; an orchestrator keeps
Opus at high. `.claude/agents/` pins model and effort and, for `explorer` and
`reviewer`, read-only tools; `scripts/hooks/thread-model-rule.json` caps thread
launches. Gemini Flash runs through T3 `delegate_task`; see
[runtime binding](references/runtime-binding.md). If a tier is unavailable, use
the other L1 model or stay with the main model and say so.

## Dispatch and integrate

- Commit what a worker or reviewer must see first; worktrees start from the last
  commit. Writers use `isolation: "worktree"`. Pass `run_in_background: false`
  unless several workers run in parallel. At most three at once.
- The brief is self-contained: objective, allowed paths, base commit, acceptance
  check, and handoff format. No secrets or athlete data.
- Workers commit on their own branch with Conventional Commit subjects and never
  push, open PRs, merge, or start other agents.
- The parent reads each worker's complete diff before taking it, then
  cherry-picks or rebases its commits (no merge commits). If the base moved or a
  conflict appears, review the result again or have the worker redo it.
- A deviation from the base branch that changes athlete-visible output or durable
  data is a regression until the athlete accepts it: fix it or ask, and never
  brief reviewers on it as intended; internal refactors and deviations the
  athlete already decided are exempt.
- Retry a failed task at most twice, then re-scope or move up a tier.
- Worker results never replace validation or the PR review.
