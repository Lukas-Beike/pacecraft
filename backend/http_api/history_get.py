"""Authenticated local change-history list route."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.history.service import ChangeHistoryService
from backend.http_api.route_table import Access, Route, RouteRequest


class HistoryGetRoutes:
    """Serve the athlete's change-history list from its owning service."""

    def __init__(
        self, change_history_service: Callable[[], ChangeHistoryService]
    ) -> None:
        self._change_history_service = change_history_service

    def routes(self) -> list[Route]:
        return [Route("GET", "/api/change-history", Access.SESSION, self._list)]

    def _list(self, handler: Any, request: RouteRequest) -> None:
        """One newest-first page of the local change history.

        Request: optional query `limit` (1 to 100, default 30) and `cursor` (the
        `next_cursor` of the previous page; an unreadable one starts at the
        newest entry). Only the last 180 days are kept, at most 1,000 entries.

        Response: 200 `{"changes", "next_cursor", "limit"}`. Each change holds
        `id`, `entity_type`, `entity_id`, `action` (`create`, `update`, `delete`
        or `undo`), `source` (`coach` for a change a Coach tool round
        records, `local` for the app's own, `undo` for an undo, other values name
        their producer; entries from before this field was set read `local`), `created_at`, the hashes, `diff.fields` (changed field names
        only), `remote_sync`, `line` (the readable change), `can_undo` (a
        create, update or delete that is still its record's newest entry) and
        `undoable: false` for an entry that is never undone. `next_cursor` is
        `null` on the last page. The entry's `id` and `after_hash` are the
        `change_id` and `expected_current_hash` of `POST /api/change-history/undo`.

        Errors: none beyond the common ones.
        """
        given = {key: value[0] for key, value in request.query.items() if value}
        handler.send_json(
            200,
            self._change_history_service().page(
                given.get("limit"), given.get("cursor")
            ),
        )
