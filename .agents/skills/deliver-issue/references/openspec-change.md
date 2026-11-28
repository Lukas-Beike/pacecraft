# OpenSpec change

Read when the delivery uses an OpenSpec change (a larger feature).
Use an OpenSpec change only for a larger feature: a new capability or a
behavior change across several flows, or when the user asks for one. Then
create it with the CLI (`npm run openspec -- new change <name>`, then
`instructions <artifact> --change <name>`), keep it to a short proposal, the
spec delta, and tasks you finish before merge (see the task rules in
`openspec/config.yaml`), and reuse an existing change for the same work.

If a change exists, close every task before archiving. Do the work, or, for a
check this environment cannot run, run what is available, tick the task, and
add the unavailable part to an `## Evidence gaps` section in `tasks.md` and the
PR body. A failing check, or an acceptance criterion with no evidence at all,
stays open and blocks delivery. Then confirm `completedTasks` equals
`totalTasks` in `npm run openspec -- list --json`, run
[spec-validate](../../pacecraft-spec-validate/SKILL.md) `--change <name>`, and only then
`npm run openspec -- archive <name> --yes`, which also updates the main specs;
then delete the created `openspec/changes/archive/<date>-<name>` folder in the
same PR (git history keeps it).
`--yes` skips the CLI's incomplete-task prompt, so never run it before that
check, and never use `--skip-specs` or `--no-validate`. Afterwards run
spec-validate again: a delta that passed alone can
push a merged requirement past the length limit.
