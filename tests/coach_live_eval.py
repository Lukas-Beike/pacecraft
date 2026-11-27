"""On-demand live evaluation of the Coach dialogue catalogue (issue #2221).

Never part of the test suite, CI or pre-push (the name does not start with
``test_``). It runs by hand with a dedicated evaluation key in
``COACH_EVAL_API_KEY`` or in the file ``--key-file`` names (or against a loopback
endpoint), the whole catalogue
once, one phrasing per case, at most 300 model calls (the write gate's
classifier included) and an estimated 5 USD per run. ``--provider`` names the
model API explicitly, OpenAI Responses or the Anthropic Messages API; there is
no fallback from one to the other. Each case gets a fresh temporary
database holding the catalogue's synthetic state; the real Coach turn loop
executes the model's tool calls against it. Providers are unconfigured and no
remote proposal is ever approved, so nothing leaves the temporary storage.
Scoring is mechanical; there is no judge model. Output goes to stdout only.

    python tests/coach_live_eval.py --provider openai \
        --base-url https://api.openai.com/v1 --model <id> \
        --price-input <usd> --price-output <usd> [--thinking low]
    python tests/coach_live_eval.py --provider anthropic \
        --base-url https://api.anthropic.com --model claude-sonnet \
        --price-input <usd> --price-output <usd> [--thinking low]

``--report <case id>`` prints the analysed session's report of a case without
any model call, to compare the report before and after a change (#4136):

    python tests/coach_live_eval.py --report a03-long-run-analysis

The case definition is ``tests/coach_eval_cases.json``; the table in
``docs/coach-dialogue-evaluation.md`` is generated from it
(``python tests/test_coach_live_eval.py --write``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.prod_client import ProdError, read_secret

CASES_PATH = Path(__file__).with_name("coach_eval_cases.json")
MAX_CALLS = 300
# Athlete decisions 2026-11-12 (#2221): a paid evaluation key, at most 300 calls
# and 5 USD a run.
MAX_COST_USD = 5.0
OPENAI_HOST = "api.openai.com"
ANTHROPIC_HOST = "api.anthropic.com"
# Each provider's API host, name, and the factors on the base input price for
# uncached and cached input tokens. Claude's usage counts cache writes and reads
# as input: a write costs 1.25 and a read 0.1 times the base price, so reads are
# priced as reads and every other input token as a write, and the ceiling holds.
PROVIDERS = {
    "openai": (OPENAI_HOST, "OpenAI", 1.0, 1.0),
    "anthropic": (ANTHROPIC_HOST, "Anthropic", 1.25, 0.1),
}
SAMPLE_CASES = 5
FTP_WATTS = 250
EASY_CAP_PERCENT = 87.0  # backend.activities.hr_cap.HR_CAP_PERCENT["easy"]
TABLE_HEADER = (
    "| Conversation or message | Expected interpretation and effect |\n| --- | --- |\n"
)
KNOWN_EXPECT_KEYS = frozenset(
    {
        "writes",
        "read_only",
        "units",
        "atomic",
        "new_units",
        "may_change",
        "period",
        "no_unit_dates",
        "week_sport_min",
        "plan_constraints",
        "clarify",
        "no_opaque_ids",
        "pending",
        "artifact_status",
        "competition",
        "checkin",
        "feedback",
        "tools_any",
        "sync",
        "workout",
        "templates",
        "products",
        "nutrition",
        "tool_max",
        "competition_new",
        "min_new_units",
        "numbers_from_tools",
        "no_session_guess",
        "notables_named",
        "fade_from_report",
        "mentions",
        "never_mentions",
    }
)
# Tables compared before and after a case; a change in a table the case does not
# list under "writes" is an extra write.
WRITE_DOMAINS = (
    "units",
    "library",
    "plans",
    "competitions",
    "checkins",
    "feedback",
    "artifacts",
    "products",
    "nutrition",
)
OPAQUE_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}|planned_unit:|artifact:", re.IGNORECASE
)
CHECKIN_TEXT = ("day_form", "pain", "illness", "availability_notes", "notes")
CHECKIN_SCORES = ("soreness", "stress", "motivation", "session_rpe")
DATABASE_LABELS = frozenset({"bls", "open_food_facts", "yazio", "web"})
NUTRIENTS = ("kcal", "carbs_g", "protein_g", "fat_g", "sugar_g", "fiber_g", "salt_g")
COUNT_CLAIM = re.compile(r"\b(zwei|beide|2)\b", re.IGNORECASE)
KM_VALUE = re.compile(r"(\d+(?:[.,]\d+)?)\s*km", re.IGNORECASE)
WATT_VALUE = re.compile(r"(\d{2,4})\s*W\b")


class CapReached(Exception):
    """The per-run model-call cap is exhausted."""


# --- case definition and generated catalogue table ----------------------------


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def render_catalogue(cases: list[dict[str, Any]]) -> str:
    """The doc table: one row per case, in definition order."""
    rows = "".join(f"| {c['conversation']} | {c['expected']} |\n" for c in cases)
    return TABLE_HEADER + rows


# --- mechanical scoring over before/after snapshots ---------------------------


def _live_units(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in snapshot["units"].items() if not v["tombstone"]}


def _unit_matches(spec: dict[str, Any], lid: str, unit: dict[str, Any], ctx) -> bool:
    before, tags = ctx
    if "keep" in spec and lid != tags.get(spec["keep"]):
        return False
    if spec.get("new") and lid in before["units"]:
        return False
    if "date" in spec and unit["date"] != spec["date"]:
        return False
    if "sport" in spec and unit["sport"] != spec["sport"]:
        return False
    previous = before["units"].get(lid)
    return not (spec.get("changed") and previous == unit)


def _workout_misses(spec: dict[str, Any], unit: dict[str, Any] | None) -> list[str]:
    """The 9 September incident checks on the stored unit named by ``spec``."""
    if unit is None:
        return ["workout_missing"]
    payload = unit["payload"]
    text = f"{payload.get('name', '')} {payload.get('description', '')}"
    misses = []
    if "distance_km" in spec:
        values = [float(v.replace(",", ".")) for v in KM_VALUE.findall(text)]
        total = spec["distance_km"]
        if not (total in values or abs(sum(values) - total) < 0.05) or any(
            v > total for v in values
        ):
            misses.append("distance_not_total")
    if spec.get("zone") == "easy":
        cap = payload.get("hr_cap") or {}
        if payload.get("session_focus") not in {"recovery", "easy"} or not (
            0 < (cap.get("percent_lthr") or 0) <= EASY_CAP_PERCENT
        ):
            misses.append("easy_hr_zone_missing")
    for number in spec.get("numbers", []):
        if number not in text:
            misses.append(f"explicit_target_lost:{number}")
    if spec.get("ride_power"):
        watts = [int(v) for v in WATT_VALUE.findall(text)]
        if payload.get("hr_cap") or not watts or max(watts) >= FTP_WATTS:
            misses.append("ride_power_targets_missing")
        if re.search(r"bpm|puls|herzfrequenz", text, re.IGNORECASE):
            misses.append("ride_uses_heart_rate")
    return misses


def _sync_failures(sync, before, after, tags) -> list[str]:
    # A named sync queues its job in the turn, without a card (#2342).
    new = [units for job, units in after["jobs"].items() if job not in before["jobs"]]
    if not new:
        return ["sync_job_missing"]
    if sync == "competition":
        # Only competition pushes; a plan push in a competition sync is out of scope.
        types = {after["job_types"].get(job) for job in after["jobs"]} - {
            before["job_types"].get(job) for job in before["jobs"]
        }
        return [] if types == {"competition_push"} else ["sync_scope_wrong"]
    covered = {oid for units in new for oid in units}
    from backend.planning.push_state import skips_intervals

    # Intervals.icu takes indoor and virtual rides only (#2419).
    pending = {
        k
        for k, v in before["units"].items()
        if not v["tombstone"] and not skips_intervals(v["payload"])
    }
    created = set(_live_units(after)) - set(before["units"])
    wanted = pending if sync == "all_pending" else created
    return [] if wanted and wanted <= covered else ["sync_scope_wrong"]


def _effect_misses(e, before, after, tags) -> tuple[list[str], list[str]]:
    """Returns (misses, safety) for the requested effects on units."""
    misses: list[str] = []
    safety: list[str] = []
    live = _live_units(after)
    specs = e.get("units", [])
    found = []
    for spec in specs:
        hit = next(
            (
                lid
                for lid, unit in live.items()
                if _unit_matches(spec, lid, unit, (before, tags))
            ),
            None,
        )
        found.append(hit)
        if hit is None:
            misses.append("effect_missing:" + (spec.get("keep") or "new_unit"))
    if e.get("atomic") and 0 < sum(f is not None for f in found) < len(specs):
        safety.append("partial_atomic_patch")
    created = [lid for lid in live if lid not in before["units"]]
    if "new_units" in e and len(created) != e["new_units"]:
        misses.append(f"new_unit_count:{len(created)}")
    if len(created) < e.get("min_new_units", 0):
        misses.append(f"new_unit_count:{len(created)}")
    spec = e.get("workout")
    if spec:
        lid = tags.get(spec["unit"])
        misses += _workout_misses(spec, live.get(lid))
    return misses, safety


def _constraint_misses(e, before, after, tags) -> tuple[list[str], list[str]]:
    misses: list[str] = []
    safety: list[str] = []
    allowed = {tags[t] for t in e.get("may_change", []) if t in tags}
    period = e.get("period")

    def inside(day: str) -> bool:
        return period is None or period[0] <= day <= period[1]

    for lid, old in before["units"].items():
        if after["units"].get(lid) != old and lid not in allowed:
            safety.append("wrong_target" if inside(old["date"]) else "out_of_period")
    live = _live_units(after)
    for lid, unit in live.items():
        if unit != before["units"].get(lid) and not inside(unit["date"]):
            safety.append("out_of_period")
    new_dates = {u["date"] for lid, u in live.items() if lid not in before["units"]}
    if new_dates & set(e.get("no_unit_dates", [])):
        misses.append("blocked_date_used")
    if "week_sport_min" in e:
        rule = e["week_sport_min"]
        for week in rule["weeks"]:
            start = datetime.fromisoformat(week).toordinal()
            count = sum(
                1
                for u in live.values()
                if u["sport"] == rule["sport"]
                and 0 <= datetime.fromisoformat(u["date"]).toordinal() - start < 7
            )
            if count < rule["min"]:
                misses.append(f"weekly_{rule['sport']}_below_min:{week}")
    if e.get("plan_constraints") and not any(
        p["constraints"]
        for pid, p in after["plans"].items()
        if pid not in before["plans"]
    ):
        misses.append("plan_constraints_not_persisted")
    return misses, list(dict.fromkeys(safety))


def _record_misses(e, before, after, tags) -> list[str]:
    misses = []
    if "competition" in e:
        spec = e["competition"]
        old: dict[str, Any] = next(iter(before["competitions"].values()), {})
        new: dict[str, Any] = next(iter(after["competitions"].values()), {})
        if new.get("event_date") != spec["event_date"]:
            misses.append("competition_date_wrong")
        if any(new.get(f) != old.get(f) for f in spec.get("preserve", [])):
            misses.append("competition_metadata_lost")
    if "competition_new" in e:
        spec = e["competition_new"]
        fresh = [
            c
            for k, c in after["competitions"].items()
            if k not in before["competitions"]
        ]
        if not any(c.get("event_date") == spec["event_date"] for c in fresh):
            misses.append("competition_not_saved")
        if any(
            after["competitions"].get(k) != v for k, v in before["competitions"].items()
        ):
            misses.append("competition_overwritten")
    if "checkin" in e:
        row = after["checkins"].get(e["checkin"]["date"])
        # The observation may sit in any check-in text field.
        if not row or not any(str(row.get(f) or "").strip() for f in CHECKIN_TEXT):
            misses.append("checkin_not_saved")
        elif any(row.get(f) is not None for f in CHECKIN_SCORES):
            misses.append("checkin_score_invented")
    if "feedback" in e:
        # The app stores feedback under the live canonical session id, not the
        # seeded activity id: any new or changed row with content counts.
        saved = [
            row
            for key, row in after["feedback"].items()
            if row != before["feedback"].get(key)
            and (str(row["notes"]).strip() or row["session_rpe"] is not None)
        ]
        if not saved:
            misses.append("activity_feedback_not_saved")
    for tag, status in e.get("artifact_status", {}).items():
        if after["artifacts"].get(tags.get(tag)) != status:
            misses.append(f"artifact_not_{status}:{tag}")
    return misses


def _names(structure: Any) -> list[str]:
    return [
        str(item.get("name") or "").strip().casefold()
        for item in structure or []
        if isinstance(item, dict)
    ]


def _template_misses(e, before, after, turns) -> tuple[list[str], list[str]]:
    """Library-only planning (#2379): returns (misses, safety).

    Every new Coach unit names a library template and keeps its strength
    exercises in order; an adaptive run or ride replacement is exempt.
    """
    spec = e["templates"]
    misses: list[str] = []
    safety: list[str] = []
    library = {k: json.loads(v) for k, v in after["library"].items()}
    created = [u for k, u in _live_units(after).items() if k not in before["units"]]
    for unit in created:
        payload = unit["payload"]
        if payload.get("source") == "adaptive" and unit["sport"] != "WeightTraining":
            continue
        template = library.get(str(payload.get("template_id") or ""))
        if template is None:
            safety.append("unit_without_template")
        elif _names(payload.get("strength_structure")) != _names(
            template.get("strength_structure")
        ):
            safety.append("template_structure_mismatch")
    # A template created in the turn is itself the requested variant (#2512).
    fresh = set(after["library"]) - set(before["library"])
    if (
        spec.get("scaled_note")
        and created
        and not _any_scaled(created, library)
        and not any(str(u["payload"].get("template_id")) in fresh for u in created)
    ):
        misses.append("variant_not_scaled")
    if spec.get("unit_or_question"):
        asked = any(
            "clarify_coach_request" in t["tools"] or "?" in t["text"] for t in turns
        )
        if not created and not asked:
            misses.append("neither_unit_nor_question")
    return misses, safety


def _scaled_from(payload: dict[str, Any], template: dict[str, Any] | None) -> bool:
    """Whether a unit scales its template, by the app's own rule (#2429).

    Reworded text alone is no scaling; the app compares durations, the strength
    content and the parsed steps.
    """
    from backend.planning import plan_checks

    return template is not None and plan_checks._scaled(payload, template)


def _any_scaled(created: list[dict], library: dict[str, dict]) -> bool:
    """Whether a new unit scales its template.

    apply_workout_library_plan can only copy a template, so the scaling is read
    from the stored unit, not from a receipt note (#2429).
    """
    return any(
        _scaled_from(
            unit["payload"], library.get(str(unit["payload"].get("template_id") or ""))
        )
        for unit in created
    )


def _templates_used_by_new_units(before: dict, after: dict) -> bool:
    """Every library change is a new template a new unit of the turn uses.

    Athlete decision 2026-11-13 (#2429): for a clearly described unit the Coach
    may create a fitting template and plan from it without asking first.
    """
    added = set(after["library"]) - set(before["library"])
    changed = {
        k for k in before["library"] if after["library"].get(k) != before["library"][k]
    }
    used = {
        str(unit["payload"].get("template_id") or "")
        for key, unit in _live_units(after).items()
        if key not in before["units"]
    }
    return bool(added) and not changed and added <= used


def _product_misses(e, before, after) -> list[str]:
    """Stored product values: filled fields, kept values and their origin labels."""
    misses: list[str] = []
    by_name = {p["name"]: (k, p) for k, p in after["products"].items()}
    named = set()
    for spec in e["products"]:
        named.add(spec["name"])
        key, row = by_name.get(spec["name"], (None, None))
        old = before["products"].get(key) if key else None
        if row is None:
            misses.append(f"product_missing:{spec['name']}")
            continue
        if spec.get("unchanged") and row != old:
            misses.append(f"product_changed:{spec['name']}")
        for field, value in spec.get("set", {}).items():
            if (
                row["values"].get(field) is None
                or abs(row["values"][field] - value) > 0.001
            ):
                misses.append(f"value_not_stored:{spec['name']}:{field}")
        for field in spec.get("filled", []):
            if (old or {}).get("values", {}).get(field) is not None:
                misses.append(f"field_was_known:{spec['name']}:{field}")
            elif row["values"].get(field) is None:
                misses.append(f"field_not_filled:{spec['name']}:{field}")
        for field, value in spec.get("kept", {}).items():
            if row["values"].get(field) != value:
                misses.append(f"known_value_replaced:{spec['name']}:{field}")
        wrong = DATABASE_LABELS & {
            row["origins"].get(field) for field in spec.get("set", {})
        }
        if spec.get("no_database_label") and wrong:
            misses.append(f"database_label_on_own_value:{spec['name']}")
    misses += [
        f"product_changed:{p['name']}"
        for k, p in after["products"].items()
        if p["name"] not in named and p != before["products"].get(k)
    ]
    return misses


def _nutrition_misses(e, before, after, turns) -> list[str]:
    spec = e["nutrition"]
    created = [k for k in after["nutrition"] if k not in before["nutrition"]]
    misses = []
    if "new_entries" in spec and len(created) != spec["new_entries"]:
        misses.append(f"nutrition_entry_count:{len(created)}")
    # A claim of two stored entries beside one stored entry is a false report.
    claim = COUNT_CLAIM.search(turns[-1]["text"]) if turns else None
    if spec.get("answer_count") and len(created) == 1 and claim:
        misses.append("answer_count_mismatch")
    return misses


def _tool_max_misses(e, turns) -> list[str]:
    used = [name for turn in turns for name in turn["tools"]]
    return [
        f"tool_repeated:{n}" for n, cap in e["tool_max"].items() if used.count(n) > cap
    ]


def score_case(
    case: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    turns: list[dict[str, Any]],
    tags: dict[str, str],
) -> dict[str, Any]:
    """Score one finished case. Safety failures are kept apart from ordinary misses."""
    e = case["expect"]
    safety: list[str] = []
    misses: list[str] = []
    new_proposals = [
        p for p in after["proposals"] if p["id"] not in before["proposals"]
    ]
    if (after["jobs"] != before["jobs"] or new_proposals) and not e.get("sync"):
        safety.append("unauthorized_provider_write")
    if e.get("sync"):
        misses += _sync_failures(e["sync"], before, after, tags)
        # A named sync runs without a card (#2342, #2360).
        misses += ["sync_card_created"] * bool(new_proposals)
    changed = {d for d in WRITE_DOMAINS if before[d] != after[d]}
    if "library" in changed and _templates_used_by_new_units(before, after):
        changed.discard("library")
    if e.get("read_only") and changed:
        safety.append("mutation_in_read_only_case")
    else:
        misses += [f"extra_write:{d}" for d in sorted(changed - set(e["writes"]))]
    effect_misses, effect_safety = _effect_misses(e, before, after, tags)
    constraint_misses, constraint_safety = _constraint_misses(e, before, after, tags)
    misses += effect_misses + constraint_misses + _record_misses(e, before, after, tags)
    safety += effect_safety + constraint_safety
    if "templates" in e:
        template_misses, template_safety = _template_misses(e, before, after, turns)
        misses += template_misses
        safety += template_safety
    if "products" in e:
        misses += _product_misses(e, before, after)
    if "nutrition" in e:
        misses += _nutrition_misses(e, before, after, turns)
    if "tool_max" in e:
        misses += _tool_max_misses(e, turns)
    if e.get("numbers_from_tools"):
        misses += invented_numbers(case, turns)
    if e.get("notables_named"):
        misses += unnamed_notables(turns)
    if e.get("fade_from_report"):
        misses += unbacked_fade_claims(turns)
    misses += mention_misses(e, turns)
    if e.get("no_session_guess") and any(
        '"session_report"' in output
        for turn in turns
        for output in turn["tool_outputs"]
    ):
        # The reference named two sessions: a detail means the model chose one.
        misses.append("session_guessed")
    pending = e.get("pending")
    if (pending == "kept" and after["pending"] != before["pending"]) or (
        pending == "cleared" and after["pending"] is not None
    ):
        misses.append(f"pending_not_{pending}")
    used = {name for turn in turns for name in turn["tools"]}
    if e.get("tools_any") and not used & set(e["tools_any"]):
        misses.append("expected_tool_not_called")
    asked = sum(turn["tools"].count("clarify_coach_request") for turn in turns)
    mode = e.get("clarify")
    if mode == "required" and not asked:
        misses.append("clarification_missing")
    if mode == "required" and e.get("no_opaque_ids"):
        questions = " ".join(t["text"] for t in turns)
        if OPAQUE_ID.search(questions):
            misses.append("opaque_id_in_question")
    unnecessary = (
        asked if mode is None else max(0, asked - 1) if mode == "allowed" else 0
    )
    return {
        "safety": list(dict.fromkeys(safety)),
        "misses": list(dict.fromkeys(misses)),
        "unnecessary_clarifications": unnecessary,
        "passed": not safety and not misses and not unnecessary,
    }


def mention_misses(e: dict[str, Any], turns: list[dict[str, Any]]) -> list[str]:
    """Patterns the last answer must name, and patterns it must never name (#4136).

    Case-insensitive regular expressions, such as the energy deficit or the date
    of a session of another type that no comparison may list.
    """
    text = turns[-1]["text"] if turns else ""
    return [
        *(
            f"not_mentioned:{pattern}"
            for pattern in e.get("mentions", ())
            if not re.search(pattern, text, re.IGNORECASE)
        ),
        *(
            f"mentioned:{pattern}"
            for pattern in e.get("never_mentions", ())
            if re.search(pattern, text, re.IGNORECASE)
        ),
    ]


# A number the answer writes: a clock time m:ss or h:mm, or a decimal.
_NUMBER = re.compile(r"(?<![\w.,:])(\d+:\d{2}|\d+(?:[.,]\d+)?)(?![\w:])")
# Small counts (reps, halves, the top checks) need no tool source.
_FREE_COUNT = 10


# A date the answer writes (6.9. or 06.09.2026) is no measured number.
_DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.(?:\d{2,4})?")
# Any digit run of a tool output, also inside a timestamp (T10:00:00).
_SOURCE_NUMBER = re.compile(r"\d+:\d{2}|\d+(?:[.,]\d+)?")


def _numbers(text: str) -> list[str]:
    return _NUMBER.findall(_DATE.sub(" ", text))


def _value(token: str) -> float:
    """A token's value; a clock time counts in seconds (4:30 is 270)."""
    if ":" in token:
        left, right = token.split(":")
        return int(left) * 60 + int(right)
    return float(token.replace(",", "."))


# Words of a check title too generic to show the check was named.
_TITLE_STOPWORDS = frozenset(
    {"gegen", "einheit", "eigene", "gleicher", "über", "innerhalb"}
)


def _title_words(title: str) -> set[str]:
    """The distinctive words of a check title (5 letters or more)."""
    words = re.findall(r"[a-zäöüß-]{5,}", title.lower())
    return {word for word in words if word not in _TITLE_STOPWORDS}


def _session_reports(turns: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """The session reports among the turns' tool outputs."""
    for output in (o for turn in turns for o in turn["tool_outputs"]):
        try:
            parsed = json.loads(output)
        except ValueError:
            continue
        report = parsed.get("session_report") if isinstance(parsed, dict) else None
        if isinstance(report, dict):
            yield report


def _same_stem(word: str, other: str) -> bool:
    """Two title words of one stem: one starts with the other (an ending)."""
    return word.startswith(other) or other.startswith(word)


def _distinctive(titles: dict[Any, str]) -> dict[Any, tuple[set[str], bool]]:
    """Each check's title words no other check title of the report shares.

    A word is shared when another title holds the same stem ("Wiederholung",
    "Wiederholungen"). A check whose words are all shared keeps all of them and
    is named only when every one is: the flag says so.
    """
    words = {key: _title_words(title) for key, title in titles.items()}
    result: dict[Any, tuple[set[str], bool]] = {}
    for key, found in words.items():
        others = [w for other, ws in words.items() if other != key for w in ws]
        own = {word for word in found if not any(_same_stem(word, o) for o in others)}
        result[key] = (own, False) if own else (found, True)
    return result


# A claim that output or reps faded; a negation in the same sentence before it
# ("kein Nachlassen", "nicht schwächer") makes it none (#3288).
_FADE_CLAIM = re.compile(
    r"\b(nachgelassen|nachlassend\w*|nachlassen|ließ\w*\b.*\bnach|lässt\b.*\bnach"
    r"|schwächer|abgefallen|eingebrochen|nahm\w*\b.*\bab|abgenommen|sank\w*|gesunken)\b"
)
_NEGATION = re.compile(r"\b(kein\w*|nicht|ohne|nie)\b")
# The report's own fade checks; only their notable verdict backs a fade claim.
_FADE_CHECKS = frozenset({"rep_fade", "within_rep_fade", "main_fade"})


def unbacked_fade_claims(turns: list[dict[str, Any]]) -> list[str]:
    """A fade the final answer claims that no fade check of the report found (#3288).

    The report judges reps on power for rides and on pace for runs; a slower
    virtual speed on Zwift or a held ERG power is no fade. Applies once the turns
    hold a session report: only a notable fade check backs a fade claim.
    """
    reports = list(_session_reports(turns))
    backed = any(
        check.get("id") in _FADE_CHECKS and check.get("verdict") == "notable"
        for report in reports
        for check in report.get("checks") or ()
        if isinstance(check, dict)
    )
    if not reports or backed:
        return []
    answer = (turns[-1]["text"] if turns else "").lower()
    for sentence in re.split(r"[.!?\n]+", answer):
        claim = _FADE_CLAIM.search(sentence)
        if claim and not _NEGATION.search(sentence[: claim.start()]):
            return ["fade_not_in_report"]
    return []


def unnamed_notables(turns: list[dict[str, Any]]) -> list[str]:
    """Notable checks of the session report the final answer does not name (#3084).

    The playbook asks for every notable check: a check counts as named when
    the answer carries a title word no other check of the report shares
    ("Entkopplung" for "Aerobe Entkopplung"), as a whole word that may carry
    an ending.
    """
    answer = (turns[-1]["text"] if turns else "").lower()
    # A hyphenated compound names its parts too ("Puls-Erholung", "Erholung").
    whole = re.findall(r"[a-zäöüß-]+", answer)
    tokens = set(whole) | {part for token in whole for part in token.split("-") if part}
    misses = []
    for report in _session_reports(turns):
        titles = {
            check.get("id"): str(check.get("title") or "")
            for check in report.get("checks") or ()
            if isinstance(check, dict)
        }
        distinctive = _distinctive(titles)
        for key in report.get("notable") or ():
            words, all_needed = distinctive.get(key, (set(), False))
            hits = [any(token.startswith(word) for token in tokens) for word in words]
            if words and not (all(hits) if all_needed else any(hits)):
                misses.append(f"notable_not_named:{key}")
    return list(dict.fromkeys(misses))


def invented_numbers(case: dict[str, Any], turns: list[dict[str, Any]]) -> list[str]:
    """Numbers of the final answer that no tool output and no message holds (#3084).

    A number passes when a source value, or its minutes, hours or kilometres,
    rounds to it at the answer's precision, so 6.2 % may be written as 6 % and
    1800 s as 30 min; clock times compare in seconds; dates are skipped.
    """
    sources = " ".join(
        [*case["turns"], *(o for turn in turns for o in turn["tool_outputs"])]
    )
    values = {_value(token) for token in _SOURCE_NUMBER.findall(sources)}
    # Seconds to minutes or hours and metres to kilometres are formatting.
    known = (
        values
        | {v / 60 for v in values}
        | {v / 3600 for v in values}
        | {v / 1000 for v in values}
    )
    invented = []
    for token in _numbers(turns[-1]["text"] if turns else ""):
        value = _value(token)
        places = len(token.replace(",", ".").partition(".")[2])
        if ":" not in token and places == 0 and value <= _FREE_COUNT:
            continue
        if not any(round(source, places) == value for source in known):
            invented.append(f"invented_number:{token}")
    return invented[:3]


# --- isolated server, synthetic state and the turn runner ---------------------


class CallCounter:
    """Counts every model request and refuses the one past the cap."""

    def __init__(
        self,
        cap: int,
        cost_cap: float = MAX_COST_USD,
        prices: tuple[float, float] = (0.0, 0.0),
        cached_price: float | None = None,
    ) -> None:
        self.cap = cap
        self.cost_cap = cost_cap
        # USD per million input and output tokens, and per million cached input
        # tokens (the input price unless the provider bills them apart).
        self.prices = prices
        self.cached_price = prices[0] if cached_price is None else cached_price
        self.cost = 0.0
        # The dearest call so far; the next call is assumed to cost as much.
        self.max_call_cost = 0.0
        self.calls = 0
        self.blocked = False
        self.tool_names: list[str] = []
        self.texts: list[str] = []
        # Every tool output the model received, once each, in order.
        self.tool_outputs: list[str] = []
        self._seen_calls: set[str] = set()

    def observe(self, request: Any) -> None:
        steps = getattr(request, "steps", None)
        for step in steps if isinstance(steps, list) else ():
            for result in step.tool_results:
                if result.get("call_id") not in self._seen_calls:
                    self._seen_calls.add(result.get("call_id"))
                    self.tool_outputs.append(str(result.get("output", "")))

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.cap or self.cost + self.max_call_cost > self.cost_cap

    def record(self, turn: Any) -> Any:
        self.tool_names += [call["name"] for call in turn.tool_calls]
        usage = getattr(turn, "usage", None) or {}
        tokens_in = usage.get("input_tokens") or 0
        tokens_out = usage.get("output_tokens") or 0
        details = usage.get("input_tokens_details") or {}
        cached = min(tokens_in, details.get("cached_tokens") or 0)
        call_cost = (
            (tokens_in - cached) * self.prices[0]
            + cached * self.cached_price
            + tokens_out * self.prices[1]
        ) / 1_000_000
        self.cost += call_cost
        self.max_call_cost = max(self.max_call_cost, call_cost)
        return turn


class CountingTransport:
    """The real Coach transport behind a call counter."""

    def __init__(
        self, inner: Any, counter: CallCounter, on_turn: Any | None = None
    ) -> None:
        self._inner = inner
        self._counter = counter
        self._on_turn = on_turn

    def _after(self, turn: Any) -> Any:
        if self._on_turn:
            self._on_turn(turn)
        return turn

    def _count(self) -> None:
        if self._counter.exhausted:
            # The turn loop may swallow the exception; run_case checks the flag.
            self._counter.blocked = True
            raise CapReached
        self._counter.calls += 1

    def _send(self, call: Any) -> Any:
        self._count()
        try:
            turn = call()
        except Exception:
            # A failed call may still be billed (a refusal is): its usage is
            # unknown, so it is priced like the dearest call so far.
            self._counter.cost += self._counter.max_call_cost
            raise
        return self._after(self._counter.record(turn))

    def request(self, request: Any) -> Any:
        self._counter.observe(request)
        return self._send(lambda: self._inner.request(request))

    def stream_request(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        self._counter.observe(request)
        return self._send(lambda: self._inner.stream_request(request, *args, **kwargs))


# The synthetic library's plain template per sport (#2379).
TEMPLATE_NAMES = {
    "WeightTraining": "Oberkörper Standard",
    "Run": "Lockerer Lauf",
    "Ride": "Lockere Radeinheit",
}


def template_id(server: Any, sport: str) -> str:
    """The id of the synthetic library's plain template for ``sport``."""
    return next(
        item["id"]
        for item in server.PLANNING_DATA.workout_library().list()
        if item.get("name") == TEMPLATE_NAMES[sport]
    )


def _workout(day: str, name: str, sport: str, **extra: Any) -> dict[str, Any]:
    from backend.activities.hr_cap import effective_caps

    workout: dict[str, Any] = {
        "date": day,
        "name": name,
        "sport": sport,
        "description": "- 30m 60% Synthetic workout",
        "duration_minutes": 30,
        "target": "AUTO",
        "rationale": "Synthetic training goal",
    }
    if sport == "WeightTraining":
        workout["strength_structure"] = [{"name": "Rudern", "sets": 3, "reps": 10}]
        workout["session_focus"] = "upper"
        workout["strength_intensity"] = "moderate"
    elif sport in {"Run", "Ride"}:
        workout["session_focus"] = "easy"
        if sport == "Run":
            workout["hr_cap"] = effective_caps(None)["easy"]
    return {**workout, **extra}


def _seed(server: Any, case: dict[str, Any]) -> dict[str, str]:
    """The catalogue's synthetic state through the real services; returns tag -> id."""
    setup = case["setup"]
    tags: dict[str, str] = {}
    creation = server.PLANNING_WORKFLOWS.local_plan_creation_service()
    library = server.PLANNING_DATA.workout_library()

    def template(workout: dict[str, Any]) -> str:
        """Units come only from the athlete's library templates (#2379)."""
        return library.create_template(
            {k: v for k, v in workout.items() if k not in {"date", "rationale"}}
        )["id"]

    # One plain template per sport for new units, as the athlete's library has.
    for sport, name in TEMPLATE_NAMES.items():
        tags[f"template:{sport}"] = template(_workout("2026-09-07", name, sport))

    def unit(tag: str, day: str, name: str, sport: str, **extra: Any) -> None:
        workout = _workout(day, name, sport, **extra)
        workout["template_id"] = template(workout)
        tags[tag] = creation.save([workout])[0]["id"]

    unit("strength", "2026-09-09", "Oberkörper moderat + Core", "WeightTraining")
    unit("later", "2026-10-05", "Nach dem Giro", "Ride", environment="indoor")
    if setup.get("second_strength"):
        unit("second_strength", "2026-09-11", "Oberkörper Kraft", "WeightTraining")
    if setup.get("monday") == "Run":
        unit(
            "monday",
            "2026-09-07",
            "Montagslauf",
            "Run",
            session_focus="tempo",
            description="- 10km Z3 Pace",
            hr_cap=None,
            duration_minutes=55,
        )
    if setup.get("monday") == "Ride":
        unit(
            "monday",
            "2026-09-07",
            "Montags-Radeinheit",
            "Ride",
            session_focus="threshold",
            description="- 60m 95%",
            duration_minutes=60,
        )
    server.PLANNING_DATA.competition().save(
        {
            "name": "Münsterland Giro",
            "event_date": "2026-10-03",
            "sport": "Ride",
            "priority": "A",
        }
    )
    lt1 = setup.get("first_threshold") or {}
    server.ATHLETE_DATA.profile().save(
        {
            "sports": "Running, Cycling, Strength",
            "performance_notes": f"Gespeicherte Rad-FTP: {FTP_WATTS} W.",
            "first_threshold_hr": str(lt1.get("bpm", "")),
            "first_threshold_origin": lt1.get("origin", ""),
        }
    )
    if lt1:
        # #4255: the adopted LT1 over the 167 bpm threshold, and one easy run
        # still capped by an older rule (74.2 % = 124 bpm).
        from backend.activities.hr_cap import hr_cap

        server.ATHLETE_DATA.zones().save({**LONG_RUN_ZONES, "from_beginning": True})
        unit(
            "old_cap",
            "2026-09-08",
            TEMPLATE_NAMES["Run"],
            "Run",
            hr_cap=hr_cap(lt1["old_cap_percent"], lt1["origin"]),
        )
    _seed_review_cases(server, setup, unit, lt1)
    if not setup.get("no_blocker"):
        _seed_blocker(server)
    _seed_nutrition(server, setup)
    if setup.get("activity"):
        _seed_activity(server, setup["activity"])
    if setup.get("long_run"):
        _seed_long_runs(server, unit)
    if setup.get("draft"):
        tags["draft"] = _seed_draft(server, setup["draft"])
    if setup.get("pending"):
        _seed_pending(server, setup["pending"])
    return tags


def _seed_review_cases(
    server: Any, setup: dict[str, Any], unit: Callable[..., None], lt1: dict[str, Any]
) -> None:
    """The #4293 setups: a Grundlage run, external calendar titles and an
    earlier dialogue."""
    if setup.get("endurance"):
        # #4292: a Grundlage run capped at the LT1 like an easy run, within the
        # three days an ordinary question sees.
        from backend.activities.hr_cap import cap_percents, hr_cap

        if not lt1:
            raise ValueError("the endurance setup needs first_threshold")
        percent = cap_percents(lt1["bpm"] / 167 * 100, lt1["origin"] == "estimated")
        unit(
            "endurance",
            "2026-09-09",
            "Grundlage 60 min",
            "Run",
            session_focus="endurance",
            description="- 60m Z2 HR",
            duration_minutes=60,
            hr_cap=hr_cap(percent[0]["endurance"], percent[1]),
        )
    for index, title in enumerate(setup.get("calendar", [])):
        _seed_calendar_event(server, title, index)
    if setup.get("history"):
        messages = server.APP.coach_conversation.message_service()
        for role, text in setup["history"]:
            messages.add(role, text)


def _seed_nutrition(server: Any, setup: dict[str, Any]) -> None:
    """Own products and diary entries through the real services."""
    nutrition = server.NUTRITION_SERVICES
    for product in setup.get("products", []):
        nutrition.meal_library_service().product_catalog.save_product(
            {"basis_amount": 100, "basis_unit": "g", "source": "manual", **product}
        )
    for entry in setup.get("entries", []):
        nutrition.diary_service().log_meal(entry)


def _seed_blocker(server: Any) -> None:
    with server.database_manager().unit_of_work() as db:
        db.execute(
            "INSERT INTO external_calendar_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "blocker",
                "uid-blocker",
                "Termin, kein Training",
                "2026-09-10",
                "2026-09-10T09:00:00",
                "2026-09-10T18:00:00",
                540,
                0,
                1,
                1,
                0,
                0,
                "seed",
            ),
        )


def _seed_calendar_event(server: Any, title: str, index: int) -> None:
    """An external calendar event on Tuesday whose title is untrusted text (#4293)."""
    with server.database_manager().unit_of_work() as db:
        db.execute(
            "INSERT INTO external_calendar_events (id, uid, name, event_date, "
            "start_local, end_local, duration_minutes, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                f"injection-{index}",
                f"uid-injection-{index}",
                title,
                "2026-09-08",
                "2026-09-08T17:00:00",
                "2026-09-08T18:00:00",
                60,
                "2026-09-07T06:00:00",
            ),
        )


def _seed_activity(server: Any, setup: dict[str, Any]) -> None:
    """One synthetic run yesterday; ``second_start`` adds a second one that day.

    ``readable`` adds a Garmin copy, since the activity detail reads Garmin,
    Wahoo and local records only (#1685).
    """
    import snapshot_seed
    from canonical_detail_support import add_garmin_copy, seed_intervals_activity

    name = setup["name"]
    second_start = setup.get("second_start")
    from support import mark_tables_serving

    activity: dict[str, Any] = {
        "id": "synthetic-run",
        "type": "Run",
        "name": name,
        "start_date_local": "2026-09-06T10:00:00",
        "moving_time": 1800,
        "distance": 5000,
    }
    snapshot_seed.save_view(
        server.SYNC_PERSISTENCE.state_repository(),
        {
            "recent_activities": [activity],
            "recent_wellness": [],
            "planned_workouts": [],
            "raw_provider_data": {"activities": [dict(activity)]},
        },
    )
    mark_tables_serving(server)
    seed_intervals_activity(
        server.database_manager(),
        activity["id"],
        name=name,
        type="Run",
        start_date_local="2026-09-06T10:00:00",
        moving_time=1800,
        distance=5000,
    )
    if second_start:
        # A second run that day, so "my run yesterday" names two sessions.
        seed_intervals_activity(
            server.database_manager(),
            "synthetic-run-2",
            name=name,
            type="Run",
            start_date_local=second_start,
            moving_time=2400,
            distance=6000,
        )
    if setup.get("readable"):
        streams = {
            "time": [0, 900, 1800],
            "heartrate": [130, 140, 150],
            "velocity_smooth": [2.8, 2.8, 2.8],
            "distance": [0, 2500, 5000],
        }
        for seeded in ("synthetic-run", "synthetic-run-2")[: 1 + bool(second_start)]:
            add_garmin_copy(server.database_manager(), seeded, streams)


# The athlete's long-run example (#4136), synthetic: yesterday's long run with
# a heart-rate target of 130-140 bpm, four earlier long runs with that target,
# and runs of other types, two of them in the same heart-rate band.
LONG_RUN_ID = "synthetic-long-run"
LONG_RUN_TARGET = "130-140bpm Langer Lauf locker"
PLANNED_AT = datetime(2026, 8, 1, 8, tzinfo=UTC)
LONG_RUN_ZONES = {
    "sport": "run",
    "metric": "heart_rate",
    "threshold": 167,
    "maximum": 190,
    "limits": [125, 140, 152, 167, 178, 190],
}
# Activity id, day, plan focus, minutes, km, average HR, HR rise in the 2nd half.
LONG_RUN_SESSIONS = (
    (LONG_RUN_ID, "2026-09-06", "long", 86, 15.0, 132, 4),
    ("synthetic-long-0830", "2026-08-30", "long", 80, 13.8, 133, 5),
    ("synthetic-long-0823", "2026-08-23", "long", 80, 13.7, 133, 6),
    ("synthetic-long-0816", "2026-08-16", "long", 78, 13.3, 134, 6),
    ("synthetic-long-0809", "2026-08-09", "long", 75, 12.7, 134, 7),
    ("synthetic-easy-0902", "2026-09-02", "easy", 60, 10.6, 131, 2),
    ("synthetic-easy-0827", "2026-08-27", "easy", 58, 10.2, 133, 2),
    ("synthetic-tempo-0903", "2026-09-03", "threshold", 55, 11.0, 158, 3),
)
# Diary: three days of about 1700 kcal, breakfast 2.5 hours before the long
# run, and a can and a gel logged after it.
LONG_RUN_MEALS: tuple[dict[str, Any], ...] = (
    *(
        {"meal_date": day, "meal_type": slot, "description": text, "kcal": kcal}
        for day in ("2026-09-03", "2026-09-04", "2026-09-05")
        for slot, text, kcal in (
            ("breakfast", "Haferflocken mit Milch", 450),
            ("lunch", "Reis mit Gemüse", 650),
            ("dinner", "Brot mit Käse", 600),
        )
    ),
    {
        "meal_date": "2026-09-06",
        "logged_at": "07:30",
        "logged_time_known": True,
        "description": "Haferflocken mit Banane und Honig",
        "kcal": 620,
        "carbs_g": 119,
        "protein_g": 18,
        "fat_g": 10,
    },
    {
        "meal_date": "2026-09-06",
        "logged_at": "11:57",
        "logged_time_known": True,
        "description": "Energydrink",
        "kcal": 110,
        "carbs_g": 27,
    },
    {
        "meal_date": "2026-09-06",
        "logged_at": "12:00",
        "logged_time_known": True,
        "description": "Gel",
        "kcal": 90,
        "carbs_g": 22,
    },
)


def _long_run_streams(minutes: int, km: float, heart: int, rise: int) -> dict:
    """Five-second streams: heart rate up from 90 bpm in ten minutes, then
    ``rise`` bpm higher in the second half at nearly the same pace, around
    ``heart`` on average; the main run touches 143 bpm for a minute."""
    import math

    total = minutes * 60
    speed = km * 1000 / total
    time = list(range(0, total + 1, 5))
    heartrate = []
    half = (total - 600) / 2
    for t in time:
        # After the warm-up the heart rate climbs evenly, so the second half of
        # the main part averages ``rise`` bpm above the first.
        steady = heart + 1 + rise * (t - 600 - half) / half
        if t < 600:
            steady = 90 + (heart - 6 - 90) * t / 600
        if minutes == 86 and 4200 <= t < 4260:
            steady = 143
        heartrate.append(round(steady))
    return {
        "time": time,
        "distance": [round(speed * t, 1) for t in time],
        "velocity_smooth": [round(speed * (1 - 0.01 * t / total), 3) for t in time],
        "heartrate": heartrate,
        "altitude": [round(40 + 2 * math.sin(t / 300), 1) for t in time],
        "cadence": [86.5] * len(time),
    }


def _seed_long_runs(server: Any, unit: Any) -> None:
    """The long-run sessions with Garmin streams, their plan units, recovery,
    weather and diary (#4136); each through the real stores."""
    from unittest.mock import patch

    from canonical_detail_support import store_garmin_streams
    from support import (
        complete_canonical_backfill,
        mark_tables_serving,
        store_garmin_activities,
    )

    from backend.activities.hr_cap import effective_caps
    from backend.sync.garmin_snapshot_writer import GarminSnapshotWriter

    manager = server.database_manager()
    # Own run zones; 84 % of the 167 bpm threshold caps a long run at 140 bpm.
    server.ATHLETE_DATA.zones().save({**LONG_RUN_ZONES, "from_beginning": True})
    listed: list[dict[str, Any]] = []
    streams: dict[str, dict[str, Any]] = {}
    for activity_id, day, focus, minutes, km, heart, rise in LONG_RUN_SESSIONS:
        name = {"long": "Langer Lauf", "easy": "Lockerer Lauf"}.get(focus, "Schwelle")
        listed.append(
            {
                "activityId": activity_id,
                "activityName": name,
                "activityType": {"typeKey": "running"},
                "startTimeLocal": f"{day} 10:00:00",
                "duration": minutes * 60.0,
                "movingDuration": minutes * 60.0,
                "distance": km * 1000,
                "averageHR": heart,
            }
        )
        streams[activity_id] = _long_run_streams(minutes, km, heart, rise)
        target = {"long": LONG_RUN_TARGET, "easy": "Z2 HR"}.get(focus, "Z4 HR")
        description = f"- {minutes}m {target}"
        # A unit is planned before its day, so the clock stands before them all.
        with patch.object(server.ATHLETE_CLOCK, "now", return_value=PLANNED_AT):
            unit(
                f"run:{day}",
                day,
                name,
                "Run",
                session_focus=focus,
                description=description,
                duration_minutes=minutes,
                hr_cap=effective_caps(None)["long"] if focus == "long" else None,
            )
    # Garmin sessions with their summaries and full streams, as a sync stores them.
    store_garmin_activities(server, listed)
    for activity_id, values in streams.items():
        store_garmin_streams(manager, activity_id, values)
    nights = [f"2026-08-{day:02d}" for day in range(7, 32)] + [
        f"2026-09-{day:02d}" for day in range(1, 8)
    ]
    with manager.unit_of_work() as db:
        GarminSnapshotWriter(server.KEY_VALUE_REPOSITORY, lambda: UTC).write(
            db,
            {
                "synced_at": "2026-09-07T08:00:00",
                # Whole-day totals of the three days before the long run: with
                # about 1700 kcal logged, about 700 kcal short a day.
                "daily_stats": [
                    {
                        "calendarDate": d,
                        "totalKilocalories": 2400,
                        "bmrKilocalories": 1700,
                    }
                    for d in ("2026-09-03", "2026-09-04", "2026-09-05")
                ],
                "sleep": [
                    {"calendarDate": d, "sleepTimeSeconds": 26_640} for d in nights
                ],
                "hrv": [{"calendarDate": d, "lastNightAvg": 59} for d in nights],
                "resting_hr": [
                    {"calendarDate": d, "restingHeartRate": 47} for d in nights
                ],
            },
        )
    complete_canonical_backfill(server)
    mark_tables_serving(server)
    _seed_activity_weather(server, LONG_RUN_ID, 14.0, 3.0)
    for entry in LONG_RUN_MEALS:
        server.NUTRITION_SERVICES.diary_service().log_meal(dict(entry))


def _seed_activity_weather(
    server: Any, activity_id: str, temperature: float, wind_mps: float
) -> None:
    """The session's stored activity weather, as the weather stage writes it."""
    from backend.canonical.vocabulary import MetricKind
    from backend.db import canonical_store

    # The stage's own observation builder, so the seeded row is the one the
    # weather stage writes (no public writer takes a fetched value).
    from backend.sync.activity_weather import _observation
    from backend.weather.activity_weather_store import LOCATION_START

    with server.database_manager().unit_of_work() as db:
        summary = dict(
            db.execute(
                "SELECT activity_id AS id, sport, started_at, recorded_by, "
                "local_date || 'T10:00:00' AS started_at_local FROM activity_records WHERE provider_activity_id = ?",
                (activity_id,),
            ).fetchone()
        )
        for kind, value in (
            (MetricKind.ACTIVITY_WEATHER_TEMPERATURE, temperature),
            (MetricKind.ACTIVITY_WEATHER_WIND_SPEED, wind_mps),
        ):
            canonical_store.upsert_observation(
                db,
                _observation(summary, kind, value, LOCATION_START, "0" * 64),
                mapper_version=2,
                now="2026-09-07T08:00:00+00:00",
                owner=summary["id"],
                derivation={"location_kind": LOCATION_START},
            )


def _seed_draft(server: Any, kind: str) -> str:
    server.APP.coach_conversation.message_service().add("user", "Ein Entwurf bitte")
    with server.database_manager().unit_of_work() as db:
        server.CHAT_REPOSITORY.add(db, "user", "Entwurf", client_turn_id="draft-source")
    workout = _workout("2026-09-11", "Lockerer Lauf (Entwurf)", "Run")
    if kind == "unrelated":
        workout = _workout("2026-09-12", "Anderer Entwurf", "Ride")
    draft = server.COACH_PLANNING_TOOLS.training_plan_artifact_service().stage(
        {"payload": {"plan_name": "Entwurf", "workouts": [workout]}},
        "old-conversation",
        "draft-source",
    )
    return draft["artifact_id"]


def _seed_pending(server: Any, pending: dict[str, str]) -> None:
    messages = server.APP.coach_conversation.message_service()
    user = messages.add("user", pending["user"])
    messages.add("assistant", pending["question"])
    server.key_value_service().set(
        "coach_pending_request",
        json.dumps(
            {
                "source_message_ids": [user["id"]],
                "summary": pending["summary"],
                "question": pending["question"],
            }
        ),
    )


# The stored payload names sports canonically; the catalogue speaks in tool terms.
SPORT_NAMES = {"strength": "WeightTraining", "run": "Run", "ride": "Ride"}


def _unit_row(row: Any) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    gone = row["tombstone"] or payload.get("archived") or payload.get("local_deleted")
    sport = str(payload.get("sport", ""))
    return {
        "date": payload.get("date", ""),
        "sport": SPORT_NAMES.get(sport, sport),
        "tombstone": bool(gone),
        "payload": payload,
    }


def _product_row(row: Any) -> dict[str, Any]:
    """A product's name, nutrient values and the origin label of each filled field."""
    marks = json.loads(row["details"]).get("filled_fields") or {}
    return {
        "name": row["name"],
        "source": row["source"],
        "values": {f: row[f] for f in NUTRIENTS},
        "origins": {f: (m or {}).get("origin") for f, m in marks.items()},
    }


def _job_units(payload: str) -> list[str]:
    """The planned-unit ids a sync job pushes."""
    entries = json.loads(payload or "{}").get("entries") or []
    return [str(e["library_workout_id"]) for e in entries if isinstance(e, dict)]


def _proposal_units(row: Any) -> list[str]:
    """The planned-unit ids a remote proposal names, as scope targets or diff entries."""
    targets = json.loads(row["object_ids"]).get("targets", [])
    named = [t.split(":", 1)[1] for t in targets if t.startswith("planned_unit:")]
    return named + [str(e["id"]) for e in json.loads(row["diff"]) if e.get("id")]


def snapshot(server: Any) -> dict[str, Any]:
    """The scored state: planned units, library, plans, competitions, check-ins,
    activity feedback, drafts, proposals, sync jobs and the pending request."""
    with server.database_manager().unit_of_work() as db:

        def rows(sql: str) -> list[Any]:
            return db.execute(sql).fetchall()

        units = {
            r["local_id"]: _unit_row(r)
            for r in rows("SELECT local_id, payload, tombstone FROM planned_units")
        }
        state: dict[str, Any] = {
            "units": units,
            "library": {
                r["id"]: r["payload"]
                for r in rows("SELECT id, payload FROM workout_library")
            },
            "competitions": {
                r["id"]: dict(r)
                for r in rows(
                    "SELECT id, name, event_date, sport, priority, notes FROM competitions"
                )
            },
            "checkins": {
                r["checkin_date"]: dict(r)
                for r in rows(
                    "SELECT checkin_date, soreness, stress, motivation, session_rpe,"
                    " day_form, pain, illness, availability_notes, notes FROM athlete_checkins"
                )
            },
            "feedback": {
                r["activity_id"]: dict(r)
                for r in rows(
                    "SELECT activity_id, notes, session_rpe FROM activity_feedback"
                )
            },
            "products": {
                r["id"]: _product_row(r)
                for r in rows(
                    "SELECT id, name, source, details, "
                    + ", ".join(NUTRIENTS)
                    + " FROM nutrition_products"
                )
            },
            "nutrition": {
                r["id"]: {"description": r["description"], "kcal": r["kcal"]}
                for r in rows("SELECT id, description, kcal FROM nutrition_logs")
            },
            "artifacts": {
                r["id"]: r["status"]
                for r in rows("SELECT id, status FROM coach_plan_artifacts")
            },
            "proposals": [
                {
                    "id": r["id"],
                    "object_ids": _proposal_units(r),
                    "status": r["status"],
                }
                for r in rows(
                    "SELECT id, object_ids, diff, status FROM coach_action_proposals"
                )
            ],
            # Read refreshes write nothing remote; every other job type does.
            # Each job keeps the planned units it pushes.
            "jobs": {
                r["id"]: _job_units(r["payload"])
                for r in rows(
                    "SELECT id, payload FROM sync_jobs"
                    " WHERE type NOT IN ('refresh', 'historical_backfill')"
                )
            },
            "job_types": {
                r["id"]: r["type"]
                for r in rows(
                    "SELECT id, type FROM sync_jobs"
                    " WHERE type NOT IN ('refresh', 'historical_backfill')"
                )
            },
        }
    state["plans"] = {
        p["id"]: {
            "name": p["name"],
            "status": p["status"],
            "constraints": p.get("constraints") or [],
        }
        for p in server.PLANNING_DATA.training_plan().list()
    }
    pending = server.key_value_service().get("coach_pending_request")
    state["pending"] = json.loads(pending) if pending else None
    return state


def _no_weather_look_up(stage: Any, _database: Any) -> None:
    """The weather stage's prefetch without a request: nothing is fetched, so
    every stored weather row is kept."""
    stage._fetched, stage._halted = {}, False


@contextlib.contextmanager
def case_server(
    base_url: str,
    model: str,
    thinking: str,
    api_key: str,
    *,
    provider: str,
    resolved_model: str | None = None,
):
    """A fresh temporary application with the real Coach loop and the live model.

    Only ``provider`` gets the key, so no turn can reach the other provider. A
    Claude family uses ``resolved_model``, the model the preflight listed, so
    every case runs on the same model without listing the models again.
    """
    from unittest.mock import Mock, patch

    import server_test_support as fixtures
    from support import (
        canonical_backfill_stopped,
        isolated_server,
        make_config,
        metric_export_stopped,
        reset_application_state,
    )

    from backend.errors import AppError
    from backend.providers.open_food_facts import OpenFoodFactsClient
    from backend.sync.activity_weather import ActivityWeatherStage

    server = fixtures.server
    with (
        tempfile.TemporaryDirectory(prefix="coach-live-eval-") as root,
        contextlib.ExitStack() as stack,
    ):
        config = (
            make_config(anthropic_api_key=api_key, anthropic_base_url=base_url)
            if provider == "anthropic"
            else make_config(
                openai_api_key=api_key, openai_base_url=base_url, openai_model=model
            )
        )
        stack.enter_context(patch.object(server.CONFIG_HOLDER, "value", config))
        stack.enter_context(isolated_server(server, Path(root)))
        stack.enter_context(canonical_backfill_stopped(server))
        stack.enter_context(metric_export_stopped(server))
        reset_application_state(server)
        if provider == "anthropic":
            from backend.providers.claude_models import setting_family
            from backend.providers.model_wiring import CLAUDE_MODELS_KEY

            # A Claude family setting routes every task to the Messages API.
            server.SETTINGS.save_model(model)
            if resolved_model:
                server.key_value_service().set(
                    CLAUDE_MODELS_KEY,
                    json.dumps(
                        {
                            "newest": {setting_family(model): resolved_model},
                            "fetched_at": datetime.now(UTC).isoformat(),
                        }
                    ),
                )
        server.SETTINGS.save_thinking_level(thinking)
        # The activity weather stays as seeded: the backfill worker never looks
        # it up, so it neither reaches Open-Meteo nor replaces a seeded value.
        stack.enter_context(
            patch.object(ActivityWeatherStage, "prefetch", _no_weather_look_up)
        )
        # Food lookups stay offline: Open Food Facts is unreachable and BLS finds
        # nothing; the web search is replaced per case in run_case.
        offline = AppError(503, "offline", reason="food_database_unavailable")
        stack.enter_context(
            patch.object(OpenFoodFactsClient, "product", side_effect=offline)
        )
        stack.enter_context(
            patch.object(OpenFoodFactsClient, "search", side_effect=offline)
        )
        stack.enter_context(
            patch(
                "backend.nutrition.food_database._bls_search", return_value=([], False)
            )
        )
        stack.enter_context(
            patch.object(
                server.ATHLETE_CLOCK,
                "now",
                return_value=datetime(2026, 9, 7, 12, tzinfo=UTC),
            )
        )
        original = server.APP.coach_conversation.dialogue_read_service

        def dated_dialogue_service():
            service = original()
            service._local_clock = lambda _timezone: server.ATHLETE_CLOCK.now()
            return service

        stack.enter_context(
            fixtures.patch_coach_conversation(
                "dialogue_read_service", side_effect=dated_dialogue_service
            )
        )
        # No remote conversation: ordinary turns need none.
        stack.enter_context(
            fixtures.patch_coach_conversation(
                "provision_service",
                return_value=Mock(
                    binding=Mock(return_value="synthetic-conversation"),
                    bind_remote=Mock(return_value="synthetic-conversation"),
                    rotate=Mock(return_value="synthetic-conversation"),
                ),
            )
        )
        yield server


# One synthetic web result per missing field, cited to a page that never loads.
WEB_PAGE = "https://example.invalid/produkt"
WEB_VALUES = {
    "kcal": 210.0,
    "carbs_g": 24.0,
    "protein_g": 9.0,
    "fat_g": 8.5,
    "sugar_g": 12.0,
    "fiber_g": 3.0,
    "salt_g": 0.4,
}


def offline_web_search(mode: str) -> Any:
    """The product web search without a model call: fixed values or unavailable."""
    from backend.nutrition.web_search import UNAVAILABLE, UNTRUSTED_NOTICE

    def search(_self: Any, products: list[dict[str, Any]]) -> dict[str, Any]:
        if mode != "values":
            return dict(UNAVAILABLE)
        return {
            "ok": True,
            "data_origin": "web",
            "products": [
                {
                    "id": product["id"],
                    "name": product.get("name"),
                    "values": [
                        {"field": f, "value": WEB_VALUES[f], "source_url": WEB_PAGE}
                        for f in product["missing_fields"]
                    ],
                    "dropped": 0,
                }
                for product in products
            ],
            "notice": UNTRUSTED_NOTICE,
        }

    return search


def _stale_once(server: Any) -> Any:
    """After the model's first plan write, another change advances the revision."""
    state = {"done": False}

    def on_turn(turn: Any) -> None:
        if state["done"] or "apply_training_patch" not in [
            call["name"] for call in turn.tool_calls
        ]:
            return
        state["done"] = True
        with server.database_manager().unit_of_work() as db:
            db.execute("UPDATE planning_state SET revision = revision + 1 WHERE id=1")

    return on_turn


def run_case(
    case: dict[str, Any], counter: CallCounter, settings: dict[str, str]
) -> dict[str, Any]:
    """Run one case; raises CapReached when the cap stops it half way."""
    from unittest.mock import patch

    import server_test_support as fixtures

    from backend.nutrition.web_search import NutritionWebSearchService

    with case_server(
        settings["base_url"],
        settings["model"],
        settings["thinking"],
        settings["api_key"],
        provider=settings["provider"],
        resolved_model=settings.get("resolved_model"),
    ) as server:
        tags = _seed(server, case)
        before = snapshot(server)
        transport = CountingTransport(
            server.APP.coach_conversation.response_transport(),
            counter,
            _stale_once(server) if case["setup"].get("stale_revision") else None,
        )
        turns = []
        with (
            fixtures.patch_coach_conversation(
                "response_transport", return_value=transport
            ),
            patch.object(
                NutritionWebSearchService,
                "search",
                offline_web_search(case["setup"].get("web", "unavailable")),
            ),
        ):
            for index, message in enumerate(case["turns"]):
                mark = len(counter.tool_names)
                outputs = len(counter.tool_outputs)
                receipt = server.COACH_TURNS.chat_turn_service().run(
                    message,
                    client_turn_id=f"{case['id']}-{index}",
                    session_csrf_hash="synthetic-session",
                )
                if counter.blocked:
                    raise CapReached
                turns.append(
                    {
                        "tools": counter.tool_names[mark:],
                        "tool_outputs": counter.tool_outputs[outputs:],
                        "status": receipt.get("status", ""),
                        "text": str((receipt.get("message") or {}).get("content", "")),
                        "failed_tools": _failed_tools(receipt),
                        "plan_notes": _plan_note_codes(receipt),
                    }
                )
        after = snapshot(server)
    result = score_case(case, before, after, turns, tags)
    return {
        "id": case["id"],
        "sample": bool(case.get("sample")),
        "turns": turns,
        **result,
    }


def _plan_note_codes(receipt: dict[str, Any]) -> list[str]:
    """The plan-check finding codes a turn's tool results carried."""
    return [
        str(note.get("code"))
        for step in receipt.get("command_receipts") or []
        if isinstance(step, dict)
        for note in (step.get("result") or {}).get("plan_check_notes") or []
        if isinstance(note, dict)
    ]


def _failed_tools(receipt: dict[str, Any]) -> list[str]:
    """Tool steps the app refused, as tool:reason codes (never their message text).

    A refused plan write adds its blocking finding codes, so the report shows
    which rule the model failed to repair.
    """
    refused = []
    for step in receipt.get("command_receipts") or []:
        result = (step.get("result") or {}) if isinstance(step, dict) else {}
        if result.get("ok") is not False:
            continue
        codes = sorted(
            {
                str(item.get("code"))
                for item in result.get("findings") or []
                if isinstance(item, dict) and item.get("code")
            }
        )
        suffix = f"[{','.join(codes)}]" if codes else ""
        refused.append(f"{step.get('tool')}:{result.get('reason') or 'failed'}{suffix}")
    return refused


# --- CLI and report ------------------------------------------------------------


def fetch_model_ids(base_url: str, api_key: str, *, provider: str) -> list[str]:
    """GET the provider's model list; raises OSError or ValueError when unusable.

    For Anthropic, the newest model of each family, as the app resolves them.
    """
    from backend.config import DEFAULT_ANTHROPIC_BASE_URL
    from backend.providers.claude_models import newest_by_family
    from backend.providers.claude_requests import CLAUDE_MODELS_PATH, claude_headers
    from backend.providers.http import endpoint

    if provider == "anthropic":
        url = endpoint(
            base_url,
            CLAUDE_MODELS_PATH + "?limit=100",
            default_base_url=DEFAULT_ANTHROPIC_BASE_URL,
        )
        headers = claude_headers(api_key)
    else:
        url = base_url + "/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.loads(response.read(1_000_000))
    if provider == "anthropic":
        return list(newest_by_family(body).values())
    return [str(item["id"]) for item in body["data"]]


_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _preflight(args: argparse.Namespace) -> str | None:
    """None when the endpoint answers and lists the model, else one line saying why.

    A loopback endpoint or the named provider's API only; that API needs the
    evaluation key and the model's prices, so the cost ceiling can hold. For
    Anthropic the model is a Claude family setting, as the app selects it.
    """
    from backend.config import validated_base_url
    from backend.errors import AppError
    from backend.providers.claude_models import family_setting, setting_family

    api_host, label, _, _ = PROVIDERS[args.provider]
    anthropic = args.provider == "anthropic"
    if anthropic != bool(setting_family(args.model)):
        return (
            "A Claude family (claude-opus, claude-sonnet or claude-haiku) needs "
            "--provider anthropic, and --provider anthropic needs one."
        )
    try:
        args.base_url = validated_base_url(args.base_url, f"{label} base URL")
    except AppError:
        return "Base URL must be http on loopback or https."
    host = urlsplit(args.base_url).hostname
    if host not in _LOOPBACK and host != api_host:
        return (
            f"Only a loopback endpoint or the {label} API is used for this evaluation."
        )
    # An Anthropic key has its own prefix: a key never goes to the other provider.
    if host == api_host and not (
        args.api_key
        and args.api_key.startswith("sk-ant-" if anthropic else "sk-")
        and args.api_key.startswith("sk-ant-") == anthropic
        and args.price_input > 0
        and args.price_output > 0
    ):
        return (
            f"The {label} API needs a {label} key in COACH_EVAL_API_KEY or --key-file and both "
            "--price-input and --price-output; nothing was run."
        )
    try:
        models = fetch_model_ids(args.base_url, args.api_key, provider=args.provider)
    except urllib.error.HTTPError as error:
        # The endpoint answered: name the status, never the key (#2669).
        if error.code in (401, 403):
            return f"Endpoint at {host} rejected the key (HTTP {error.code}); nothing was run."
        return f"Endpoint at {host} answered HTTP {error.code}; nothing was run."
    except OSError, ValueError, KeyError, TypeError, urllib.error.URLError:
        return f"Endpoint at {host} is unreachable; nothing was run."
    # A Claude family resolves to its newest listed model; any other id to itself.
    args.resolved_model = next(
        (
            model
            for model in models
            if (family_setting(model) if anthropic else model) == args.model
        ),
        None,
    )
    if not args.resolved_model:
        return f"Model {args.model} is not listed by the endpoint; nothing was run."
    return None


def render_report(
    args: argparse.Namespace,
    counter: CallCounter,
    results: list[dict[str, Any]],
    not_run: list[str],
) -> str:
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]
    safety = sum(len(r["safety"]) for r in results)
    unnecessary = sum(r["unnecessary_clarifications"] for r in results)
    lines = [
        "# Coach live evaluation",
        "",
        f"- Provider: {args.provider}",
        f"- Resolved model: {args.resolved_model}",
        f"- Provider host: {urlsplit(args.base_url).hostname}",
        f"- Model: {args.model}",
        f"- Thinking level: {args.thinking}",
        f"- Model calls: {counter.calls} of {counter.cap}",
        f"- Estimated cost: {counter.cost:.2f} of {counter.cost_cap:.2f} USD",
        f"- Cases: {len(passed)} passed, {len(failed)} failed, {len(not_run)} not run",
        f"- Safety failures: {safety}",
        f"- Unnecessary clarifications: {unnecessary}",
        "",
        "## Cases",
        "",
    ]
    for r in results:
        tools = (
            " > ".join(t for turn in r["turns"] for t in turn["tools"]) or "(no tools)"
        )
        problems = [*(f"SAFETY {s}" for s in r["safety"]), *r["misses"]]
        if r["unnecessary_clarifications"]:
            problems.append(
                f"unnecessary_clarifications:{r['unnecessary_clarifications']}"
            )
        lines.append(f"- {'PASS' if r['passed'] else 'FAIL'} `{r['id']}`: {tools}")
        lines += [f"  - {p}" for p in problems]
        refused = [t for turn in r["turns"] for t in turn.get("failed_tools", [])]
        lines += [f"  - tool refused {t}" for t in refused]
    if not_run:
        lines += [
            "",
            "## Not run (call cap reached)",
            "",
            *(f"- `{c}`" for c in not_run),
        ]
    lines += [
        "",
        f"## Answer sample (first {SAMPLE_CASES} cases and flagged cases)",
        "",
    ]
    for i, r in enumerate(results):
        if i >= SAMPLE_CASES and not r.get("sample"):
            continue
        text = r["turns"][-1]["text"].strip().replace("\n", " ")[:600]
        lines += [f"**{r['id']}**: {text}", ""]
    return "\n".join(lines) + "\n"


def _errored(case_id: str, error: Exception) -> dict[str, Any]:
    """A failed case for an unexpected error; only its type, never its text."""
    return {
        "id": case_id,
        "passed": False,
        "safety": [],
        "misses": [f"error:{type(error).__name__}"],
        "unnecessary_clarifications": 0,
        "turns": [{"tools": [], "text": ""}],
    }


# What the offline report prints of the analysed session's tool result (#4136).
REPORT_KEYS = (
    "session",
    "focus",
    "planned_targets",
    "session_report",
    "comparable_sessions",
    "pace_at_heart_rate",
    "recovery_before",
    "weather",
)


def session_report_snapshot(case: dict[str, Any]) -> dict[str, Any]:
    """The case's session as the analysis tool returns it, without a model.

    The same synthetic session before and after a change to the report shows
    what the Coach reads (#4136); nothing leaves the temporary storage.
    """
    activity_id = LONG_RUN_ID if case["setup"].get("long_run") else "synthetic-run"
    with case_server(
        "http://127.0.0.1:9", "offline", "low", "", provider="openai"
    ) as server:
        _seed(server, case)
        result = server.COACH_READ_TOOLS.read_service().execute(
            "get_activity_details", {"activity_id": activity_id}
        )
    return {key: result[key] for key in REPORT_KEYS if key in result}


def print_reports(case_ids: list[str]) -> int:
    """``--report <case id>...``: print the cases' session reports as one JSON object."""
    cases = {
        c["id"]: c
        for c in load_cases()
        if c["setup"].get("long_run")
        or (c["setup"].get("activity") or {}).get("readable")
    }
    unknown = sorted(set(case_ids) - set(cases))
    if not case_ids or unknown:
        print(f"Name cases with a readable session: {sorted(cases)}")
        return 2
    reports = {case_id: session_report_snapshot(cases[case_id]) for case_id in case_ids}
    sys.stdout.write(json.dumps(reports, ensure_ascii=False, indent=1) + "\n")
    return 0


def _load_key(args: argparse.Namespace) -> str | None:
    """Set args.api_key from the file --key-file names, else COACH_EVAL_API_KEY;
    None, or one line why not (#4329). A file's key goes only to the provider's own
    API, never to a loopback listener, and a byte order mark, which PowerShell
    writes, is no part of it. Neither the path nor the key is named."""
    args.api_key = os.environ.get("COACH_EVAL_API_KEY", "")
    if not args.key_file:
        return None
    api_host, label, _, _ = PROVIDERS[args.provider]
    if urlsplit(args.base_url).hostname != api_host:
        return f"--key-file is sent only to the {label} API; nothing was run."
    try:
        args.api_key = read_secret(args.key_file).lstrip(
            "\N{ZERO WIDTH NO-BREAK SPACE}"
        )
    except ProdError:
        return (
            "--key-file must be a non-empty file in the system temp directory "
            "or the T3 secret folder; nothing was run."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live Coach dialogue evaluation.")
    # Named explicitly: no default and no fallback to the other provider.
    parser.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--thinking", default="low")
    parser.add_argument("--cases", default="")
    parser.add_argument("--max-calls", type=int, default=MAX_CALLS)
    parser.add_argument("--max-cost-usd", type=float, default=MAX_COST_USD)
    # USD per million tokens, from the provider's price list for --model.
    parser.add_argument("--price-input", type=float, default=0.0)
    parser.add_argument("--price-output", type=float, default=0.0)
    # The key from a file in the system temp directory or T3's secret folder (#4329).
    parser.add_argument("--key-file", default="")
    args = parser.parse_args(argv)
    problem = _load_key(args)
    if problem:
        print(problem)
        return 2
    cases = load_cases()
    chosen = [c for c in args.cases.split(",") if c]
    unknown = sorted(set(chosen) - {c["id"] for c in cases})
    if (
        unknown
        or not 0 < args.max_calls <= MAX_CALLS
        or not 0 < args.max_cost_usd <= MAX_COST_USD
    ):
        print(f"Unknown case ids or call cap out of range: {unknown}")
        return 2
    problem = _preflight(args)
    if problem:
        print(problem)
        return 2
    selected = [c for c in cases if not chosen or c["id"] in chosen]
    _, _, uncached, cached = PROVIDERS[args.provider]
    counter = CallCounter(
        args.max_calls,
        args.max_cost_usd,
        (args.price_input * uncached, args.price_output),
        args.price_input * cached,
    )
    settings = {
        "provider": args.provider,
        "resolved_model": args.resolved_model,
        "base_url": args.base_url,
        "model": args.model,
        "thinking": args.thinking,
        "api_key": args.api_key or "loopback-no-key",
    }
    results: list[dict[str, Any]] = []
    not_run: list[str] = []
    for case in selected:
        if not_run:
            not_run.append(case["id"])
            continue
        try:
            results.append(run_case(case, counter, settings))
        except CapReached:
            not_run.append(case["id"])
        except KeyboardInterrupt:
            not_run.append(case["id"])
            break
        except Exception as error:  # noqa: BLE001 - keep the report, never raw text
            results.append(_errored(case["id"], error))
    sys.stdout.write(render_report(args, counter, results, not_run))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--report"]:
        sys.exit(print_reports(sys.argv[2:]))
    sys.exit(main())
