"""Content cache component: provider context externalization as a plugin.

Provides ``ctx.content_cache`` — externalizing oversized provider context
while preserving persisted messages (bound message copies for the model,
plus one-off externalization of long strings).  The engine binds its model
messages through this service.
"""

from __future__ import annotations

from typing import Any

from XBotv2.agentloop import EventContext, Events
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.content_cache.content_cache import (
    MAX_INLINE_CHARS,
    MAX_USER_INLINE_CHARS,
    bound_context_messages,
    externalize_content,
)


class ContentCacheService:
    """Bound and externalize oversized provider context."""

    def bound_context_messages(
        self,
        messages: list[Any],
        artifacts: ArtifactStorePort,
        *,
        max_inline_chars: int = MAX_INLINE_CHARS,
    ) -> list[Any]:
        return bound_context_messages(
            messages, artifacts, max_inline_chars=max_inline_chars
        )

    def externalize_content(
        self,
        content: str,
        artifacts: ArtifactStorePort,
        *,
        max_inline_chars: int = MAX_INLINE_CHARS,
        kind: str = "content",
    ) -> str:
        return externalize_content(
            content,
            artifacts,
            max_inline_chars=max_inline_chars,
            kind=kind,
        )

    async def bind_model_request(
        self,
        event: EventContext,
        artifacts: ArtifactStorePort,
    ) -> None:
        request = event.model_request
        if request is not None:
            request.messages = self.bound_context_messages(
                request.messages,
                artifacts,
            )


class ContentCacheComponent:
    """Register the content cache service as ``ctx.content_cache``."""

    inject = ["artifacts"]
    name = "xbot.content_cache"

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = ContentCacheService()
        ctx.set("content_cache", service)
        handler = ContentCacheHandler(service, ctx.artifacts)
        ctx.on(Events.MODEL_REQUEST_READY, handler.bind_model_request)


class ContentCacheHandler:
    def __init__(self, service: ContentCacheService, artifacts: ArtifactStorePort) -> None:
        self._service = service
        self._artifacts = artifacts

    async def bind_model_request(self, event: EventContext) -> None:
        await self._service.bind_model_request(event, self._artifacts)


plugin = ContentCacheComponent()
