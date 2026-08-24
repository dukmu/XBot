"""Tool-result externalization hook."""

from __future__ import annotations

from typing import Any

from XBotv2.core.artifacts import ArtifactKind, ArtifactStorePort
from XBotv2.core.prompts import (
    CACHED_CONTENT_KEY,
    DISPLAY_CONTENT_KEY,
    cached_content_prompt,
)


DEFAULT_MAX_INLINE_CHARS = 12000
DEFAULT_PREVIEW_CHARS = 4000
DEFAULT_TAIL_CHARS = 1000


def make_tool_result_cache_hook(
    artifacts: ArtifactStorePort,
    *,
    max_inline_chars: int = DEFAULT_MAX_INLINE_CHARS,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
):
    """Create an AFTER_TOOLS hook that caches large tool message contents.

    The hook mutates ``ctx.tool_results`` in place so the engine persists and
    emits the bounded message instead of the full output.
    """

    async def cache_large_tool_results(ctx: Any) -> None:
        if not ctx.tool_results:
            return None

        for message in ctx.tool_results:
            candidate = _cache_candidate(message, max_inline_chars)
            if candidate is None:
                continue
            content, suffix = candidate

            tool_call_id = getattr(message, "tool_call_id", "tool")
            stored = artifacts.put(
                ArtifactKind.TOOL_RESULT,
                content.encode("utf-8"),
                media_type="application/json" if suffix == "json" else "text/plain",
                name=f"{_safe_name(tool_call_id)}.{suffix}",
                suffix=f".{suffix}",
            )
            cache_path = artifacts.model_path(stored)
            replacement = _format_cached_result(
                content=content,
                cache_path=cache_path,
                max_inline_chars=max_inline_chars,
                preview_chars=preview_chars,
                sha256=stored.sha256,
            )
            message.content = replacement
            message.additional_kwargs[CACHED_CONTENT_KEY] = True
            message.additional_kwargs[DISPLAY_CONTENT_KEY] = (
                f"Tool result cached at {cache_path} ({len(content)} characters)."
            )
            message.artifact = [stored]

        return None

    return cache_large_tool_results


def _format_cached_result(
    *,
    content: str,
    cache_path: str,
    max_inline_chars: int,
    preview_chars: int,
    sha256: str,
) -> str:
    preview_chars = max(0, min(preview_chars, len(content)))
    tail_chars = min(DEFAULT_TAIL_CHARS, preview_chars)
    head_chars = preview_chars - tail_chars
    head = content[:head_chars]
    tail = content[-tail_chars:] if tail_chars else ""
    omitted = len(content) - len(head) - len(tail)
    return cached_content_prompt(
        kind="tool_result",
        cache_path=str(cache_path),
        original_chars=len(content),
        omitted_chars=omitted,
        beginning=head,
        ending=tail,
        sha256=sha256,
        inline_limit_chars=max_inline_chars,
    )


def _cache_candidate(message: Any, limit: int) -> tuple[str, str] | None:
    content = getattr(message, "content", "")
    if not isinstance(content, str):
        content = str(content)
    return (content, "txt") if len(content) > limit else None


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:80] or "tool"
