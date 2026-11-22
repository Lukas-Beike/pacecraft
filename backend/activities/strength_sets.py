"""Executed strength sets of a session: selection, summary and plan comparison.

One stored set list per session is used, never merged set by set: Garmin's
exercise sets (which carry the athlete's Connect corrections) first, then the
file set lists in a stable provider order. The summary groups the active sets of each
exercise over the whole session (supersets included), in order of first
appearance; the rest after a set is the duration of the rest set that follows
it. Exercise names are provider codes (upper-case identifiers), never free text.
Strength volume is reported on its own and never added to TSS or session-RPE
load.

Exercise categories decide leg strength: any leg category makes the session leg;
it is not leg only when every exercise has a known non-leg category. ``other``
(unrecognized) and the neutral ``cardio``, ``warm_up`` and ``run`` categories
decide nothing.

A planned ``strength_structure`` exercise is compared by exercise category: the
category of its Garmin catalogue exercise (explicit, stored pair, name or the
small alias list). A planned exercise without a known category is ``unknown``,
never a miss; so is one whose category was not recognized in a session that
has unrecognized sets. A planned exercise counts its sets by reps (athlete,
#3521): done reps over planned reps, each set at most 1, so 12 of 15 counts 0.8;
a set without reported reps counts as done, as nothing shows it fell short.
The plan is the structure the athlete trained: a version the unit had at the
session's start (``trained_structure``), never a later edit.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from backend.canonical.activity_detail import (
    ActivityStrengthSet,
    ActivityStrengthSetSet,
)
from backend.canonical.planning_fields import (
    STRENGTH_STRUCTURE,
    STRENGTH_STRUCTURE_VERSIONS,
)
from backend.canonical.vocabulary import (
    ExerciseCategory,
    ProviderId,
    StrengthSetSource,
    StrengthSetType,
)
from backend.db.activity_detail_store import (
    StoredDetailSet,
    strength_sets_for_sessions,
)
from backend.providers import garmin_strength_workout

# Exercise categories that load the legs: lower-body strength, full body and
# plyometrics (the same rule as the legs, full_body and plyometrics focus).
LEG_CATEGORIES: Final = frozenset(
    {
        ExerciseCategory.SQUAT,
        ExerciseCategory.DEADLIFT,
        ExerciseCategory.LUNGE,
        ExerciseCategory.LEG_CURL,
        ExerciseCategory.CALF_RAISE,
        ExerciseCategory.HIP_RAISE,
        ExerciseCategory.HIP_SWING,
        ExerciseCategory.OLYMPIC_LIFT,
        ExerciseCategory.PLYO,
        ExerciseCategory.TOTAL_BODY,
    }
)
# The upper body is every other known category; a total body exercise counts for both sides.
# Categories that say nothing about the body part trained.
NEUTRAL_CATEGORIES: Final = frozenset(
    {
        ExerciseCategory.OTHER,
        ExerciseCategory.CARDIO,
        ExerciseCategory.WARM_UP,
        ExerciseCategory.RUN,
    }
)
MAX_EXERCISES: Final = 20
# Strength is recorded on the Garmin watch, so Garmin's file sets lead Wahoo's;
# output-only Intervals.icu ranks last. (Streams, laps and zones follow the
# session order instead, Wahoo first.)
_PROVIDER_RANK: Final = {
    provider.value: rank
    for rank, provider in enumerate(
        (ProviderId.GARMIN, ProviderId.WAHOO, ProviderId.INTERVALS)
    )
}
STRENGTH_KEY: Final = "strength_sets"


def select_strength_sets(
    stored: Iterable[StoredDetailSet],
) -> StoredDetailSet | None:
    """The one strength set list a session uses, or None without any."""
    candidates = [
        item for item in stored if isinstance(item.detail, ActivityStrengthSetSet)
    ]
    return min(candidates, key=strength_rank, default=None)


def strength_rank(item: StoredDetailSet) -> tuple[Any, ...]:
    """Exercise sets (with the athlete's corrections) before file sets."""
    detail = _detail(item)
    return (
        detail.source is not StrengthSetSource.GARMIN_EXERCISE_SETS,
        _PROVIDER_RANK.get(item.provider, len(_PROVIDER_RANK)),
        item.provider_activity_id,
    )


def _detail(item: StoredDetailSet) -> ActivityStrengthSetSet:
    detail = item.detail
    if not isinstance(detail, ActivityStrengthSetSet):
        raise TypeError("not a strength set list")
    return detail


def strength_summary(item: StoredDetailSet, *, set_list: bool) -> dict[str, Any]:
    """Bounded summary of one stored set list; ``set_list`` adds every set."""
    detail = _detail(item)
    exercises = _exercises(detail.sets)
    active = [s for s in detail.sets if s.set_type is StrengthSetType.ACTIVE]
    reps = [s.reps for s in active if s.reps is not None]
    volume = [s.reps * s.weight_kg for s in active if s.reps and s.weight_kg]
    by_category: dict[str, list[int | None]] = {}
    unrecognized = 0
    for item_set in active:
        category = item_set.exercise_category
        if category is None or category is ExerciseCategory.OTHER:
            unrecognized += 1
        else:
            by_category.setdefault(category.value, []).append(item_set.reps)
    summary: dict[str, Any] = {
        "source": detail.source.value,
        "provider": item.provider,
        "active_sets": len(active),
        "total_reps": sum(reps) if reps else None,
        "volume_kg": round(sum(volume), 1) if volume else None,
        "leg": _leg(group["category"] for group in exercises),
        "upper": _upper(group["category"] for group in exercises),
        # Reps per recognized category over every active set, for the plan
        # comparison; the exercise list below is bounded for display.
        "by_category": by_category,
        "unrecognized_sets": unrecognized,
        "exercises": [
            _exercise_entry(group, set_list=set_list)
            for group in exercises[:MAX_EXERCISES]
        ],
    }
    if len(exercises) > MAX_EXERCISES:
        summary["omitted_exercises"] = len(exercises) - MAX_EXERCISES
    return summary


def _exercises(sets: Sequence[ActivityStrengthSet]) -> list[dict[str, Any]]:
    """Active sets per exercise over the session; a rest set adds its duration."""
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    last_set: dict[str, Any] | None = None
    for item in sets:
        if item.set_type is StrengthSetType.REST:
            if last_set is not None and item.duration_s is not None:
                last_set["rest_s"] = round(item.duration_s)
            continue
        key = (item.exercise_category, item.exercise_name)
        group = groups.setdefault(
            key,
            {
                "category": item.exercise_category,
                "name": item.exercise_name,
                "sets": [],
            },
        )
        last_set = {
            "reps": item.reps,
            "weight_kg": item.weight_kg,
            "duration_s": None if item.duration_s is None else round(item.duration_s),
        }
        group["sets"].append(last_set)
    return list(groups.values())


def _exercise_entry(group: Mapping[str, Any], *, set_list: bool) -> dict[str, Any]:
    category: ExerciseCategory | None = group["category"]
    sets: list[dict[str, Any]] = group["sets"]
    loaded = [s for s in sets if s["weight_kg"] is not None]
    # Without any weight (pull-ups, push-ups) the top set is the one with the most reps (#3764).
    counted = [s for s in sets if s["reps"] is not None]
    top = None
    if loaded:
        top = max(loaded, key=lambda s: s["weight_kg"])
    elif counted:
        top = max(counted, key=lambda s: s["reps"])
    entry: dict[str, Any] = {
        "category": None if category is None else category.value,
        "name": group["name"],
        "leg": _category_leg(category),
        "upper": _category_upper(category),
        "sets": len(sets),
        "top_kg": None if top is None else top["weight_kg"],
        "top_reps": None if top is None else top["reps"],
    }
    if set_list:
        entry["reps"] = [s["reps"] for s in sets]
        entry["set_list"] = sets
    return entry


def _category_leg(category: ExerciseCategory | None) -> bool | None:
    if category is None or category in NEUTRAL_CATEGORIES:
        return None
    return category in LEG_CATEGORIES


def _category_upper(category: ExerciseCategory | None) -> bool | None:
    if category is None or category in NEUTRAL_CATEGORIES:
        return None
    return category not in LEG_CATEGORIES or category is ExerciseCategory.TOTAL_BODY


def _upper(categories: Iterable[ExerciseCategory | None]) -> bool | None:
    """True with any upper body exercise, False when every exercise is known leg only."""
    decided = [
        _category_upper(category)
        for category in categories
        if category not in NEUTRAL_CATEGORIES
    ]
    if any(decided):
        return True
    if decided and all(value is False for value in decided):
        return False
    return None


def _leg(categories: Iterable[ExerciseCategory | None]) -> bool | None:
    """True with any leg exercise, False when every exercise is known non-leg."""
    decided = [
        _category_leg(category)
        for category in categories
        if category not in NEUTRAL_CATEGORIES
    ]
    if any(decided):
        return True
    if decided and all(value is False for value in decided):
        return False
    return None


def strength_summaries(
    db: Any, activity_ids: Collection[str], *, set_list: bool = False
) -> dict[str, dict[str, Any]]:
    """Summary of the selected strength set list per session id."""
    return {
        activity_id: strength_summary(selected, set_list=set_list)
        for activity_id, selected in strength_sets_for_sessions(
            db, activity_ids, rank=strength_rank
        ).items()
    }


def with_strength_summaries(
    summaries: Iterable[Mapping[str, Any]], stored: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Return the summaries with their executed strength sets, when stored."""
    result = []
    for summary in summaries:
        item = dict(summary)
        found = stored.get(str(item.get("id")))
        if found is not None:
            item[STRENGTH_KEY] = dict(found)
        result.append(item)
    return tuple(result)


def planned_category(exercise: Mapping[str, Any]) -> ExerciseCategory | None:
    """Category of one planned exercise through the Garmin catalogue, else None."""
    resolved = garmin_strength_workout.resolve_exercise(exercise)
    if resolved is None:
        return None
    try:
        return ExerciseCategory(resolved.category.lower())
    except ValueError:
        return ExerciseCategory.OTHER


def _utc(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except TypeError, ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def trained_structure(unit: Mapping[str, Any], started_at: Any) -> Any:
    """The unit's ``strength_structure`` as trained in a session started at
    ``started_at`` (#3521): the earliest version replaced after the start, else
    the current one, so a later edit never changes a past session's verdict."""
    start = _utc(started_at)
    versions = unit.get(STRENGTH_STRUCTURE_VERSIONS)
    if start is not None and isinstance(versions, list):
        for version in versions:
            replaced = (
                _utc(version.get("replaced_at"))
                if isinstance(version, Mapping)
                else None
            )
            if replaced is not None and replaced > start:
                structure = version.get("structure")
                # A structure the history could not keep (over its field limit) is
                # unknown, so the current one stands in.
                if isinstance(structure, list):
                    return structure
                break
    return unit.get(STRENGTH_STRUCTURE)


def plan_comparison(
    structure: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Planned against executed sets per exercise, or None without a usable side.

    Executed sets of one category go to the planned exercises of that category
    in plan order. ``completed_sets`` counts each set by done over planned reps,
    at most 1 (#3521); ``percentage`` is its share of the planned sets of the
    matched exercises; None when nothing matched.
    """
    by_category = summary.get("by_category")
    if not structure or not isinstance(by_category, Mapping) or not by_category:
        return None
    pools = {key: list(value) for key, value in by_category.items()}
    unrecognized = bool(summary.get("unrecognized_sets"))
    rows: list[dict[str, Any]] = []
    planned_total = 0
    done_total = 0.0
    for item in structure:
        rows.append(row := _planned_row(item, pools, unrecognized=unrecognized))
        if row["status"] == "matched":
            planned_total += row["planned_sets"]
            done_total += row["completed_sets"]
    return {
        "basis": "strength_sets",
        "planned_sets": planned_total,
        "completed_sets": round(done_total, 2),
        "percentage": round(done_total * 100 / planned_total)
        if planned_total
        else None,
        "exercises": rows,
    }


def _planned_row(
    item: Mapping[str, Any],
    pools: dict[str, list[int | None]],
    *,
    unrecognized: bool,
) -> dict[str, Any]:
    category = planned_category(item)
    planned_sets = int(item.get("sets") or 0)
    planned_reps = int(item.get("reps") or 0)
    row: dict[str, Any] = {
        "name": item.get("name"),
        "category": None if category is None else category.value,
        "planned_sets": planned_sets,
        "planned_reps": planned_reps,
        "planned_kg": item.get("kg"),
    }
    pool = [] if category is None else pools.get(category.value, [])
    if (
        category is None
        or category is ExerciseCategory.OTHER
        or (not pool and unrecognized)
    ):
        return {**row, "status": "unknown"}
    # Garmin records the planned warm-up sets first, as ordinary sets of the
    # same category; they are not working sets. Only sets beyond the planned
    # working sets count as warm-ups, so a skipped warm-up costs no working set.
    warmup = item.get("warmup")
    warmup_sets = int(warmup.get("sets") or 0) if isinstance(warmup, Mapping) else 0
    skip = min(warmup_sets, max(0, len(pool) - planned_sets))
    pool = pool[skip:]
    taken, pools[category.value] = pool[:planned_sets], pool[planned_sets:]
    return {
        **row,
        "status": "matched",
        "done_sets": len(taken),
        "completed_sets": round(
            sum(_set_share(reps, planned_reps) for reps in taken), 2
        ),
        "reps": taken,
    }


def _set_share(reps: int | None, planned_reps: int) -> float:
    """One set's share of the plan: done over planned reps, at most 1 (#3521);
    a set without reported reps or a plan without reps counts as done."""
    if reps is None or planned_reps <= 0:
        return 1.0
    return min(reps, planned_reps) / planned_reps
