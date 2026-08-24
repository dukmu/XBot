"""Cumulative model usage stored through the shared state protocol."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from xcore import Context
from xcore.state import StateService

from XBotv2.application import APPLICATION_INITIALIZED, ApplicationInitialized
from XBotv2.agentloop import EventContext, Events, LoopState
from XBotv2.core.messages import Message
from XBotv2.core.usage import UsageDelta
from XBotv2.usage.models import UsageSnapshot


class UsageService:
    """Own the one cumulative usage snapshot for a thread."""

    def __init__(self, store: StateService) -> None:
        self._store = store
        self._snapshot = UsageSnapshot()
        self._initialized = False

    async def initialize(self, messages: Sequence[Message]) -> None:
        if self._initialized:
            return
        stored = await self._store.get("snapshot")
        if stored is None:
            snapshot = UsageSnapshot()
            for message in messages:
                usage = message.usage_metadata
                if usage:
                    delta = UsageDelta.from_mapping(usage)
                    if not delta.is_empty():
                        snapshot = snapshot.add(delta)
            self._snapshot = snapshot
            if snapshot.requests:
                await self._store.set("snapshot", snapshot.to_dict())
        else:
            if not isinstance(stored, Mapping):
                raise TypeError("Persisted usage snapshot must be an object")
            self._snapshot = UsageSnapshot.from_dict(stored)
        self._initialized = True

    def snapshot(self) -> dict[str, int]:
        return self._snapshot.totals()

    async def add(self, usage: Mapping[str, object]) -> bool:
        if not self._initialized:
            raise RuntimeError("UsageService must be initialized before recording usage")
        delta = UsageDelta.from_mapping(usage)
        if delta.is_empty():
            return False
        self._snapshot = self._snapshot.add(delta)
        await self._store.set("snapshot", self._snapshot.to_dict())
        return True


class UsageHandlers:
    def __init__(self, service: UsageService, state: LoopState) -> None:
        self._service = service
        self._state = state

    async def initialize(self, _event: ApplicationInitialized) -> None:
        await self._service.initialize(self._state.messages)

    async def record(self, event: EventContext) -> None:
        response = event.model_response
        if response is not None and response.usage_metadata:
            await self._service.add(response.usage_metadata)


class UsageComponent:
    inject = ["state", "loop_state"]
    name = "xbot.usage"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        service = UsageService(ctx.state.namespace("usage"))
        handlers = UsageHandlers(service, ctx.loop_state)
        ctx.set("usage", service)
        ctx.on(Events.AFTER_MODEL_RESPONSE, handlers.record)
        ctx.on(APPLICATION_INITIALIZED, handlers.initialize, prepend=True)


plugin = UsageComponent()

__all__ = ["UsageComponent", "UsageHandlers", "UsageService"]
