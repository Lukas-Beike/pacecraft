"""Synthetic 1 Hz run and ride streams with laps for execution tests."""

from __future__ import annotations

from typing import Any


def synthetic_session(
    segments: list[tuple[str, int, float, tuple[float, float]]],
    *,
    sport: str = "run",
    output: str = "velocity_smooth",
    cadence: float | None = 85.0,
    kinds: bool = True,
) -> dict[str, Any]:
    """1 Hz streams from (kind, seconds, output, (start hr, end hr)) segments."""
    times: list[int] = []
    values: list[float] = []
    heart: list[float] = []
    laps: list[dict[str, Any]] = []
    clock = 0
    for kind, seconds, value, (hr_start, hr_end) in segments:
        laps.append(
            {
                "start_time": float(clock),
                "end_time": float(clock + seconds),
                "kind": kind if kinds else "lap",
                "distance_m": None,
            }
        )
        for second in range(seconds):
            times.append(clock + second)
            values.append(value)
            heart.append(hr_start + (hr_end - hr_start) * second / max(1, seconds - 1))
        clock += seconds
    times.append(clock)
    values.append(values[-1])
    heart.append(heart[-1])
    streams: dict[str, Any] = {
        "time": times,
        output: values,
        "heartrate": heart,
        "moving": [True] * len(times),
    }
    if cadence is not None:
        streams["cadence"] = [cadence] * len(times)
    return {"sport": sport, "streams": streams, "laps": laps}


def erg_ride(jitter: float = 0.0) -> dict[str, Any]:
    """#3288's shape: 5 x 30 s at about 350 W held by ERG, 2:30 at 168 W, on Zwift.

    The reps' power drops 1.4 % at most while their virtual speed swings from 50 to
    30 km/h, as Zwift's terrain moves it; the shape only, no recorded values.
    ``jitter`` swings each work second's power up and down by that share around
    the lap's mean, as a trainer's per-second power does.
    """
    segments: list[tuple[str, int, float, tuple[float, float]]] = [
        ("warmup", 600, 150.0, (105.0, 125.0))
    ]
    for power in (351.0, 351.0, 351.0, 350.0, 346.0):
        segments += [
            ("work", 30, power, (130.0, 152.0)),
            ("recovery", 150, 168.0, (150.0, 132.0)),
        ]
    segments.append(("cooldown", 600, 130.0, (130.0, 110.0)))
    session = synthetic_session(segments, sport="ride", output="watts", cadence=92.0)
    session["environment"] = "virtual"
    kmh = iter((50.0, 30.0, 44.0, 38.0, 39.0))
    speed: list[float] = []
    for lap in session["laps"]:
        value = next(kmh) if lap["kind"] == "work" else 28.0
        speed += [value / 3.6] * int(lap["end_time"] - lap["start_time"])
    session["streams"]["velocity_smooth"] = [*speed, speed[-1]]
    watts = session["streams"]["watts"]
    for lap in session["laps"]:
        if lap["kind"] == "work":
            for second in range(int(lap["start_time"]), int(lap["end_time"])):
                watts[second] *= 1 + (jitter if second % 2 else -jitter)
    return session
