"""Tool catalog routes: the enabled tool registry for one thread."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, TypeAlias, TypeGuard

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from XBotv2.protocol import WireModel
from XBotv2.agentloop.contracts import LIST_TOOLS
from XBotv2.core.domain import UsageDelta
from XBotv2.core.messages import AssistantMessage
from XBotv2.core.operations import EmptyRequest
from XBotv2.core.tools import ToolCall, ToolExecution

if TYPE_CHECKING:
    from XBotv2.session.contracts import SessionsPort


class ToolInfo(WireModel):
    name: str = Field(min_length=1)
    registered_name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    description: str
    parameters: dict[str, JsonValue]
    timeout_seconds: float | None = Field(default=None, gt=0)


class ToolListResponse(WireModel):
    tools: list[ToolInfo] = Field(default_factory=list)


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


def build_tools_router(*, sessions: "SessionsPort") -> APIRouter:
    """Read-only tool catalog for the active thread."""

    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/tools",
        operation_id="list_tools",
    )
    async def list_tools_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ToolListResponse:
        catalog = await sessions.dispatch(
            session_id, thread_id, LIST_TOOLS, EmptyRequest()
        )
        return ToolListResponse(tools=[
            ToolInfo(
                name=tool.name,
                registered_name=tool.registered_name,
                namespace=tool.namespace,
                description=tool.description,
                parameters=tool.parameters,
                timeout_seconds=tool.timeout_seconds,
            )
            for tool in catalog.tools
        ])

    return router


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
    "ToolInfo",
    "ToolListResponse",
    "TurnCancelled",
    "TurnFinished",
    "TurnOutcome",
    "UsageObserved",
    "build_tools_router",
    "is_loop_event",
]
