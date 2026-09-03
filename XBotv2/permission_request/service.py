"""Approval service: the live permission-approval seam.

Provides ``ctx.approval`` — transport for a plugin-owned permission request.
It owns pending-response coordination but no permission policy.
"""

from __future__ import annotations

from pydantic import JsonValue

from XBotv2.agentloop import EventContext, Events
from XBotv2.application.services import ApplicationEventsPort, ClientEventsPort
from XBotv2.core.tools import ClientEvent
from XBotv2.interactions.interactions import InteractionWaiter

class ApprovalService:
    """Live approval transport with no permission-policy knowledge."""

    def __init__(
        self,
        events: ApplicationEventsPort,
        client_events: ClientEventsPort,
    ) -> None:
        self._events = events
        self._client_events = client_events
        self._waiter = InteractionWaiter()

    @property
    def waiter(self) -> InteractionWaiter:
        """Fallback waiter used when no live sink answerer is installed."""
        return self._waiter

    def session_closed(self, _event: EventContext) -> None:
        self._waiter.cancel_all("session_closed")

    async def request(self, client_event: ClientEvent) -> dict[str, JsonValue]:
        """Publish a request and return the client's raw decision record."""
        await self._events.emit(
            Events.CLIENT_EVENT,
            EventContext(client_event=client_event),
        )
        sink_result = await self._client_events.request(client_event)
        if sink_result is not None:
            return sink_result
        return {"status": "unavailable", "decision": "", "scope": "once"}
