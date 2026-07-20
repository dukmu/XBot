"""Externalize oversized provider context while preserving persisted messages."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from xbotv2.api.messages import Message
from xbotv2.api.prompts import cached_content_prompt

MAX_INLINE_CHARS = 12_000
MAX_USER_INLINE_CHARS = 48_000
HEAD_CHARS = 3_000
TAIL_CHARS = 1_000


def bound_context_messages(
    messages: list[Message],
    state_store: Any,
    *,
    max_inline_chars: int = MAX_INLINE_CHARS,
) -> list[Message]:
    """Return provider-only message copies with oversized strings externalized."""
    return [
        _bound_message(message, state_store, max_inline_chars)
        for message in messages
    ]


def externalize_content(
    content: str,
    state_store: Any,
    *,
    max_inline_chars: int = MAX_INLINE_CHARS,
    kind: str = "content",
) -> str:
    """Externalize one non-message string through the context cache."""
    return _externalize(content, state_store, max_inline_chars, kind=kind)


def _bound_message(message: Message, state_store: Any, limit: int) -> Message:
    content = str(message.content or "")
    content_limit = MAX_USER_INLINE_CHARS if message.role == "user" else limit
    content_kind = {
        "user": "user_input",
        "assistant": "assistant_content",
        "tool": "tool_result",
    }.get(message.role, "message_content")
    bounded_content = (
        content
        if message.role == "system"
        else _externalize(
            content,
            state_store,
            content_limit,
            kind=content_kind,
        )
    )
    bounded_kwargs = dict(message.additional_kwargs)
    reasoning = bounded_kwargs.get("reasoning_content")
    if isinstance(reasoning, str):
        bounded_kwargs["reasoning_content"] = _externalize(
            reasoning,
            state_store,
            limit,
            kind="reasoning_content",
        )
    if (
        bounded_content == content
        and bounded_kwargs == message.additional_kwargs
    ):
        return message
    return replace(
        message,
        content=bounded_content,
        additional_kwargs=bounded_kwargs,
    )


def _externalize(
    content: str,
    state_store: Any,
    limit: int,
    *,
    kind: str,
) -> str:
    if len(content) <= limit:
        return content
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cache_dir = Path(state_store.artifacts_dir) / "context"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{digest[:16]}.txt"
    if not path.exists():
        path.write_text(content, encoding="utf-8")
    relative = Path("session") / path.relative_to(Path(state_store.root))
    omitted = len(content) - HEAD_CHARS - TAIL_CHARS
    return cached_content_prompt(
        kind=kind,
        cache_path=str(relative),
        original_chars=len(content),
        omitted_chars=omitted,
        beginning=content[:HEAD_CHARS],
        ending=content[-TAIL_CHARS:],
        sha256=digest,
        inline_limit_chars=limit,
    )


__all__ = [
    "MAX_INLINE_CHARS",
    "MAX_USER_INLINE_CHARS",
    "bound_context_messages",
    "externalize_content",
]
