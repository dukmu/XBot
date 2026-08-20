"""Tool catalog routes: the enabled tool registry for one thread."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import Field, model_validator
from XBotv2.protocol import WireModel
from XBotv2.server import contribute_router
from XBotv2.agentloop.contracts import LIST_TOOLS
from XBotv2.core.operations import EmptyRequest
from XBotv2.session import SessionRef, dispatch_session_operation


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


class ToolCallData(WireModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    args: dict[str, Any]
    type: Literal["tool_call"] = "tool_call"


class ToolCallsStartedData(WireModel):
    tool_calls: list[ToolCallData] = Field(min_length=1)


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


class TurnData(WireModel):
    turn: int = Field(ge=1)
    status_slots: dict[str, str] = Field(default_factory=dict)


class TurnCancelledData(TurnData):
    reason: str = Field(min_length=1)


def build_tools_router(*, events: Any) -> APIRouter:
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
        catalog = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            LIST_TOOLS,
            EmptyRequest(),
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


class ToolsProtocolPlugin:
    """Contribute the tool catalog route through the XCore route event."""

    inject = ["server"]
    name = "xbot.protocol.tools"

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_tools_router(events=ctx),
        )


plugin = ToolsProtocolPlugin()


__all__ = [
    "AssistantMessageData",
    "AssistantMessageDeltaData",
    "InputRejectedData",
    "ToolCallData",
    "ToolCallDeltaData",
    "ToolCallDeltaItemData",
    "ToolCallsStartedData",
    "ToolInfo",
    "ToolListResponse",
    "ToolResultData",
    "TurnCancelledData",
    "TurnData",
    "build_tools_router",
]
