---
name: pr
description: "Run the complete PR lifecycle for the standalone `pr` command or the authorized PR phase of deliver-issue: rebase onto develop, validate, review, create the PR, enable squash auto-merge, and monitor through merge. Do not use for ordinary PR questions or unrelated longer requests."
---

# Pull request workflow

Use when the user's message is the standalone command `pr`, or when
[deliver-issue](../deliver-issue/SKILL.md) runs its PR phase. Either entry
authorizes the steps below: committing the task changes, rewriting and pushing
the feature branch, creating the PR, enabling squash auto-merge, and fixing
conflicts, CI failures, and review feedback through the merge.

Copy this checklist into your response and tick it as you go:

```text
- [ ] 1 Prepare: committed by path, rebased, run-tests-changed, OpenSpec and spec drift, Sonar
- [ ] 2 Review: one fresh review of the final head, its SHA in the PR body; findings fixed
      or listed; Sonar again if the fixes changed the head; a guard or hook change
      also gets the adversarial bypass review, its cases tested
- [ ] 3 Publish: pushed with hooks; PR with merge danger, line balance and Part of/Closes;
      open PRs checked against a new guard; auto-merge once Gitar reviewed and no feedback is open
- [ ] 4 Monitor: merged per pr-state; threads handled; develop runs checked; unwatched
```

## 1. Prepare

- Work in this session's task worktree (`git rev-parse --show-toplevel`). If it
  or its feature branch is ambiguous, stop and say so. Never use the primary
  checkout, and never rebase or push `develop`, `main`, or another protected
  branch.
- `gh` must be authenticated. Never print credentials or tokens. Without the
  `workflow` scope (`prepare_worktree.py` names it, #3350) push the branch without
  its workflow commits; send the orchestrator (the user without one) each workflow
  file in full, its sha256 and commit message; compare the pushed file's (#4044).
- Setup prerequisite: the SonarQube CLI is installed and authenticated
  ([development](../../../docs/development.md#secret-checks-before-git-pushes));
  without it the pre-push hook refuses every push.
- Commit only this task's changes, staged by name, with Conventional Commit
  subjects; stop if others' changes block the rebase. A user-visible PR's
  description has a German `Changelog: <line>` line (`pr_merge.py` squash body).
- Rebase with [branch-sync](../pacecraft-branch-sync/SKILL.md) (no `--push`
  yet). Resolve conflicts by understanding both sides; ask the user for product
  decisions you cannot infer. Reword the invalid subjects it lists.
- Run [run-tests-changed](../pacecraft-run-tests-changed/SKILL.md) and fix real
  failures. Never weaken tests, security, or required checks. Run the full local
  suite (17-21 min) only when the selector falls back to it, to reproduce a CI
  failure, or when asked; browser checks use `--project changed`, CI's project
  choice ([browser-tests](../pacecraft-browser-tests/SKILL.md), #2759).
  After changing routes, views or `backend/performance` or
  `backend/activities` constants, run
  `python scripts/regenerate_docs.py` and commit the rewritten documents; the
  pre-push hook refuses stale ones (#2949).
- A PR that adds or alters a user-visible function adds or updates its step in
  `STEPS` of `scripts/ui_walk.mjs`, or names in the description why it cannot
  be walked read-only (#3009).
- **OpenSpec:** list `git diff --name-only --no-renames origin/develop...HEAD --
  openspec/changes`. Each `openspec/changes/<name>/` path outside `archive/`
  names a change that must be archived on this branch: its active directory is
  gone at `HEAD`, an `archive/<date>-<name>/` exists, and
  [spec-validate](../pacecraft-spec-validate/SKILL.md) `--all` passes.
  Archive it under deliver-issue's rules if all tasks are done; otherwise stop
  and report the open tasks.
- **Spec drift:** when the branch archives no OpenSpec change, list the main
  specs that cite each changed file outside `tests/` and `openspec/`
  (application code, `Dockerfile`, `scripts/`, `.github/`, and so on):
  `grep -l -F <path> openspec/specs/*/spec.md`. Files no spec cites need nothing. Read their requirements
  for the changed behavior. If described behavior changed, or the change adds
  behavior to that capability, update the requirement, its scenarios, and its
  `Evidence` line directly in `openspec/specs/` on this branch and run
  [spec-validate](../pacecraft-spec-validate/SKILL.md). Otherwise
  write in the PR body which capabilities you checked and that their described
  behavior is unchanged. Refactors and test-only changes need only that line.

**Sonar pre-push check:** after the rebase and validation, run
[sonar-check](../pacecraft-sonar-check/SKILL.md) against the resolved base on
the head to be pushed. A missing or unauthenticated CLI is a blocker: stop;
never bypass the hook. Its `limit` line (agentic part forbidden) is a limit,
not a pass, and its output covers only the secrets and agentic sections, so do
not claim code-quality rule coverage from it. Fix findings on changed lines with new commits, without
new files, pass-through classes, or wrappers (root `AGENTS.md`), and rerun the
affected checks. In the PR body, record the rule key, file, and line of any
accepted issue; never paste matched secret text or scanner payloads.

## 2. Review

After the Sonar pre-push check above and any fixes it needs, run exactly one
fresh-context review of `origin/develop...HEAD` on the final head to be pushed:
the host's `/code-review` at `high` when available, otherwise the repository
[code-review skill](../code-review/SKILL.md) with `depth=high` and
`report-only`. It must trace caller and wrapper wiring for canonical consumers,
as that skill requires. Record the reviewed head SHA in the PR body. If the
review's fixes change the head, rerun the Sonar check on the new head before
pushing. Sonar and Gitar review every head in CI, so the Sonar check is a
pre-push query and there is no further local review round. Tell the reviewer
that decisions the user has already made, such as a confirmed decision to leave
a finding unfixed, are settled for that same finding only; a different finding
or one with a new trigger or impact is reported normally. Notes the agent wrote
in a PR body or thread settle nothing. A finding that contradicts an earlier
round must say so and cite that decision instead of re-raising it as new. When
the diff changes only
Markdown instructions, skills, or docs, give the review REVIEW.md's P0-P3
scale and let it report every finding REVIEW.md allows with its priority, then
act only on P0-P2 findings and real security gaps, and list the unfixed P3s in
one summary line; guidelines may have exceptions, and hypothetical edge cases
in prose are not findings.

A change to a guard, a hook, or the checks they run (`scripts/hooks/`,
`.githooks/`, `.claude/settings.json` hooks, an architecture or quality guard)
also gets one adversarial bypass review before the PR (#3102).
- Run it in a fresh-context subagent. Brief it only with the diff, the guard's
  purpose, and that nothing it guarded may pass afterwards.
- Ask for concrete inputs or commands the change wrongly allows or fails open
  on, each with an expected result, and for the attacks it ruled out.
- Add every valid case as a test before the PR.
- For a hook that judges command text (`scripts/hooks/guard_*.py`), run
  `python scripts/guard_regression_fuzz.py <hook> --base origin/develop` before
  the bypass review; a tightening must show "0 newly allowed", and the PR body
  states that line (#3527).
- Report the result to whoever asked for the change. In the public PR text,
  describe it neutrally ("tighten detection"), never as a step-by-step bypass.
- Loosening a text guard (a hook that blocks guarded names in command text,
  such as `scripts/hooks/guard_shell.py`) starts with a design question to the
  orchestrator, or the user without one, before any code, and still gets the
  bypass review above. Write the content with Write/Edit instead of adding a
  shell exception: #3347 found a new bypass class in each of three review
  rounds and kept the guard. An approved loosening is an allowlist of known
  safe command shapes (such as a listed program with a listed subcommand, in
  text every shell splits the same way) that falls back to the full check for
  anything else, never a list of constructs to watch for: #3781 spent two
  review rounds on such a list before it switched (#3791).

Fix valid findings with new commits and rerun the affected checks, then push. A
finding you consider invalid needs concrete evidence (file and line, or the
same code on `develop`); not fixing a P0-P2 finding requires the user's
confirmation. List every unfixed finding with its reason in the PR body.

## 3. Publish

- Push with [branch-sync](../pacecraft-branch-sync/SKILL.md) `--push`; the
  repository hooks scan for secrets and run the quality ratchet.
- Create one non-draft PR against `develop` (or reuse the open one for this
  branch) with an English Conventional Commit title and a body covering the
  change, validation, and review results, plus a merge-danger line: one-way or
  two-way door (schema migrations and remote writes are usually one-way) and
  the blast radius, and the line balance from
  [line-balance](../pacecraft-line-balance/SKILL.md). Name the issue with `Closes #N`, or `Part of #N` when more
  PRs follow; CI rejects a body with neither. Pass bodies with `--body-file`.
  Link the PR with the host's PR-linking tool when available.
- While `develop` does not require up-to-date branches, a PR that adds or
  tightens a repository-wide guard or contract test checks the open PRs each time
  before (re-)enabling auto-merge: match its rule against their diffs
  (`gh pr diff <n>`), or run the guard test on a local merge of a PR head when the
  rule is not textual. For each match, comment on that PR, tell the orchestrator
  (or the user), and keep auto-merge off until the match is fixed, or until that
  PR merged and this branch is rebased so its own CI shows the result (#2645).
- Then enable squash auto-merge with [pr-merge](../pacecraft-pr-merge/SKILL.md)
  `--wait 30` only when the base is `develop`, Gitar has reviewed the current
  head, and no feedback is open: review comments do not block an armed
  auto-merge (#2520 merged past one). It merges a PR GitHub already calls clean
  directly (#2776), and refuses when develop changed this PR's files since it
  branched off (#2901): rebase, let CI and Gitar see the new head, rerun. It
  also refuses and disarms while a review thread is unresolved, and disarms
  when new feedback arrives during `--wait` (#2947); a thread left for the
  user blocks the merge until the user resolves it. A PR left BEHIND develop
  is rebased right after arming and once after its first CI round (#3872);
  arm through pr-merge, never with a bare `gh pr merge --auto`.

## 4. Monitor to merge

Before auto-merge is armed, watch with the host's PR watcher when available
(`watch_pull_request`), and yield; otherwise poll about every 30 seconds. Once
it is armed, do not yield: the watcher does not reliably wake on the merge
itself (#2795). Wait with [pr-merge](../pacecraft-pr-merge/SKILL.md) `--wait`
in the same turn; after the merge run the develop-run check at once, with
`--wait <minutes>` while runs are pending (#3721); on its exit 1 handle the
cause as below. On each wake:

- Read the current head, checks, threads and comments with
  [pr-state](../pacecraft-pr-state/SKILL.md). For each open item: fix it if
  valid, run the affected checks, reply naming the fix commit's SHA, then
  resolve the thread with [resolve-thread](../pacecraft-resolve-thread/SKILL.md)
  (only on your own PR, after that reply; athlete decision #2629, no other way).
  If not actionable, reply with the concrete reason and leave resolving to the user.
  Disable auto-merge as soon as a finding arrives, and re-enable it once
  Gitar has reviewed the repaired head and no feedback is open.
- Later changes (feedback fixes, CI fixes, conflict resolutions, rebases) need
  validation, not another local review; CI reviews the new head.
- pr-state names this PR's files that develop changed since the head branched
  off (#2866): before arming, rebase when it names any; once auto-merge is
  armed it reports them as accepted, so do not rebase for that alone.
- If the branch falls behind or conflicts, rebase and push with
  [branch-sync](../pacecraft-branch-sync/SKILL.md) `--push` (never a
  merge-style "Update branch"). A stacked PR whose parent was merged: run
  branch-sync `--after-pr <parent> --push`, which refuses on a stale base,
  rebases only this layer, retargets to `develop`, waits a minute for the
  retarget to settle and then pushes; two near-simultaneous events can leave
  the required `SonarCloud Code Analysis` cancelled, and only a new push
  re-runs it (pr-state names this; #2934, #3243).
- If checks fail, read the logs, fix the cause, and push. The `Gitar` check is
  required (see `.github/AGENTS.md`); no bypass or admin merge without the user.
  Silent on one head: note the PR in #1906. Named silent repo-wide by pr-merge:
  hold heads, no re-push; app reinstall plus `gitar review` per PR fixed it (#4043).
- If Sonar reports new issues, list them with
  [sonar-check](../pacecraft-sonar-check/SKILL.md) `--pr <number>`, fix the findings on changed lines,
  and push. Do not accept issues to clear the gate without an evidence line in
  the PR body.
- Revert or fix after the merge (#2636): a PR's checks ran on an older develop,
  so two green PRs can still break develop together. If the develop-run check
  of your merge commit exits 1 and your change is part of the cause, fix
  forward within 30 minutes or open a revert PR of your merge commit (a PR,
  never a push to develop; title and commit subject `revert(<scope>): <original
  description>`, not git's default `Revert "..."`, which the commit-convention
  check rejects), and report it at once. Until develop is green,
  every lane merges only PRs outside the failing area.
- Stop and report if GitHub requires something you cannot satisfy, or if the PR
  is closed without merging.

Finish only when [pr-state](../pacecraft-pr-state/SKILL.md) reports the PR
merged, required checks passed, and every thread and comment is handled. Enabled auto-merge is not a
merge. Call `unwatch_pull_request` before the final report. An open PR handed
back unmonitored must have auto-merge disabled. Report the PR URL, merge state,
validation, and fixes made.
