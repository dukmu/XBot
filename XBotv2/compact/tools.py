"""Agent-facing compact tool."""

from __future__ import annotations

from typing import Protocol

from XBotv2.core import Tool, ToolResult


class _CompactToolOwner(Protocol):
    def request_manual_compaction(self) -> None: ...


def build_compact_tool(owner: _CompactToolOwner) -> Tool:
    async def request_compaction() -> ToolResult:
        """Request one semantic compaction before the next model call.

        Use this when older conversation detail is consuming context but the
        task must continue. It summarizes an old completed prefix, preserves
        recent turns, and does not complete the current task. Do not call it
        repeatedly when automatic compaction is already active.
        """
        owner.request_manual_compaction()
        return ToolResult.success(
            "Conversation compaction requested."
        )

    return Tool.from_function(request_compaction, name="compact")


__all__ = ["build_compact_tool"]
