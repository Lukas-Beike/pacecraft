"""Execute one claimed durable Coach job and publish its stream outcome."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

from backend.coach.authorization import automatic_read_only
from backend.coach.chat_turn import CoachChatTurnService
from backend.coach.job_store import MAX_FAILED_ATTEMPTS, CoachJobStore
from backend.coach.morning import ManualMorningCheckinService
from backend.coach.morning_completion import MorningCoachJobCompletionService
from backend.coach.response_transport import raise_if_chat_cancelled
from backend.coach.streams import ChatStreamRegistry
from backend.coach.turn_failures import CoachTurnFailureService
from backend.errors import INTERNAL_SERVER_ERROR, AppError
from backend.observability import Redactor
from backend.runtime.maintenance import MaintenanceGate
from backend.sync.observation import operation_error_code

# A morning check-in waits up to two hours, in quarter-hour steps, for the night.
MORNING_SLEEP_WAIT_SECONDS = 15 * 60
MORNING_SLEEP_MAX_WAITS = 8


def _automatic(job: dict[str, Any]) -> bool:
    """The job is an automatic read-only turn the server queued (#4274)."""
    return automatic_read_only(job.get("receipt"), job.get("intent"))


class CoachSessionBinding(Protocol):
    """Narrow session-key read needed by a claimed Coach job."""

    def restore_coach_session_csrf_hash(self, session_key: str) -> str: ...


class CoachBackgroundJobRunner:
    """Own claim-generation execution, retry, cancellation and SSE publication."""

    def __init__(
        self,
        jobs: CoachJobStore,
        chat_turn: Callable[[], CoachChatTurnService],
        sessions: Callable[[], CoachSessionBinding],
        streams: ChatStreamRegistry,
        morning: Callable[[], ManualMorningCheckinService],
        morning_completion: Callable[[], MorningCoachJobCompletionService],
        failures: Callable[[], CoachTurnFailureService],
        maintenance: MaintenanceGate,
        redactor: Redactor,
        logger: logging.Logger,
        on_answer: Callable[[str], None] | None = None,
        on_automatic: Callable[[dict[str, Any], str, str], None] | None = None,
    ) -> None:
        self._on_answer = on_answer
        self._on_automatic = on_automatic
        self._jobs = jobs
        self._chat_turn = chat_turn
        self._sessions = sessions
        self._streams = streams
        self._morning = morning
        self._morning_completion = morning_completion
        self._failures = failures
        self._maintenance = maintenance
        self._redactor = redactor
        self._logger = logger

    def run(self, job: dict[str, Any]) -> None:
        """Discard generations invalidated by restore without failure writes."""
        try:
            with self._maintenance.operation(job["_maintenance_generation"]):
                outcome, answer = self._run_claimed(job)
        except AppError as exc:
            if exc.reason not in {"operation_invalidated", "maintenance"}:
                self._end_exhausted_job(job)
                raise
        except Exception:
            self._end_exhausted_job(job)
            raise
        else:
            self._announce(job, outcome, answer)

    def _announce(self, job: dict[str, Any], outcome: str, answer: str) -> None:
        """Tell the athlete's phone the answer is there (#3914); an automatic
        turn (#4274) reports its outcome instead, also a failed one.

        It runs after the turn's operation, so a failing delivery never fails or
        holds the turn.
        """
        automatic = _automatic(job)
        try:
            if automatic and self._on_automatic and outcome in {"completed", "failed"}:
                self._on_automatic(job, outcome, answer)
            elif not automatic and self._on_answer and answer:
                self._on_answer(str(job.get("client_turn_id") or ""))
        except Exception:  # noqa: BLE001 - the answer is stored; the push is a courtesy
            self._logger.warning(
                "Coach answer notification failed",
                extra={"event": "coach_answer_push_failed"},
            )

    def _end_exhausted_job(self, job: dict[str, Any]) -> None:
        """Count a failure that escaped job handling and end a job that keeps failing.

        The worker re-queues interrupted jobs after such a failure; without a bound a
        job that fails before its own error handling would be retried forever.
        """
        client_turn_id = str(job.get("client_turn_id") or "")
        try:
            if self._jobs.record_failed_attempt(client_turn_id) < MAX_FAILED_ATTEMPTS:
                return
            failed = self._failures().persist(
                client_turn_id,
                {},
                AppError(
                    500,
                    "Der Coach-Auftrag ist mehrfach fehlgeschlagen und wurde beendet.",
                    reason="background_job_exhausted",
                ),
            )
            stored = job.get("receipt")
            operation_id = (
                stored.get("operation_id") if isinstance(stored, dict) else ""
            )
            if failed and operation_id:
                self._publish_receipt(str(operation_id), failed)
            self._announce(job, "failed", "")
        except Exception:  # the original failure is re-raised to the worker
            self._logger.exception(
                "Coach background job could not be ended after repeated failures",
                extra={"event": "coach_background_job_exhaustion_failed"},
            )

    def _run_claimed(self, job: dict[str, Any]) -> tuple[str, str]:
        """Run the job: ``completed`` with the answer text (empty without one),
        ``failed`` or ``queued`` (waits for a slot or the night's data)."""
        receipt = job.get("receipt") if isinstance(job.get("receipt"), dict) else {}
        operation_id = str(receipt.get("operation_id") or "")
        client_turn_id = str(job.get("client_turn_id") or "")
        # An automatic read-only turn belongs to no login session (#4274).
        session_csrf_hash = (
            ""
            if _automatic(job)
            else self._sessions().restore_coach_session_csrf_hash(
                receipt.get("session_key")
            )
        )
        cancel_event = self._streams.get_or_create_background_event(operation_id)
        if self._jobs.cancel_requested(client_turn_id):
            self._streams.cancel_background_event(operation_id)
        stream_attached = (
            self._streams.events(session_csrf_hash, operation_id) is not None
        )
        try:
            result = self._execute(
                job,
                receipt,
                operation_id,
                client_turn_id,
                session_csrf_hash,
                cancel_event,
                stream_attached,
            )
            self._publish_receipt(operation_id, result)
            if result.get("status") != "completed":
                return "failed", ""
            message = result.get("message")
            if isinstance(message, dict):
                message = message.get("content")
            return "completed", str(message or "")
        except AppError as exc:
            if self._handle_app_error(client_turn_id, operation_id, exc):
                return "queued", ""
        except Exception as exc:  # noqa: BLE001 - terminal worker boundary must persist unexpected failures
            self._handle_exception(client_turn_id, operation_id, exc)
        finally:
            self._streams.remove_background_event(operation_id)
        return "failed", ""

    def _delta_callback(
        self,
        operation_id: str,
        receipt: dict[str, Any],
        stream_attached: bool,
    ) -> Any:
        # A re-requested turn may have streamed its first attempt already.
        if not stream_attached or receipt.get("openai_reached"):
            return None

        def publish_delta(text: str) -> None:
            self._streams.publish(operation_id, "delta", {"text": text})

        return publish_delta

    def _publish_receipt(self, operation_id: str, value: dict[str, Any]) -> None:
        self._streams.publish(
            operation_id,
            "completed",
            {key: item for key, item in value.items() if key != "session_key"},
        )

    def _execute(
        self,
        job: dict[str, Any],
        receipt: dict[str, Any],
        operation_id: str,
        client_turn_id: str,
        session_csrf_hash: str,
        cancel_event: threading.Event,
        stream_attached: bool,
    ) -> dict[str, Any]:
        if not session_csrf_hash and not _automatic(job):
            raise AppError(
                401,
                "Die Sitzung des Coach-Auftrags ist abgelaufen.",
                reason="session_expired",
            )
        message = self._jobs.message(job)
        prepared = bool(receipt.get("morning_checkin_date"))
        worker_phase = {"status": "running"}
        if not receipt.get("openai_reached"):
            worker_phase["phase"] = "preparing"
        self._jobs.merge_receipt(client_turn_id, worker_phase)
        # The marker names a finished preparation; a re-requested turn does not
        # run the blocking Garmin sync again, and a cancel stops it before and
        # after it instead of running it first.
        if receipt.get("request_kind") == "morning_checkin" and not prepared:
            raise_if_chat_cancelled(cancel_event)
            prepared_for = self._morning().prepare()
            if isinstance(prepared_for, date):
                # The marker names the day prepared for, also when the turn
                # completes after midnight or resumes (#2134, E-014).
                self._jobs.merge_receipt(
                    client_turn_id, {"morning_checkin_date": prepared_for.isoformat()}
                )
            raise_if_chat_cancelled(cancel_event)
        result = self._chat_turn().run(
            message,
            on_text_delta=self._delta_callback(operation_id, receipt, stream_attached),
            cancel_event=cancel_event,
            session_csrf_hash=session_csrf_hash,
            client_turn_id=client_turn_id,
            background_job=True,
        )
        if (
            receipt.get("request_kind") == "morning_checkin"
            and result.get("status") == "completed"
            and result.get("message")
            and not result.get("awaiting_clarification")
        ):
            result = self._morning_completion().complete(client_turn_id) or result
        return result

    def _handle_app_error(
        self, client_turn_id: str, operation_id: str, exc: AppError
    ) -> bool:
        """Requeue or end the job after ``exc``; True when it waits in the queue."""
        requeued = False
        if exc.reason in {"chat_queue_full", "chat_request_timeout"}:
            self._jobs.requeue(client_turn_id, exc.reason)
            requeued = True
        elif exc.reason == "garmin_sleep_not_ready":
            requeued = self._jobs.requeue(
                client_turn_id,
                exc.reason,
                wait_seconds=MORNING_SLEEP_WAIT_SECONDS,
                max_waits=MORNING_SLEEP_MAX_WAITS,
            )
        if requeued:
            self._logger.warning(
                "Persistent Coach background job requeued after contention",
                extra={
                    "event": "coach_background_job_requeued",
                    "context": {"operation_id": operation_id, "reason": exc.reason},
                },
            )
            self._streams.publish(
                operation_id,
                "background",
                {
                    "status": "queued",
                    "mode": "background",
                    "operation_id": operation_id,
                },
            )
            return True
        failed = self._failures().persist(client_turn_id, {}, exc)
        if failed:
            self._publish_receipt(operation_id, failed)
            return False
        self._streams.publish(
            operation_id,
            "error",
            {
                "reason": exc.reason or "request_failed",
                "message": self._redactor.redact_text(exc.message)[:1000],
            },
        )
        return False

    def _handle_exception(
        self, client_turn_id: str, operation_id: str, exc: Exception
    ) -> None:
        failed = self._failures().persist(client_turn_id, {}, exc)
        if failed:
            self._publish_receipt(operation_id, failed)
        else:
            self._streams.publish(
                operation_id,
                "error",
                {
                    "reason": "internal_error",
                    "message": INTERNAL_SERVER_ERROR,
                },
            )
        self._logger.exception(
            "Persistent Coach background job failed",
            extra={
                "event": "coach_background_job_failed",
                "context": {
                    "operation_id": operation_id,
                    "error_code": operation_error_code(exc),
                },
            },
        )
