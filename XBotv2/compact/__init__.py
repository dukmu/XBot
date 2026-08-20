"""Public declarations for conversation history compaction."""

from XBotv2.compact.protocol import (
    CompactEventType,
    CompactionCompletedData,
    CompactionFailedData,
    CompactionMetrics,
    CompactionStartedData,
    compact_event,
)

__all__ = [
    "CompactEventType",
    "CompactionCompletedData",
    "CompactionFailedData",
    "CompactionMetrics",
    "CompactionStartedData",
    "compact_event",
]
