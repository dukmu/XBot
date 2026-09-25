"""Typed lifecycle events owned by conversation compaction."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.compact.contracts import CompactionPlan
from XBotv2.core.messages import ConversationMessage
from XBotv2.session.contracts import SessionRuntimeState


PRE_COMPACT = "before/compact"
POST_COMPACT = "after/compact"


@dataclass(frozen=True, slots=True)
class BeforeCompact:
    plan: CompactionPlan
    session: SessionRuntimeState | None = None


@dataclass(frozen=True, slots=True)
class AfterCompact:
    plan: CompactionPlan
    """Notification emitted after compacted history has been committed."""

    messages: tuple[ConversationMessage, ...]
    previous_message_count: int
    current_message_count: int
    session: SessionRuntimeState | None = None


__all__ = [
    "AfterCompact",
    "BeforeCompact",
    "POST_COMPACT",
    "PRE_COMPACT",
]
