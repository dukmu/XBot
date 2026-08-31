"""Cache only the current oversized user input at the provider boundary."""

from __future__ import annotations

from typing import Any

from XBotv2.agentloop import EventContext, Events
from XBotv2.content_cache.content_cache import cache_user_message
from XBotv2.content_cache.config import (
    CONFIG_SCHEMA,
    ContentCacheConfig,
    parse_content_cache_config,
)
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import Message


class ContentCacheService:
    """Create and reuse provider copies for oversized current user messages."""

    def __init__(
        self,
        artifacts: ArtifactStorePort,
        config: ContentCacheConfig,
    ) -> None:
        self._artifacts = artifacts
        self._config = config
        self._cached: dict[int, tuple[Message, Message]] = {}

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
        cached = self._cached.get(id(source))
        if cached is not None and cached[0] is source:
            bounded = cached[1]
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
            self._cached[id(source)] = (source, bounded)
        bound = list(messages)
        bound[index] = bounded
        return bound


class ContentCacheHandler:
    def __init__(self, service: ContentCacheService) -> None:
        self._service = service

    async def bind_model_request(self, event: EventContext) -> None:
        request = event.model_request
        if request is not None:
            request.messages = self._service.bind_current_user_message(
                request.messages
            )


class ContentCacheComponent:
    inject = ["artifacts"]
    name = "xbot.content_cache"
    Config = CONFIG_SCHEMA

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = ContentCacheService(
            ctx.artifacts,
            parse_content_cache_config(config),
        )
        ctx.set("content_cache", service)
        ctx.on(
            Events.BEFORE_MODEL_REQUEST,
            ContentCacheHandler(service).bind_model_request,
        )


plugin = ContentCacheComponent()
