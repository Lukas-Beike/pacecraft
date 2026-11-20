"""Walk over Garmin weight payloads.

The health mapper in ``backend/providers/garmin_health_mapping`` searches the nested
weight payload with the limits and skip rules of the retired legacy weight reader;
the caller supplies its day resolver and value reader.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

from backend.providers.garmin_fields import GARMIN_DATE_ALIASES

WEIGHT_SECTIONS: Final = ("weight", "weigh_ins", "weighIns")
WALK_DATE_KEYS: Final = (*GARMIN_DATE_ALIASES, "timestampGMT", "timestamp")
# ``totalAverage`` is the range answer's undated aggregate, not a weigh-in (#2467).
# ``previousDateWeight`` and ``nextDateWeight`` carry a reading from outside the
# fetched window into its answer, and the stored envelope merges them across
# fetches, so they are no weigh-in of the window (#3718).
_CARRIED_KEYS: Final = frozenset(
    {"totalaverage", "previousdateweight", "nextdateweight"}
)
WEIGHT_SKIP_KEYS: Final = (
    frozenset({"minweight", "maxweight", "weightdelta"}) | _CARRIED_KEYS
)
BODY_FAT_SKIP_KEYS: Final = (
    frozenset({"minbodyfat", "maxbodyfat", "bodyfatdelta"}) | _CARRIED_KEYS
)
# The lists of readings or daily summaries; each item gets its own visit budget, so
# a stored envelope that merges many fetches keeps its older weigh-ins (#3718).
READING_LIST_KEYS: Final = frozenset(
    {*WEIGHT_SECTIONS, "dailyWeightSummaries", "dateWeightList"}
)
WALK_MAX_DEPTH: Final = 20
WALK_MAX_VISITS: Final = 2000
WALK_MAX_LIST_ITEMS: Final = 500


def alnum_key(value: object) -> str:
    """Casefolded text with only letters and digits, used to compare keys."""
    return "".join(
        character for character in str(value).casefold() if character.isalnum()
    )


def weight_payload(sections: Mapping[str, object]) -> object:
    """First truthy weight section, as the legacy ``or`` chain picks it."""
    payload: object = None
    for name in WEIGHT_SECTIONS:
        payload = sections.get(name)
        if payload:
            break
    return payload


@dataclass(frozen=True, slots=True)
class WalkedRecord[DayT]:
    """One mapping visited by the walk.

    ``day`` is the record's own day, or its nearest dated ancestor's when it has none.
    ``ordinal`` counts mappings in pre-order. ``list_index`` is the index of the
    nearest enclosing list item, or None. ``marked`` is true below a marker key.
    """

    record: Mapping[str, Any]
    day: DayT | None
    ordinal: int
    list_index: int | None
    marked: bool


def walk_records[DayT](
    payload: object,
    *,
    skip_keys: frozenset[str],
    resolve_day: Callable[[Mapping[str, Any]], DayT],
    marker_keys: frozenset[str] = frozenset(),
    list_keys: frozenset[str] = frozenset(),
) -> Iterator[WalkedRecord[DayT]]:
    """Yield every mapping under ``payload`` in pre-order, within the walk limits.

    ``resolve_day`` returns a record's own day, or None to inherit the nearest dated
    ancestor's day. A child whose ``alnum_key`` is in ``skip_keys`` is not entered.
    A child under a key in ``marker_keys`` is marked. Depth, visit and list limits
    match the legacy reader, and scalar values count as visits. With ``list_keys``,
    each item of the root list and of a list under one of those keys is walked with
    its own visit budget, so one reading cannot use up the next one's (#3718).
    """
    walk = _Walk(skip_keys, resolve_day, marker_keys, list_keys)
    if list_keys and isinstance(payload, list):
        yield from walk.items(payload, None, 0, False)
    else:
        yield from walk.visit(payload, None, 0, None, False)


class _Walk[DayT]:
    def __init__(
        self,
        skip_keys: frozenset[str],
        resolve_day: Callable[[Mapping[str, Any]], DayT],
        marker_keys: frozenset[str],
        list_keys: frozenset[str],
    ) -> None:
        self._skip_keys = skip_keys
        self._resolve_day = resolve_day
        self._marker_keys = marker_keys
        self._list_keys = list_keys
        self._visits = 0
        self._ordinal = 0

    def items(
        self, values: list[Any], inherited: DayT | None, depth: int, marked: bool
    ) -> Iterator[WalkedRecord[DayT]]:
        """Walk each list item with a fresh visit budget, then restore the caller's."""
        spent = self._visits
        for index, item in enumerate(values[:WALK_MAX_LIST_ITEMS]):
            self._visits = 0
            yield from self.visit(item, inherited, depth + 1, index, marked)
        self._visits = spent + 1

    def visit(
        self,
        value: object,
        inherited: DayT | None,
        depth: int,
        list_index: int | None,
        marked: bool,
    ) -> Iterator[WalkedRecord[DayT]]:
        if depth > WALK_MAX_DEPTH or self._visits >= WALK_MAX_VISITS:
            return
        self._visits += 1
        if isinstance(value, Mapping):
            ordinal = self._ordinal
            self._ordinal += 1
            day: DayT | None = self._resolve_day(value)
            if day is None:
                day = inherited
            yield WalkedRecord(value, day, ordinal, list_index, marked)
            for key, child in value.items():
                if alnum_key(key) in self._skip_keys:
                    continue
                if key in self._list_keys and isinstance(child, list):
                    if depth < WALK_MAX_DEPTH and self._visits < WALK_MAX_VISITS:
                        yield from self.items(
                            child, day, depth + 1, marked or key in self._marker_keys
                        )
                else:
                    yield from self.visit(
                        child,
                        day,
                        depth + 1,
                        list_index,
                        marked or key in self._marker_keys,
                    )
        elif isinstance(value, list):
            for index, item in enumerate(value[:WALK_MAX_LIST_ITEMS]):
                yield from self.visit(item, inherited, depth + 1, index, marked)
