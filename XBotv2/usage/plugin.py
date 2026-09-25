"""Cumulative request usage owned by one thread."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from pydantic import JsonValue
from xcore import Context
from xcore.state import StateService

from XBotv2.agentloop import Events, LoopState
from XBotv2.agentloop.events import ModelResponseObserved
from XBotv2.application import (
    APPLICATION_INITIALIZED,
    RUNTIME_EVENT,
    ApplicationInitialized,
    RuntimeEvent,
)
from XBotv2.core.domain import RequestObservation, UsageDelta, UsageSnapshot
from XBotv2.core.messages import AssistantMessage, ConversationMessage
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.usage.contracts import (
    USAGE_SNAPSHOT_KEY,
    USAGE_STATE_NAMESPACE,
    UsageUpdated,
)


class UsageService:
    """Own the sole cumulative usage snapshot for a thread."""

    def __init__(
        self,
        store: StateService,
        events,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._store = store
        self._events = events
        self._log = runtime_log.bind("usage")
        self._snapshot = UsageSnapshot()
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self, messages: Sequence[ConversationMessage]) -> None:
        async with self._lock:
            if self._initialized:
                return
            stored = await self._store.get(USAGE_SNAPSHOT_KEY)
            source = "history"
            if stored is None:
                snapshot = UsageSnapshot()
                for message in messages:
                    if isinstance(message, AssistantMessage):
                        snapshot = snapshot.add(
                            message.exchange.observation,
                            message.exchange.usage,
                        )
                self._snapshot = snapshot
                if snapshot.requests:
                    await self._persist()
            else:
                source = "snapshot"
                if not isinstance(stored, Mapping):
                    raise TypeError("Persisted usage snapshot must be an object")
                self._snapshot = UsageSnapshot.model_validate(stored)
            self._initialized = True
            self._log.info(
                "usage.initialized",
                source=source,
                messages=len(messages),
                requests=len(self._snapshot.requests),
                counters=self._snapshot.total_counters.model_dump(mode="json"),
            )

    def snapshot(self) -> UsageSnapshot:
        return self._snapshot

    async def record(
        self,
        observation: RequestObservation,
        usage: UsageDelta,
    ) -> UsageSnapshot:
        async with self._lock:
            if not self._initialized:
                raise RuntimeError(
                    "UsageService must be initialized before recording usage"
                )
            self._snapshot = self._snapshot.add(observation, usage)
            await self._persist()
            self._log.info(
                "usage.recorded",
                purpose=observation.purpose.kind,
                delta=usage.counters.model_dump(mode="json"),
                cumulative=self._snapshot.total_counters.model_dump(mode="json"),
            )
            await self._events.emit(
                RUNTIME_EVENT,
                RuntimeEvent(event=UsageUpdated(snapshot=self._snapshot)),
            )
            return self._snapshot

    async def _persist(self) -> None:
        await self._store.set(
            USAGE_SNAPSHOT_KEY,
            self._snapshot.model_dump(mode="json"),
        )


class UsageHandlers:
    def __init__(self, service: UsageService, state: LoopState) -> None:
        self._service = service
        self._state = state

    async def initialize(self, _event: ApplicationInitialized) -> None:
        await self._service.initialize(self._state.messages)

    async def record(self, event: ModelResponseObserved) -> None:
        await self._service.record(event.exchange.observation, event.exchange.usage)


class UsageComponent:
    inject = ["state", "loop_state", "runtime_log"]
    name = "xbot.usage"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        service = UsageService(
            ctx.state.namespace(USAGE_STATE_NAMESPACE),
            ctx,
            ctx.runtime_log,
        )
        handlers = UsageHandlers(service, ctx.loop_state)
        ctx.set("usage", service)
        ctx.on(Events.MODEL_RESPONSE_OBSERVED, handlers.record)
        ctx.on(APPLICATION_INITIALIZED, handlers.initialize, prepend=True)


plugin = UsageComponent()

__all__ = ["UsageComponent", "UsageHandlers", "UsageService"]
