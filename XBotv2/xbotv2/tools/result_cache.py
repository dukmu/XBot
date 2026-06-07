"""Tool-result caching and truncation hooks."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MAX_INLINE_CHARS = 12000
DEFAULT_PREVIEW_CHARS = 4000


def make_tool_result_cache_hook(
    state_store: Any,
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

        cache_dir = Path(state_store.artifacts_dir) / "tool_results"
        for message in ctx.tool_results:
            content = getattr(message, "content", "")
            if not isinstance(content, str):
                content = str(content)
            if len(content) <= max_inline_chars:
                continue

            cache_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            tool_call_id = getattr(message, "tool_call_id", "tool")
            path = cache_dir / f"{_safe_name(tool_call_id)}-{digest}.txt"
            path.write_text(content, encoding="utf-8")

            replacement = _format_cached_result(
                content=content,
                cache_path=path,
                max_inline_chars=max_inline_chars,
                preview_chars=preview_chars,
            )
            message.content = replacement
            artifact = {
                "kind": "cached_tool_result",
                "tool_call_id": tool_call_id,
                "cache_path": str(path),
                "original_chars": len(content),
                "inline_chars": len(replacement),
                "sha256": digest,
            }
            message.artifact = artifact

            if hasattr(state_store, "append_event"):
                state_store.append_event(
                    "tool_result_cached",
                    artifact,
                )

        return None

    return cache_large_tool_results


def _format_cached_result(
    *,
    content: str,
    cache_path: Path,
    max_inline_chars: int,
    preview_chars: int,
) -> str:
    preview = content[:preview_chars]
    omitted = len(content) - len(preview)
    return (
        "[Tool result cached]\n"
        f"cache_path: {cache_path}\n"
        f"original_chars: {len(content)}\n"
        f"inline_limit_chars: {max_inline_chars}\n"
        f"cached_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"preview_chars: {len(preview)}\n"
        f"omitted_chars: {omitted}\n"
        "\n"
        "Preview:\n"
        f"{preview}"
    )


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:80] or "tool"
