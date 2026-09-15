"""Cache only the current oversized user input at the provider boundary."""

from __future__ import annotations

from xcore import Context

from XBotv2.agentloop import EventContext, Events
from XBotv2.content_cache.content_cache import cache_user_message
from XBotv2.content_cache.contracts import (
    ContentCacheConfig,
)
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import Message


class ContentCacheService:
    """Create and reuse provider copies for oversized current user messages.

    Only the most recent oversized user message is ever consulted, so the
    memoization holds at most one entry and is cleared at turn end.
    """

    def __init__(
        self,
        artifacts: ArtifactStorePort,
        config: ContentCacheConfig,
    ) -> None:
        self._artifacts = artifacts
        self._config = config
        self._cached: tuple[int, Message, Message] | None = None

    def bind_current_user_message(self, messages: list[Message]) -> list[Message]:
        index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].role == "user"
            ),
            None,
        )
        if index is None:
            return messages
        source = messages[index]
        cached = (
            self._cached
            if self._cached is not None
            and self._cached[0] == id(source)
            and self._cached[1] is source
            else None
        )
        if cached is not None:
            bounded = cached[2]
        else:
            bounded, artifact = cache_user_message(
                source,
                self._artifacts,
                cache_threshold_chars=self._config.cache_threshold_chars,
                preview_chars=self._config.preview_chars,
                tail_chars=self._config.tail_chars,
            )
            if artifact is None:
                return messages
            # Bounded by design: replacing the slot keeps exactly one entry.
            self._cached = (id(source), source, bounded)
        bound = list(messages)
        bound[index] = bounded
        return bound

    def clear(self) -> None:
        self._cached = None


class ContentCacheHandler:
    def __init__(self, service: ContentCacheService) -> None:
        self._service = service

    async def bind_model_request(self, event: EventContext) -> None:
        request = event.model_request
        if request is not None:
            request.messages = self._service.bind_current_user_message(
                request.messages
            )

    async def clear_cache(self, _event: EventContext) -> None:
        self._service.clear()


class ContentCacheComponent:
    inject = ["artifacts"]
    name = "xbot.content_cache"
    Config = ContentCacheConfig

    def apply(self, ctx: Context, config: ContentCacheConfig) -> None:
        service = ContentCacheService(
            ctx.artifacts,
            config,
        )
        ctx.set("content_cache", service)
        # An independent observer: binding the bounded copy of an oversized
        # user message must happen even when another listener answers the
        # request first (e.g. a compaction rebuild).
        ctx.on(
            Events.BEFORE_MODEL_REQUEST,
            ContentCacheHandler(service).bind_model_request,
            prepend=True,
        )
        ctx.on(
            Events.TURN_END,
            ContentCacheHandler(service).clear_cache,
        )


plugin = ContentCacheComponent()
