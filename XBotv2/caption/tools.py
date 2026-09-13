"""Agent-facing caption tool."""

from __future__ import annotations

from typing import Protocol

from XBotv2.core import Tool, ToolResult


class _CaptionToolOwner(Protocol):
    async def caption_get(self) -> dict[str, str]: ...
    async def caption_set(self, title: str) -> str: ...
    def caption_available(self) -> bool: ...


def build_caption_tool(owner: _CaptionToolOwner) -> Tool:
    async def caption(
        action: str,
        title: str = "",
    ) -> ToolResult:
        """Get or set the session's human-readable title.

        Sessions are labelled after the first message by default; use this to
        give the session a clear, meaningful name once the user states what
        the conversation is about, or to read the current name back.
        """
        if not owner.caption_available():
            return ToolResult.failure(
                "caption_unavailable",
                "The caption tool is only available on the main thread.",
            )
        if action == "get":
            current = await owner.caption_get()
            return ToolResult.success(
                f"Session title: {current['title']!r} "
                f"(session {current['session_id']}, thread {current['thread_id']})"
            )
        if action == "set":
            if not title.strip():
                return ToolResult.failure(
                    "caption_empty_title", "caption requires a non-empty title."
                )
            applied = await owner.caption_set(title)
            return ToolResult.success(f"Session title set to {applied!r}.")
        return ToolResult.failure(
            "caption_bad_action", "caption action must be 'get' or 'set'."
        )

    return Tool.from_function(caption, name="caption")


__all__ = ["build_caption_tool"]
