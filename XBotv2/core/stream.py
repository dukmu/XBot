"""Canonical model response and streaming events."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.core.domain import (
    MeasurementUnavailable,
    ModelStop,
    ObservedContext,
    ProviderExtensions,
    ProviderError,
    UsageDelta,
)
from XBotv2.core.parts import ReasoningPart, TextPart
from XBotv2.core.tools import ToolCall


class ModelResponse(BaseModel):
    parts: tuple[TextPart | ReasoningPart | ToolCall, ...] = ()
    usage: UsageDelta
    observed_context: ObservedContext
    stop: ModelStop
    provider_extensions: ProviderExtensions
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextDelta(BaseModel):
    kind: Literal["text_delta"] = "text_delta"
    text: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReasoningDelta(BaseModel):
    kind: Literal["reasoning_delta"] = "reasoning_delta"
    text: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCallDelta(BaseModel):
    kind: Literal["tool_call_delta"] = "tool_call_delta"
    call_id: str = Field(min_length=1)
    name_delta: str = ""
    arguments_delta: str = ""
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelCompleted(BaseModel):
    kind: Literal["completed"] = "completed"
    response: ModelResponse
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelFailed(BaseModel):
    kind: Literal["failed"] = "failed"
    error: ProviderError
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelCancelled(BaseModel):
    kind: Literal["cancelled"] = "cancelled"
    reason: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


ModelStreamEvent: TypeAlias = (
    TextDelta
    | ReasoningDelta
    | ToolCallDelta
    | ModelCompleted
    | ModelFailed
    | ModelCancelled
)


__all__ = [
    "ModelCancelled",
    "ModelCompleted",
    "ModelFailed",
    "ModelResponse",
    "ModelStreamEvent",
    "ToolCallDelta",
    "ReasoningDelta",
    "TextDelta",
]
