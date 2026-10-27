"""Cumulative request usage owned by one thread."""

from __future__ import annotations

from XBotv2.agentloop.api.state import LOOP_STATE, LoopState
import asyncio
from collections.abc import Mapping, Sequence
from pydantic import JsonValue
from xcore.api.context import Context
from XBotv2.persistence.api.contracts import StatePort
from XBotv2.session.api.storage import THREAD_REPOSITORY
from XBotv2.agentloop.api.events import Events, ModelResponseObserved
from XBotv2.agentloop.api.events import THREAD_INITIALIZED
from XBotv2.agentloop.api.events import RUNTIME_EVENT
from XBotv2.agentloop.api.events import ThreadInitialized
from XBotv2.agentloop.api.events import RuntimeEvent
from XBotv2.core.api.domain import (
    RequestObservation,
    TokenCounters,
    UsageDelta,
    UsageSnapshot,
)
from XBotv2.core.api.messages import AssistantMessage, ConversationMessage
from XBotv2.core.api.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.usage.api.service import USAGE_COUNTERS_KEY, USAGE_STATE_NAMESPACE, USAGE
from XBotv2.usage.api.events import UsageUpdated


class UsageService:
    """Own the sole cumulative usage snapshot for a thread."""

    def __init__(
        self,
        store: StatePort,
        events,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
        *,
        thread_repository,
    ) -> None:
        self._store = store
        self._thread_repository = thread_repository
        self._events = events
        self._log = runtime_log.bind("usage")
        self._snapshot = UsageSnapshot()
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self, messages: Sequence[ConversationMessage]) -> None:
        async with self._lock:
            if self._initialized:
                return
            stored = await self._store.get(USAGE_COUNTERS_KEY)
            source = "history"
            if stored is None:
                self._snapshot = UsageSnapshot()
            else:
                source = "snapshot"
                if not isinstance(stored, Mapping):
                    raise TypeError("Persisted usage snapshot must be an object")
                if set(stored) != {"total_counters"}:
                    raise ValueError("Persisted usage must contain only total_counters")
                counters = TokenCounters.model_validate(stored["total_counters"])
                latest = next(
                    (
                        message.exchange.observation
                        for message in reversed(messages)
                        if isinstance(message, AssistantMessage)
                        and message.exchange.observation.purpose.kind == "turn"
                    ),
                    None,
                )
                self._snapshot = UsageSnapshot(
                    total_counters=counters,
                    latest_turn_observation=latest,
                )
            self._initialized = True
            self._log.info(
                "usage.initialized",
                source=source,
                messages=len(messages),
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
        await self._thread_repository.ensure()
        await self._store.set(
            USAGE_COUNTERS_KEY,
            {"total_counters": self._snapshot.total_counters.model_dump(mode="json")},
        )


class UsageHandlers:
    def __init__(self, service: UsageService, state: LoopState) -> None:
        self._service = service
        self._state = state

    async def initialize(self, _event: ThreadInitialized) -> None:
        await self._service.initialize(self._state.messages)

    async def record(self, event: ModelResponseObserved) -> None:
        await self._service.record(event.exchange.observation, event.exchange.usage)


class UsageComponent:
    inject = ["state", LOOP_STATE, "runtime_log", THREAD_REPOSITORY]
    name = "xbot.usage"

    def apply(self, ctx: Context, config: dict[str, JsonValue] | None = None) -> None:
        service = UsageService(
            ctx.state.namespace(USAGE_STATE_NAMESPACE),
            ctx,
            ctx.runtime_log,
            thread_repository=ctx.require(THREAD_REPOSITORY),
        )
        handlers = UsageHandlers(service, ctx.require(LOOP_STATE))
        ctx.set(USAGE, service)
        ctx.on(Events.MODEL_RESPONSE_OBSERVED, handlers.record)
        ctx.on(THREAD_INITIALIZED, handlers.initialize, prepend=True)


def mount_thread(ctx, config):
    UsageComponent().apply(ctx, config)


mount_thread.inject = getattr(UsageComponent, "inject", ())
mount_thread.provide = ("usage",)
mount_thread.Config = getattr(UsageComponent, "Config", None)


def mount_process(ctx, config):
    from XBotv2.usage.api.service import USAGE_ARCHIVE
    from XBotv2.usage.impl.storage import UsageArchive

    ctx.provide(
        USAGE_ARCHIVE, UsageArchive(ctx.require(PERSISTENCE), ctx.require(ARCHIVE))
    )
    from XBotv2.session.api.storage import FORK_COPY_STATE

    async def fork_state(event):
        backend = ctx.require(PERSISTENCE)
        for thread in event.thread_ids:
            source = (
                await backend.state(event.source_session_id, thread)
                .namespace(USAGE_STATE_NAMESPACE)
                .get(USAGE_COUNTERS_KEY)
            )
            baseline = (
                source["total_counters"]
                if source is not None
                else TokenCounters().model_dump(mode="json")
            )
            event.origins.setdefault(thread, {})["usage"] = baseline
            await (
                backend.state(event.target_session_id, thread)
                .namespace(USAGE_STATE_NAMESPACE)
                .set(
                    USAGE_COUNTERS_KEY,
                    {"total_counters": TokenCounters().model_dump(mode="json")},
                )
            )
        event.handled.add(USAGE_STATE_NAMESPACE)

    ctx.on(FORK_COPY_STATE, fork_state)


from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.usage.api.service import USAGE_ARCHIVE

mount_process.inject = (PERSISTENCE, ARCHIVE)
mount_process.provide = (USAGE_ARCHIVE,)
