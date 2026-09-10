"""Public configuration and protocol contracts for conversation compaction."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field
from pydantic import JsonValue

from XBotv2.core.messages import Message


class CompactConfig(BaseModel):
    """The persisted and resolved configuration for ``compact``."""

    model_config = ConfigDict(extra="forbid")

    automatic: bool = True
    output_reservation: int | None = Field(default=None, ge=0)
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    keep_recent_turns: int = Field(default=4, ge=1)
    summary_max_chars: int = Field(default=8_000, ge=1)


class CompactionMetrics(TypedDict, total=False):
    context_tokens_before: int
    context_tokens_after_estimate: int
    context_tokens_released_estimate: int
    context_limit: int | None
    max_context_tokens: int | None
    output_reservation: int | None
    request_estimate: int | None
    estimate_source: str
    history_chars_before: int
    history_chars_after: int
    summary_chars: int
    summary_truncated: bool
    messages_before: int
    messages_after: int
    messages_removed: int
    model_usage: dict[str, int]


class CompactionProposal(TypedDict):
    messages: list[Message]
    prefix_end: int
    compaction_id: str
    summary: str
    raw_output: dict[str, JsonValue]
    compact_reason: str
    compact_metrics: CompactionMetrics
    source_node_ids: NotRequired[list[str]]


__all__ = ["CompactConfig", "CompactionMetrics", "CompactionProposal"]
