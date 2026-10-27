"""Lossless externalization of large model-visible text."""

from __future__ import annotations

import logging
from xcore.api.context import Context
from XBotv2.agentloop.api.events import (
    Events, AfterToolExecution, PrepareInput, InputAccepted, ReplaceExecution,
)
from XBotv2.content_cache.impl.content_cache import (
    cache_tool_execution, cache_user_message, externalize_text,
)
from XBotv2.content_cache.api.models import ContentCachePolicy
from XBotv2.core.api.artifacts import ArtifactStorePort, ArtifactKind
from XBotv2.agentloop.api.contracts import HumanInput
from XBotv2.core.api.parts import TextPart

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

    async def prepare_input(self, event: PrepareInput) -> PrepareInput | None:
        payload = event.item.input
        if not isinstance(payload, HumanInput):
            return None
        artifacts = payload.artifacts
        if event.editing:
            artifacts = tuple(
                ref for ref in artifacts if not (
                    ref.kind == ArtifactKind.CONTEXT
                    and ref.name == "original-user-input.txt"
                )
            )
        externalized = await externalize_text(
            payload.content, self._artifacts, self._policy,
            kind=ArtifactKind.CONTEXT, name="original-user-input.txt",
            resources=event.resources,
        )
        if externalized is None:
            if artifacts == payload.artifacts:
                return None
            projected = payload.model_copy(update={"artifacts": artifacts})
        else:
            projected = payload.model_copy(update={
                "content": externalized.preview,
                "artifacts": (*artifacts, externalized.original),
            })
        return PrepareInput(
            event.item.model_copy(update={"input": projected}),
            event.resources, editing=event.editing,
        )

    async def externalize_expanded_input(self, event: InputAccepted) -> InputAccepted | None:
        # Prompt expansion can introduce a new large body after acceptance.
        # This is a derived content version, not another copy of the request.
        text = tuple(
            part for part in event.message.parts if isinstance(part, TextPart)
        )
        if (
            text == (TextPart(text=event.source.input.content),)
            and event.message.artifacts == event.source.input.artifacts
        ):
            return None
        projected, externalized = await cache_user_message(
            event.message, self._artifacts, self._policy,
        )
        return InputAccepted(event.input, projected, event.source) if externalized else None

    async def externalize_tool_result(
        self,
        event: AfterToolExecution,
    ) -> ReplaceExecution | None:
        try:
            replacement, externalized = await cache_tool_execution(
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
        ctx.on(Events.PREPARE_INPUT, service.prepare_input)
        ctx.on(Events.INPUT_ACCEPTED, service.externalize_expanded_input)
        ctx.on(Events.AFTER_TOOL_CALL, service.externalize_tool_result)


def mount_thread(ctx, config):
    ContentCacheComponent().apply(ctx, config)


mount_thread.inject = getattr(ContentCacheComponent, "inject", ())
mount_thread.provide = ()
mount_thread.Config = getattr(ContentCacheComponent, "Config", None)
