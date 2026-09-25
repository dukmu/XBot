"""Lossless externalization of large model-visible text."""

from __future__ import annotations

import logging

from xcore import Context

from XBotv2.agentloop import Events
from XBotv2.agentloop.events import (
    AfterToolExecution,
    InputAccepted,
    ReplaceExecution,
)
from XBotv2.content_cache.content_cache import (
    cache_tool_execution,
    cache_user_message,
)
from XBotv2.content_cache.contracts import ContentCachePolicy
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import HumanInputMessage

logger = logging.getLogger(__name__)


class ContentCacheService:
    """Externalize complete originals into canonical message artifacts."""

    def __init__(
        self,
        artifacts: ArtifactStorePort,
        policy: ContentCachePolicy,
    ) -> None:
        self._artifacts = artifacts
        self._policy = policy

    async def externalize_accepted_input(
        self,
        event: InputAccepted,
    ) -> InputAccepted | None:
        message = event.message
        if not isinstance(message, HumanInputMessage):
            return None
        try:
            projected, externalized = cache_user_message(
                message,
                self._artifacts,
                self._policy,
            )
        except OSError:
            logger.exception("content_cache.user_input.write_failed")
            return None
        if externalized is None:
            return None
        return InputAccepted(event.input, projected)

    async def externalize_tool_result(
        self,
        event: AfterToolExecution,
    ) -> ReplaceExecution | None:
        try:
            replacement, externalized = cache_tool_execution(
                event.execution,
                self._artifacts,
                self._policy,
            )
        except OSError:
            logger.exception("content_cache.tool_result.write_failed")
            return None
        if externalized is None:
            return None
        return ReplaceExecution(replacement)

class ContentCacheComponent:
    inject = ["artifacts"]
    name = "xbot.content_cache"
    Config = ContentCachePolicy

    def apply(self, ctx: Context, config: ContentCachePolicy) -> None:
        service = ContentCacheService(ctx.artifacts, config)
        ctx.on(Events.INPUT_ACCEPTED, service.externalize_accepted_input)
        ctx.on(Events.AFTER_TOOL_CALL, service.externalize_tool_result)


plugin = ContentCacheComponent()

__all__ = ["ContentCacheComponent", "ContentCacheService", "plugin"]
