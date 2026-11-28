# Development

## AI coding instructions

Keep shared policy in [AGENTS.md](../AGENTS.md) and its scoped files, PR review
criteria in [REVIEW.md](../REVIEW.md), and task workflows in the
[repository skill index](../.agents/skills/README.md). Load relevant workflows on
demand; keep deployment commands here. All project skills have one canonical
root, `.agents/skills/`; full review and `pr` were moved from the legacy `.codex/`
root without duplicate copies. No duplicate `CLAUDE.md` is needed.

Rules without exceptions are enforced, not just written down:
[`.claude/settings.json`](../.claude/settings.json) denies agent file access to any
`.env`/`.env.local` and to the `data/` directory of this checkout and the
primary `ai-coach` checkout. Before every shell command it runs
`scripts/hooks/guard_private_paths.py`, a tested text-matching safety net (not
a sandbox) that blocks clear `.env`, `data`, and Garmin token path forms with no
exceptions, so you run the Docker runbook commands that pass `.env` or mount
`data` yourself. It also blocks commands that merely mention such a path, such
as a commit message; pass that text through a file.
T3's secret folder may only be the `--password-file` value of `scripts/prod_diagnostics.py`, `prod_guards.py`, `prod_logs.py`,
`scripts/ui_walk.mjs` (fixed read-only walk), `scripts/ui_explore.mjs` (read-only
explorer of a JSON plan; it aborts every request but GET, HEAD, login and logout
and fails the run) and `scripts/post_release_actions.mjs` (#3758, #3913).
Importing or running `server.py` reads the checkout's and the data directory's
`.env` at import, so the hook also blocks a Python command that imports or runs
`server`, unless it sets `DATA_DIR` to a temporary directory and neither the
checkout root nor that directory holds a `.env` (#3076); tests set it through
their support module. Beside it,
`scripts/hooks/guard_destructive_git.py` blocks git commands that discard work
(`reset --hard`, `clean -f`, `branch -D`, `checkout .`, `restore .` and a force
push without `--force-with-lease`); normal and lease pushes stay allowed. It
also blocks `--no-verify`, `git add -A` or `.`, `gh pr merge --auto` without
`--squash`, `docker rm -v` and `ruff format` of a folder (#3923).
`scripts/hooks/guard_shell_backslashes.py` blocks Bash edits whose backslashes
the Windows shell tool collapses: a doubled backslash in a heredoc fed to an
interpreter or written to a file, or in `python -c`, `sed -i` and similar inline
edits; write such files and scripts with the Write or Edit tool.
`scripts/hooks/guard_empty_body.py` blocks a `gh pr` or `gh issue` write whose
`--body-file` is empty or whose `--body` is empty, which would blank the public
text (#2954). One hook, `scripts/hooks/guard_shell.py`, runs these shell guards
and `guard_resolve_thread.py` in turn in a single Python process, so a loaded
machine does not time out five interpreter starts per command (#2982); a guard
that fails blocks the command, and each guard still runs on its own.
`.claude/agents/` defines the `explorer` (Haiku, low effort, read-only),
`implementer` (Sonnet, medium), and `reviewer` (Sonnet, medium, read-only)
subagents. `scripts/hooks/thread_model_rule.py` blocks a T3 thread launch whose
model, effort or explicit runtime mode the rule in
`scripts/hooks/thread-model-rule.json` does not allow; an effort above `high` is
refused whatever the file says. After each `t3_thread_read`,
`scripts/hooks/orchestrator_question_flag.py` notes a lane item that was cut
off or an `ORCHESTRATOR:` question no later message answers.

Sessions default to `medium` effort (`effortLevel` in `.claude/settings.json`);
raise it with `/effort high` for tricky work. Agents may not go above `high`
without your approval. `/pr`, `/next-issue`, `/pacecraft-release-unraid`, and `/pacecraft-post-release-check` are user-only Claude Code
commands (`disable-model-invocation: true`); `/deliver-issue` is also model-invocable,
so agents deliver queued issues through it (#2274). All load the canonical
workflows in `.agents/skills/`.

To keep usage down, start a fresh session (`/clear`) for each unrelated task;
every turn re-sends the whole conversation. Check `/usage` occasionally to see
which skills and subagents earn their cost, and delete skills nobody loads.

The local [code-review skill](../.agents/skills/code-review/SKILL.md) adapts
Claude's review workflow for PRs and pre-push branch diffs. Agents read it
directly; the `pr` workflow requires no host-specific slash command.

The [orchestration skill](../.agents/skills/orchestrated-plan-implementation/SKILL.md)
decides per task whether delegating to subagents pays off; by default the main
model does the work. Delegated tasks use fixed tiers without a model question:
Claude Haiku alternating with Gemini Flash for easy tasks, Claude Sonnet for
standard work, and Claude Opus only for genuinely hard or risky tasks, each at
its newest available version. Writers use isolated worktrees; the main model
reviews and integrates their branches.

The [retro skill](../.agents/skills/pacecraft-retro/SKILL.md) runs on request and
briefly after a `deliver-issue` run. It prefers removing or shortening
instructions; a new rule needs an observed failure that a hook or test cannot
prevent.

Coding tasks default to the repository's [Ponytail workflow](../.agents/skills/pacecraft-ponytail/SKILL.md)
in `full` mode: the smallest complete change, reusing existing code before adding
dependencies or abstractions. Any agent can read its Markdown directly;
the external Ponytail plugin is optional. Say `ponytail lite`, `ponytail full`,
`ponytail ultra`, or `stop ponytail` to change the session mode. Repository safety
rules and required checks still apply. This is instruction routing, not a CI gate.

For any coding agent, start in the task worktree and load root plus applicable
scoped AGENTS guidance. Identify the requested behavior, affected owner, and
acceptance check; select the relevant skill, implement the complete flow, inspect
the final diff, and run affected validation. Include final-state evidence and
unverified checks in the handoff. Source code and current workflow behavior take
precedence over remembered paths or stale operating assumptions.

## Local runtime on Windows

The pinned SQLCipher dependency has no Windows wheel, so application and UI
integration run in Docker ([setup](setup.md)); runtime data never enters the
image. Browser checks prefer the disposable fixture in
`.agents/skills/pacecraft-pwa-e2e/references/browser-validation.md`.

## Secret checks before Git pushes

Source reads, incoming prompts/provider data, and Graphify do not require a secret
scan. Outgoing Git objects are checked by `.githooks/pre-push`, which invokes
`python scripts/check_push_secrets.py`. It scans the lines the outgoing
commits add (so flagged lines already on the branch's base never block a push)
and commit/tag messages, including intermediate commits, in temporary storage and suppresses scanner payload output.
A finding, missing Sonar CLI, or scanner error blocks the push. Deleted refs need
no scan. After a clean scan the hook runs CI's repository quality ratchet
(`python tools/quality_baseline.py`: mypy and Ruff, about 35 seconds
on Windows) on a clean export of HEAD; new findings block the push. It runs the
worktree's own Ruff and mypy, which `python scripts/prepare_worktree.py` installs
hash-locked into the git-ignored `.quality-tools/` (format with
`.quality-tools/bin/ruff`), so worktrees on different tool pins no longer reset one
shared version (#3217); without that directory, as in CI, it uses the
interpreter's tools and says so. A Python
file `ruff format --check` would rewrite is a `format` finding (#3061): the
files unformatted when the check arrived are baseline debt, and a push that
leaves a formatted file unformatted is new. The check never rewrites a file;
format the files you change with `ruff format <files>`. Ruff's
executable-bit rules (`EXE001` shebang on a non-executable file, `EXE002`
executable file without a shebang) need a file mode Windows lacks, so there
the ratchet reports them itself from the git modes: the index in a working
tree, the clone's HEAD (`PACECRAFT_GIT_ROOT`) in the pre-push export (#3091).
Make a script executable with `git update-index --chmod=+x <file>`. It records
the word count of every `AGENTS.md` and skill `SKILL.md` (words, not lines: a
21-line skill held 1,027 words; #4290): a file that grows is a new finding, one that
shrank is removed from the baseline by `python tools/quality_baseline.py --prune`,
which removes obsolete entries and lowers sizes and records nothing new (#3925). The same ratchet reports a function above Sonar's cognitive complexity 15 (rule
`python:S3776`) as a `cognitive` finding named by function, not score, and a
conditional expression nested in another (`python:S3358`, #3271), and a
string literal used three times in one file (`python:S1192`, with
sonar-python's exclusions: under 5 characters, word-like, formatting or colour
literals, docstrings, f-strings, decorators, annotations, and files named
`test*`; #3381). The patterns of `re` calls go through `eslint-plugin-sonarjs`'s
`super-linear-regex` (`python:S8786`, an `eslint` finding, #3381) and
`regex-complexity` (`python:S5843`, #3792). Ruff's
`PLR0913` reports a function with more than 13 parameters, `self` not
counted, as Sonar's `python:S107` does (#3316). It
reports a selector list repeated in the same at-rule context of `public/*.css`
(Sonar's `css:S4666`, Stylelint `no-duplicate-selectors`) as a `css` finding,
since CI's Sonar gate otherwise is the first to see it (#3077). It
also checks the JavaScript Sonar analyzes (`public/*.js`, the `scripts/` and
`tools/` modules, the `e2e/` specs, `tests/*.cjs` and the root `*.cjs`, #3058)
for Sonar's cognitive complexity above 15 (S3776), nested ternaries (S3358),
nested template literals (S4624, #3271), super-linear regular expressions
(S8786), too complex ones (S5843, #3792) and commands found through `PATH`
(S4036, #3381) through
`eslint-plugin-sonarjs`; Sonar's
Set-membership rule (S7776) needs `eslint-plugin-unicorn`, which this repository
does not install, so it stays Sonar-only. It checks the page scripts for
identifiers declared nowhere (ESLint `no-undef`, #3016): page scripts may use
the browser globals and the names any page script declares in the shared
global scope or on `globalThis`, derived from `public/`; the service worker
only its own globals; only a file with the Node-test export guard
(`typeof module !== "undefined"`) may use `module`. It uses the
clone's `node_modules` (`npm ci`). In the pre-push hook and in
`scripts/check_changed.py` a clone without ESLint fails the ratchet (run
`npm ci`, #2832); elsewhere, as in CI's `quality` job, it prints that it
skipped the check. A `NOSONAR` comment on the reported line
suppresses a finding as it does in Sonar. Beside the ratchet, in the same
export, `python scripts/regenerate_docs.py --check` reruns the generators of
`docs/performance-methods/`, `docs/api-routes.md`, `docs/kv-catalogue.md` and
`docs/view-catalogue.md`
and blocks a push whose committed copies are stale (#2949; it needs the
clone's `node_modules` and adds no wall time). Run
`python scripts/regenerate_docs.py` to rewrite them. Also beside the ratchet,
the fast architecture guards run on the same export (#3071): provider coupling,
package layering and the server composition, module and ownership tests, about
half a minute without a database. A failure blocks the push with the guard's
finding. Do not use `--no-verify`.

Enable the hook in each clone (the relative path also works in its worktrees):

```powershell
git config core.hooksPath .githooks
```

For a one-off push without changing clone configuration, use
`git -c core.hooksPath=.githooks push <remote> <branch>`.
