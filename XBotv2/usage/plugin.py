"""Cumulative model usage stored through the shared state protocol."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from xcore import Context
from xcore.state import StateService

from XBotv2.application import APPLICATION_INITIALIZED, ApplicationInitialized
from XBotv2.agentloop import EventContext, Events, LoopState
from XBotv2.core.messages import Message
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.core.usage import UsageData


class UsageService:
    """Own the one cumulative usage snapshot for a thread."""

    def __init__(
        self,
        store: StateService,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._store = store
        self._log = runtime_log.bind("usage")
        self._snapshot = UsageData()
        self._initialized = False

    async def initialize(self, messages: Sequence[Message]) -> None:
        if self._initialized:
            return
        stored = await self._store.get("snapshot")
        source = "history"
        if stored is None:
            snapshot = UsageData()
            for message in messages:
                usage = message.usage_metadata
                if usage:
                    delta = UsageData.from_provider(usage)
                    if not delta.is_empty():
                        snapshot = snapshot.add(delta)
            self._snapshot = snapshot
            if snapshot.requests:
                await self._store.set("snapshot", snapshot.to_snapshot())
        else:
            source = "snapshot"
            if not isinstance(stored, Mapping):
                raise TypeError("Persisted usage snapshot must be an object")
            self._snapshot = UsageData.from_snapshot(stored)
        self._initialized = True
        self._log.info(
            "usage.initialized",
            source=source,
            messages=len(messages),
            **self._snapshot.totals(),
        )

    def snapshot(self) -> dict[str, int]:
        return self._snapshot.totals()

    async def add(
        self,
        usage: Mapping[str, object],
        *,
        update_context: bool = True,
    ) -> dict[str, int] | None:
        if not self._initialized:
            raise RuntimeError("UsageService must be initialized before recording usage")
        delta = UsageData.from_provider(usage)
        if delta.is_empty():
            return None
        updated = self._snapshot.add(delta)
        self._snapshot = (
            updated
            if update_context
            else updated.model_copy(
                update={"context_tokens": self._snapshot.context_tokens}
            )
        )
        await self._store.set("snapshot", self._snapshot.to_snapshot())
        self._log.info(
            "usage.recorded",
            delta=delta.totals(),
            context_updated=update_context,
            cumulative=self._snapshot.totals(),
        )
        event = delta.to_event_dict()
        if not update_context:
            event["context_tokens"] = self._snapshot.context_tokens
        return event

    async def update_context(self, context_tokens: int) -> dict[str, int]:
        """Persist and publish a new effective-context size without a request."""
        if not self._initialized:
            raise RuntimeError("UsageService must be initialized before updating context")
        if isinstance(context_tokens, bool) or context_tokens < 0:
            raise ValueError("context_tokens must be a non-negative integer")
        self._snapshot = self._snapshot.model_copy(
            update={"context_tokens": context_tokens}
        )
        await self._store.set("snapshot", self._snapshot.to_snapshot())
        self._log.info(
            "usage.context_updated",
            context_tokens=context_tokens,
            cumulative=self._snapshot.totals(),
        )
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "requests": 0,
            "context_tokens": context_tokens,
        }


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
    inject = ["state", "loop_state", "runtime_log"]
    name = "xbot.usage"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        service = UsageService(ctx.state.namespace("usage"), ctx.runtime_log)
        handlers = UsageHandlers(service, ctx.loop_state)
        ctx.set("usage", service)
        ctx.on(Events.AFTER_MODEL_RESPONSE, handlers.record)
        ctx.on(APPLICATION_INITIALIZED, handlers.initialize, prepend=True)


plugin = UsageComponent()

__all__ = ["UsageComponent", "UsageHandlers", "UsageService"]
