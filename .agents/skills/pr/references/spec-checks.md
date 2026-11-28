# OpenSpec archive check

Read when the diff touches `openspec/changes`. Moved unchanged from the pr skill (#4290).

- **OpenSpec:** list `git diff --name-only --no-renames origin/develop...HEAD --
  openspec/changes`. Each `openspec/changes/<name>/` path outside `archive/`
  names a change that must be archived on this branch: its active directory is
  gone at `HEAD`, an `archive/<date>-<name>/` exists, and
  [spec-validate](../../pacecraft-spec-validate/SKILL.md) `--all` passes.
  Archive it under deliver-issue's rules if all tasks are done; otherwise stop
  and report the open tasks.
