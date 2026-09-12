"""Typed lifecycle events owned by conversation compaction."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.compact.protocol import CompactionMetrics, CompactionReason
from XBotv2.core import Message
from XBotv2.session.contracts import SessionInfo


PRE_COMPACT = "before/compact"
POST_COMPACT = "after/compact"


@dataclass(slots=True)
class BeforeCompact:
    """Mutable proposal exposed before compacted history is committed."""

    messages: list[Message]
    reason: CompactionReason
    session: SessionInfo | None = None


@dataclass(frozen=True, slots=True)
class AfterCompact:
    """Notification emitted after compacted history has been committed."""

    messages: tuple[Message, ...]
    reason: CompactionReason
    metrics: CompactionMetrics
    previous_message_count: int
    current_message_count: int
    session: SessionInfo | None = None


__all__ = [
    "AfterCompact",
    "BeforeCompact",
    "POST_COMPACT",
    "PRE_COMPACT",
]
