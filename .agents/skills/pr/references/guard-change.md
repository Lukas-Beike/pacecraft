# Guard or hook change

Read when the diff changes a guard, a hook or the checks they run, or adds or tightens a
repository-wide guard or contract test (the last section).

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

## Open PRs before auto-merge

While `develop` does not require up-to-date branches, a PR that adds or
tightens a repository-wide guard or contract test checks the open PRs each time
before (re-)enabling auto-merge: match its rule against their diffs
(`gh pr diff <n>`), or run the guard test on a local merge of a PR head when the
rule is not textual. For each match, comment on that PR, tell the orchestrator
(or the user), and keep auto-merge off until the match is fixed, or until that
PR merged and this branch is rebased so its own CI shows the result (#2645).
