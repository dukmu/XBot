"""Inbox component: model-visible runtime notifications as a plugin.

Provides ``ctx.inbox`` — the agent inbox that accumulates internal
completion notices (background jobs, subagents).  The live session owns one
``AgentInbox``; the engine drains it into the next turn's model context.
"""

from __future__ import annotations

from typing import Any

from XBotv2.inbox.inbox import AgentInbox, InboxMessage


class InboxService:
    """Factory for per-session agent inboxes."""

    def new_inbox(self) -> AgentInbox:
        return AgentInbox()

    def message(
        self,
        type: str,
        source: str,
        payload: dict[str, Any] | None = None,
        *,
        trigger_turn: bool = False,
    ) -> InboxMessage:
        return InboxMessage(
            type=type,
            source=source,
            payload=payload or {},
            trigger_turn=trigger_turn,
        )


class InboxComponent:
    """Register the inbox factory as ``ctx.inbox``."""

    name = "xbot.inbox"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("inbox", InboxService())


plugin = InboxComponent()
