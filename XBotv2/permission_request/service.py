"""Approval service: the live permission-approval seam.

Provides ``ctx.approval`` — transport for a plugin-owned permission request.
It owns pending-response coordination but no permission policy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from XBotv2.agentloop import EventContext, Events
from XBotv2.core.tools import ClientEvent
from XBotv2.permission_request.waiter import ApprovalWaiter

Answerer = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | str]]


class ApprovalService:
    """Live approval transport with no permission-policy knowledge."""

    def __init__(self, ctx: Any, client_events: Any) -> None:
        self.ctx = ctx
        self.client_events = client_events
        self._answerers: list[Answerer] = []
        self._waiter = ApprovalWaiter()

    @property
    def waiter(self) -> ApprovalWaiter:
        """Fallback waiter used when no live sink answerer is installed."""
        return self._waiter

    # ------------------------------------------------------------------
    # Answerer registration
    # ------------------------------------------------------------------

    def register_answerer(self, answerer: Answerer) -> Any:
        """Register one answerer; the returned disposer removes it.

        Answerers run in registration order and short-circuit: the first
        outcome returned wins.  A sink answerer (live protocol) dispatches
        the client event and waits for the human; a machine answerer (ACP)
        returns a decision directly.
        """
        self._answerers.append(answerer)

        def dispose() -> bool:
            try:
                self._answerers.remove(answerer)
            except ValueError:
                return False
            return True

        return dispose

    # ------------------------------------------------------------------
    # Session-facing API (submit / cancel / pending)
    # ------------------------------------------------------------------

    def submit(self, request_id: str, decision: str, scope: str = "once") -> Any:
        """Answer one pending approval (protocol response path)."""
        return self._waiter.answer(request_id, decision=decision, scope=scope)

    def cancel(self, request_id: str, reason: str = "cancelled") -> Any:
        return self._waiter.cancel(request_id, reason)

    def cancel_all(self, reason: str = "cancelled") -> list[Any]:
        return self._waiter.cancel_all(reason)

    def pending_request_ids(self) -> list[str]:
        return self._waiter.pending_request_ids()

    def is_pending(self, request_id: str) -> bool:
        return self._waiter.is_pending(request_id)

    # ------------------------------------------------------------------
    # Approval flow
    # ------------------------------------------------------------------

    async def request(self, client_event: dict[str, Any]) -> dict[str, Any]:
        """Publish a request and return the client's raw decision record."""
        envelope = ClientEvent.from_mapping(client_event)
        await self.ctx.emit(
            Events.CLIENT_EVENT,
            EventContext(client_event=envelope),
        )
        sink_result = await self.client_events.request(envelope)
        if sink_result is not None:
            return sink_result
        for answerer in list(self._answerers):
            outcome = await answerer(client_event)
            if isinstance(outcome, dict):
                return outcome
            if outcome == "allowed-once":
                return {"status": "answered", "decision": "allow", "scope": "once"}
            if outcome == "rejected":
                return {"status": "answered", "decision": "deny", "scope": "once"}
            if outcome in {"cancelled", "unavailable"}:
                return {"status": outcome, "decision": "", "scope": "once"}
        return {"status": "unavailable", "decision": "", "scope": "once"}
