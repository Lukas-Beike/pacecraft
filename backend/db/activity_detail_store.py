"""Persistence of canonical activity detail sets and per-session analysis.

Each provider activity keeps at most one set per detail kind, so several
providers can hold alternative streams, laps, zone times and strength sets for
one canonical session. Functions run inside the caller's unit of work and never commit.
Readers return only details of active records whose session is not deleted;
the rows of tombstoned activities are kept, not destroyed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sys
import zlib
from array import array
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final, cast

from backend.canonical.activity_detail import (
    DFA_SERIES_METHOD,
    LAP_METRIC_FIELDS,
    MAX_STREAM_POINTS,
    ActivityDetailSet,
    ActivityDfaAlpha1,
    ActivityLap,
    ActivityLapSet,
    ActivitySensor,
    ActivityStreamSet,
    ActivityStrengthSet,
    ActivityStrengthSetSet,
    DeviceZoneBasis,
    DfaWindow,
    WorkoutStep,
    ZoneBucket,
    ZoneDistribution,
    without_positions,
)
from backend.canonical.records import Provenance
from backend.canonical.sensor_registry import SensorUse
from backend.canonical.vocabulary import (
    POSITION_CHANNELS,
    ActivityDetailKind,
    ActivityDetailOrigin,
    ActivityRecordState,
    ActivitySessionState,
    DeviceHeartRateCalc,
    DevicePowerCalc,
    DfaQuality,
    ExerciseCategory,
    HeartRateSource,
    LapKind,
    LapSource,
    LapTrigger,
    PowerSource,
    ProviderId,
    ProviderRecordState,
    RecordingOrigin,
    SensorRole,
    SourceKind,
    Sport,
    StreamChannel,
    StreamResolution,
    StrengthSetSource,
    StrengthSetType,
    WorkoutDurationType,
    WorkoutStepIntensity,
    WorkoutTargetType,
    WorkoutTargetUnit,
    ZoneBoundarySource,
    ZoneMetric,
)
from backend.db.canonical_store import (
    WriteOutcome,
    canonical_json,
    insert_row,
    update_row,
)

# Stored blobs are bounded by the schema; decompression never exceeds the
# declared point count, so a corrupt or hostile blob cannot inflate memory.
MAX_CHANNEL_BLOB_BYTES: Final = 4 * 1024 * 1024
_POINT_BYTES: Final = 8
_SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")

_LAP_METRIC_COLUMNS: Final = LAP_METRIC_FIELDS
_LAP_COLUMNS: Final = (
    "lap_index",
    "kind",
    "start_offset_s",
    "end_offset_s",
    "name",
    *_LAP_METRIC_COLUMNS,
    "workout_step_index",
    "lap_trigger",
)
_STRENGTH_COLUMNS: Final = (
    "set_index",
    "set_type",
    "exercise_category",
    "exercise_name",
    "reps",
    "weight_kg",
    "duration_s",
    "start_offset_s",
    "workout_step_index",
)
_CHILD_TABLES: Final = (
    "activity_stream_channels",
    "activity_laps",
    "activity_zone_times",
    "activity_strength_sets",
)
_ZONE_METRIC_BY_KIND: Final = {metric.detail_kind: metric for metric in ZoneMetric}
LOGGER = logging.getLogger("intervals_coach")


@dataclass(frozen=True, slots=True)
class StoredDetailSet:
    """One stored detail set with the bookkeeping that decides re-fetching."""

    provider: str
    provider_activity_id: str
    activity_id: str
    mapper_version: int
    summary_sha256: str | None
    fetched_at: str
    detail: ActivityDetailSet


def encode_series(values: Sequence[float | None]) -> tuple[bytes, int]:
    """Return the zlib-compressed little-endian float64 points and the valid count.

    Gaps are stored as NaN; validated series never contain NaN otherwise.
    """
    points, valid = _pack_series(values)
    return _compress(points), valid


def _compress(points: bytes) -> bytes:
    return zlib.compress(points, 6)


def _pack_series(values: Sequence[float | None]) -> tuple[bytes, int]:
    points = array("d", (math.nan if value is None else value for value in values))
    if sys.byteorder != "little":
        points.byteswap()
    return points.tobytes(), sum(value is not None for value in values)


def decode_series(
    blob: bytes, point_count: int, valid_count: int | None = None
) -> tuple[float | None, ...]:
    """Decode a stored channel; raise ValueError when it does not match its counts."""
    if not 1 <= point_count <= MAX_STREAM_POINTS:
        raise ValueError("point_count is out of range")
    if not isinstance(blob, bytes | bytearray) or len(blob) > MAX_CHANNEL_BLOB_BYTES:
        raise ValueError("stream data is not a bounded blob")
    expected = point_count * _POINT_BYTES
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(bytes(blob), expected + 1)
    except zlib.error as error:
        raise ValueError("stream data is not valid zlib data") from error
    if (
        len(raw) != expected
        or decompressor.unconsumed_tail
        or decompressor.unused_data
        or not decompressor.eof
    ):
        raise ValueError("stream data does not match its point count")
    points = array("d")
    points.frombytes(raw)
    if sys.byteorder != "little":
        points.byteswap()
    values = tuple(None if math.isnan(value) else value for value in points)
    if valid_count is not None and valid_count != point_count - values.count(None):
        raise ValueError("stream data does not match its valid count")
    return values


GPS_TRACKS_KEY: Final = "gps_tracks_enabled"


def gps_tracks_enabled(db: Any) -> bool:
    """Whether new GPS tracks are stored; on unless the athlete turned it off."""
    row = db.execute("SELECT value FROM kv WHERE key = ?", (GPS_TRACKS_KEY,)).fetchone()
    return row is None or row["value"] != "0"


def stored_track_count(db: Any) -> int:
    """Count the stream sets that hold a stored GPS position."""
    names = sorted(channel.value for channel in POSITION_CHANNELS)
    row = db.execute(
        "SELECT COUNT(DISTINCT provider || char(31) || provider_activity_id) AS total "
        f"FROM activity_stream_channels WHERE channel IN ({', '.join('?' * len(names))})",
        names,
    ).fetchone()
    return int(row["total"])


def delete_stored_tracks(db: Any) -> int:
    """Delete every stored GPS position channel; return the affected stream sets.

    The sets keep their other channels and get the content hash of the
    remaining channels, so later fetches compare against what is stored.
    """
    names = sorted(channel.value for channel in POSITION_CHANNELS)
    marks = ", ".join("?" * len(names))
    keys = [
        (row["provider"], row["provider_activity_id"])
        for row in db.execute(
            "SELECT DISTINCT provider, provider_activity_id FROM activity_stream_channels "
            f"WHERE channel IN ({marks})",
            names,
        )
    ]
    db.execute(
        f"DELETE FROM activity_stream_channels WHERE channel IN ({marks})", names
    )
    for provider, activity_id in keys:
        _rehash_stream_set(db, provider, activity_id)
    return len(keys)


def _rehash_stream_set(db: Any, provider: str, provider_activity_id: str) -> None:
    key = (provider, provider_activity_id, ActivityDetailKind.STREAMS.value)
    header = db.execute(
        "SELECT mapper_version, origin, source_kind, recorded_by, source_record_id, "
        "resolution, hr_source, power_source, sensors, dfa_alpha1, device_zones "
        "FROM activity_detail_sets WHERE provider = ? "
        "AND provider_activity_id = ? AND detail_kind = ?",
        key,
    ).fetchone()
    if header is None:
        return
    children: list[tuple[object, ...]] = []
    for row in db.execute(
        "SELECT channel, point_count, valid_count, data FROM activity_stream_channels "
        "WHERE provider = ? AND provider_activity_id = ? ORDER BY channel",
        key[:2],
    ):
        values = decode_series(row["data"], row["point_count"], row["valid_count"])
        points, valid = _pack_series(values)
        children.append((row["channel"], row["point_count"], valid, points))
    content = _content_hash(
        _header_fields(
            detail_kind=key[2],
            mapper_version=header["mapper_version"],
            origin=header["origin"],
            source_kind=header["source_kind"],
            recorded_by=header["recorded_by"],
            source_record_id=header["source_record_id"],
            resolution=header["resolution"],
            lap_source=None,
            zone_boundary_source=None,
            optional={
                "hr_source": header["hr_source"],
                "power_source": header["power_source"],
                "sensors": header["sensors"],
                "dfa_alpha1": header["dfa_alpha1"],
                "device_zones": header["device_zones"],
            },
        ),
        children,
    )
    db.execute(
        "UPDATE activity_detail_sets SET content_sha256 = ? WHERE provider = ? "
        "AND provider_activity_id = ? AND detail_kind = ?",
        (content, *key),
    )


def _validate_write_inputs(mapper_version: object, summary_sha256: str | None) -> None:
    if isinstance(mapper_version, bool) or not isinstance(mapper_version, int):
        raise TypeError("mapper_version must be an integer")
    if mapper_version < 1:
        raise ValueError("mapper_version must be at least 1")
    if summary_sha256 is not None and not _SHA256_PATTERN.fullmatch(summary_sha256):
        raise ValueError("summary_sha256 must be a lowercase SHA-256 hex digest")


def replace_detail_set(
    db: Any,
    detail: ActivityDetailSet,
    *,
    mapper_version: int,
    summary_sha256: str | None,
    now: str,
) -> WriteOutcome:
    """Store ``detail`` as the provider activity's only set of its kind.

    The provider activity record must exist. Equal content only refreshes the
    fetch bookkeeping and returns UNCHANGED; changed content replaces all rows.
    """
    _validate_write_inputs(mapper_version, summary_sha256)
    if isinstance(detail, ActivityStreamSet) and not gps_tracks_enabled(db):
        stripped = without_positions(detail)
        if stripped is None:
            return WriteOutcome.UNCHANGED
        detail = stripped
    provenance = detail.provenance
    if provenance.provider is None:
        raise ValueError("provenance.provider is required for activity details")
    provider = provenance.provider.value
    key = (provider, detail.provider_activity_id, detail.kind.value)
    header = _header(detail, mapper_version)
    children = _child_rows(detail)
    content_sha256 = _content_hash(header, children)
    fetched_at = (
        provenance.retrieved_at.isoformat()
        if provenance.retrieved_at is not None
        else now
    )
    existing = db.execute(
        "SELECT content_sha256 FROM activity_detail_sets "
        "WHERE provider = ? AND provider_activity_id = ? AND detail_kind = ?",
        key,
    ).fetchone()
    if existing is not None and existing["content_sha256"] == content_sha256:
        db.execute(
            "UPDATE activity_detail_sets SET summary_sha256 = ?, fetched_at = ? "
            "WHERE provider = ? AND provider_activity_id = ? AND detail_kind = ?",
            (summary_sha256, fetched_at, *key),
        )
        return WriteOutcome.UNCHANGED
    row = {
        **dict.fromkeys(_OPTIONAL_HEADER_KEYS),
        **{
            column: value for column, value in header.items() if column != "detail_kind"
        },
        "summary_sha256": summary_sha256,
        "content_sha256": content_sha256,
        "fetched_at": fetched_at,
        "updated_at": now,
    }
    where = dict(zip(("provider", "provider_activity_id", "detail_kind"), key))
    if existing is None:
        insert_row(db, "activity_detail_sets", {**where, **row, "created_at": now})
    else:
        _delete_children(db, key)
        update_row(db, "activity_detail_sets", row, where)
    _insert_children(db, key, detail, children)
    return WriteOutcome.WRITTEN if existing is None else WriteOutcome.UPDATED


def detail_sets_for(
    db: Any, provider: str, provider_activity_id: str
) -> list[StoredDetailSet]:
    """Return the readable sets of one active provider activity."""
    return _read_sets(
        db,
        "r.provider = ? AND r.provider_activity_id = ?",
        (provider, provider_activity_id),
    )


def detail_sets_for_session(db: Any, activity_id: str) -> list[StoredDetailSet]:
    """Return the readable sets of every provider record bundled in one session.

    Records of sessions merged into ``activity_id`` belong to it as alternatives.
    A deleted session returns nothing.
    """
    return _read_sets(
        db,
        "(s.activity_id = ? OR (s.state = ? AND s.merged_into = ?)) "
        "AND NOT EXISTS (SELECT 1 FROM activity_sessions AS owner "
        "WHERE owner.activity_id = ? AND owner.state = ?)",
        (
            activity_id,
            ActivitySessionState.DUPLICATE.value,
            activity_id,
            activity_id,
            ActivitySessionState.DELETED.value,
        ),
    )


@dataclass(frozen=True, slots=True)
class FullStreamKey:
    """Identity and version of one stored full-resolution stream set."""

    provider: str
    provider_activity_id: str
    content_sha256: str
    summary_sha256: str | None
    # The merge breaks ties between equal sets by the newest fetch.
    fetched_at: str


def strength_sets_for_sessions(
    db: Any,
    activity_ids: Iterable[str],
    *,
    rank: Callable[[StoredDetailSet], Any],
) -> dict[str, StoredDetailSet]:
    """Return the one strength set list per session that ``rank`` puts first.

    Lists of sessions merged into one of ``activity_ids`` belong to that
    survivor. Headers are read in chunks; only the chosen list's sets are read.
    """
    ids = list(
        dict.fromkeys(activity_id for activity_id in activity_ids if activity_id)
    )
    chosen: dict[str, tuple[Any, dict[str, Any], StoredDetailSet]] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        marks = ", ".join("?" * len(chunk))
        rows = db.execute(
            "SELECT CASE WHEN s.state = ? THEN s.merged_into ELSE s.activity_id END "
            "AS owner, d.provider, d.provider_activity_id, d.detail_kind, "
            "d.mapper_version, d.origin, d.source_kind, d.recorded_by, "
            "d.source_record_id, d.set_source, d.summary_sha256, d.fetched_at, "
            "r.activity_id FROM activity_detail_sets AS d "
            "JOIN activity_records AS r ON r.provider = d.provider "
            "AND r.provider_activity_id = d.provider_activity_id "
            "JOIN activity_sessions AS s ON s.activity_id = r.activity_id "
            "WHERE r.state = ? AND s.state <> ? AND d.detail_kind = ? "
            f"AND (s.activity_id IN ({marks}) "
            f"OR (s.state = ? AND s.merged_into IN ({marks})))",
            (
                ActivitySessionState.DUPLICATE.value,
                ActivityRecordState.ACTIVE.value,
                ActivitySessionState.DELETED.value,
                ActivityDetailKind.STRENGTH_SETS.value,
                *chunk,
                ActivitySessionState.DUPLICATE.value,
                *chunk,
            ),
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            # The header alone ranks the list; its sets are read only when chosen.
            # An unreadable header skips that list only, like an unreadable set.
            try:
                header = _stored(row, _strength_header(row))
            except (TypeError, ValueError) as error:
                _log_unreadable(row, error)
                continue
            key = rank(header)
            current = chosen.get(row["owner"])
            if current is None or key < current[0]:
                chosen[row["owner"]] = (key, row, header)
    result: dict[str, StoredDetailSet] = {}
    for owner, (_key, row, _header) in chosen.items():
        try:
            detail = _rebuild(db, row)
        except (TypeError, ValueError) as error:
            _log_unreadable(row, error)
            continue
        result[owner] = _stored(row, detail)
    return result


def _strength_header(row: Mapping[str, Any]) -> ActivityStrengthSetSet:
    """A placeholder list with the stored header, for ranking without the sets."""
    return ActivityStrengthSetSet(
        provider_activity_id=row["provider_activity_id"],
        provenance=_provenance(row),
        origin=ActivityDetailOrigin(row["origin"]),
        source=StrengthSetSource(row["set_source"]),
        sets=(ActivityStrengthSet(index=0, set_type=StrengthSetType.ACTIVE),),
    )


def _stored(row: Mapping[str, Any], detail: ActivityDetailSet) -> StoredDetailSet:
    return StoredDetailSet(
        provider=row["provider"],
        provider_activity_id=row["provider_activity_id"],
        activity_id=row["activity_id"],
        mapper_version=row["mapper_version"],
        summary_sha256=row["summary_sha256"],
        fetched_at=row["fetched_at"],
        detail=detail,
    )


# The detail-set kinds the session curves read: full-resolution streams and laps.
_FULL_STREAMS: Final = (
    "d.detail_kind = ? AND d.resolution = ?",
    (ActivityDetailKind.STREAMS.value, StreamResolution.FULL.value),
)
_LAPS: Final = ("d.detail_kind = ?", (ActivityDetailKind.LAPS.value,))
_RECORD_SOURCE: Final = (
    "activity_records AS r JOIN activity_sessions AS s ON s.activity_id = r.activity_id"
)
_SET_SOURCE: Final = (
    "activity_detail_sets AS d JOIN activity_records AS r "
    "ON r.provider = d.provider AND r.provider_activity_id = d.provider_activity_id "
    "JOIN activity_sessions AS s ON s.activity_id = r.activity_id"
)


def full_stream_keys(
    db: Any, activity_ids: Iterable[str]
) -> dict[str, tuple[FullStreamKey, ...]]:
    """Return the keys of each session's full-resolution stream sets.

    Like ``detail_sets_for_session``, records of sessions merged into a session
    count as its own. Sessions without such a set are absent. No sensor data is
    decoded, so a caller can tell whether a computed curve is still current.
    A stored Intervals.icu set is never a key: Intervals.icu is output only
    (#1685, #2148), as for every key read here and ``full_stream_sets_for_session``.
    """
    return _set_keys(db, activity_ids, *_FULL_STREAMS)


def lap_set_keys(
    db: Any, activity_ids: Iterable[str]
) -> dict[str, tuple[FullStreamKey, ...]]:
    """Return the keys of each session's lap sets, as ``full_stream_keys`` does."""
    return _set_keys(db, activity_ids, *_LAPS)


def analysis_set_keys(
    db: Any, activity_ids: Iterable[str]
) -> dict[str, tuple[FullStreamKey, ...]]:
    """Return the keys of each session's stream and lap sets of any resolution."""
    return _set_keys(
        db,
        activity_ids,
        "d.detail_kind IN (?, ?)",
        (ActivityDetailKind.STREAMS.value, ActivityDetailKind.LAPS.value),
    )


def session_curve_inputs(
    db: Any, activity_ids: Iterable[str], record_kind: str
) -> list[tuple[Any, ...]]:
    """Return the stored rows the session curves of ``activity_ids`` are read from.

    The same sessions, merged copies and sets that ``curves`` reads (the rules of
    ``full_stream_keys`` and ``lap_set_keys``): each active record with its
    session, merge target and change time, and each full-resolution stream set and
    lap set with its content and summary hashes, fetch time and the payload hash
    of the active provider summary of ``record_kind`` that decides whether the set
    is still current. Any change to what a curve is read from changes the rows, so
    a caller can tell without reading each session whether its curves are current.
    """
    ids = list(activity_ids)
    kind_clause = f"({_FULL_STREAMS[0]}) OR ({_LAPS[0]})"
    active = ActivityRecordState.ACTIVE.value
    rows = [
        *_owned_rows(
            db,
            ids,
            "r.provider, r.provider_activity_id, r.activity_id, r.mapper_version, "
            "r.updated_at",
            _RECORD_SOURCE,
            (),
            "r.state = ?",
            (active,),
        ),
        *_owned_rows(
            db,
            ids,
            "d.provider, d.provider_activity_id, d.detail_kind, d.content_sha256, "
            "d.summary_sha256, d.fetched_at, p.payload_hash",
            f"{_SET_SOURCE} LEFT JOIN provider_records AS p ON p.provider = d.provider "
            "AND p.source_record_id = d.provider_activity_id AND p.record_kind = ? "
            "AND p.state = ?",
            (record_kind, ProviderRecordState.ACTIVE.value),
            f"r.state = ? AND ({kind_clause})",
            (active, *_FULL_STREAMS[1], *_LAPS[1]),
        ),
    ]
    # A record bundled in two requested sessions comes back twice.
    return sorted({tuple(dict(row).values()) for row in rows}, key=repr)


def _owned_rows(
    db: Any,
    activity_ids: list[str],
    columns: str,
    source: str,
    source_values: tuple[str, ...],
    where: str,
    where_values: tuple[str, ...],
) -> list[Any]:
    """Return ``columns`` of the rows owned by each live session in ``activity_ids``.

    Rows of records of the session itself and of sessions merged into it count as
    its own, and the ``owner`` column names that session; rows of records whose
    own session is deleted are left out. ``source`` is the FROM clause with the
    aliases ``r`` and ``s``.
    """
    ids = list(
        dict.fromkeys(activity_id for activity_id in activity_ids if activity_id)
    )
    rows: list[Any] = []
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        marks = ", ".join("?" * len(chunk))
        rows += db.execute(
            "SELECT CASE WHEN s.state = ? THEN s.merged_into ELSE s.activity_id END "
            f"AS owner, {columns} FROM {source} WHERE s.state <> ? AND {where} "
            f"AND (s.activity_id IN ({marks}) "
            f"OR (s.state = ? AND s.merged_into IN ({marks})))",
            (
                ActivitySessionState.DUPLICATE.value,
                *source_values,
                ActivitySessionState.DELETED.value,
                *where_values,
                *chunk,
                ActivitySessionState.DUPLICATE.value,
                *chunk,
            ),
        ).fetchall()
    return rows


def _set_keys(
    db: Any,
    activity_ids: Iterable[str],
    kind_clause: str,
    kind_values: tuple[str, ...],
) -> dict[str, tuple[FullStreamKey, ...]]:
    keys: dict[str, set[FullStreamKey]] = {}
    for row in _owned_rows(
        db,
        list(activity_ids),
        "d.provider, d.provider_activity_id, d.content_sha256, d.summary_sha256, "
        "d.fetched_at",
        _SET_SOURCE,
        (),
        f"r.state = ? AND d.provider != ? AND ({kind_clause})",
        (ActivityRecordState.ACTIVE.value, ProviderId.INTERVALS.value, *kind_values),
    ):
        keys.setdefault(row["owner"], set()).add(
            FullStreamKey(
                provider=row["provider"],
                provider_activity_id=row["provider_activity_id"],
                content_sha256=row["content_sha256"],
                summary_sha256=row["summary_sha256"],
                fetched_at=row["fetched_at"],
            )
        )
    return {
        owner: tuple(sorted(values, key=lambda key: key.content_sha256))
        for owner, values in keys.items()
    }


def full_stream_sets_for_session(db: Any, activity_id: str) -> list[StoredDetailSet]:
    """Return the full-resolution stream sets of ``detail_sets_for_session``.

    Without Intervals.icu sets, as ``full_stream_keys``.
    """
    return _read_sets(
        db,
        "d.detail_kind = ? AND d.resolution = ? AND d.provider != ? "
        "AND (s.activity_id = ? OR (s.state = ? AND s.merged_into = ?)) "
        "AND NOT EXISTS (SELECT 1 FROM activity_sessions AS owner "
        "WHERE owner.activity_id = ? AND owner.state = ?)",
        (
            ActivityDetailKind.STREAMS.value,
            StreamResolution.FULL.value,
            ProviderId.INTERVALS.value,
            activity_id,
            ActivitySessionState.DUPLICATE.value,
            activity_id,
            activity_id,
            ActivitySessionState.DELETED.value,
        ),
    )


def lap_sets_for_session(db: Any, activity_id: str) -> list[StoredDetailSet]:
    """Return only the lap sets of ``detail_sets_for_session``."""
    return _read_sets(
        db,
        "d.detail_kind = ? "
        "AND (s.activity_id = ? OR (s.state = ? AND s.merged_into = ?)) "
        "AND NOT EXISTS (SELECT 1 FROM activity_sessions AS owner "
        "WHERE owner.activity_id = ? AND owner.state = ?)",
        (
            ActivityDetailKind.LAPS.value,
            activity_id,
            ActivitySessionState.DUPLICATE.value,
            activity_id,
            activity_id,
            ActivitySessionState.DELETED.value,
        ),
    )


def strength_record_ids(
    db: Any, provider: str, provider_activity_ids: Collection[str]
) -> set[str]:
    """Return the given provider activity ids whose record is a strength activity."""
    ids = sorted(set(provider_activity_ids))
    if not ids:
        return set()
    return {
        row["provider_activity_id"]
        for row in db.execute(
            "SELECT provider_activity_id FROM activity_records WHERE provider = ? "
            f"AND sport = ? AND provider_activity_id IN ({', '.join('?' for _ in ids)})",
            (provider, Sport.STRENGTH.value, *ids),
        ).fetchall()
    }


def activity_detail_backfill_universe(
    db: Any, provider: str, *, since: datetime
) -> list[str]:
    """Return ids of active records of ``provider`` recorded since ``since``.

    The backfill uses this window universe to prune its empty-marker map.
    """
    return [
        row["provider_activity_id"]
        for row in db.execute(
            "SELECT DISTINCT provider_activity_id FROM activity_records "
            "WHERE provider = ? AND state = ? AND julianday(started_at) >= julianday(?)",
            (provider, ActivityRecordState.ACTIVE.value, since.isoformat()),
        ).fetchall()
    ]


def activity_detail_backfill_candidates(
    db: Any,
    provider: str,
    record_kind: str,
    *,
    since: datetime,
    limit: int,
    mapper_version: int,
    retry_ids: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Return recent records whose details are missing or outdated, newest first.

    A record qualifies when it is an active, non-manual record of a live session
    and either has no detail set or only sets whose known summary hash differs
    from the current active raw record. Once one kind was fetched for the current
    summary, kinds Garmin no longer returns do not make the record a candidate
    again, so the backfill does not refetch it on every sync. A set without a
    known hash counts as current. A set stored by an older ``mapper_version`` than
    the one given is outdated, so a mapper change refetches the window.
    ``started_at`` and ``since`` are compared as instants (julianday), so stored
    UTC offsets order correctly. Each row carries the current summary hash,
    which is stored with any fetched details, and ``replay``: True when a set of
    the current summary exists whose mapper version is older, so the record is
    not new or changed but re-fetched for a mapper change (also after a replay
    that kept some of its sets).
    ``retry_ids`` qualify despite current sets, because their last fetch was
    incomplete; the caller bounds how often they are retried.
    """
    retry = sorted(set(retry_ids))
    retry_clause = (
        f" OR r.provider_activity_id IN ({', '.join('?' for _ in retry)})"
        if retry
        else ""
    )
    rows = db.execute(
        "SELECT r.provider_activity_id, r.recorded_by, p.payload_hash AS summary_sha256, "
        "EXISTS (SELECT 1 FROM activity_detail_sets AS c "
        "WHERE c.provider = r.provider AND c.provider_activity_id = r.provider_activity_id "
        "AND (c.summary_sha256 IS NULL OR c.summary_sha256 = p.payload_hash) "
        "AND c.mapper_version < ?) AS replay "
        "FROM activity_records AS r "
        "JOIN provider_records AS p ON p.provider = r.provider "
        "AND p.record_kind = ? AND p.source_record_id = r.provider_activity_id AND p.state = ? "
        "JOIN activity_sessions AS s ON s.activity_id = r.activity_id AND s.state <> ? "
        "WHERE r.provider = ? AND r.state = ? AND r.recorded_by <> ? "
        "AND julianday(r.started_at) >= julianday(?) "
        "AND (NOT EXISTS (SELECT 1 FROM activity_detail_sets AS d "
        "WHERE d.provider = r.provider AND d.provider_activity_id = r.provider_activity_id "
        "AND (d.summary_sha256 IS NULL OR d.summary_sha256 = p.payload_hash) "
        "AND d.mapper_version >= ?)"
        + retry_clause
        + ") ORDER BY julianday(r.started_at) DESC, r.provider_activity_id LIMIT ?",
        (
            mapper_version,
            record_kind,
            ProviderRecordState.ACTIVE.value,
            ActivitySessionState.DELETED.value,
            provider,
            ActivityRecordState.ACTIVE.value,
            RecordingOrigin.MANUAL.value,
            since.isoformat(),
            mapper_version,
            *retry,
            limit,
        ),
    ).fetchall()
    return [
        {
            "provider_activity_id": row["provider_activity_id"],
            "recorded_by": row["recorded_by"],
            "summary_sha256": row["summary_sha256"],
            "replay": bool(row["replay"]),
        }
        for row in rows
    ]


def outdated_file_set_records(
    db: Any, provider: str, record_kind: str, *, since: datetime, mapper_version: int
) -> set[str]:
    """Ids of window records whose streams or laps are below ``mapper_version``.

    Records as in ``activity_detail_backfill_candidates`` (active, non-manual, of a
    live session, started since ``since``); only sets of the current summary count,
    and only the streams and laps kinds, which a file replaces.
    """
    rows = db.execute(
        "SELECT DISTINCT r.provider_activity_id FROM activity_records AS r "
        "JOIN provider_records AS p ON p.provider = r.provider "
        "AND p.record_kind = ? AND p.source_record_id = r.provider_activity_id AND p.state = ? "
        "JOIN activity_sessions AS s ON s.activity_id = r.activity_id AND s.state <> ? "
        "WHERE r.provider = ? AND r.state = ? AND r.recorded_by <> ? "
        "AND julianday(r.started_at) >= julianday(?) "
        "AND EXISTS (SELECT 1 FROM activity_detail_sets AS d "
        "WHERE d.provider = r.provider AND d.provider_activity_id = r.provider_activity_id "
        "AND (d.summary_sha256 IS NULL OR d.summary_sha256 = p.payload_hash) "
        "AND d.detail_kind IN (?, ?) AND d.mapper_version < ?)",
        (
            record_kind,
            ProviderRecordState.ACTIVE.value,
            ActivitySessionState.DELETED.value,
            provider,
            ActivityRecordState.ACTIVE.value,
            RecordingOrigin.MANUAL.value,
            since.isoformat(),
            ActivityDetailKind.STREAMS.value,
            ActivityDetailKind.LAPS.value,
            mapper_version,
        ),
    ).fetchall()
    return {row["provider_activity_id"] for row in rows}


def sensor_uses(db: Any) -> list[SensorUse]:
    """Return the sensor lists of every live session's stream sets, oldest first.

    Records of one session that name the same sensors are separate entries; the
    registry de-duplicates by sensor key. A merged alias reports the session it
    is merged into, so a bundled activity counts once for a sensor. Rows with an
    unreadable start or sensor list are skipped.
    """
    uses: list[SensorUse] = []
    for row in db.execute(
        "SELECT CASE WHEN s.state = ? THEN s.merged_into ELSE r.activity_id END "
        "AS session_id, r.sport, r.started_at, d.sensors "
        "FROM activity_detail_sets AS d "
        "JOIN activity_records AS r ON r.provider = d.provider "
        "AND r.provider_activity_id = d.provider_activity_id "
        "JOIN activity_sessions AS s ON s.activity_id = r.activity_id "
        "WHERE d.detail_kind = ? AND d.sensors IS NOT NULL AND r.state = ? "
        "AND s.state <> ? ORDER BY julianday(r.started_at), r.provider_activity_id",
        (
            ActivitySessionState.DUPLICATE.value,
            ActivityDetailKind.STREAMS.value,
            ActivityRecordState.ACTIVE.value,
            ActivitySessionState.DELETED.value,
        ),
    ).fetchall():
        sensors = stored_sensors(row["sensors"])
        try:
            started_at = datetime.fromisoformat(row["started_at"])
        except TypeError, ValueError:
            continue
        if sensors:
            uses.append(SensorUse(row["session_id"], row["sport"], started_at, sensors))
    return uses


@dataclass(frozen=True, slots=True)
class SessionFirstThreshold:
    """The first threshold estimate stored for one session, on its local day."""

    session_id: str
    day: date
    result: ActivityDfaAlpha1


def first_threshold_results(
    db: Any, *, since: date, until: date, sport: Sport
) -> list[SessionFirstThreshold]:
    """Return one stored DFA-a1 estimate per live session of ``sport`` in a day range.

    Records of one session that carry an estimate compete: the better quality
    wins, then one of method 3 or later (with its window series), then the one
    with more windows. Methods 2 and 3 both count. Rows with an unreadable day or
    without a 0.75 estimate are skipped. Oldest session first.
    """
    return _session_dfa_results(
        db,
        since=since,
        until=until,
        sport=sport,
        keep=lambda result: result.estimate_bpm is not None,
        rank=_dfa_rank,
    )


def dfa_series_results(
    db: Any, *, since: date, until: date, sport: Sport
) -> list[SessionFirstThreshold]:
    """Return one stored DFA-a1 result per live session for the window series (#3273).

    Within a session a record whose series is readable wins, then the one with
    more windows; a session without one keeps its older record, which counts as
    not evaluated. Oldest session first.
    """
    return _session_dfa_results(
        db,
        since=since,
        until=until,
        sport=sport,
        keep=lambda _result: True,
        rank=lambda result: (1 if result.series_evaluated else 0, result.windows),
    )


def _session_dfa_results(
    db: Any,
    *,
    since: date,
    until: date,
    sport: Sport,
    keep: Callable[[ActivityDfaAlpha1], bool],
    rank: Callable[[ActivityDfaAlpha1], tuple[int, ...]],
) -> list[SessionFirstThreshold]:
    best: dict[str, SessionFirstThreshold] = {}
    for row in db.execute(
        "SELECT CASE WHEN s.state = ? THEN s.merged_into ELSE s.activity_id END "
        "AS session_id, "
        "COALESCE(s.local_date, substr(r.started_at, 1, 10)) AS day, d.dfa_alpha1 "
        "FROM activity_detail_sets AS d "
        "JOIN activity_records AS r ON r.provider = d.provider "
        "AND r.provider_activity_id = d.provider_activity_id "
        "JOIN activity_sessions AS s ON s.activity_id = r.activity_id "
        "WHERE d.detail_kind = ? AND d.dfa_alpha1 IS NOT NULL AND r.state = ? "
        "AND s.state <> ? AND r.sport = ? "
        "AND COALESCE(s.local_date, substr(r.started_at, 1, 10)) BETWEEN ? AND ? "
        "ORDER BY day, r.provider_activity_id",
        (
            ActivitySessionState.DUPLICATE.value,
            ActivityDetailKind.STREAMS.value,
            ActivityRecordState.ACTIVE.value,
            ActivitySessionState.DELETED.value,
            sport.value,
            since.isoformat(),
            until.isoformat(),
        ),
    ).fetchall():
        result = stored_dfa_alpha1(row["dfa_alpha1"])
        try:
            day = date.fromisoformat(str(row["day"]))
        except ValueError:
            continue
        if result is None or not keep(result):
            continue
        found = best.get(row["session_id"])
        if found is None or rank(result) > rank(found.result):
            best[row["session_id"]] = SessionFirstThreshold(
                row["session_id"], day, result
            )
    return sorted(best.values(), key=lambda item: (item.day, item.session_id))


def dfa_series_counts(db: Any, *, since: date) -> dict[str, int]:
    """Chest-strap run stream sets since ``since`` by DFA-a1 method, counts only.

    ``with_series`` rows hold a readable window series (method 3 or later,
    #3273), ``unreadable_series`` rows of method 3 or later do not,
    ``before_series`` rows come from an earlier method, and ``without_result``
    rows hold no DFA-a1 result: too few windows, a file without RR intervals or,
    before method 3, a recording without a 0.75 crossing, so such a row is never
    read as "no crossing".
    """
    counts = dict.fromkeys(
        ("runs", "with_series", "unreadable_series", "before_series", "without_result"),
        0,
    )
    for row in db.execute(
        "SELECT d.dfa_alpha1 "
        "FROM activity_detail_sets AS d "
        "JOIN activity_records AS r ON r.provider = d.provider "
        "AND r.provider_activity_id = d.provider_activity_id "
        "JOIN activity_sessions AS s ON s.activity_id = r.activity_id "
        "WHERE d.detail_kind = ? AND d.hr_source = ? AND r.state = ? "
        "AND s.state <> ? AND r.sport = ? "
        "AND COALESCE(s.local_date, substr(r.started_at, 1, 10)) >= ?",
        (
            ActivityDetailKind.STREAMS.value,
            HeartRateSource.CHEST_STRAP.value,
            ActivityRecordState.ACTIVE.value,
            ActivitySessionState.DELETED.value,
            Sport.RUN.value,
            since.isoformat(),
        ),
    ).fetchall():
        counts["runs"] += 1
        result = stored_dfa_alpha1(row["dfa_alpha1"])
        if result is None:
            counts["without_result"] += 1
        elif result.series_evaluated:
            counts["with_series"] += 1
        elif result.method >= DFA_SERIES_METHOD:
            counts["unreadable_series"] += 1
        else:
            counts["before_series"] += 1
    return counts


def _dfa_rank(result: ActivityDfaAlpha1) -> tuple[int, int, int]:
    """The better quality first; among equals a record with the window series (#3273)."""
    return (
        1 if result.quality is DfaQuality.GOOD else 0,
        1 if result.method >= DFA_SERIES_METHOD else 0,
        result.windows,
    )


def stored_strength_source(
    db: Any, provider: str, provider_activity_id: str
) -> str | None:
    """Return the source of the stored strength set list of one provider activity."""
    row = db.execute(
        "SELECT set_source FROM activity_detail_sets WHERE provider = ? "
        "AND provider_activity_id = ? AND detail_kind = ?",
        (provider, provider_activity_id, ActivityDetailKind.STRENGTH_SETS.value),
    ).fetchone()
    return None if row is None else row["set_source"]


def stored_stream_resolution(
    db: Any, provider: str, provider_activity_id: str
) -> str | None:
    """Return the resolution of the stored stream set of one provider activity."""
    row = db.execute(
        "SELECT resolution FROM activity_detail_sets WHERE provider = ? "
        "AND provider_activity_id = ? AND detail_kind = ?",
        (provider, provider_activity_id, ActivityDetailKind.STREAMS.value),
    ).fetchone()
    return None if row is None else row["resolution"]


def restamp_detail_set(
    db: Any,
    provider: str,
    provider_activity_id: str,
    kind: ActivityDetailKind,
    *,
    summary_sha256: str | None,
    fetched_at: str,
) -> None:
    """Mark a kept set as current for a summary without changing its content."""
    db.execute(
        "UPDATE activity_detail_sets SET summary_sha256 = ?, fetched_at = ? "
        "WHERE provider = ? AND provider_activity_id = ? AND detail_kind = ?",
        (summary_sha256, fetched_at, provider, provider_activity_id, kind.value),
    )


def save_analysis(
    db: Any,
    activity_id: str,
    *,
    analysis_version: int,
    input_sha256: str,
    payload: Mapping[str, Any],
    now: str,
) -> WriteOutcome:
    """Store the analysis computed for one session; equal input keeps the row."""
    if isinstance(analysis_version, bool) or not isinstance(analysis_version, int):
        raise TypeError("analysis_version must be an integer")
    if analysis_version < 1:
        raise ValueError("analysis_version must be at least 1")
    if not _SHA256_PATTERN.fullmatch(input_sha256):
        raise ValueError("input_sha256 must be a lowercase SHA-256 hex digest")
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    text = canonical_json(payload)
    existing = db.execute(
        "SELECT analysis_version, input_sha256, payload FROM activity_detail_analysis "
        "WHERE activity_id = ?",
        (activity_id,),
    ).fetchone()
    if existing is not None and (
        existing["analysis_version"],
        existing["input_sha256"],
        existing["payload"],
    ) == (analysis_version, input_sha256, text):
        return WriteOutcome.UNCHANGED
    row = {
        "analysis_version": analysis_version,
        "input_sha256": input_sha256,
        "payload": text,
        "computed_at": now,
        "updated_at": now,
    }
    if existing is None:
        insert_row(
            db,
            "activity_detail_analysis",
            {"activity_id": activity_id, **row, "created_at": now},
        )
        return WriteOutcome.WRITTEN
    update_row(db, "activity_detail_analysis", row, {"activity_id": activity_id})
    return WriteOutcome.UPDATED


def planned_unit_links(db: Any, activity_ids: Iterable[str]) -> dict[str, str]:
    """The planned unit each session's frozen target snapshot names, by session id."""
    ids = list(
        dict.fromkeys(activity_id for activity_id in activity_ids if activity_id)
    )
    links: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for row in db.execute(
            "SELECT activity_id, json_extract(payload, '$.target_snapshot.planned_unit_id') "
            "AS unit FROM activity_detail_analysis "
            f"WHERE activity_id IN ({', '.join('?' * len(chunk))})",
            chunk,
        ).fetchall():
            if isinstance(row["unit"], str) and row["unit"]:
                links[row["activity_id"]] = row["unit"]
    return links


def planned_unit_sessions(db: Any, unit_ids: Iterable[str]) -> dict[str, str]:
    """The session whose frozen target snapshot names each planned unit, by unit id."""
    ids = list(dict.fromkeys(unit_id for unit_id in unit_ids if unit_id))
    sessions: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for row in db.execute(
            "SELECT activity_id, json_extract(payload, '$.target_snapshot.planned_unit_id') "
            "AS unit FROM activity_detail_analysis WHERE json_extract(payload, "
            f"'$.target_snapshot.planned_unit_id') IN ({', '.join('?' * len(chunk))})",
            chunk,
        ).fetchall():
            sessions[row["unit"]] = row["activity_id"]
    return sessions


# Sessions whose stored curves are kept (#2821); merged and deleted ones are not.
_LIVE_SESSION_STATES: Final = (
    ActivitySessionState.ACTIVE.value,
    ActivitySessionState.HIDDEN.value,
)


def session_curves(
    db: Any, activity_ids: Iterable[str]
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Return each session's stored curve payload with its fingerprint (#2821)."""
    rows: dict[str, tuple[str, dict[str, Any]]] = {}
    for chunk in _id_chunks(activity_ids):
        for row in db.execute(
            "SELECT activity_id, fingerprint, payload FROM session_curves "
            f"WHERE activity_id IN ({', '.join('?' * len(chunk))})",
            chunk,
        ).fetchall():
            rows[row["activity_id"]] = (row["fingerprint"], json.loads(row["payload"]))
    return rows


def session_curve_fingerprints(db: Any, activity_ids: Iterable[str]) -> dict[str, str]:
    """Return each session's stored curve fingerprint without its payload."""
    fingerprints: dict[str, str] = {}
    for chunk in _id_chunks(activity_ids):
        for row in db.execute(
            "SELECT activity_id, fingerprint FROM session_curves "
            f"WHERE activity_id IN ({', '.join('?' * len(chunk))})",
            chunk,
        ).fetchall():
            fingerprints[row["activity_id"]] = row["fingerprint"]
    return fingerprints


def save_session_curve(
    db: Any,
    activity_id: str,
    *,
    fingerprint: str,
    payload: Mapping[str, Any],
    now: str,
) -> bool:
    """Store one live session's curve under its fingerprint; False for a session
    that is no longer live (merged or deleted), which keeps no curve."""
    if not _SHA256_PATTERN.fullmatch(fingerprint):
        raise ValueError("fingerprint must be a lowercase SHA-256 hex digest")
    live = db.execute(
        "SELECT 1 FROM activity_sessions WHERE activity_id = ? "
        f"AND state IN ({', '.join('?' * len(_LIVE_SESSION_STATES))})",
        (activity_id, *_LIVE_SESSION_STATES),
    ).fetchone()
    if live is None:
        return False
    db.execute(
        "INSERT INTO session_curves(activity_id, fingerprint, payload, computed_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(activity_id) DO UPDATE SET fingerprint = excluded.fingerprint, "
        "payload = excluded.payload, computed_at = excluded.computed_at, "
        "updated_at = excluded.updated_at",
        (activity_id, fingerprint, canonical_json(payload), now, now, now),
    )
    return True


def delete_unlive_session_curves(db: Any) -> int:
    """Delete the stored curves of sessions that are no longer live; returns the count."""
    return db.execute(
        "DELETE FROM session_curves WHERE activity_id IN (SELECT activity_id FROM "
        "activity_sessions WHERE state NOT IN "
        f"({', '.join('?' * len(_LIVE_SESSION_STATES))}))",
        _LIVE_SESSION_STATES,
    ).rowcount


def _id_chunks(activity_ids: Iterable[str]) -> Iterable[list[str]]:
    ids = list(
        dict.fromkeys(activity_id for activity_id in activity_ids if activity_id)
    )
    for start in range(0, len(ids), 500):
        yield ids[start : start + 500]


def analysis_rows_for(
    db: Any, activity_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Return stored analyses of sessions that are not deleted, keyed by session id."""
    ids = list(
        dict.fromkeys(activity_id for activity_id in activity_ids if activity_id)
    )
    rows: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for row in db.execute(
            "SELECT a.activity_id, a.analysis_version, a.input_sha256, a.payload, "
            "a.computed_at FROM activity_detail_analysis AS a "
            "JOIN activity_sessions AS s ON s.activity_id = a.activity_id "
            f"WHERE s.state <> ? AND a.activity_id IN ({', '.join('?' * len(chunk))})",
            (ActivitySessionState.DELETED.value, *chunk),
        ).fetchall():
            rows[row["activity_id"]] = {
                "analysis_version": row["analysis_version"],
                "input_sha256": row["input_sha256"],
                "payload": json.loads(row["payload"]),
                "computed_at": row["computed_at"],
            }
    return rows


def analysis_row_for_record(
    db: Any, provider: str, provider_activity_id: str
) -> dict[str, Any] | None:
    """Return the stored analysis of the session that holds one provider record.

    A session merged into another keeps its analysis row under its own id.
    """
    row = db.execute(
        "SELECT r.activity_id FROM activity_records AS r "
        "JOIN activity_detail_analysis AS a ON a.activity_id = r.activity_id "
        "WHERE r.provider = ? AND r.provider_activity_id = ?",
        (provider, provider_activity_id),
    ).fetchone()
    if row is None:
        return None
    return analysis_rows_for(db, [row["activity_id"]]).get(row["activity_id"])


def stream_export_rows(db: Any) -> Iterable[dict[str, Any]]:
    """Yield every stored stream channel with decoded points for the privacy export.

    Tombstoned activities are included like their records; a channel that no
    longer decodes is exported with ``values`` set to None.
    """
    for row in db.execute(
        "SELECT provider, provider_activity_id, channel, point_count, valid_count, data "
        "FROM activity_stream_channels ORDER BY provider, provider_activity_id, channel"
    ):
        try:
            values: list[float | None] | None = list(
                decode_series(row["data"], row["point_count"], row["valid_count"])
            )
        except ValueError:
            values = None
        yield {
            "provider": row["provider"],
            "provider_activity_id": row["provider_activity_id"],
            "channel": row["channel"],
            "point_count": row["point_count"],
            "valid_count": row["valid_count"],
            "values": values,
        }


def analysis_export_rows(db: Any) -> Iterable[dict[str, Any]]:
    """Yield every stored session analysis with its JSON payload decoded."""
    for row in db.execute(
        "SELECT activity_id, analysis_version, input_sha256, payload, computed_at, "
        "created_at, updated_at FROM activity_detail_analysis ORDER BY activity_id"
    ):
        exported = dict(row)
        exported["payload"] = json.loads(exported["payload"])
        yield exported


def _header(detail: ActivityDetailSet, mapper_version: int) -> dict[str, Any]:
    provenance = detail.provenance
    stream = detail if isinstance(detail, ActivityStreamSet) else None
    return _header_fields(
        detail_kind=detail.kind.value,
        mapper_version=mapper_version,
        origin=detail.origin.value,
        source_kind=provenance.source_kind.value,
        recorded_by=provenance.recorded_by.value,
        source_record_id=provenance.source_record_id,
        resolution=stream.resolution.value if stream else None,
        lap_source=(
            detail.lap_source.value if isinstance(detail, ActivityLapSet) else None
        ),
        zone_boundary_source=(
            detail.boundary_source.value
            if isinstance(detail, ZoneDistribution)
            else None
        ),
        optional={
            "set_source": (
                detail.source.value
                if isinstance(detail, ActivityStrengthSetSet)
                else None
            ),
            "hr_source": stream.hr_source.value
            if stream and stream.hr_source
            else None,
            "power_source": (
                stream.power_source.value if stream and stream.power_source else None
            ),
            "sensors": (
                json.dumps(
                    [_sensor_json(sensor) for sensor in stream.sensors],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if stream and stream.sensors
                else None
            ),
            "dfa_alpha1": (
                json.dumps(
                    _dfa_json(stream.dfa_alpha1), sort_keys=True, separators=(",", ":")
                )
                if stream and stream.dfa_alpha1 is not None
                else None
            ),
            **_watch_header(detail),
        },
    )


def _watch_header(detail: ActivityDetailSet) -> dict[str, str | None]:
    """The stored text of the watch's zone basis (streams) and workout steps (laps)."""
    zones = detail.device_zones if isinstance(detail, ActivityStreamSet) else None
    steps = detail.workout_steps if isinstance(detail, ActivityLapSet) else ()
    return {
        "device_zones": None if zones is None else _compact_json(_zones_json(zones)),
        "workout_steps": (
            _compact_json([workout_step_json(step) for step in steps])
            if steps
            else None
        ),
    }


_OPTIONAL_HEADER_KEYS = (
    "set_source",
    "hr_source",
    "power_source",
    "sensors",
    "dfa_alpha1",
    "device_zones",
    "workout_steps",
)


def _header_fields(
    *,
    detail_kind: str,
    mapper_version: int,
    origin: str,
    source_kind: str,
    recorded_by: str,
    source_record_id: str | None,
    resolution: str | None,
    lap_source: str | None,
    zone_boundary_source: str | None,
    optional: Mapping[str, str | None],
) -> dict[str, Any]:
    """The hashed header of a detail set, from values as they are stored.

    The single place that decides which keys are hashed: a write builds it from
    the detail through ``_header`` and a rehash from the stored row, so both
    produce the same content hash for the same set.
    """
    header: dict[str, Any] = {
        "detail_kind": detail_kind,
        "mapper_version": mapper_version,
        "origin": origin,
        "source_kind": source_kind,
        "recorded_by": recorded_by,
        "source_record_id": source_record_id,
        "resolution": resolution,
        "lap_source": lap_source,
        "zone_boundary_source": zone_boundary_source,
    }
    # Only a set that carries a value has its key, so sets without them keep
    # their content hash.
    # ``optional`` carries set_source, hr_source, power_source, sensors,
    # dfa_alpha1, device_zones and workout_steps as stored text.
    for key in _OPTIONAL_HEADER_KEYS:
        value = optional.get(key)
        if value:
            header[key] = value
    return header


def _dfa_json(result: ActivityDfaAlpha1) -> dict[str, Any]:
    """The stored DFA-a1 object; the series is a list of [start_s, bpm, alpha1, share].

    A result without a 0.75 crossing stores null for estimate and quality, which a
    build before method 3 reads as no result at all (#3273).
    """
    value: dict[str, Any] = {
        "estimate_bpm": result.estimate_bpm,
        "windows": result.windows,
        "artifact_share": result.artifact_share,
        "quality": None if result.quality is None else result.quality.value,
        "method": result.method,
    }
    if result.series:
        value["series"] = [
            [w.start_s, w.heart_rate_bpm, w.alpha1, w.artifact_share]
            for w in result.series
        ]
    return value


def _dfa_series(value: object) -> tuple[DfaWindow, ...]:
    """The stored window series; a malformed one is empty, never an error (#3273)."""
    if not isinstance(value, list):
        return ()
    try:
        return tuple(
            DfaWindow(start_s=a, heart_rate_bpm=b, alpha1=c, artifact_share=d)
            for a, b, c, d in value
        )
    except TypeError, ValueError:
        return ()


def stored_dfa_alpha1(text: object) -> ActivityDfaAlpha1 | None:
    """Decode a stored DFA-a1 result; an unreadable one yields None.

    The series is decoded on its own, so a malformed series leaves the 0.75
    result readable with an empty series.
    """
    if text is None:
        return None
    try:
        item = json.loads(str(text))
        quality = item["quality"]
        return ActivityDfaAlpha1(
            estimate_bpm=item["estimate_bpm"],
            windows=item["windows"],
            artifact_share=item["artifact_share"],
            quality=None if quality is None else DfaQuality(quality),
            method=item["method"],
            series=_dfa_series(item.get("series")),
        )
    except TypeError, ValueError, KeyError, AttributeError:
        return None


def _compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _enum_or_none[E: StrEnum](vocabulary: type[E], value: object) -> E | None:
    """The vocabulary member a stored value names; an unknown one is None."""
    try:
        return vocabulary(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _zones_json(zones: DeviceZoneBasis) -> dict[str, Any]:
    """The values the watch's zone basis holds, by fixed key; numbers and names only."""
    values = {
        "max_hr": zones.max_hr,
        "threshold_hr": zones.threshold_hr,
        "ftp_w": zones.ftp_w,
        "hr_calc": None if zones.hr_calc is None else zones.hr_calc.value,
        "power_calc": None if zones.power_calc is None else zones.power_calc.value,
    }
    return {key: value for key, value in values.items() if value is not None}


def stored_device_zones(text: object) -> DeviceZoneBasis | None:
    """Decode a stored zone basis, each unusable value as None; none usable is None."""
    if text is None:
        return None
    try:
        item = json.loads(str(text))
        zones = DeviceZoneBasis(
            max_hr=item.get("max_hr"),
            threshold_hr=item.get("threshold_hr"),
            ftp_w=item.get("ftp_w"),
            hr_calc=_enum_or_none(DeviceHeartRateCalc, item.get("hr_calc")),
            power_calc=_enum_or_none(DevicePowerCalc, item.get("power_calc")),
        )
    except TypeError, ValueError, AttributeError:
        return None
    return None if zones.is_empty else zones


def workout_step_json(step: WorkoutStep) -> dict[str, Any]:
    """One executed workout step by fixed key: numbers and vocabulary names only."""
    return {
        "index": step.index,
        "intensity": step.intensity.value,
        "duration_type": step.duration_type.value,
        "duration_value": step.duration_value,
        "repeat_from": step.repeat_from,
        "repeat_count": step.repeat_count,
        "target_type": step.target_type.value,
        "target_low": step.target_low,
        "target_high": step.target_high,
        "target_unit": None if step.target_unit is None else step.target_unit.value,
        "target_zone": step.target_zone,
    }


def stored_workout_steps(text: object) -> tuple[WorkoutStep, ...]:
    """Decode stored workout steps; an unreadable list yields no steps."""
    if text is None:
        return ()
    try:
        return tuple(
            WorkoutStep(
                index=item["index"],
                intensity=WorkoutStepIntensity(item["intensity"]),
                duration_type=WorkoutDurationType(item["duration_type"]),
                duration_value=item.get("duration_value"),
                repeat_from=item.get("repeat_from"),
                repeat_count=item.get("repeat_count"),
                target_type=WorkoutTargetType(item["target_type"]),
                target_low=item.get("target_low"),
                target_high=item.get("target_high"),
                target_unit=_enum_or_none(WorkoutTargetUnit, item.get("target_unit")),
                target_zone=item.get("target_zone"),
            )
            for item in json.loads(str(text))
        )
    except TypeError, ValueError, KeyError, AttributeError:
        return ()


def _sensor_json(sensor: ActivitySensor) -> dict[str, Any]:
    # ``key`` is a pseudonymous hash of the serial, not the serial itself.
    return {
        "role": sensor.role.value,
        "key": sensor.key,
        "connection": sensor.connection,
        "manufacturer": sensor.manufacturer,
        "product": sensor.product,
    }


def _stored_sensors(text: object) -> tuple[ActivitySensor, ...]:
    if text is None:
        return ()
    return tuple(
        ActivitySensor(
            role=SensorRole(item["role"]),
            key=item["key"],
            connection=item.get("connection"),
            manufacturer=item.get("manufacturer"),
            product=item.get("product"),
        )
        for item in json.loads(str(text))
    )


def stored_sensors(text: object) -> tuple[ActivitySensor, ...]:
    """Decode a stored sensor list; an unreadable list yields no sensors."""
    try:
        return _stored_sensors(text)
    except TypeError, ValueError, KeyError, AttributeError:
        return ()


def _child_rows(detail: ActivityDetailSet) -> list[tuple[object, ...]]:
    if isinstance(detail, ActivityStreamSet):
        rows: list[tuple[object, ...]] = []
        for channel in sorted(detail.channels, key=lambda item: item.value):
            values = detail.channels[channel]
            # Packed, not compressed: the content hash decides whether to write.
            points, valid = _pack_series(values)
            rows.append((channel.value, len(values), valid, points))
        return rows
    if isinstance(detail, ActivityLapSet):
        return [
            (
                lap.index,
                lap.kind.value,
                lap.start_offset_s,
                lap.end_offset_s,
                lap.name,
                *(getattr(lap, column) for column in _LAP_METRIC_COLUMNS),
                lap.workout_step_index,
                None if lap.lap_trigger is None else lap.lap_trigger.value,
            )
            for lap in detail.laps
        ]
    if isinstance(detail, ActivityStrengthSetSet):
        return [
            (
                item.index,
                item.set_type.value,
                None
                if item.exercise_category is None
                else item.exercise_category.value,
                item.exercise_name,
                item.reps,
                item.weight_kg,
                item.duration_s,
                item.start_offset_s,
                item.workout_step_index,
            )
            for item in detail.sets
        ]
    return [
        (
            bucket.index,
            float(bucket.seconds),
            _optional_float(bucket.lower),
            _optional_float(bucket.upper),
            bucket.label,
        )
        for bucket in detail.buckets
    ]


def _content_hash(header: Mapping[str, Any], children: list[tuple[object, ...]]) -> str:
    digest = hashlib.sha256(canonical_json(header).encode("utf-8"))
    for row in children:
        for value in row:
            if isinstance(value, bytes):
                # Hash the packed points, not zlib output, which may vary by library.
                digest.update(value)
            else:
                digest.update(json.dumps(value).encode("utf-8"))
            digest.update(b"\x1f")
        digest.update(b"\x1e")
    return digest.hexdigest()


def _delete_children(db: Any, key: tuple[str, str, str]) -> None:
    for table in _CHILD_TABLES:
        db.execute(
            f"DELETE FROM {table} WHERE provider = ? AND provider_activity_id = ? "
            "AND detail_kind = ?",
            key,
        )


def _insert_children(
    db: Any,
    key: tuple[str, str, str],
    detail: ActivityDetailSet,
    rows: list[tuple[object, ...]],
) -> None:
    if isinstance(detail, ActivityStreamSet):
        db.executemany(
            "INSERT INTO activity_stream_channels(provider, provider_activity_id, "
            "detail_kind, channel, point_count, valid_count, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (*key, channel, count, valid, _compress(cast(bytes, points)))
                for channel, count, valid, points in rows
            ],
        )
    elif isinstance(detail, ActivityLapSet):
        db.executemany(
            "INSERT INTO activity_laps(provider, provider_activity_id, detail_kind, "
            f"{', '.join(_LAP_COLUMNS)}) "
            f"VALUES ({', '.join('?' * (3 + len(_LAP_COLUMNS)))})",
            [(*key, *row) for row in rows],
        )
    elif isinstance(detail, ActivityStrengthSetSet):
        db.executemany(
            "INSERT INTO activity_strength_sets(provider, provider_activity_id, "
            f"detail_kind, {', '.join(_STRENGTH_COLUMNS)}) "
            f"VALUES ({', '.join('?' * (3 + len(_STRENGTH_COLUMNS)))})",
            [(*key, *row) for row in rows],
        )
    else:
        db.executemany(
            "INSERT INTO activity_zone_times(provider, provider_activity_id, "
            "detail_kind, zone_index, seconds, lower_bound, upper_bound, label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(*key, *row) for row in rows],
        )


def _read_sets(
    db: Any, where: str, params: tuple[object, ...]
) -> list[StoredDetailSet]:
    """Rebuild typed sets; a set that no longer validates is skipped, not raised."""
    cursor = db.execute(
        "SELECT d.provider, d.provider_activity_id, d.detail_kind, d.mapper_version, "
        "d.origin, d.source_kind, d.recorded_by, d.source_record_id, d.resolution, "
        "d.lap_source, d.zone_boundary_source, d.set_source, d.summary_sha256, "
        "d.fetched_at, d.hr_source, d.power_source, d.sensors, d.dfa_alpha1, "
        "d.device_zones, d.workout_steps, r.activity_id FROM activity_detail_sets AS d "
        "JOIN activity_records AS r ON r.provider = d.provider "
        "AND r.provider_activity_id = d.provider_activity_id "
        "JOIN activity_sessions AS s ON s.activity_id = r.activity_id "
        f"WHERE r.state = ? AND s.state <> ? AND {where} "
        "ORDER BY d.provider, d.provider_activity_id, d.detail_kind",
        (
            ActivityRecordState.ACTIVE.value,
            ActivitySessionState.DELETED.value,
            *params,
        ),
    )
    stored: list[StoredDetailSet] = []
    for raw in cursor.fetchall():
        row = dict(raw)
        try:
            detail = _rebuild(db, row)
        except (TypeError, ValueError) as error:
            _log_unreadable(row, error)
            continue
        stored.append(_stored(row, detail))
    return stored


def _log_unreadable(row: Mapping[str, Any], error: Exception) -> None:
    # One unreadable set must not hide the others; report it without stored
    # values or error text so the corruption stays visible.
    LOGGER.warning(
        "Stored activity detail set is unreadable",
        extra={
            "event": "activity_detail_unreadable",
            "context": {
                "provider": row["provider"],
                "detail_kind": row["detail_kind"],
                "error": type(error).__name__,
            },
        },
    )


def _provenance(row: Mapping[str, Any]) -> Provenance:
    return Provenance(
        provider=ProviderId(row["provider"]),
        source_kind=SourceKind(row["source_kind"]),
        source_record_id=row["source_record_id"],
        recorded_by=RecordingOrigin(row["recorded_by"]),
        retrieved_at=_aware_datetime(row["fetched_at"]),
        label=None,
    )


def _rebuild(db: Any, row: Mapping[str, Any]) -> ActivityDetailSet:
    key = (row["provider"], row["provider_activity_id"], row["detail_kind"])
    kind = ActivityDetailKind(row["detail_kind"])
    common = {
        "provider_activity_id": row["provider_activity_id"],
        "provenance": _provenance(row),
        "origin": ActivityDetailOrigin(row["origin"]),
    }
    if kind is ActivityDetailKind.STREAMS:
        channels = {
            StreamChannel(channel["channel"]): decode_series(
                channel["data"], channel["point_count"], channel["valid_count"]
            )
            for channel in db.execute(
                "SELECT channel, point_count, valid_count, data FROM activity_stream_channels "
                "WHERE provider = ? AND provider_activity_id = ? AND detail_kind = ?",
                key,
            ).fetchall()
        }
        return ActivityStreamSet(
            **common,
            resolution=StreamResolution(row["resolution"]),
            channels=channels,
            hr_source=(
                None if row["hr_source"] is None else HeartRateSource(row["hr_source"])
            ),
            power_source=(
                None
                if row["power_source"] is None
                else PowerSource(row["power_source"])
            ),
            # Optional metadata must never make the stream set unreadable.
            sensors=stored_sensors(row["sensors"]),
            dfa_alpha1=stored_dfa_alpha1(row["dfa_alpha1"]),
            device_zones=stored_device_zones(row["device_zones"]),
        )
    if kind is ActivityDetailKind.LAPS:
        laps = tuple(
            ActivityLap(
                index=lap["lap_index"],
                kind=LapKind(lap["kind"]),
                start_offset_s=lap["start_offset_s"],
                end_offset_s=lap["end_offset_s"],
                name=lap["name"],
                **{column: lap[column] for column in _LAP_METRIC_COLUMNS},
                workout_step_index=lap["workout_step_index"],
                lap_trigger=_enum_or_none(LapTrigger, lap["lap_trigger"]),
            )
            for lap in db.execute(
                f"SELECT {', '.join(_LAP_COLUMNS)} FROM activity_laps "
                "WHERE provider = ? AND provider_activity_id = ? AND detail_kind = ? "
                "ORDER BY lap_index",
                key,
            ).fetchall()
        )
        return ActivityLapSet(
            **common,
            lap_source=LapSource(row["lap_source"]),
            laps=laps,
            workout_steps=stored_workout_steps(row["workout_steps"]),
        )
    if kind is ActivityDetailKind.STRENGTH_SETS:
        return ActivityStrengthSetSet(
            **common,
            source=StrengthSetSource(row["set_source"]),
            sets=tuple(
                _strength_set(item)
                for item in db.execute(
                    f"SELECT {', '.join(_STRENGTH_COLUMNS)} FROM activity_strength_sets "
                    "WHERE provider = ? AND provider_activity_id = ? AND detail_kind = ? "
                    "ORDER BY set_index",
                    key,
                ).fetchall()
            ),
        )
    buckets = tuple(
        ZoneBucket(
            index=bucket["zone_index"],
            seconds=bucket["seconds"],
            lower=bucket["lower_bound"],
            upper=bucket["upper_bound"],
            label=bucket["label"],
        )
        for bucket in db.execute(
            "SELECT zone_index, seconds, lower_bound, upper_bound, label "
            "FROM activity_zone_times WHERE provider = ? AND provider_activity_id = ? "
            "AND detail_kind = ? ORDER BY zone_index",
            key,
        ).fetchall()
    )
    return ZoneDistribution(
        **common,
        metric=_ZONE_METRIC_BY_KIND[kind],
        boundary_source=ZoneBoundarySource(row["zone_boundary_source"]),
        buckets=buckets,
    )


def _strength_set(row: Mapping[str, Any]) -> ActivityStrengthSet:
    category = row["exercise_category"]
    return ActivityStrengthSet(
        index=row["set_index"],
        set_type=StrengthSetType(row["set_type"]),
        exercise_category=None if category is None else ExerciseCategory(category),
        exercise_name=row["exercise_name"],
        reps=row["reps"],
        weight_kg=row["weight_kg"],
        duration_s=row["duration_s"],
        start_offset_s=row["start_offset_s"],
        workout_step_index=row["workout_step_index"],
    )


def _aware_datetime(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]
