"""Agent-facing caption tool."""

from __future__ import annotations

from typing import Protocol

from XBotv2.caption.contracts import CaptionResult, CaptionTitleError
from XBotv2.core import Tool, failed_text, succeeded_text, ToolOutcome


class _CaptionToolOwner(Protocol):
    async def caption_get(self) -> CaptionResult: ...
    async def caption_set(self, title: str) -> CaptionResult: ...


def build_caption_tool(owner: _CaptionToolOwner) -> Tool:
    async def caption(
        action: str,
        title: str = "",
    ) -> ToolOutcome:
        """Get or set the session's human-readable title.

        Sessions are labelled after the first message by default; use this to
        give the session a clear, meaningful name once the user states what
        the conversation is about, or to read the current name back.
        """
        if action == "get":
            current = await owner.caption_get()
            return succeeded_text(f"Session title: {current.title!r}")
        if action == "set":
            try:
                applied = await owner.caption_set(title)
            except CaptionTitleError as exc:
                return failed_text(
                    exc.code,
                    str(exc),
                )
            return succeeded_text(f"Session title set to {applied.title!r}.")
        return failed_text(
            "caption_bad_action", "caption action must be 'get' or 'set'."
        )

    return Tool.from_function(caption, name="caption")


__all__ = ["build_caption_tool"]
