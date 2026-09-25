"""Public configuration and protocol contracts for conversation compaction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.compact.protocol import CompactionMetrics, CompactionReason
from XBotv2.core.domain import HistoryRevision, MessageId
from XBotv2.core.messages import CompactionSummaryMessage


class CompactConfig(BaseModel):
    """The persisted and resolved configuration for ``compact``."""

    model_config = ConfigDict(extra="forbid")

    automatic: bool = True
    output_reservation: int | None = Field(default=None, ge=0)
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    keep_recent_turns: int = Field(default=4, ge=1)
    summary_max_chars: int = Field(default=8_000, ge=1)
    summary_output_tokens: int = Field(default=2_048, ge=1)


class CompactionSelection(BaseModel):
    expected_revision: HistoryRevision
    source_ids: tuple[MessageId, ...] = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompactionPlan(BaseModel):
    id: str = Field(min_length=1)
    reason: CompactionReason
    selection: CompactionSelection
    summary: CompactionSummaryMessage
    metrics: CompactionMetrics
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = [
    "CompactConfig",
    "CompactionMetrics",
    "CompactionPlan",
    "CompactionSelection",
    "CompactionReason",
]
