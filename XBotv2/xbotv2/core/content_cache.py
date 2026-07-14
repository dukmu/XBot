"""Externalize oversized provider context while preserving persisted messages."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from xbotv2.api.messages import Message
from xbotv2.api.tools import ToolCall

MAX_INLINE_CHARS = 12_000
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


def _bound_message(message: Message, state_store: Any, limit: int) -> Message:
    content = str(message.content or "")
    bounded_content = _externalize(content, state_store, limit)
    bounded_calls = [
        ToolCall(
            id=call.id,
            name=call.name,
            args=_bound_value(call.args, state_store, limit),
        )
        for call in message.tool_calls
    ]
    bounded_kwargs = dict(message.additional_kwargs)
    reasoning = bounded_kwargs.get("reasoning_content")
    if isinstance(reasoning, str):
        bounded_kwargs["reasoning_content"] = _externalize(
            reasoning, state_store, limit
        )
    if (
        bounded_content == content
        and bounded_calls == message.tool_calls
        and bounded_kwargs == message.additional_kwargs
    ):
        return message
    return replace(
        message,
        content=bounded_content,
        tool_calls=bounded_calls,
        additional_kwargs=bounded_kwargs,
    )


def _bound_value(value: Any, state_store: Any, limit: int) -> Any:
    if isinstance(value, str):
        return _externalize(value, state_store, limit)
    if isinstance(value, dict):
        return {
            key: _bound_value(item, state_store, limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_bound_value(item, state_store, limit) for item in value]
    return value


def _externalize(content: str, state_store: Any, limit: int) -> str:
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
    return (
        "[Long context cached]\n"
        f"cache_path: {relative}\n"
        f"original_chars: {len(content)}\n"
        f"sha256: {digest}\n"
        f"omitted_chars: {omitted}\n\n"
        "Beginning excerpt:\n"
        f"{content[:HEAD_CHARS]}\n\n"
        "Ending excerpt:\n"
        f"{content[-TAIL_CHARS:]}"
    )


__all__ = ["MAX_INLINE_CHARS", "bound_context_messages"]
