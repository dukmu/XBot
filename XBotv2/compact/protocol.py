"""Outbound event contracts owned by conversation compaction."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from XBotv2.protocol import WireModel
from XBotv2.core.messages import CompactionSummaryMessage
from XBotv2.core.domain import TokenCounters

# Why a compaction ran; the only reason vocabulary shared with clients.
CompactionReason = Literal["automatic", "manual", "context-overflow"]

# Reasons a user did not request explicitly.
AUTOMATIC_COMPACTION_REASONS: frozenset[str] = frozenset(
    {"automatic", "context-overflow"}
)


def is_automatic_compaction(reason: str) -> bool:
    return reason in AUTOMATIC_COMPACTION_REASONS


class CompactionStarted(WireModel):
    kind: Literal["compaction_started"] = "compaction_started"
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
    model_usage: TokenCounters = Field(default_factory=TokenCounters)
    summary_output_tokens: int = Field(default=1, ge=1)


class CompactionCompleted(WireModel):
    kind: Literal["compaction_completed"] = "compaction_completed"
    reason: CompactionReason
    metrics: CompactionMetrics
    # Clients render a notice for unrequested compaction without re-deriving
    # the reason vocabulary themselves.
    automatic: bool = False
    summary: CompactionSummaryMessage


class CompactionFailed(WireModel):
    kind: Literal["compaction_failed"] = "compaction_failed"
    reason: CompactionReason
    message: str = Field(min_length=1)
    automatic: bool = False


__all__ = [
    "AUTOMATIC_COMPACTION_REASONS",
    "CompactionCompleted",
    "CompactionFailed",
    "CompactionMetrics",
    "CompactionReason",
    "CompactionStarted",
    "is_automatic_compaction",
]
