"""Externalize oversized provider context while preserving persisted messages."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Any

from api.messages import Message, ReasoningPart, TextPart
from api.prompts import cached_content_prompt

MAX_INLINE_CHARS = 12_000
MAX_USER_INLINE_CHARS = 48_000
HEAD_CHARS = 3_000
TAIL_CHARS = 1_000

# Memoize per-session externalization: the same oversized content is re-bound
# for every ReAct iteration, so skip the digest + cache write + render on hits.
_MAX_EXTERNALIZE_CACHE = 64
_externalize_cache: OrderedDict[
    tuple[int, str, str, str], tuple[str, Path, str]
] = OrderedDict()


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
    parts = []
    text_replaced = False
    for part in message.parts:
        if isinstance(part, TextPart):
            if bounded_content == content:
                parts.append(part)
            elif not text_replaced:
                parts.append(TextPart(bounded_content))
                text_replaced = True
        elif isinstance(part, ReasoningPart):
            parts.append(
                part
                if part.provider_data
                else ReasoningPart(
                    _externalize(
                        part.text,
                        state_store,
                        limit,
                        kind="reasoning_content",
                    )
                )
            )
        else:
            parts.append(part)
    if parts == message.parts:
        return message
    return replace(message, parts=parts)


def _externalize(
    content: str,
    state_store: Any,
    limit: int,
    *,
    kind: str,
) -> str:
    if len(content) <= limit:
        return content
    cache_key = (limit, kind, content, str(state_store.artifacts_dir))
    cached = _externalize_cache.get(cache_key)
    if cached is not None:
        digest, path, rendered = cached
        if path.exists():
            return rendered
        _externalize_cache.pop(cache_key, None)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cache_dir = Path(state_store.artifacts_dir) / "context"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{digest[:16]}.txt"
    if not path.exists():
        path.write_text(content, encoding="utf-8")
    relative = Path("session") / path.relative_to(Path(state_store.root))
    omitted = len(content) - HEAD_CHARS - TAIL_CHARS
    rendered = cached_content_prompt(
        kind=kind,
        cache_path=str(relative),
        original_chars=len(content),
        omitted_chars=omitted,
        beginning=content[:HEAD_CHARS],
        ending=content[-TAIL_CHARS:],
        sha256=digest,
        inline_limit_chars=limit,
    )
    _externalize_cache[cache_key] = (digest, path, rendered)
    if len(_externalize_cache) > _MAX_EXTERNALIZE_CACHE:
        _externalize_cache.popitem(last=False)
    return rendered


__all__ = [
    "MAX_INLINE_CHARS",
    "MAX_USER_INLINE_CHARS",
    "bound_context_messages",
    "externalize_content",
]
