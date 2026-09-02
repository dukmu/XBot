"""Application-owned routing for live client events.

Feature services publish their own event payloads and register their own
waiters. Transports install one sink here instead of discovering every
feature plugin that may need a client response.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from XBotv2.application.services import ClientEventSink, InteractionWaiterPort
from pydantic import JsonValue

from XBotv2.core.tools import ClientEvent


class ClientEventRouter:
    """Route client events without coupling transports to feature services."""

    def __init__(self, parent: "ClientEventRouter | None" = None) -> None:
        self._parent = parent
        self._sink: ClientEventSink | None = None
        self._waiters: dict[str, InteractionWaiterPort] = {}

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
        if self._sink is not None:
            return await self._sink(
                event,
                timeout_seconds=timeout_seconds,
                tool_call_id=tool_call_id,
            )
        if self._parent is not None:
            return await self._parent.request(
                event,
                timeout_seconds=timeout_seconds,
                tool_call_id=tool_call_id,
            )
        return None

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
