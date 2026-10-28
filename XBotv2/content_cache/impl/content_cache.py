"""Lossless text externalization for provider-facing conversation content."""

from __future__ import annotations

from XBotv2.content_cache.api.models import ContentCachePolicy, ExternalizedContent
from XBotv2.core.api.artifacts import ArtifactKind, ArtifactStorePort
from XBotv2.core.api.messages import ConversationMessage, HumanInputMessage
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.tools import ToolExecution, ToolFailed, ToolOutput, ToolSucceeded


async def externalize_text(
    text: str,
    artifacts: ArtifactStorePort,
    policy: ContentCachePolicy,
    *,
    kind: ArtifactKind,
    name: str,
    resources=None,
) -> ExternalizedContent | None:
    """Store complete UTF-8 text and return its bounded provider preview."""
    if len(text) <= policy.threshold_chars:
        return None

    async def chunks():
        for start in range(0, len(text), 65536):
            yield text[start:start + 65536].encode("utf-8")

    metadata = dict(media_type="text/plain; charset=utf-8", name=name, suffix=".txt")
    if resources is None:
        artifact = await artifacts.put_stream(kind, chunks(), **metadata)
    else:
        artifact = await resources.enter_async_context(
            artifacts.prepare_stream(kind, chunks(), **metadata)
        )
    head_chars = policy.preview_chars - policy.tail_chars
    head = text[:head_chars]
    tail = text[-policy.tail_chars:] if policy.tail_chars else ""
    omitted_chars = len(text) - len(head) - len(tail)
    marker = (
        f"\n\n[… {omitted_chars} characters omitted; "
        "the complete original is attached. …]\n\n"
    )
    return ExternalizedContent(
        preview=f"{head}{marker}{tail}",
        original=artifact,
        original_chars=len(text),
    )


async def cache_user_message(
    message: ConversationMessage,
    artifacts: ArtifactStorePort,
    policy: ContentCachePolicy,
) -> tuple[ConversationMessage, ExternalizedContent | None]:
    """Build a bounded human message carrying its complete original artifact."""
    if not isinstance(message, HumanInputMessage):
        return message, None

    text_parts = [part for part in message.parts if isinstance(part, TextPart)]
    if len(text_parts) != 1:
        return message, None
    source = text_parts[0]
    externalized = await externalize_text(
        source.text,
        artifacts,
        policy,
        kind=ArtifactKind.CONTEXT,
        name="original-user-input.txt",
    )
    if externalized is None:
        return message, None

    projected_parts = tuple(
        TextPart(text=externalized.preview) if part is source else part
        for part in message.parts
    )
    projected = message.model_copy(update={
        "parts": projected_parts,
        "artifacts": (*message.artifacts, externalized.original),
    })
    return projected, externalized


async def cache_tool_execution(
    execution: ToolExecution,
    artifacts: ArtifactStorePort,
    policy: ContentCachePolicy,
) -> tuple[ToolExecution, ExternalizedContent | None]:
    """Return a new execution whose oversized textual result points to an artifact."""
    outcome = execution.message.outcome
    if not isinstance(outcome, (ToolSucceeded, ToolFailed)):
        return execution, None

    output = outcome.output
    if len(output.parts) != 1 or not isinstance(output.parts[0], TextPart):
        return execution, None
    externalized = await externalize_text(
        output.parts[0].text,
        artifacts,
        policy,
        kind=ArtifactKind.TOOL_RESULT,
        name="original-tool-result.txt",
    )
    if externalized is None:
        return execution, None

    shortened = output.model_copy(update={
        "parts": (TextPart(text=externalized.preview),),
        "artifacts": (*output.artifacts, externalized.original),
    })
    shortened_outcome = outcome.model_copy(update={"output": shortened})
    shortened_message = execution.message.model_copy(update={
        "outcome": shortened_outcome,
    })
    return execution.model_copy(update={"message": shortened_message}), externalized
