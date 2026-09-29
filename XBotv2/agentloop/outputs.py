"""Typed observable output of the Agent loop."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias, TypeGuard

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.core.domain import UsageDelta
from XBotv2.core.messages import AssistantMessage
from XBotv2.core.tools import ToolCall, ToolExecution


class _LoopEventModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LoopTurnStarted(_LoopEventModel):
    kind: Literal["turn_started"] = "turn_started"
    turn: int = Field(ge=1)


class AssistantTextDelta(_LoopEventModel):
    kind: Literal["assistant_text_delta"] = "assistant_text_delta"
    text: str


class AssistantReasoningDelta(_LoopEventModel):
    kind: Literal["assistant_reasoning_delta"] = "assistant_reasoning_delta"
    text: str


class AssistantCompleted(_LoopEventModel):
    kind: Literal["assistant_completed"] = "assistant_completed"
    message: AssistantMessage


class StartedToolCall(BaseModel):
    call: ToolCall
    category: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCallsStarted(_LoopEventModel):
    kind: Literal["tool_calls_started"] = "tool_calls_started"
    calls: tuple[StartedToolCall, ...] = Field(min_length=1)


class ToolCallArgumentsDelta(_LoopEventModel):
    kind: Literal["tool_call_delta"] = "tool_call_delta"
    call_id: str = Field(min_length=1)
    name_delta: str
    arguments_delta: str


class ToolCompleted(_LoopEventModel):
    kind: Literal["tool_completed"] = "tool_completed"
    execution: ToolExecution


class UsageObserved(_LoopEventModel):
    kind: Literal["usage"] = "usage"
    usage: UsageDelta


class TurnFinished(BaseModel):
    kind: Literal["finished"] = "finished"
    stop_reason: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class TurnCancelled(BaseModel):
    kind: Literal["cancelled"] = "cancelled"
    reason: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


TurnOutcome: TypeAlias = Annotated[
    TurnFinished | TurnCancelled,
    Field(discriminator="kind"),
]


class LoopTurnEnded(_LoopEventModel):
    kind: Literal["turn_ended"] = "turn_ended"
    turn: int = Field(ge=1)
    outcome: TurnOutcome


class LoopError(_LoopEventModel):
    kind: Literal["error"] = "error"
    code: str = Field(min_length=1)
    message: str
    exception_type: str = ""


LoopEvent: TypeAlias = Annotated[
    LoopTurnStarted
    | AssistantTextDelta
    | AssistantReasoningDelta
    | AssistantCompleted
    | ToolCallsStarted
    | ToolCallArgumentsDelta
    | ToolCompleted
    | UsageObserved
    | LoopTurnEnded
    | LoopError,
    Field(discriminator="kind"),
]

_LOOP_EVENT_TYPES = (
    LoopTurnStarted,
    AssistantTextDelta,
    AssistantReasoningDelta,
    AssistantCompleted,
    ToolCallsStarted,
    ToolCallArgumentsDelta,
    ToolCompleted,
    UsageObserved,
    LoopTurnEnded,
    LoopError,
)


def is_loop_event(value: object) -> TypeGuard[LoopEvent]:
    return type(value) in _LOOP_EVENT_TYPES


__all__ = [
    "AssistantCompleted",
    "AssistantReasoningDelta",
    "AssistantTextDelta",
    "LoopError",
    "LoopEvent",
    "LoopTurnEnded",
    "LoopTurnStarted",
    "StartedToolCall",
    "ToolCallArgumentsDelta",
    "ToolCallsStarted",
    "ToolCompleted",
    "TurnCancelled",
    "TurnFinished",
    "TurnOutcome",
    "UsageObserved",
    "is_loop_event",
]
