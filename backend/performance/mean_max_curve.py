"""Per-session mean-maximal curves from canonical full-resolution streams.

Only stream sets stored at full resolution are read: a downsampled set never
enters a best value. A set whose provider summary changed since it was fetched is
stale and left out, as the activity detail withholds its analysis. A session
counts once, with the streams of every record bundled in it merged as in the
activity detail. Curves are computed at read time and kept in memory under a
fingerprint of the method, the session's records and the content hashes of its
stream sets, so any source change or new method version recomputes them. The
all-time build also stores each curve under that fingerprint (``session_curves``,
#2821), and a memo miss loads a stored curve of the same fingerprint instead of
computing it, so a restart does not recompute the history. Reads never touch a
provider.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from functools import partial
from types import MappingProxyType
from typing import Any, Final

from backend.activities.detail_contract import measured_order
from backend.activities.detail_merge import merge_details
from backend.canonical.records import ActivityRecord
from backend.db import activity_detail_store, canonical_store
from backend.db.activity_detail_store import FullStreamKey
from backend.db.manager import background_writer
from backend.performance.analysis_input import analysis_input, analysis_sport
from backend.performance.canonical_detail import (
    SessionRecordReader,
    detail_candidates,
    primary_analysis_record,
)
from backend.performance.critical_speed_test import session_test_efforts
from backend.performance.pace_at_hr import METHOD as PACE_METHOD
from backend.performance.pace_at_hr import (
    POWER_METHOD,
    pace_at_heart_rate,
    power_at_heart_rate,
)
from backend.performance.power_profile import (
    METHOD_ID,
    METHOD_VERSION,
    power_profile,
    running_profile,
)
from backend.performance.running_dynamics import METHOD as DYNAMICS_METHOD
from backend.performance.running_dynamics import pace_bands
from backend.performance.session_analysis import gap_variation_pct
from backend.performance.session_execution import METHOD as EXECUTION_METHOD
from backend.performance.session_execution import (
    comparison_metrics,
    session_execution,
)
from backend.providers.canonical_plans import ACTIVITY_RECORD_KIND

_LOGGER = logging.getLogger("intervals_coach")
# About 9 KB per session; room for a long all-time history (#2694). The
# all-time build grows it to the stored history (``reserve``, #2822).
_DEFAULT_CAPACITY: Final = 2048
# A failed background build is tried again after this pause.
_RETRY_AFTER_S: Final = 60.0
# The curve computation's own version (#2821): it covers the inputs without a
# method version of their own (merge_details, analysis_input, grade_adjustment)
# and the stored payload fields. Bump it whenever any of them changes the curves,
# so stored curves are computed again; the golden-curve test pins it.
CURVE_FORMAT_VERSION: Final = 4


@dataclass(frozen=True, slots=True)
class SessionCurve:
    """The power and running curves, the pace- and power-at-heart-rate bins, the running-dynamics pace bands of one session and the fingerprint of their input."""

    power: dict[str, Any]
    running: dict[str, Any]
    fingerprint: str
    pace_at_hr: dict[str, Any]
    power_at_hr: dict[str, Any]
    # The comparison numbers of the session's execution (streams and current
    # laps); only these are kept, not the full per-rep execution.
    execution_summary: dict[str, Any] | None = None
    # A run's running dynamics per pace band (#2829), None without them.
    running_dynamics: dict[str, Any] | None = None
    # The three effort laps of a critical-speed test session (#3919), else None.
    cs_test_efforts: list[dict[str, Any]] | None = None


# The stored payload holds every curve field but the fingerprint, its row key.
_PAYLOAD_FIELDS: Final = tuple(
    item.name for item in dataclass_fields(SessionCurve) if item.name != "fingerprint"
)


class SessionCurveReader:
    """Read session curves, recomputing only sessions whose input changed."""

    def __init__(
        self,
        session_records: SessionRecordReader,
        *,
        capacity: int = _DEFAULT_CAPACITY,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._session_records = session_records
        self._capacity = capacity
        self._memo: OrderedDict[str, SessionCurve] = OrderedDict()
        # Fingerprints being computed; each is set when its computation ends,
        # stored or failed, and waiters then look again.
        self._computing: dict[str, threading.Event] = {}
        self._shared: tuple[str, tuple[Mapping[str, Any], ...]] | None = None
        self._background: tuple[str, tuple[Mapping[str, Any], ...]] | None = None
        self._building = False
        self._failed: tuple[str, float] | None = None
        # Background build outcomes, the last one's duration and when the
        # running one started (#2852).
        self._outcomes = {"builds": 0, "failures": 0}
        # Curves computed from streams and loaded from the store (#2821).
        self._curve_counts = {
            "curves_computed": 0,
            "curves_loaded": 0,
            "curves_stored": 0,
            "curve_store_failures": 0,
        }
        self._last_build_s: float | None = None
        self._build_started: float | None = None
        self._spawn = spawn or _daemon_thread
        self._lock = threading.Lock()

    def reserve(self, sessions: int) -> None:
        """Grow the memo to hold ``sessions`` curves; it never shrinks (#2822)."""
        with self._lock:
            self._capacity = max(self._capacity, sessions)

    def curves(self, db: Any, activity_ids: Iterable[str]) -> dict[str, SessionCurve]:
        """Return the curve of each session that has current full-resolution streams."""
        wanted = list(activity_ids)
        keys_by_session = activity_detail_store.full_stream_keys(db, wanted)
        laps_by_session = activity_detail_store.lap_set_keys(db, wanted)
        inputs = []
        for activity_id, keys in keys_by_session.items():
            current = _current(db, keys)
            records = tuple(self._session_records(db, activity_id))
            if not current or not records:
                continue
            laps = _current(db, laps_by_session.get(activity_id, ()))
            fingerprint = _fingerprint(records, current, laps)
            inputs.append((activity_id, fingerprint, records, current, laps))
        # Only memo misses read their stored payload, so a warm read loads none.
        with self._lock:
            missing = [item[0] for item in inputs if item[1] not in self._memo]
        stored = activity_detail_store.session_curves(db, missing) if missing else {}
        curves: dict[str, SessionCurve] = {}
        for activity_id, fingerprint, records, current, laps in inputs:
            compute = partial(
                _session_curve, db, activity_id, records, current, laps, fingerprint
            )
            curves[activity_id] = self._memoized(
                fingerprint,
                partial(
                    self._loaded_or_computed,
                    fingerprint,
                    stored.get(activity_id),
                    compute,
                ),
            )
        return curves

    def store_changed(
        self,
        manager: Any,
        curves: Mapping[str, SessionCurve],
        stored_fingerprints: Mapping[str, str],
    ) -> None:
        """Store each curve whose stored fingerprint differs from its own (#2821).

        Called by the all-time build outside any read snapshot: each curve is one
        background-writer unit, so a waiting request is served first. A curve a
        request computed is a memo hit in the build and is stored here too. A
        failed write is logged without content and counted; the next build tries
        it again, and the other curves are stored.
        """
        for activity_id, curve in curves.items():
            if stored_fingerprints.get(activity_id) == curve.fingerprint:
                continue
            save = partial(
                activity_detail_store.save_session_curve,
                activity_id=activity_id,
                fingerprint=curve.fingerprint,
                payload=curve_payload(curve),
                now=datetime.now(UTC).isoformat(),
            )
            self._write(manager, save, "curves_stored")

    def drop_unlive(self, manager: Any) -> None:
        """Delete the stored curves of merged and deleted sessions (#2821)."""
        self._write(manager, activity_detail_store.delete_unlive_session_curves, None)

    def _write(
        self, manager: Any, write: Callable[[Any], object], counted: str | None
    ) -> None:
        try:
            with background_writer(), manager.unit_of_work() as db:
                done = write(db)
        except Exception as exc:  # noqa: BLE001 - one failed curve never stops the build
            _LOGGER.warning(
                "Session curve not stored",
                extra={
                    "event": "session_curve_store_failed",
                    "context": {"error_class": type(exc).__name__},
                },
            )
            with self._lock:
                self._curve_counts["curve_store_failures"] += 1
            return
        if counted and done:
            with self._lock:
                self._curve_counts[counted] += 1

    def _loaded_or_computed(
        self,
        fingerprint: str,
        stored: tuple[str, Mapping[str, Any]] | None,
        compute: Callable[[], SessionCurve],
    ) -> SessionCurve:
        """The stored curve when its fingerprint is current (#2821), else a computed one."""
        curve = None
        if stored is not None and stored[0] == fingerprint:
            curve = curve_from_payload(fingerprint, stored[1])
        loaded = curve is not None
        if curve is None:
            curve = compute()
        with self._lock:
            self._curve_counts["curves_loaded" if loaded else "curves_computed"] += 1
        return curve

    def _memoized(
        self, fingerprint: str, compute: Callable[[], SessionCurve]
    ) -> SessionCurve:
        """The memoized curve of ``fingerprint``, computed once across threads.

        A miss while another thread computes the same curve waits for it instead
        of computing it again, as parallel view loads did after a start (#2783);
        after a failed computation one waiter takes it over.
        """
        while True:
            with self._lock:
                cached = self._memo.get(fingerprint)
                if cached is not None:
                    self._memo.move_to_end(fingerprint)
                    return cached
                pending = self._computing.get(fingerprint)
                if pending is None:
                    owned = self._computing[fingerprint] = threading.Event()
                    break
            pending.wait()
        try:
            cached = compute()
            with self._lock:
                self._memo[fingerprint] = cached
                while len(self._memo) > self._capacity:
                    self._memo.popitem(last=False)
            return cached
        finally:
            with self._lock:
                del self._computing[fingerprint]
            owned.set()

    def shared_records(
        self,
        db: Any,
        activity_ids: Sequence[str],
        key: object,
        build: Callable[[], Iterable[Mapping[str, Any]]],
    ) -> tuple[Mapping[str, Any], ...]:
        """Return the last built session records while their inputs are unchanged.

        The stamp is ``key`` (what the caller's records show of the sessions) with
        the stored records and detail sets of ``activity_ids`` that ``curves``
        reads, so a new day, session or merge, or a changed stream, lap set or
        provider summary builds the records again, and an unchanged read queries no
        session. Callers share one tuple of read-only records. Of concurrent
        builds the first to finish is kept: a build stores its result only if the
        slot still holds what it held when the call started, and the next read
        rebuilds if that result is no longer current. Concurrent misses each build
        once; the per-session memo makes the repeat cheap.
        """
        stamp = _stamp(db, activity_ids, key)
        with self._lock:
            held = self._shared[0] if self._shared else None
            if self._shared and held == stamp:
                return self._shared[1]
        records = tuple(MappingProxyType(record) for record in build())
        with self._lock:
            if (self._shared[0] if self._shared else None) == held:
                self._shared = (stamp, records)
        return records

    def background_records(
        self,
        db: Any,
        activity_ids: Sequence[str],
        key: object,
        build: Callable[[], Iterable[Mapping[str, Any]]],
    ) -> tuple[tuple[Mapping[str, Any], ...], bool] | None:
        """Return records built off the caller's path and whether they are current (#2694).

        Stamped like ``shared_records``, in a slot of its own. A changed stamp
        never builds in the caller: it starts ``build`` once in the background
        (it opens its own reads; the per-session memo keeps a rebuild after a new
        session cheap) and returns the last finished records meanwhile, marked
        not current, None before the first. A failed build is logged; for
        ``_RETRY_AFTER_S`` the same stamp raises RuntimeError instead of building
        again.
        """
        stamp = _stamp(db, activity_ids, key)
        with self._lock:
            ready = self._background
            if ready and ready[0] == stamp:
                return ready[1], True
            failed = self._failed
            if (
                failed
                and failed[0] == stamp
                and time.monotonic() - failed[1] < _RETRY_AFTER_S
            ):
                raise RuntimeError("background session curves failed")
            start = not self._building
            self._building = True
        if start:
            try:
                self._spawn(lambda: self._build_in_background(stamp, build))
            except BaseException:
                with self._lock:
                    self._building = False
                raise
        with self._lock:
            ready = self._background
        return None if ready is None else (ready[1], ready[0] == stamp)

    def _build_in_background(
        self, stamp: str, build: Callable[[], Iterable[Mapping[str, Any]]]
    ) -> None:
        built = None
        started = time.monotonic()
        with self._lock:
            self._build_started = started
        try:
            built = tuple(MappingProxyType(record) for record in build())
        except Exception:
            _LOGGER.exception(
                "Background session curves failed",
                extra={"event": "background_session_curves_failed"},
            )
        finished = time.monotonic()
        with self._lock:
            if built is None:
                self._failed = (stamp, finished)
            else:
                self._background, self._failed = (stamp, built), None
            self._building, self._build_started = False, None
            self._outcomes["failures" if built is None else "builds"] += 1
            self._last_build_s = finished - started

    def background_state(self) -> dict[str, Any]:
        """The background build as a state word, counts and seconds (#2852).

        ``building`` while one runs (``building_s`` so far, so a stall shows),
        else ``failed`` after a failed one (``failed_age_s`` since then),
        ``ready`` once records exist (a changed input rebuilds on the next
        all-time read, so they may be older), ``pending`` before the first.
        ``builds`` and ``failures`` count finished builds by outcome.
        """
        now = time.monotonic()
        with self._lock:
            started, failed = self._build_started, self._failed
            if self._building:
                status = "building"
            elif failed:
                status = "failed"
            else:
                status = "ready" if self._background else "pending"
            return {
                "status": status,
                **self._outcomes,
                **self._curve_counts,
                "last_build_s": _seconds(self._last_build_s),
                "building_s": _seconds(None if started is None else now - started),
                "failed_age_s": _seconds(None if failed is None else now - failed[1]),
            }


def _seconds(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _daemon_thread(run: Callable[[], None]) -> None:
    """Run ``run`` in a daemon thread with a fresh context (no request state)."""
    threading.Thread(
        target=contextvars.Context().run,
        args=(run,),
        name="session-curves",
        daemon=True,
    ).start()


def _stamp(db: Any, activity_ids: Sequence[str], key: object) -> str:
    """``key`` with the stored records and detail sets the curves of the sessions read."""
    return _sha256(
        [
            key,
            activity_detail_store.session_curve_inputs(
                db, activity_ids, ACTIVITY_RECORD_KIND
            ),
        ]
    )


def input_fingerprint(parts: Iterable[object]) -> str:
    """Fingerprint of a result built from several inputs, independent of order."""
    return _sha256(sorted({_canonical(part) for part in parts}))


def _current(db: Any, keys: Iterable[FullStreamKey]) -> list[FullStreamKey]:
    """The keys whose sets still belong to the provider's current summary."""
    return [key for key in keys if not _stale(db, key)]


def _stale(db: Any, key: FullStreamKey) -> bool:
    """Whether the provider summary changed since the set was fetched."""
    if key.summary_sha256 is None:
        return False
    current = canonical_store.active_provider_record_hash(
        db, key.provider, ACTIVITY_RECORD_KIND, key.provider_activity_id
    )
    return current is not None and current != key.summary_sha256


def _session_curve(
    db: Any,
    activity_id: str,
    records: tuple[ActivityRecord, ...],
    keys: Sequence[FullStreamKey],
    laps: Sequence[FullStreamKey],
    fingerprint: str,
) -> SessionCurve:
    # Each kind keeps only its own current sets, so a stale set of one kind
    # never returns through a current set of the other.
    streams_wanted = {(key.provider, key.provider_activity_id) for key in keys}
    laps_wanted = {(key.provider, key.provider_activity_id) for key in laps}
    stored = [
        item
        for item in activity_detail_store.full_stream_sets_for_session(db, activity_id)
        if (item.provider, item.provider_activity_id) in streams_wanted
    ]
    stored += [
        item
        for item in activity_detail_store.lap_sets_for_session(db, activity_id)
        if (item.provider, item.provider_activity_id) in laps_wanted
    ]
    merged = merge_details(detail_candidates(records, stored))
    primary = primary_analysis_record(records)
    adapted = analysis_input(
        primary, merged.streams, merged.laps, members=measured_order(records)
    )
    return SessionCurve(
        power=power_profile(
            adapted.activity, source_label=adapted.label_for("watts", "moving")
        ),
        running=running_profile(
            adapted.activity,
            source_label=adapted.label_for("velocity_smooth", "distance", "moving"),
        ),
        fingerprint=fingerprint,
        pace_at_hr=pace_at_heart_rate(
            adapted.activity,
            source_label=adapted.label_for(
                "velocity_smooth", "heartrate", "grade_smooth", "altitude", "moving"
            ),
        ),
        power_at_hr=power_at_heart_rate(
            adapted.activity,
            source_label=adapted.label_for("watts", "heartrate", "moving"),
        ),
        execution_summary=_with_steadiness(
            comparison_metrics(
                session_execution(
                    adapted.activity,
                    source_label=adapted.label_for(
                        "watts", "heartrate", "velocity_smooth", "cadence", "laps"
                    ),
                )
            ),
            adapted.activity,
        ),
        cs_test_efforts=session_test_efforts(adapted.activity.get("laps") or []),
        running_dynamics=pace_bands(
            adapted.activity,
            source_label=adapted.label_for(
                "ground_contact_time",
                "ground_contact_balance",
                "vertical_oscillation",
                "vertical_ratio",
                "step_length",
                "cadence",
                "velocity_smooth",
            ),
        ),
    )


def curve_payload(curve: SessionCurve) -> dict[str, Any]:
    """The stored form of a curve: every field but its fingerprint (#2821)."""
    return {name: getattr(curve, name) for name in _PAYLOAD_FIELDS}


def curve_from_payload(
    fingerprint: str, payload: Mapping[str, Any]
) -> SessionCurve | None:
    """The curve of a stored payload, None for a payload of another format."""
    if set(payload) != set(_PAYLOAD_FIELDS):
        return None
    return SessionCurve(fingerprint=fingerprint, **payload)


def _with_steadiness(
    summary: dict[str, Any] | None, activity: Mapping[str, Any]
) -> dict[str, Any] | None:
    """A continuous run's comparison numbers with its GAP steadiness (#3084).

    A curve-level addition beside the execution's own numbers, so
    ``CURVE_FORMAT_VERSION`` (not the execution method) covers it.
    """
    if (
        summary is None
        or summary.get("structure") != "continuous"
        or summary.get("output_unit") != "s/km"
        or (variation := gap_variation_pct(dict(activity))) is None
    ):
        return summary
    return {**summary, "gap_cv_pct": variation}


def _fingerprint(
    records: Sequence[ActivityRecord],
    keys: Sequence[FullStreamKey],
    laps: Sequence[FullStreamKey] = (),
) -> str:
    """Hash every input that changes the merged streams, laps, labels or the sport.

    ``recorded_by`` chooses the primary streams, channels and laps of the merge,
    and ``CURVE_FORMAT_VERSION`` covers the unversioned computation (#2821).
    """
    return _sha256(
        [
            METHOD_ID,
            METHOD_VERSION,
            CURVE_FORMAT_VERSION,
            PACE_METHOD,
            POWER_METHOD,
            EXECUTION_METHOD,
            DYNAMICS_METHOD,
            sorted([key.content_sha256, key.fetched_at] for key in laps),
            analysis_sport(primary_analysis_record(records), measured_order(records)),
            sorted(
                _canonical(
                    [
                        record.provenance.provider,
                        record.activity_id,
                        record.started_at,
                        record.device_type,
                        record.provenance.label,
                        record.provenance.recorded_by,
                    ]
                )
                for record in records
            ),
            sorted([key.content_sha256, key.fetched_at] for key in keys),
        ]
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
