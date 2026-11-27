"""Persist failed Coach turns without losing confirmed local effects."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend import observability
from backend.coach.authorization import authorized_operations
from backend.coach.change_receipts import change_summary
from backend.coach.constants import turn_ai_provider
from backend.coach.context_selection import infer_coach_domain
from backend.coach.outcomes import (
    coach_effect_label,
    coach_failure_lines,
    coach_observed_sync_lines,
    unresolved_coach_steps,
)
from backend.coach.receipt_compaction import compact_receipt
from backend.coach.service import command_receipt
from backend.coach.tool_specs import ACTION_LABELS
from backend.db import coach_store
from backend.db.manager import DatabaseManager
from backend.db.repositories import ChatRepository, KeyValueRepository
from backend.errors import AppError
from backend.providers.claude_messages_adapter import REFUSED_MESSAGE
from backend.providers.claude_models import family_setting
from backend.providers.claude_requests import ANTHROPIC_API_KEY_ERROR
from backend.providers.error_texts import FAILURE_REASONS, failure_text
from backend.providers.state import turn_usage
from backend.runtime.events import StateEventBuffer

BACKEND_SOURCE_ROOT = Path(__file__).resolve().parents[1]
OPENAI_NOT_CONFIGURED_MESSAGE = (
    "OpenAI ist nicht eingerichtet. Zugangsdaten unter Mehr → Anbindungen hinterlegen."
)
OPENAI_TIMEOUT_MESSAGE = (
    "Der KI-Dienst hat nicht rechtzeitig geantwortet. Bitte versuche es erneut."
)

_EXPLANATIONS = {
    "openai_not_configured": OPENAI_NOT_CONFIGURED_MESSAGE,
    # OpenAI configuration errors carry the shared not_configured reason.
    "not_configured": OPENAI_NOT_CONFIGURED_MESSAGE,
    "anthropic_not_configured": ANTHROPIC_API_KEY_ERROR,
    "refused": REFUSED_MESSAGE,
    "conversation_state_invalid": "Der KI-Dienst konnte den Gesprächszustand nicht fortsetzen. Bitte versuche es erneut; dein lokaler Chat bleibt erhalten.",
    "conversation_locked": "Der KI-Dienst verarbeitet noch eine andere Anfrage. Bitte warte kurz und versuche es erneut.",
    "database_busy": "Die lokale Datenbank war kurz ausgelastet. Bitte versuche es erneut.",
    "provider_timeout": OPENAI_TIMEOUT_MESSAGE,
    "response_error": "Der KI-Dienst konnte die Antwort nicht fertigstellen. Bitte versuche es erneut.",
    "response_failed": "Der KI-Dienst konnte die Antwort nicht fertigstellen. Bitte versuche es erneut.",
}


def coach_error_metadata(exc: BaseException, repository_root: Path) -> dict[str, Any]:
    """Keep technical call sites, never exception text, source lines or locals."""
    result = observability.safe_diagnostic_error(exc)
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        filename = Path(trace.tb_frame.f_code.co_filename).resolve()
        if filename == repository_root / "server.py":
            safe_file = "server.py"
        elif filename.is_relative_to(BACKEND_SOURCE_ROOT):
            safe_file = filename.relative_to(BACKEND_SOURCE_ROOT.parent).as_posix()
        else:
            safe_file = None
        if safe_file is not None:
            frames.append(
                {
                    "file": safe_file,
                    "function": trace.tb_frame.f_code.co_name,
                    "line": trace.tb_lineno,
                }
            )
        trace = trace.tb_next
    result["frames"] = frames[-8:]
    return result


@dataclass(frozen=True)
class CoachTurnFailureDependencies:
    database_manager: Callable[[], DatabaseManager]
    chat_repository: ChatRepository
    key_values: KeyValueRepository
    event_buffer: StateEventBuffer
    redactor: observability.Redactor
    utc_now: Callable[[], str]
    repository_root: Path
    read_only_tools: frozenset[str]


class CoachTurnFailureService:
    """Own terminal failure projection, atomic receipt, and committed event."""

    def __init__(self, dependencies: CoachTurnFailureDependencies) -> None:
        self._deps = dependencies

    def persist(
        self, client_turn_id: str, intent: dict[str, Any], error: BaseException
    ) -> dict[str, Any]:
        deps = self._deps
        safe_error = (
            deps.redactor.redact_text(error.message)[:1000]
            if isinstance(error, AppError)
            else "Die Coach-Verarbeitung wurde unterbrochen."
        )
        with deps.database_manager().unit_of_work() as db:
            row = coach_store.command_row(db, client_turn_id, "receipt, status")
            if not row:
                return {}
            receipt = command_receipt(row["receipt"])
            if row["status"] == "completed":
                return receipt
            commands, successes, failures, pending = self._steps(receipt, intent)
            status, text, question, cancelled = self._response(
                error,
                commands,
                successes,
                failures,
                pending,
                turn_ai_provider(family_setting(str(receipt.get("model") or ""))),
            )
            self._pending_request(
                db, receipt, question=question, cancelled=cancelled, successes=successes
            )
            receipt.update(
                {
                    "status": status,
                    "awaiting_clarification": bool(question) and not cancelled,
                    "error": safe_error,
                    "diagnostic_error": coach_error_metadata(
                        error, deps.repository_root
                    ),
                    "client_turn_id": client_turn_id,
                    "command_receipts": commands,
                    "sync_job_ids": receipt.get("sync_job_ids") or [],
                    "intent": intent,
                    "pending_operations": pending,
                    "proposed_actions": [
                        step["result"]["proposed_action"]
                        for step in commands
                        if step.get("result", {}).get("proposed_action")
                    ],
                }
            )
            if (usage := turn_usage()) is not None:
                receipt["usage"] = usage
            # Keep applied local writes undoable, as a successful turn does.
            summary = change_summary(commands)
            if summary:
                receipt["change_summary"] = summary
            # A failed turn is no attachment anchor and has no open tool calls.
            for key in ("openai_reached", "pending_tool_calls"):
                receipt.pop(key, None)
            receipt["message"] = deps.chat_repository.add(
                db, "assistant", text, client_turn_id=client_turn_id
            )
            if summary:
                receipt["message"]["coach_changes"] = summary
            flags = self._outcome_flags(status, receipt, error)
            receipt["message"].update(flags)
            if "coach_failure_reason" in flags:
                receipt["failure_reason"] = flags["coach_failure_reason"]
            coach_store.write_command(
                db,
                client_turn_id,
                compact_receipt(receipt),
                deps.utc_now(),
                status="completed",
            )
        deps.event_buffer.publish(
            "coach",
            {
                "message_id": receipt["message"]["id"],
                "role": "assistant",
                "client_turn_id": client_turn_id,
            },
        )
        return receipt

    def _steps(
        self, receipt: dict[str, Any], intent: dict[str, Any]
    ) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]
    ]:
        commands = list(receipt.get("command_receipts") or [])
        internal = self._deps.read_only_tools | {
            "clarify_coach_request",
            "cancel_coach_request",
        }
        successes = [
            step
            for step in commands
            if step.get("result", {}).get("ok") and step["tool"] not in internal
        ]
        failures = unresolved_coach_steps(commands)
        for step in commands:
            if not step.get("result", {}).get("ok"):
                step["resolved"] = not any(step is failure for failure in failures)
        pending = sorted(
            {step["tool"] for step in failures}
            | {
                step["tool"]
                for step in receipt.get("pending_tool_calls", [])
                if step.get("tool")
            }
            | (
                authorized_operations(intent)
                - {step["tool"] for step in successes}
                - {""}
            )
        )
        return commands, successes, failures, pending

    @staticmethod
    def _outcome_flags(
        status: str, receipt: dict[str, Any], error: BaseException
    ) -> dict[str, Any]:
        """What the chat reads off a stopped or failed reply: kind, reason (#3931)."""
        if status == "cancelled":
            # Stored on the receipt: a finished turn that only cleared a pending request is no stop.
            receipt["stopped"] = True
            return {"coach_cancelled": True}
        if status != "failed":
            return {}
        flags: dict[str, Any] = {"coach_failed": True}
        if receipt.get("request_kind"):
            flags["coach_request_kind"] = receipt["request_kind"]
        # The reason decides whether a retry or the connections help.
        reason = getattr(error, "reason", "")
        if isinstance(reason, str) and reason:
            flags["coach_failure_reason"] = reason[:60]
        return flags

    @staticmethod
    def _base_response(
        error: BaseException,
        commands: list[dict[str, Any]],
        provider: str = "openai",
    ) -> tuple[str, str, str | None, bool]:
        cancelled = isinstance(error, AppError) and error.reason == "chat_cancelled"
        status = "cancelled" if cancelled else "failed"
        reason = getattr(error, "reason", "")
        if not isinstance(reason, str):
            reason = ""
        failure = failure_text(reason, provider) if reason in FAILURE_REASONS else None
        text = (
            "Die Coach-Verarbeitung wurde abgebrochen."
            if cancelled
            else failure
            or _EXPLANATIONS.get(
                reason,
                "Bei der Coach-Verarbeitung ist ein technischer Fehler aufgetreten. Bitte versuche es erneut. Wenn der Fehler wieder auftritt, exportiere die Diagnose in den Einstellungen.",
            )
        )
        question = next(
            (
                step["result"].get("question")
                for step in reversed(commands)
                if step["tool"] == "clarify_coach_request"
                and step.get("result", {}).get("ok")
            ),
            None,
        )
        if question and not cancelled:
            status = "completed"
            text = question
        rate_limited = isinstance(error, AppError) and (
            error.reason == "rate_limit_exceeded"
            or getattr(error, "provider_error_code", None) == "rate_limit_exceeded"
        )
        if rate_limited and not question:
            text = failure_text("rate_limit_exceeded", provider)
        return status, text, question, cancelled

    @staticmethod
    def _effect_text(
        text: str,
        commands: list[dict[str, Any]],
        successes: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        pending: list[str],
    ) -> str:
        if successes:
            completed = [
                step
                for step in successes
                if (step.get("result") or {}).get("status") != "approval_required"
            ]
            if completed:
                text += (
                    "\nBereits erfolgreich ausgeführt: "
                    + "; ".join(coach_effect_label(step) for step in completed)
                    + ". Diese Schritte bleiben gespeichert."
                )
            if len(completed) != len(successes):
                text += "\nEine Remote-Änderung wurde noch nicht ausgeführt und wartet auf deine Freigabe."
            if any(
                step["tool"] in {"start_intervals_plan_sync", "sync_competitions"}
                and step["result"].get("status") == "queued"
                for step in successes
            ):
                text += "\nDer Sync-Auftrag bleibt bestehen und wird unabhängig vom Coach verarbeitet. Sein Abschluss ist in dieser Antwort noch nicht bestätigt."
        if failures:
            text += "\n" + coach_failure_lines(failures, set(pending))
        observed_sync = coach_observed_sync_lines(commands)
        if observed_sync:
            text += "\n" + observed_sync
        if pending:
            text += (
                "\nNoch offen: "
                + ", ".join(
                    ACTION_LABELS.get(name, "Angeforderter Schritt") for name in pending
                )
                + "."
            )
        return text

    def _response(
        self,
        error: BaseException,
        commands: list[dict[str, Any]],
        successes: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        pending: list[str],
        provider: str = "openai",
    ) -> tuple[str, str, str | None, bool]:
        status, text, question, cancelled = self._base_response(
            error, commands, provider
        )
        if successes and (cancelled or not question):
            status = "partial"
        return (
            status,
            self._effect_text(text, commands, successes, failures, pending),
            question,
            cancelled,
        )

    def _pending_request(
        self,
        db: Any,
        receipt: dict[str, Any],
        *,
        question: str | None,
        cancelled: bool,
        successes: list[dict[str, Any]],
    ) -> None:
        if receipt.get("automatic") is True:
            # An automatic turn (#4274) is no athlete request: it neither opens
            # nor clears the athlete's pending request.
            return
        if cancelled:
            self._deps.key_values.set(db, "coach_pending_request", "null")
        elif receipt.get("user_message_id") and not question:
            user = coach_store.message_by_id(db, receipt["user_message_id"], "content")
            if user:
                tool_name = receipt.get("tool")
                tools = [tool_name] if tool_name else []
                domain = infer_coach_domain(user["content"], tools=tools)
                self._deps.key_values.set(
                    db,
                    "coach_pending_request",
                    json.dumps(
                        {
                            "summary": user["content"],
                            "source_message_ids": [receipt["user_message_id"]],
                            "origin_message_ids": [receipt["user_message_id"]],
                            "status": "failed",
                            "domain": domain,
                            "question": None,
                            "completed_steps": [
                                {
                                    "tool": step["tool"],
                                    "status": step["result"].get("status"),
                                }
                                for step in successes
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
