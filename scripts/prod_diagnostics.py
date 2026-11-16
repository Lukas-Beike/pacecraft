"""Read named fields and counts from a running app's health and diagnostics.

    python scripts/prod_diagnostics.py --url <app address> --password-file <secret file>
        [--expect-version 2.1.2] [--expect-schema 27] [--out result.json]
        [--compare previous.json] [--ca-file ca.pem]
    python scripts/prod_diagnostics.py --url <app address> --password-file <secret file>
        --section <name> [--section <name> ...]

Prints one line per check (`ok` / `FAIL` / `info`); never a response body,
cookie, password or error text. Exit 0 all ok, 1 a check failed (a count
dropped against --compare, a version mismatch, a stale or stuck sync), 2 the
app could not be reached or read. `--section <name>` (repeatable) prints,
instead of the checks, only the allow-listed counts, flags, state words and
ages in hours (a `*_at` time as `*_hours`) of the sections in SECTION_FIELDS,
or the sums of a section in DERIVED_SECTIONS (`coach_local_first`: the #3080
counts over the Coach history); the state words in FAIL_STATES and a missing
section print as FAIL.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import UTC, datetime
from typing import Any

from prod_client import ProdError, Session, read_result, write_result

COUNTS = (
    "messages",
    "workout_library",
    "competitions",
    "athlete_checkins",
    "activity_feedback",
    "change_history",
    "external_calendar_events",
)
MEMORY = ("rss_bytes", "peak_rss_bytes")
# Storage and runtime health compared between releases (#2848).
STORAGE = ("file_bytes", "wal_bytes", "data_dir_free_bytes")
# Hours. Provider and calendar syncs are due hourly (backend/sync/daily.py), so
# STALE_HOURS flags only a long outage; a job still open after 2 h is stuck.
STALE_HOURS = 36
STUCK_HOURS = 2
OPEN_JOB = {"queued", "running", "pending"}
WORD = re.compile(r"[a-z][a-z_]{0,31}")
ERROR_KIND = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")
# What `--section` prints of a section: dotted paths of numbers and flags, and
# state words from STATES; nothing else of the section is printed (#2779, #2781).
SECTION_FIELDS = {
    # Storage and runtime health for the post-release check (#2848).
    "database": re.compile(
        "|".join(("schema_version", *COUNTS, *STORAGE))
        + r"|(workout_library_state|workout_library_archive|stored_payload_bytes)\.[a-z_]+"
    ),
    "runtime": re.compile(
        r"uptime_s|malloc_arena_max|process_memory\.[a-z_]+|reader_slots\.[a-z_0-9]+"
        r"|heavy_read_gate\.[a-z_0-9]+"
        # Session reports, checks by verdict and resolutions: counts only (#3084).
        r"|session_reports\.(reports|(checks_by_verdict|resolutions)\.[a-z_]+)"
    ),
    "recovery_copies": re.compile(
        r"status|available|count|newest_age_hours|total_bytes"
    ),
    # The newest backup the app made (#2996).
    "backup": re.compile(r"status|known|age_hours|bytes|verified"),
    # The all-time curve build (#2852) and its stored curves (#2821).
    "curve_build": re.compile(
        r"status|available|builds|failures|last_build_s|building_s|failed_age_s"
        r"|curves_(computed|loaded|stored)|curve_store_failures"
    ),
    # Strength units sent to Garmin by shape, and Intervals.icu-only ones (#2850).
    "garmin_strength_push": re.compile(
        r"status|garmin\.(units|changed_since_push|without_structure|one_exercise"
        r"|several_exercises|with_kg)|intervals\.(units|without_structure)"
    ),
    # Past Garmin days without daily calories (#3157).
    "garmin_daily_calories": re.compile(
        r"status|days|complete|partial|without_calories|without_summary"
        r"|several_summaries"
    ),
    # Why closed days lack a Verlauf expenditure (#3157).
    "nutrition_expenditure_gaps": re.compile(
        r"status|days|with_total|emptied_by_refresh_mark|without_total"
        r"|with_active_only"
    ),
    "garmin_past_workout_removal": re.compile(
        r"status|one_time_done|checked|removed|last_run_hours|last_outcome"
        r"|past_links_pending"
    ),
    "stream_backfill": re.compile(
        r"status|(garmin|wahoo)\.(candidates|with_streams|stored|no_file|no_streams"
        r"|skipped_by_error|blocked|cursor|finished)"
    ),
    # Per recording origin and sport (their vocabularies, in backend/canonical):
    # files, files per FIT message or field number, and the counts of the new
    # decodes (#2220).
    "fit_inventory": re.compile(
        r"status|(garmin|wahoo|strava|zwift|manual|upload|unknown)\."
        r"(ride|run|swim|walk|hike|strength|row|nordic_ski|alpine_ski|yoga|other"
        r"|unknown)\.(files|(messages|unmapped_fields|developer_fields)\.[0-9.]+"
        r"|new_decodes\.[a-z_]+(\.(in_bounds|out_of_bounds))?)"
    ),
    # Window records the next detail mapper bump downloads again (#2220).
    "mapper_replay": re.compile(
        r"status|garmin\.(window_days|pending|passed_over|records|strength|daily_budget"
        r"|per_refresh|replayed_today|clean_today)"
        r"|wahoo\.(window_days|records)"
    ),
}
# Sums over the Coach history (`coach_commands`: the newest 20 commands, at most
# 40 steps each) for the local-first checks of #3080: how each searched food
# term was answered, records created beside an existing one, and steps refused
# for an existing record (a retried refusal counts again). Counts only: no
# message, id, name, date or single step is printed. The session resolutions
# are in `--section runtime` (session_reports). The names are the backend's;
# tests/test_prod_checks.py pins them to it.
LOOKUP_OUTCOMES = ("local_hit", "external_after_miss", "external_requested")
DESPITE_TOOLS = ("manage_training_templates", "save_competition")
EXISTING_RECORD_REASONS = (
    "template_exists",
    "template_exists_in_call",
    "competition_exists",
)
STATES = {
    "ready",
    "building",
    "pending",
    "ok",
    "skipped",
    "finished",
    "in_progress",
    "not_started",
    "failed",
    "unavailable",
    "blocked",
}
FAIL_STATES = {"failed", "unavailable", "blocked"}


def hours_since(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (now - then).total_seconds() / 3600


def numbers(value: Any) -> dict[str, int | float]:
    """Only numeric leaves: summaries may hold text that must not be printed."""
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(item, int | float) and not isinstance(item, bool)
    }


def plan_errors(jobs: list[dict[str, Any]]) -> dict[str, int]:
    """Error kinds (never error text) of the recent plan-sync jobs and their items."""
    kinds: dict[str, int] = {}
    for job in jobs:
        for item in [job, *(job.get("items") or [])]:
            if kind := error_kind(item):
                kinds[kind] = kinds.get(kind, 0) + 1
    return kinds


def error_kind(item: dict[str, Any]) -> str:
    """The error class as an identifier, `?` for anything else, `` for none."""
    kind = str(item.get("error_class") or "")
    return kind if not kind or ERROR_KIND.fullmatch(kind) else "?"


def word(value: Any) -> str:
    """A provider or status word; `?` for anything else, so no text is printed."""
    text = str(value or "")
    return text if WORD.fullmatch(text) else "?"


def plan_pushes(push_counts: Any) -> dict[str, dict[str, int]]:
    """Per provider: job and item statuses and error kinds (#2849, #2878).

    Read from the report's ``plan_sync.push_counts``: every push of the count
    window, settled items included. Providers and statuses print as short
    words, error kinds as identifiers, anything else as ``?``.
    """

    def kind_name(kind: Any) -> str:
        return error_kind({"error_class": kind})

    result: dict[str, dict[str, int]] = {}
    for provider, groups in (
        push_counts if isinstance(push_counts, dict) else {}
    ).items():
        counts = result.setdefault(word(provider), {})
        for group, label, name in (
            ("jobs", "job", word),
            ("items", "item", word),
            ("job_errors", "job error", kind_name),
            ("item_errors", "item error", kind_name),
        ):
            values = groups.get(group) if isinstance(groups, dict) else None
            for value, count in numbers(values).items():
                if math.isfinite(count):
                    key = f"{label} {name(value)}"
                    counts[key] = counts.get(key, 0) + int(count)
    return result


def push_error_kinds(pushes: dict[str, dict[str, int]]) -> dict[str, int]:
    """Error kinds of every push of the window, all providers and levels."""
    kinds: dict[str, int] = {}
    for counts in pushes.values():
        for key, count in counts.items():
            if " error " in key:
                kind = key.split(" error ", 1)[1]
                kinds[kind] = kinds.get(kind, 0) + count
    return kinds


def stuck_count(plan: dict[str, Any], now: datetime) -> int | None:
    """Stuck plan-push jobs: every provider's from the report's ``stuck_jobs``
    (#2953), else among the recent jobs; None when the section is unavailable.
    """
    if plan.get("status") == "unavailable":
        return None
    reported = plan.get("stuck_jobs")
    if isinstance(reported, dict):
        return int(sum(numbers(reported).values()))
    return stuck_jobs(plan.get("recent_jobs") or [], now)


def stuck_jobs(jobs: list[dict[str, Any]], now: datetime) -> int:
    return sum(
        1
        for job in jobs
        if job.get("status") in OPEN_JOB
        and (hours_since(job.get("created_at"), now) or 0) > STUCK_HOURS
    )


def extract(report: dict[str, Any], now: datetime) -> dict[str, Any]:
    database = report.get("database") or {}
    sync = report.get("sync") or {}
    plan = report.get("plan_sync") or {}
    jobs = plan.get("recent_jobs") or []
    calendar = report.get("external_calendar") or {}
    pushes = plan_pushes(plan.get("push_counts"))
    runtime = report.get("runtime") or {}
    memory = numbers(runtime.get("process_memory"))
    copies = report.get("recovery_copies") or {}
    storage = {name: database.get(name) for name in STORAGE}
    storage |= {name: runtime.get(name) for name in ("uptime_s", "malloc_arena_max")}
    storage |= {f"recovery_{name}": value for name, value in copies.items()}
    return {
        "kind": "diagnostics",
        "generated_at": now.isoformat(timespec="seconds"),
        "version": (report.get("app") or {}).get("version"),
        "schema": database.get("schema_version"),
        "sync_error_set": bool(sync.get("last_error")),
        "providers": {
            name: {"last_sync_hours": hours_since(times.get("last_sync_at"), now)}
            for name, times in (sync.get("providers") or {}).items()
            if isinstance(times, dict)
        },
        "calendar_sync_hours": hours_since(calendar.get("last_sync_at"), now)
        if calendar.get("configured")
        else None,
        "counts": {name: database.get(name) for name in COUNTS},
        "library_state": numbers(database.get("workout_library_state")),
        "unsynced_total": plan.get("unsynced_total"),
        "units_with_error": sum(
            1 for unit in plan.get("unsynced_units") or [] if unit.get("has_error")
        ),
        # Error kinds of the count window when the report has it (#2878).
        "plan_error_classes": push_error_kinds(pushes)
        if "push_counts" in plan
        else plan_errors(jobs),
        "plan_pushes": pushes,
        "push_counts_reported": "push_counts" in plan,
        "stuck_jobs": stuck_count(plan, now),
        "memory": {key: memory[key] for key in MEMORY if key in memory},
        "storage": numbers(storage),
        "backup": backup_state(report.get("backup")),
    }


def leaves(value: Any, path: str = "") -> list[tuple[str, Any]]:
    """The dotted path and value of every leaf, sorted by path."""
    if not isinstance(value, dict):
        return [(path, value)]
    return [
        leaf
        for key in sorted(value, key=str)
        for leaf in leaves(value[key], f"{path}.{key}" if path else str(key))
    ]


def printable(value: Any) -> bool:
    if isinstance(value, str):
        return value in STATES
    return value is None or isinstance(value, int | float)


def aged(leaf: tuple[str, Any], now: datetime) -> tuple[str, Any]:
    """A `*_at` time as its age `*_hours` (none yet: null); every other leaf unchanged."""
    path, value = leaf
    if not path.endswith("_at"):
        return leaf
    age = hours_since(value if isinstance(value, str) else None, now)
    return path[:-3] + "_hours", None if age is None else round(age, 1)


def section_lines(
    report: dict[str, Any], name: str, now: datetime | None = None
) -> list[tuple[str, str]]:
    """The allow-listed fields of one section, sorted by path."""
    section = report.get(name)
    if not isinstance(section, dict):
        return [("FAIL", f"section {name} missing")]
    allowed = SECTION_FIELDS[name]
    lines = []
    for leaf in leaves(section):
        path, value = aged(leaf, now or datetime.now(UTC))
        if allowed.fullmatch(path) and printable(value):
            state = "FAIL" if value in FAIL_STATES else "info"
            lines.append((state, f"{name}.{path} {json.dumps(value)}"))
    return lines or [("info", f"section {name} empty")]


def local_first_counts(commands: list[Any]) -> dict[str, int]:
    """The #3080 counts over the history's steps; every named count, zeros too."""
    counts = dict.fromkeys(
        [
            *(f"lookup_outcomes.{name}" for name in LOOKUP_OUTCOMES),
            *(f"created_despite_candidate.{name}" for name in DESPITE_TOOLS),
            *(f"refusals.{name}" for name in EXISTING_RECORD_REASONS),
        ],
        0,
    )
    counted = [command for command in commands if isinstance(command, dict)]
    counts["commands"] = len(counted)
    steps = [
        step
        for command in counted
        for step in command.get("steps") or ()
        if isinstance(step, dict)
    ]
    for step in steps:
        for name, value in step_counts(step):
            counts[name] += value
    return counts


def step_counts(step: dict[str, Any]) -> list[tuple[str, int]]:
    """The named counts one history step adds; unknown names add nothing."""
    outcomes = step.get("lookup_outcomes")
    added = [
        (f"lookup_outcomes.{name}", value)
        for name, value in (outcomes if isinstance(outcomes, dict) else {}).items()
        if name in LOOKUP_OUTCOMES and type(value) is int
    ]
    despite = step.get("created_despite_candidate")
    if step.get("tool") in DESPITE_TOOLS and type(despite) is int:
        added.append((f"created_despite_candidate.{step['tool']}", despite))
    error = step.get("error")
    reason = error.get("reason") if isinstance(error, dict) else None
    if reason in EXISTING_RECORD_REASONS:
        added.append((f"refusals.{reason}", 1))
    return added


def local_first_lines(report: dict[str, Any]) -> list[tuple[str, str]]:
    commands = report.get("coach_commands")
    if not isinstance(commands, list):
        return [("FAIL", "section coach_commands missing")]
    return [
        ("info", f"coach_local_first.{name} {value}")
        for name, value in sorted(local_first_counts(commands).items())
    ]


# Sections `--section` derives from the report instead of printing its leaves.
DERIVED_SECTIONS = {"coach_local_first": local_first_lines}


def named_section_lines(report: dict[str, Any], name: str) -> list[tuple[str, str]]:
    derived = DERIVED_SECTIONS.get(name)
    return derived(report) if derived else section_lines(report, name)


def mark(ok: bool, text: str) -> tuple[str, str]:
    return ("ok" if ok else "FAIL", text)


def expected(name: str, value: Any, wanted: Any) -> tuple[str, str]:
    suffix = "" if wanted is None else f" (expected {wanted})"
    return mark(wanted in (None, value), f"{name} {value}{suffix}")


def freshness_lines(result: dict[str, Any]) -> list[tuple[str, str]]:
    lines = []
    for name, ages in result["providers"].items():
        age = ages["last_sync_hours"]
        # Intervals.icu is output-only, and an unconfigured provider never syncs.
        if age is None or name == "intervals":
            text = "never" if age is None else f"{age:.1f} h ago"
            lines.append(("info", f"provider {name} last sync {text}"))
        else:
            lines.append(
                mark(age <= STALE_HOURS, f"provider {name} last sync {age:.1f} h ago")
            )
    age = result["calendar_sync_hours"]
    if age is not None:
        lines.append(mark(age <= STALE_HOURS, f"calendar last sync {age:.1f} h ago"))
    return lines


def plan_line(result: dict[str, Any]) -> tuple[str, str]:
    kinds = ", ".join(
        f"{k} {v}" for k, v in sorted(result["plan_error_classes"].items())
    )
    text = (
        f"plan sync: {result['unsynced_total']} unsynced, "
        f"{result['units_with_error']} with error"
    )
    return ("info", f"{text}; error kinds: {kinds}" if kinds else text)


def push_lines(result: dict[str, Any]) -> list[tuple[str, str]]:
    """One line per provider of the plan pushes of the count window, sorted."""
    if not result.get("push_counts_reported"):
        return [("info", "plan push counts: not in this report")]
    lines = []
    for provider, counts in sorted((result.get("plan_pushes") or {}).items()):
        text = ", ".join(f"{key} {count}" for key, count in sorted(counts.items()))
        lines.append(("info", f"plan push {provider}: {text}"))
    return lines


def comparison_lines(
    result: dict[str, Any], previous: dict[str, Any] | None
) -> list[tuple[str, str]]:
    """Record counts must not drop against the previous result.

    Memory and storage (database, WAL, free space, uptime, arena limit,
    recovery copies) are shown with the previous value.

    The calendar sync refills external_calendar_events from today on every
    refresh, so a drop there is no data loss.
    """
    before = (previous or {}).get("counts") or {}
    lines = []
    for name, count in result["counts"].items():
        old = before.get(name)
        if isinstance(count, int) and isinstance(old, int):
            line = mark(count >= old, f"count {name} {old} -> {count}")
            calendar = name == "external_calendar_events"
            lines.append(("info", line[1]) if calendar else line)
        else:
            lines.append(("info", f"count {name} {count}"))
    return lines + shown_lines(result, previous)


def shown_lines(
    result: dict[str, Any], previous: dict[str, Any] | None
) -> list[tuple[str, str]]:
    """Memory and storage values, each with the previous release's value."""
    lines = []
    for group in ("memory", "storage"):
        old = (previous or {}).get(group) or {}
        for name, value in (result.get(group) or {}).items():
            was = old.get(name)
            suffix = "" if was is None else f" (was {was})"
            lines.append(("info", f"{group} {name} {value}{suffix}"))
    return lines


def backup_state(section: Any) -> dict[str, Any]:
    """Known, verified, age and size of the newest app backup; nothing else."""
    if not isinstance(section, dict):
        return {"known": False}
    state: dict[str, Any] = {"known": section.get("known") is True}
    state["verified"] = section.get("verified") is True
    state |= numbers({key: section.get(key) for key in ("age_hours", "bytes")})
    return state


def backup_line(result: dict[str, Any], max_hours: float | None) -> tuple[str, str]:
    """The newest app backup; with ``max_hours`` it must be younger and checked.

    A schema release needs a backup younger than 24 h whose check passed
    (#2996); an unknown backup fails then, so the check never passes blind.
    """
    state = result.get("backup") or {"known": False}
    age = state.get("age_hours")
    text = (
        f"backup {age} h old, {state.get('bytes')} bytes, verified {state.get('verified') is True}"
        if state.get("known") and age is not None
        else "backup unknown"
    )
    if max_hours is None:
        return ("info", text)
    # A future record time (a clock that jumped back) never counts as fresh.
    ok = bool(state.get("known") and state.get("verified") is True) and (
        age is not None and 0 <= age < max_hours
    )
    return mark(ok, f"{text} (needs under {max_hours:g} h, verified)")


def checks(
    result: dict[str, Any],
    previous: dict[str, Any] | None,
    expect_version: str | None,
    expect_schema: int | None,
    expect_backup_hours: float | None = None,
) -> list[tuple[str, str]]:
    return [
        expected("version", result["version"], expect_version),
        expected("schema", result["schema"], expect_schema),
        backup_line(result, expect_backup_hours),
        mark(
            not result["sync_error_set"],
            f"sync last_error set: {result['sync_error_set']}",
        ),
        *freshness_lines(result),
        mark(
            result["stuck_jobs"] == 0,
            f"stuck plan-sync jobs: {result['stuck_jobs'] if result['stuck_jobs'] is not None else 'unknown'}",
        ),
        plan_line(result),
        *push_lines(result),
        (
            "info",
            "workout library: "
            + (
                ", ".join(
                    f"{k} {v}" for k, v in sorted(result["library_state"].items())
                )
                or "no counts"
            ),
        ),
        *comparison_lines(result, previous),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--expect-version")
    parser.add_argument("--expect-schema", type=int)
    parser.add_argument("--expect-backup-hours", type=float)
    parser.add_argument("--out")
    parser.add_argument("--compare")
    parser.add_argument("--ca-file")
    parser.add_argument(
        "--section",
        action="append",
        choices=sorted({*SECTION_FIELDS, *DERIVED_SECTIONS}),
    )
    args = parser.parse_args(argv)
    checks_asked = (
        args.out,
        args.compare,
        args.expect_version,
        args.expect_schema,
        args.expect_backup_hours,
    )
    if args.section and any(value is not None for value in checks_asked):
        parser.error("--section prints sections; it runs no checks or results")
    try:
        previous = read_result(args.compare)
        session = Session(args.url, args.ca_file)
        health = session.get_json("/api/health")
        healthy = isinstance(health, dict) and health.get("status") == "ok"
        print(f"{'ok' if healthy else 'FAIL'} health")
        session.login(args.password_file)
        try:
            report = session.get_json("/api/diagnostics")
        finally:
            logout = session.logout()
            print(f"{'ok' if logout == 200 else 'FAIL'} logout {logout}")
        if not args.section:
            result = extract(report, datetime.now(UTC))
            write_result(args.out, result)
    except ProdError as exc:
        print(f"FAIL {exc}")
        return 2
    lines = (
        [line for name in args.section for line in named_section_lines(report, name)]
        if args.section
        else checks(
            result,
            previous,
            args.expect_version,
            args.expect_schema,
            args.expect_backup_hours,
        )
    )
    for state, text in lines:
        print(f"{state} {text}")
    failed = not healthy or logout != 200 or any(s == "FAIL" for s, _ in lines)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
