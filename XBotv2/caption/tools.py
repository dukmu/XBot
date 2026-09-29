"""Agent-facing caption tool."""

from __future__ import annotations

from typing import Literal, Protocol

from XBotv2.caption.contracts import CaptionResult, CaptionTitleError
from XBotv2.core import Tool, failed_text, succeeded_text, ToolOutcome


class _CaptionToolOwner(Protocol):
    async def caption_get(self) -> CaptionResult: ...
    async def caption_set(self, title: str) -> CaptionResult: ...


def build_caption_tool(owner: _CaptionToolOwner) -> Tool:
    async def caption(
        action: Literal["get", "set"],
        title: str = "",
    ) -> ToolOutcome:
        """Get or set the session's human-readable title.

        Sessions are labelled after the first message by default; use this to
        give the session a clear, meaningful name once the user states what the
        conversation is about, or to read the current name back.

        Args:
            action: ``"get"`` to read the current title, ``"set"`` to replace it.
            title: New title. Required when ``action="set"``; ignored otherwise.
        """
        if action == "get":
            current = await owner.caption_get()
            return succeeded_text(f"Session title: {current.title!r}")
        # action == "set": the Literal type narrows this branch at runtime
        # because any other value was rejected by the JSON Schema enum.
        try:
            applied = await owner.caption_set(title)
        except CaptionTitleError as exc:
            return failed_text(
                exc.code,
                str(exc),
            )
        return succeeded_text(f"Session title set to {applied.title!r}.")

    return Tool.from_function(caption, name="caption")


__all__ = ["build_caption_tool"]
