"""Tool catalog routes: the enabled tool registry for one thread."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter
from pydantic import Field, JsonValue, model_validator
from XBotv2.protocol import ErrorEventData, WireModel
from XBotv2.core.usage import UsageData
from XBotv2.agentloop.contracts import LIST_TOOLS
from XBotv2.core.operations import EmptyRequest
from XBotv2.core.tools import ToolCall

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


class AssistantMessageData(WireModel):
    id: str = ""
    content: str
    tool_calls: list[dict[str, JsonValue]] = Field(default_factory=list)
    timing: "ModelTimingData | None" = None
    # Why the provider stopped generating ("length", "max_tokens", "end_turn",
    # ...).  Clients use it to flag a reply that was cut off mid-answer.
    stop_reason: str = ""


class ModelTimingData(WireModel):
    llm_ms: float = Field(ge=0)
    ttft_ms: float | None = Field(default=None, ge=0)
    decode_ms: float | None = Field(default=None, ge=0)


class ToolTimingData(WireModel):
    duration_ms: float = Field(ge=0)


class AssistantMessageDeltaData(WireModel):
    content: str | None = None
    reasoning: str | None = None

    @model_validator(mode="after")
    def _require_delta_field(self) -> "AssistantMessageDeltaData":
        if not self.model_fields_set.intersection({"content", "reasoning"}):
            raise ValueError("assistant message delta requires content or reasoning")
        return self


class ToolCallStartedItem(WireModel):
    """One started tool call with its owner-declared category.

    ``kind`` is declared by the tool's owning package and rendered by
    clients (ACP); it is never re-derived from the tool name.
    """

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    args: dict[str, JsonValue] = Field(default_factory=dict)
    type: Literal["tool_call"] = "tool_call"
    kind: str = "other"


class ToolCallsStartedData(WireModel):
    tool_calls: list[ToolCallStartedItem] = Field(min_length=1)


class ToolCallDeltaItemData(WireModel):
    tool_call_id: str = Field(min_length=1)
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    args_delta: str | dict[str, JsonValue]
    args: str | dict[str, JsonValue]
    index: int = Field(ge=0)
    replaces_tool_call_id: str | None = None


class ToolCallDeltaData(WireModel):
    tool_calls: list[ToolCallDeltaItemData] = Field(min_length=1)


class ToolResultData(WireModel):
    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content: JsonValue = ""
    status: Literal["success", "error", "denied", "cancelled"]
    data: JsonValue = None
    error: dict[str, JsonValue] | None = None
    artifacts: list[dict[str, JsonValue]] = Field(default_factory=list)
    images: list[dict[str, JsonValue]] = Field(default_factory=list)
    timing: ToolTimingData | None = None


class TurnData(WireModel):
    turn: int = Field(ge=1)
    status_slots: dict[str, str] = Field(default_factory=dict)
    # The session runtime enriches a terminal turn frame after the engine
    # validated it: the status slots above, and the conversation's running
    # statistics. The transport model must declare everything that travels, or a
    # client that validates what it receives rejects a legitimate frame.
    session_stats: dict[str, JsonValue] = Field(default_factory=dict)


class TurnCancelledData(TurnData):
    reason: str = Field(min_length=1)


AgentLoopEventType = Literal[
    "assistant_message",
    "assistant_message_delta",
    "error",
    "tool_call_delta",
    "tool_calls_started",
    "tool_result",
    "turn_cancelled",
    "turn_finished",
    "turn_started",
    "usage",
]

_EVENT_MODELS: dict[str, type[WireModel]] = {
    "assistant_message": AssistantMessageData,
    "assistant_message_delta": AssistantMessageDeltaData,
    "error": ErrorEventData,
    "tool_call_delta": ToolCallDeltaData,
    "tool_calls_started": ToolCallsStartedData,
    "tool_result": ToolResultData,
    "turn_cancelled": TurnCancelledData,
    "turn_finished": TurnData,
    "turn_started": TurnData,
    "usage": UsageData,
}


def agentloop_event(
    type: AgentLoopEventType,
    data: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Validate one Agent-loop-owned event at its producer boundary."""
    payload = _EVENT_MODELS[type].model_validate(data)
    return {"type": type, "data": payload.model_dump(exclude_unset=True)}


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
    "AgentLoopEventType",
    "AssistantMessageData",
    "AssistantMessageDeltaData",
    "ToolCallDeltaData",
    "ToolCallDeltaItemData",
    "ToolCallStartedItem",
    "ToolCallsStartedData",
    "ToolInfo",
    "ToolListResponse",
    "ToolResultData",
    "TurnCancelledData",
    "TurnData",
    "agentloop_event",
    "build_tools_router",
]
