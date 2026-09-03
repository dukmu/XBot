"""Tool-result externalization hook."""

from __future__ import annotations

from XBotv2.agentloop import EventContext
from XBotv2.core.artifacts import ArtifactKind, ArtifactStorePort
from XBotv2.core.messages import Message
from XBotv2.core.prompts import (
    CACHED_CONTENT_KEY,
    DISPLAY_CONTENT_KEY,
    cached_content_prompt,
    content_preview,
)


DEFAULT_CACHE_THRESHOLD_CHARS = 12_000
DEFAULT_PREVIEW_CHARS = 8_000
DEFAULT_TAIL_CHARS = 2_000


def make_tool_result_cache_hook(
    artifacts: ArtifactStorePort,
    *,
    cache_threshold_chars: int = DEFAULT_CACHE_THRESHOLD_CHARS,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
):
    """Create an AFTER_TOOLS hook that caches large tool message contents.

    The hook mutates ``ctx.tool_results`` in place so the engine persists and
    emits the bounded message instead of the full output.
    """

    if cache_threshold_chars < 1:
        raise ValueError("cache_threshold_chars must be positive")
    if preview_chars < 0 or preview_chars > cache_threshold_chars:
        raise ValueError(
            "preview_chars must be between zero and cache_threshold_chars"
        )
    if tail_chars < 0 or tail_chars > preview_chars:
        raise ValueError("tail_chars must be between zero and preview_chars")

    async def cache_large_tool_results(ctx: EventContext) -> None:
        if not ctx.tool_results:
            return None

        for message in ctx.tool_results:
            candidate = _cache_candidate(message, cache_threshold_chars)
            if candidate is None:
                continue
            content, suffix = candidate

            tool_call_id = message.tool_call_id or "tool"
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
                cache_threshold_chars=cache_threshold_chars,
                preview_chars=preview_chars,
                tail_chars=tail_chars,
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
    cache_threshold_chars: int,
    preview_chars: int,
    tail_chars: int,
    sha256: str,
) -> str:
    head, tail = content_preview(
        content,
        preview_chars=preview_chars,
        tail_chars=tail_chars,
    )
    omitted = len(content) - len(head) - len(tail)
    return cached_content_prompt(
        kind="tool_result",
        cache_path=str(cache_path),
        original_chars=len(content),
        omitted_chars=omitted,
        beginning=head,
        ending=tail,
        sha256=sha256,
        inline_limit_chars=len(head) + len(tail),
        cache_threshold_chars=cache_threshold_chars,
    )


def _cache_candidate(message: Message, limit: int) -> tuple[str, str] | None:
    content = message.content
    return (content, "txt") if len(content) > limit else None


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:80] or "tool"
