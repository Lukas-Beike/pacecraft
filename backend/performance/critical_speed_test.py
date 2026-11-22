"""The running critical-speed test read from one session's laps (card M4, #3919).

The Coach plans three maximal runs of 3600, 2400 and 1200 m in one visit with
about 30 min easy between (Galbraith 2014, R82). A session is that test when
its laps hold exactly one effort lap of each distance (within 3 %); warm-up,
cool-down and recovery laps never count. Each effort keeps its lap distance and
elapsed time, so the fit ``distance = CS x t + D'`` reads the three runs
themselves, not the best windows inside them. Pure; stdlib only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

METHOD: Final = "critical-speed-test-laps"
TEST_METERS: Final = (1200, 2400, 3600)
DISTANCE_TOLERANCE: Final = 0.03
_NOT_EFFORT: Final = frozenset({"warmup", "cooldown", "recovery"})


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def session_test_efforts(
    laps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    """The three effort laps (distance m, duration s), shortest first, or None.

    None unless every test distance is matched by exactly one effort lap, so a
    session of repeated 1200 m intervals is no test.
    """
    efforts: list[dict[str, Any]] = []
    for nominal in TEST_METERS:
        matches = []
        for lap in laps:
            distance = _number(lap.get("distance_m"))
            start = _number(lap.get("start_time"))
            end = _number(lap.get("end_time"))
            if (
                lap.get("kind") not in _NOT_EFFORT
                and distance is not None
                and start is not None
                and end is not None
                and end > start
                and abs(distance - nominal) <= nominal * DISTANCE_TOLERANCE
            ):
                matches.append(
                    {"distance_m": distance, "duration_seconds": end - start}
                )
        if len(matches) != 1:
            return None
        efforts.append(matches[0])
    return efforts
