"""Externalize the current oversized user input for one model request."""

from __future__ import annotations

from dataclasses import replace

from XBotv2.core.artifacts import ArtifactKind, ArtifactRef, ArtifactStorePort
from XBotv2.core.messages import Message, TextPart
from XBotv2.core.prompts import cached_content_prompt, content_preview

DEFAULT_CACHE_THRESHOLD_CHARS = 48_000
DEFAULT_PREVIEW_CHARS = 12_000
DEFAULT_TAIL_CHARS = 2_000


def cache_user_message(
    message: Message,
    artifacts: ArtifactStorePort,
    *,
    cache_threshold_chars: int = DEFAULT_CACHE_THRESHOLD_CHARS,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> tuple[Message, ArtifactRef | None]:
    """Return a provider-only copy of one oversized user message."""
    if (
        message.role != "user"
        or len(message.content) <= cache_threshold_chars
    ):
        return message, None

    content = message.content
    artifact = artifacts.put(
        ArtifactKind.CONTEXT,
        content.encode("utf-8"),
        media_type="text/plain",
        suffix=".txt",
    )
    head, tail = content_preview(
        content,
        preview_chars=preview_chars,
        tail_chars=tail_chars,
    )
    rendered = cached_content_prompt(
        kind="user_input",
        cache_path=artifacts.model_path(artifact),
        original_chars=len(content),
        omitted_chars=len(content) - len(head) - len(tail),
        beginning=head,
        ending=tail,
        sha256=artifact.sha256,
        inline_limit_chars=len(head) + len(tail),
        cache_threshold_chars=cache_threshold_chars,
    )
    parts = [
        TextPart(rendered) if isinstance(part, TextPart) else part
        for part in message.parts
    ]
    return replace(message, parts=parts), artifact


__all__ = ["DEFAULT_CACHE_THRESHOLD_CHARS", "cache_user_message"]
