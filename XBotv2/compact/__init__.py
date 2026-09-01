"""Public declarations for conversation history compaction."""

from XBotv2.compact.events import (
    POST_COMPACT,
    PRE_COMPACT,
    AfterCompact,
    BeforeCompact,
)
from XBotv2.compact.protocol import (
    CompactEventType,
    CompactionCompletedData,
    CompactionFailedData,
    CompactionMetrics,
    CompactionRestoredData,
    CompactionStartedData,
    compact_event,
)

__all__ = [
    "AfterCompact",
    "BeforeCompact",
    "CompactEventType",
    "CompactionCompletedData",
    "CompactionFailedData",
    "CompactionMetrics",
    "CompactionRestoredData",
    "CompactionStartedData",
    "POST_COMPACT",
    "PRE_COMPACT",
    "compact_event",
]
