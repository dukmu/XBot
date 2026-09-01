"""Outbound event contracts owned by conversation compaction."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from XBotv2.core import ClientEvent
from XBotv2.protocol import WireModel


class CompactionStartedData(WireModel):
    reason: str = Field(min_length=1)
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


class CompactionCompletedData(WireModel):
    reason: str = Field(min_length=1)
    metrics: CompactionMetrics
    checkpoint_id: str | None = None


class CompactionRestoredData(WireModel):
    checkpoint_id: str = Field(min_length=1)
    messages: int = Field(ge=0)


class CompactionFailedData(WireModel):
    reason: str = Field(min_length=1)
    message: str = Field(min_length=1)


CompactEventType = Literal[
    "compaction_started",
    "compaction_completed",
    "compaction_failed",
    "compaction_restored",
]

_EVENT_MODELS: dict[str, type[WireModel]] = {
    "compaction_started": CompactionStartedData,
    "compaction_completed": CompactionCompletedData,
    "compaction_failed": CompactionFailedData,
    "compaction_restored": CompactionRestoredData,
}


def compact_event(type: CompactEventType, data: dict[str, Any]) -> ClientEvent:
    """Validate a Compact-owned event before publishing it through XCore."""
    payload = _EVENT_MODELS[type].model_validate(data)
    return ClientEvent(type, payload.model_dump(exclude_unset=True))


__all__ = [
    "CompactEventType",
    "CompactionCompletedData",
    "CompactionFailedData",
    "CompactionMetrics",
    "CompactionRestoredData",
    "CompactionStartedData",
    "compact_event",
]
