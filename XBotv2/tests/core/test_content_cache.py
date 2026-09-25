"""Lossless content-cache projections and their ArtifactStore references."""

import pytest

from XBotv2.content_cache.content_cache import (
    cache_tool_execution,
    cache_user_message,
    externalize_text,
)
from XBotv2.content_cache.contracts import ContentCachePolicy
from XBotv2.content_cache.plugin import ContentCacheService
from XBotv2.agentloop.contracts import HumanInput, InboxItem, InboxTarget
from XBotv2.agentloop.events import (
    AfterToolExecution,
    InputAccepted,
)
from XBotv2.core.artifacts import ArtifactKind
from XBotv2.core.domain import InputId, MessageId, ToolCallId, ToolTiming
from XBotv2.core.messages import HumanInputMessage, ToolMessage
from XBotv2.core.parts import TextPart
from xcore import Context
from XBotv2.core.tools import (
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


def test_externalized_content_keeps_the_complete_utf8_original(artifact_store):
    source = "start α middle-secret end 二"

    result = externalize_text(
        source,
        artifact_store,
        policy(),
        kind=ArtifactKind.CONTEXT,
        name="source.txt",
    )

    assert result is not None
    assert result.original.kind is ArtifactKind.CONTEXT
    assert result.original_chars == len(source)
    assert artifact_store.read(result.original) == source.encode("utf-8")
    assert artifact_store.exists(result.original)
    assert result.preview.startswith("start")
    assert result.preview.endswith("二")
    assert "middle-secret" not in result.preview


def test_short_text_is_not_externalized(artifact_store):
    assert externalize_text(
        "short",
        artifact_store,
        policy(),
        kind=ArtifactKind.CONTEXT,
        name="source.txt",
    ) is None


def test_user_cache_returns_a_projection_without_mutating_canonical_history(
    artifact_store,
):
    source = "start α " + ("middle-secret " * 5) + "end 二"
    message = HumanInputMessage(
        id=MessageId("message-long"),
        input_id=InputId("input-long"),
        parts=(TextPart(text=source),),
    )

    projected, externalized = cache_user_message(
        message,
        artifact_store,
        policy(),
    )

    assert isinstance(projected, HumanInputMessage)
    assert projected is not message
    assert message.parts == (TextPart(text=source),)
    assert message.artifacts == ()
    assert externalized is not None
    assert projected.parts[0].text == externalized.preview
    assert projected.artifacts == (externalized.original,)
    assert artifact_store.read(projected.artifacts[0]) == source.encode("utf-8")


def test_tool_execution_cache_returns_new_preview_and_preserves_full_result(
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

    projected, externalized = cache_tool_execution(
        execution,
        artifact_store,
        policy(),
    )

    assert projected is not execution
    assert execution.message.outcome.output.parts == (TextPart(text=source),)
    assert externalized is not None
    assert projected.message.outcome.output.parts == (
        TextPart(text=externalized.preview),
    )
    assert projected.message.outcome.output.artifacts == (externalized.original,)
    assert artifact_store.read(externalized.original) == source.encode("utf-8")


@pytest.mark.parametrize("value", ["short", "", "small result"])
def test_under_threshold_inputs_and_tool_results_keep_identity(artifact_store, value):
    message = HumanInputMessage(
        id=MessageId("message-short"),
        input_id=InputId("input-short"),
        parts=(TextPart(text=value),),
    )
    projected_message, input_externalized = cache_user_message(
        message,
        artifact_store,
        policy(),
    )
    execution = ToolExecution(
        message=ToolMessage(
            id=MessageId("tool-short"),
            call=ToolCallRef(id=ToolCallId("call-short"), name="read"),
            outcome=ToolSucceeded(output=ToolOutput(parts=(TextPart(text=value),))),
            timing=ToolTiming(duration_ms=1),
        ),
        directive=ContinueTurn(),
    )
    projected_execution, tool_externalized = cache_tool_execution(
        execution,
        artifact_store,
        policy(),
    )

    assert projected_message is message
    assert input_externalized is None
    assert projected_execution is execution
    assert tool_externalized is None


class _FailingArtifactStore:
    def put(self, *_args, **_kwargs):
        raise OSError("artifact store is unavailable")


@pytest.mark.asyncio
async def test_artifact_write_failure_keeps_large_user_input_unchanged(
    artifact_store,
    caplog,
):
    source = "complete user input " + ("secret " * 5)
    message = HumanInputMessage(
        id=MessageId("message-write-failure"),
        input_id=InputId("input-write-failure"),
        parts=(TextPart(text=source),),
    )
    service = ContentCacheService(_FailingArtifactStore(), policy())
    result = await service.externalize_accepted_input(InputAccepted(
        input=InboxItem(
            id="input-write-failure",
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content=source),
        ),
        message=message,
    ))

    assert result is None
    assert message.parts == (TextPart(text=source),)
    assert message.artifacts == ()
    assert "content_cache.user_input.write_failed" in caplog.text


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


@pytest.mark.asyncio
async def test_accepted_input_replacement_persists_original_artifact(artifact_store):
    source = "complete user input " + ("secret " * 5)
    message = HumanInputMessage(
        id=MessageId("message-turn-end"),
        input_id=InputId("input-turn-end"),
        parts=(TextPart(text=source),),
    )
    service = ContentCacheService(artifact_store, policy())
    accepted = InputAccepted(
        input=InboxItem(
            id="input-turn-end",
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content=source),
        ),
        message=message,
    )
    result = await service.externalize_accepted_input(accepted)

    assert isinstance(result, InputAccepted)
    assert result is not accepted
    assert result.input == accepted.input
    assert result.message is not message
    assert result.message.id == message.id
    assert result.message.input_id == message.input_id
    assert result.message.parts[0].text != source
    assert len(result.message.artifacts) == 1
    assert artifact_store.read(result.message.artifacts[0]) == source.encode("utf-8")
    assert message.parts == (TextPart(text=source),)
    assert message.artifacts == ()
