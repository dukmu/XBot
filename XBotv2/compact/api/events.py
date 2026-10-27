"""Typed lifecycle events owned by conversation compaction."""

from __future__ import annotations

from dataclasses import dataclass
from XBotv2.compact.api.contracts import CompactionPlan
from XBotv2.core.api.messages import ConversationMessage
from XBotv2.agentloop.api.state import ThreadExecution


PRE_COMPACT = "before/compact"
POST_COMPACT = "after/compact"


@dataclass(frozen=True, slots=True)
class BeforeCompact:
    plan: CompactionPlan
    session: ThreadExecution | None = None


@dataclass(frozen=True, slots=True)
class AfterCompact:
    plan: CompactionPlan
    """Notification emitted after compacted history has been committed."""

    messages: tuple[ConversationMessage, ...]
    previous_message_count: int
    current_message_count: int
    session: ThreadExecution | None = None


__all__ = [
    "AfterCompact",
    "BeforeCompact",
    "POST_COMPACT",
    "PRE_COMPACT",
]
