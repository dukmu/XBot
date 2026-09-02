"""Tool catalog routes: the enabled tool registry for one thread."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import Field, model_validator
from XBotv2.protocol import ErrorEventData, WireModel
from XBotv2.usage import UsageData
from XBotv2.agentloop.contracts import LIST_TOOLS
from XBotv2.core.operations import EmptyRequest
from XBotv2.core.tools import ToolCall


class ToolInfo(WireModel):
    name: str = Field(min_length=1)
    registered_name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    description: str
    parameters: dict[str, Any]
    timeout_seconds: float | None = Field(default=None, gt=0)


class ToolListResponse(WireModel):
    tools: list[ToolInfo] = Field(default_factory=list)


class AssistantMessageData(WireModel):
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    timing: "ModelTimingData | None" = None


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


class InputRejectedData(WireModel):
    reason: str
    request_id: str = ""


class ToolCallsStartedData(WireModel):
    tool_calls: list[ToolCall] = Field(min_length=1)


class ToolCallDeltaItemData(WireModel):
    tool_call_id: str = Field(min_length=1)
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    args_delta: str | dict[str, Any]
    args: str | dict[str, Any]
    index: int = Field(ge=0)
    replaces_tool_call_id: str | None = None


class ToolCallDeltaData(WireModel):
    tool_calls: list[ToolCallDeltaItemData] = Field(min_length=1)


class ToolResultData(WireModel):
    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content: Any = ""
    status: Literal["success", "error", "denied", "cancelled"]
    data: Any = None
    error: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    timing: ToolTimingData | None = None


class TurnData(WireModel):
    turn: int = Field(ge=1)
    status_slots: dict[str, str] = Field(default_factory=dict)


class TurnCancelledData(TurnData):
    reason: str = Field(min_length=1)


AgentLoopEventType = Literal[
    "assistant_message",
    "assistant_message_delta",
    "error",
    "input_rejected",
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
    "input_rejected": InputRejectedData,
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
    data: dict[str, Any],
) -> dict[str, Any]:
    """Validate one Agent-loop-owned event at its producer boundary."""
    payload = _EVENT_MODELS[type].model_validate(data)
    return {"type": type, "data": payload.model_dump(exclude_unset=True)}


def build_tools_router(*, sessions: Any) -> APIRouter:
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
    "InputRejectedData",
    "ToolCallDeltaData",
    "ToolCallDeltaItemData",
    "ToolCallsStartedData",
    "ToolInfo",
    "ToolListResponse",
    "ToolResultData",
    "TurnCancelledData",
    "TurnData",
    "agentloop_event",
    "build_tools_router",
]
