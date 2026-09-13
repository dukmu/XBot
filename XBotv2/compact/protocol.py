"""Outbound event contracts owned by conversation compaction."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from XBotv2.core import ClientEvent
from XBotv2.core.history import TrajectoryTransaction
from XBotv2.core.tools import _validated_client_event
from XBotv2.protocol import WireModel

# Why a compaction ran; the only reason vocabulary shared with clients.
CompactionReason = Literal["automatic", "manual", "context-overflow"]

# The durable bracket one compaction commit must complete exactly once.
COMPACTION_TRANSACTION = TrajectoryTransaction(
    start_event="compaction/start",
    end_event="compaction/end",
    id_field="compaction_id",
)

# Reasons a user did not request explicitly.
AUTOMATIC_COMPACTION_REASONS: frozenset[str] = frozenset(
    {"automatic", "context-overflow"}
)


def is_automatic_compaction(reason: str) -> bool:
    return reason in AUTOMATIC_COMPACTION_REASONS


class CompactionStartedData(WireModel):
    reason: CompactionReason
    messages_before: int = Field(ge=0)
    history_chars_before: int = Field(ge=0)
    context_tokens_before: int = Field(ge=0)
    context_limit: int | None = Field(default=None, gt=0)


class CompactionMetrics(WireModel):
    context_tokens_before: int = Field(ge=0)
    context_tokens_after_estimate: int = Field(ge=0)
    context_tokens_released_estimate: int = Field(ge=0)
    context_limit: int | None = Field(default=None, gt=0)
    max_context_tokens: int | None = Field(default=None, gt=0)
    output_reservation: int | None = Field(default=None, ge=0)
    request_estimate: int | None = Field(default=None, ge=0)
    estimate_source: str = Field(min_length=1)
    history_chars_before: int = Field(ge=0)
    history_chars_after: int = Field(ge=0)
    summary_chars: int = Field(ge=0)
    summary_truncated: bool
    messages_before: int = Field(ge=0)
    messages_after: int = Field(ge=0)
    messages_removed: int
    model_usage: dict[str, int] = Field(default_factory=dict)
    summary_output_tokens: int = Field(default=1, ge=1)


class CompactionCompletedData(WireModel):
    reason: CompactionReason
    metrics: CompactionMetrics
    # Clients render a notice for unrequested compaction without re-deriving
    # the reason vocabulary themselves.
    automatic: bool = False
    # The live summary travels on the event so a client can show what was kept
    # without reading the trajectory back; the durable copy stays in the
    # surface replacement.
    summary: str = ""


class CompactionFailedData(WireModel):
    reason: CompactionReason
    message: str = Field(min_length=1)
    automatic: bool = False


CompactEventType = Literal[
    "compaction_started",
    "compaction_completed",
    "compaction_failed",
]

_EVENT_MODELS: dict[str, type[WireModel]] = {
    "compaction_started": CompactionStartedData,
    "compaction_completed": CompactionCompletedData,
    "compaction_failed": CompactionFailedData,
}


def compact_event(type: CompactEventType, data: dict[str, JsonValue]) -> ClientEvent:
    """Validate a Compact-owned event before publishing it through XCore."""
    return _validated_client_event(type, data, _EVENT_MODELS[type])


__all__ = [
    "AUTOMATIC_COMPACTION_REASONS",
    "COMPACTION_TRANSACTION",
    "CompactEventType",
    "CompactionCompletedData",
    "CompactionFailedData",
    "CompactionMetrics",
    "CompactionReason",
    "CompactionStartedData",
    "compact_event",
    "is_automatic_compaction",
]
