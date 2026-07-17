"""Tool-result externalization hook."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from xbotv2.api.prompts import (
    CACHED_CONTENT_KEY,
    DISPLAY_CONTENT_KEY,
    cached_content_prompt,
)


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
            data_cache = _large_data_content(message, max_inline_chars)
            data_text = data_cache[0] if data_cache is not None else None
            data_suffix = data_cache[1] if data_cache is not None else ""
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
                cached_content = data_text if data_suffix == "txt" else content
                digest = hashlib.sha256(cached_content.encode("utf-8")).hexdigest()
                suffix = "json" if data_text == content and data_suffix == "json" else "txt"
                content_path = cache_dir / f"{name}-{digest[:16]}.{suffix}"
                content_path.write_text(cached_content, encoding="utf-8")
                content_cache_path = (
                    Path("session")
                    / content_path.relative_to(Path(state_store.root))
                )
                replacement = _format_cached_result(
                    content=cached_content,
                    cache_path=content_cache_path,
                    max_inline_chars=max_inline_chars,
                    preview_chars=preview_chars,
                )
                message.content = replacement
                message.additional_kwargs[CACHED_CONTENT_KEY] = True
                message.additional_kwargs[DISPLAY_CONTENT_KEY] = (
                    f"Tool result cached at {content_cache_path} "
                    f"({len(cached_content)} characters)."
                )
                artifact.update({
                    "kind": "cached_tool_result",
                    "cache_path": str(content_cache_path),
                    "original_chars": len(cached_content),
                    "inline_chars": len(replacement),
                    "sha256": digest,
                })
            if data_text is not None:
                _cache_large_data(
                    message,
                    data_text,
                    data_suffix,
                    cache_dir,
                    name,
                    data_text if data_suffix == "txt" else content,
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
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        inline_limit_chars=max_inline_chars,
    )


def _cache_large_data(
    message: Any,
    serialized: str,
    suffix: str,
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
        data_path = cache_dir / f"{name}-{digest}-data.{suffix}"
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


def _large_data_content(message: Any, limit: int) -> tuple[str, str] | None:
    metadata = getattr(message, "additional_kwargs", None)
    if not isinstance(metadata, dict) or "xbotv2_data" not in metadata:
        return None
    value = metadata["xbotv2_data"]
    if isinstance(value, str):
        return (value, "txt") if len(value) > limit else None
    if isinstance(value, dict):
        original_text = value.get("content")
        if isinstance(original_text, str) and len(original_text) > limit:
            return original_text, "txt"
    if isinstance(value, (dict, list)):
        serialized = json.dumps(value, ensure_ascii=False)
        return (serialized, "json") if len(serialized) > limit else None
    return None


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:80] or "tool"
