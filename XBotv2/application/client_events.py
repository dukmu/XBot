"""Application-owned routing for live client events.

Feature services publish their own event payloads and register their own
waiters. Transports install one sink here instead of discovering every
feature plugin that may need a client response.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from XBotv2.application.contracts import ClientEventSink, InteractionWaiterPort
from pydantic import JsonValue

from XBotv2.core.tools import ClientEvent
from XBotv2.application.contracts import ClientEventsPort


class ClientEventRouter(ClientEventsPort):
    """Route client events without coupling transports to feature services."""

    def __init__(self, parent: "ClientEventRouter | None" = None) -> None:
        self._parent = parent
        self._sink: ClientEventSink | None = None
        self._waiters: dict[str, InteractionWaiterPort] = {}
        self._pending: dict[str, ClientEvent] = {}

    def set_sink(self, sink: ClientEventSink | None) -> ClientEventSink | None:
        previous = self._sink
        self._sink = sink
        return previous

    async def request(
        self,
        event: ClientEvent,
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, JsonValue] | None:
        if self._sink is None:
            if self._parent is not None:
                return await self._parent.request(
                    event,
                    timeout_seconds=timeout_seconds,
                    tool_call_id=tool_call_id,
                )
            return None
        request_id = str(event.data.get("request_id") or "")
        if request_id:
            # Retain the payload, not only the id: a client that reloads or
            # reconnects while the interaction is unanswered can only present
            # the dialog again if the session snapshot can replay it.
            self._pending[request_id] = event
        try:
            return await self._sink(
                event,
                timeout_seconds=timeout_seconds,
                tool_call_id=tool_call_id,
            )
        finally:
            if request_id:
                self._pending.pop(request_id, None)

    def register_waiter(
        self,
        event_type: str,
        waiter: InteractionWaiterPort,
    ) -> Callable[[], bool]:
        if event_type in self._waiters:
            raise ValueError(f"client event waiter already registered: {event_type}")
        self._waiters[event_type] = waiter

        return partial(self._unregister_waiter, event_type, waiter)

    def _unregister_waiter(
        self, event_type: str, waiter: InteractionWaiterPort
    ) -> bool:
        return self._waiters.pop(event_type, None) is waiter

    def waiter(self, event_type: str) -> InteractionWaiterPort | None:
        return self._waiters.get(event_type)

    def pending_request_ids(self) -> list[str]:
        pending: list[str] = []
        for waiter in self._waiters.values():
            pending.extend(waiter.pending_request_ids())
        return pending

    def pending_interactions(self) -> list[ClientEvent]:
        """Payloads of the requests this router is still waiting on."""
        return list(self._pending.values())
