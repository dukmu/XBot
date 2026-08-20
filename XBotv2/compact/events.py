"""Compaction stream event DTOs owned by the compact capability."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from XBotv2.protocol.models import UsageData, WireModel, _empty_usage


class CompactionStartedData(WireModel):
    reason: Literal["manual", "automatic"]
    messages_before: int = Field(ge=0)
    history_chars_before: int = Field(ge=0)
    context_tokens_before: int = Field(default=0, ge=0)
    context_limit: int | None = Field(default=None, ge=1)


class CompactionCompletedData(WireModel):
    reason: str = Field(min_length=1)
    metrics: dict[str, Any] = Field(default_factory=dict)
    usage: UsageData = Field(default_factory=_empty_usage)


class CompactionFailedData(WireModel):
    reason: Literal["manual", "automatic"]
    message: str = Field(min_length=1)