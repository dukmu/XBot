"""Lossless content-cache projections and their ArtifactStore references."""

from contextlib import AsyncExitStack

import pytest
from XBotv2.content_cache.impl.content_cache import (
    cache_tool_execution,
    cache_user_message,
    externalize_text,
)
from XBotv2.content_cache.api.models import ContentCachePolicy
from XBotv2.content_cache.impl.plugin import ContentCacheService
from XBotv2.agentloop.api.contracts import HumanInput, InboxItem
from XBotv2.agentloop.api.events import AfterToolExecution, PrepareInput
from XBotv2.core.api.artifacts import ArtifactKind
from XBotv2.core.api.domain import InboxTarget, MessageId, ToolCallId, ToolTiming
from XBotv2.core.api.messages import HumanInputMessage, ToolMessage
from XBotv2.core.api.parts import TextPart
from xcore.api.context import Context
from XBotv2.core.api.tools import (
    ContinueTurn,
    ToolCallRef,
    ToolExecution,
    ToolSucceeded,
    ToolOutput,
)


def policy() -> ContentCachePolicy:
    return ContentCachePolicy(
        threshold_chars=12,
        preview_chars=8,
        tail_chars=3,
    )


async def test_externalized_content_keeps_the_complete_utf8_original(artifact_store):
    source = "start α middle-secret end 二"

    result = (await externalize_text(
        source,
        artifact_store,
        policy(),
        kind=ArtifactKind.CONTEXT,
        name="source.txt",
    ))

    assert result is not None
    assert result.original.kind is ArtifactKind.CONTEXT
    assert result.original_chars == len(source)
    assert (await artifact_store.read(result.original)) == source.encode("utf-8")
    assert (await artifact_store.exists(result.original))
    assert result.preview.startswith("start")
    assert result.preview.endswith("二")
    assert "middle-secret" not in result.preview


async def test_short_text_is_not_externalized(artifact_store):
    assert (await externalize_text(
        "short",
        artifact_store,
        policy(),
        kind=ArtifactKind.CONTEXT,
        name="source.txt",
    )) is None


async def test_user_cache_returns_a_projection_without_mutating_canonical_history(
    artifact_store,
):
    source = "start α " + ("middle-secret " * 5) + "end 二"
    message = HumanInputMessage(
        id=MessageId("message-long"),
        parts=(TextPart(text=source),),
    )

    projected, externalized = (await cache_user_message(
        message,
        artifact_store,
        policy(),
    ))

    assert isinstance(projected, HumanInputMessage)
    assert projected is not message
    assert message.parts == (TextPart(text=source),)
    assert message.artifacts == ()
    assert externalized is not None
    assert projected.parts[0].text == externalized.preview
    assert projected.artifacts == (externalized.original,)
    assert (await artifact_store.read(projected.artifacts[0])) == source.encode("utf-8")


async def test_tool_execution_cache_returns_new_preview_and_preserves_full_result(
    artifact_store,
):
    source = "tool-start α " + ("middle-secret " * 5) + "tool-end 二"
    execution = ToolExecution(
        message=ToolMessage(
            id=MessageId("tool-message"),
            call=ToolCallRef(id=ToolCallId("call-1"), name="read"),
            outcome=ToolSucceeded(output=ToolOutput(parts=(TextPart(text=source),))),
            timing=ToolTiming(duration_ms=1),
        ),
        directive=ContinueTurn(),
    )

    projected, externalized = (await cache_tool_execution(
        execution,
        artifact_store,
        policy(),
    ))

    assert projected is not execution
    assert execution.message.outcome.output.parts == (TextPart(text=source),)
    assert externalized is not None
    assert projected.message.outcome.output.parts == (
        TextPart(text=externalized.preview),
    )
    assert projected.message.outcome.output.artifacts == (externalized.original,)
    assert (await artifact_store.read(externalized.original)) == source.encode("utf-8")


@pytest.mark.parametrize("value", ["short", "", "small result"])
async def test_under_threshold_inputs_and_tool_results_keep_identity(artifact_store, value):
    message = HumanInputMessage(
        id=MessageId("message-short"),
        parts=(TextPart(text=value),),
    )
    projected_message, input_externalized = (await cache_user_message(
        message,
        artifact_store,
        policy(),
    ))
    execution = ToolExecution(
        message=ToolMessage(
            id=MessageId("tool-short"),
            call=ToolCallRef(id=ToolCallId("call-short"), name="read"),
            outcome=ToolSucceeded(output=ToolOutput(parts=(TextPart(text=value),))),
            timing=ToolTiming(duration_ms=1),
        ),
        directive=ContinueTurn(),
    )
    projected_execution, tool_externalized = (await cache_tool_execution(
        execution,
        artifact_store,
        policy(),
    ))

    assert projected_message is message
    assert input_externalized is None
    assert projected_execution is execution
    assert tool_externalized is None


class _FailingArtifactStore:
    async def put_stream(self, *_args, **_kwargs):
        raise OSError("artifact store is unavailable")

    def prepare_stream(self, *_args, **_kwargs):
        raise OSError("artifact store is unavailable")


async def test_input_preparation_failure_prevents_acceptance():
    service = ContentCacheService(_FailingArtifactStore(), policy())
    item = InboxItem(id="failed", target=InboxTarget.NEXT_TURN,
                     input=HumanInput(content="complete user input " * 5))
    with pytest.raises(OSError, match="unavailable"):
        async with AsyncExitStack() as resources:
            await service.prepare_input(PrepareInput(item, resources))


@pytest.mark.asyncio
async def test_artifact_write_failure_keeps_large_tool_result_unchanged(caplog):
    source = "complete tool result " + ("secret " * 5)
    execution = ToolExecution(
        message=ToolMessage(
            id=MessageId("tool-write-failure"),
            call=ToolCallRef(id=ToolCallId("call-write-failure"), name="read"),
            outcome=ToolSucceeded(output=ToolOutput(parts=(TextPart(text=source),))),
            timing=ToolTiming(duration_ms=1),
        ),
        directive=ContinueTurn(),
    )
    service = ContentCacheService(_FailingArtifactStore(), policy())

    result = await service.externalize_tool_result(
        AfterToolExecution(execution),
    )

    assert result is None
    assert execution.message.outcome.output.parts == (TextPart(text=source),)
    assert "content_cache.tool_result.write_failed" in caplog.text


async def test_prepared_input_carries_original_artifact_before_persistence(artifact_store):
    source = "complete user input " + ("secret " * 5)
    item = InboxItem(id="prepared", target=InboxTarget.NEXT_TURN,
                     input=HumanInput(content=source))
    service = ContentCacheService(artifact_store, policy())
    async with AsyncExitStack() as resources:
        prepared = await service.prepare_input(PrepareInput(item, resources))
    assert prepared.item.id == item.id
    assert prepared.item.input.content != source
    assert await artifact_store.read(prepared.item.input.artifacts[0]) == source.encode("utf-8")
    assert item.input.content == source
    assert item.input.artifacts == ()
