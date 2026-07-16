"""Tool-result externalization hook."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_MAX_INLINE_CHARS = 12000
DEFAULT_PREVIEW_CHARS = 4000
DEFAULT_TAIL_CHARS = 1000


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
            data_text = _large_data_json(message, max_inline_chars)
            if len(content) <= max_inline_chars and data_text is None:
                continue

            cache_dir.mkdir(parents=True, exist_ok=True)
            tool_call_id = getattr(message, "tool_call_id", "tool")
            name = _safe_name(tool_call_id)
            artifact = {
                "tool_call_id": tool_call_id,
            }
            content_path = None
            content_cache_path = None
            if len(content) > max_inline_chars:
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
                content_path = cache_dir / f"{name}-{digest}.txt"
                content_path.write_text(content, encoding="utf-8")
                content_cache_path = (
                    Path("session")
                    / content_path.relative_to(Path(state_store.root))
                )
                replacement = _format_cached_result(
                    content=content,
                    cache_path=content_cache_path,
                    max_inline_chars=max_inline_chars,
                    preview_chars=preview_chars,
                )
                message.content = replacement
                artifact.update({
                    "kind": "cached_tool_result",
                    "cache_path": str(content_cache_path),
                    "original_chars": len(content),
                    "inline_chars": len(replacement),
                    "sha256": digest,
                })
            if data_text is not None:
                _cache_large_data(
                    message,
                    data_text,
                    cache_dir,
                    name,
                    content,
                    content_path,
                    content_cache_path,
                    artifact,
                    state_store,
                )
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
    tail_chars = min(DEFAULT_TAIL_CHARS, preview_chars)
    head_chars = max(0, preview_chars - tail_chars)
    head = content[:head_chars]
    tail = content[-tail_chars:]
    omitted = len(content) - len(head) - len(tail)
    return (
        "[Tool result cached]\n"
        f"cache_path: {cache_path}\n"
        f"original_chars: {len(content)}\n"
        f"inline_limit_chars: {max_inline_chars}\n"
        f"preview_chars: {len(head) + len(tail)}\n"
        f"omitted_chars: {omitted}\n"
        "\n"
        "Beginning excerpt:\n"
        f"{head}\n\n"
        "Ending excerpt:\n"
        f"{tail}\n\n"
        "Read the cached file with filesystem_read using offset and limit "
        "before acting when omitted content may matter."
    )


def _cache_large_data(
    message: Any,
    serialized: str,
    cache_dir: Path,
    name: str,
    content: str,
    content_path: Path | None,
    content_cache_path: Path | None,
    artifact: dict[str, Any],
    state_store: Any,
) -> None:
    if serialized == content and content_path is not None:
        data_path = content_path
        data_cache_path = content_cache_path
    else:
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        data_path = cache_dir / f"{name}-{digest}-data.json"
        data_path.write_text(serialized, encoding="utf-8")
        data_cache_path = (
            Path("session") / data_path.relative_to(Path(state_store.root))
        )
    message.additional_kwargs["xbotv2_data"] = {
        "cached": True,
        "cache_path": str(data_cache_path),
        "original_chars": len(serialized),
    }
    artifact["data_cache_path"] = str(data_cache_path)
    if "kind" not in artifact:
        artifact.update({
            "kind": "cached_tool_data",
            "cache_path": str(data_cache_path),
            "original_chars": len(serialized),
        })


def _large_data_json(message: Any, limit: int) -> str | None:
    metadata = getattr(message, "additional_kwargs", None)
    if not isinstance(metadata, dict) or "xbotv2_data" not in metadata:
        return None
    serialized = json.dumps(metadata["xbotv2_data"], ensure_ascii=False)
    return serialized if len(serialized) > limit else None


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:80] or "tool"
