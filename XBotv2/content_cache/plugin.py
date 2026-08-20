"""Content cache component: provider context externalization as a plugin.

Provides ``ctx.content_cache`` — externalizing oversized provider context
while preserving persisted messages (bound message copies for the model,
plus one-off externalization of long strings).  The engine binds its model
messages through this service.
"""

from __future__ import annotations

from typing import Any

from XBotv2.agentloop import Events
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
        state_store: Any,
        *,
        max_inline_chars: int = MAX_INLINE_CHARS,
    ) -> list[Any]:
        return bound_context_messages(
            messages, state_store, max_inline_chars=max_inline_chars
        )

    def externalize_content(
        self,
        content: str,
        state_store: Any,
        *,
        max_inline_chars: int = MAX_INLINE_CHARS,
        kind: str = "content",
    ) -> str:
        return externalize_content(
            content,
            state_store,
            max_inline_chars=max_inline_chars,
            kind=kind,
        )


class ContentCacheComponent:
    """Register the content cache service as ``ctx.content_cache``."""

    inject = ["storage"]
    name = "xbot.content_cache"

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = ContentCacheService()
        ctx.set("content_cache", service)

        async def bind_model_request(event: Any) -> None:
            request = event.model_request
            if request is None or "messages" not in request:
                return
            request["messages"] = service.bound_context_messages(
                request["messages"],
                ctx.storage,
            )

        ctx.on(Events.MODEL_REQUEST_READY, bind_model_request)


plugin = ContentCacheComponent()
