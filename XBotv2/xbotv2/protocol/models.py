"""Versioned HTTP request and response models."""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from xbotv2.protocol.version import PROTOCOL_VERSION


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HelloRequest(WireModel):
    protocol_version: str = PROTOCOL_VERSION
    client_name: str = "xbotv2-client"
    session_id: str | None = None
    thread_id: str = "agent"


class HelloResponse(WireModel):
    server_name: str
    protocol_version: str = PROTOCOL_VERSION
    session_id: str = ""
    thread_id: str = "agent"


class OpenSessionRequest(WireModel):
    session_id: str | None = None
    thread_id: str = "agent"
    workspace_root: str | None = None
    mode: Literal["new", "resume"] = "new"


class SessionHistoryItem(WireModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: str = ""
    status: str = ""
    data: Any = None
    error: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class OpenSessionResponse(WireModel):
    session_id: str
    thread_id: str
    status: Literal["ready"] = "ready"
    agent_name: str
    workspace_root: str
    provider: str
    model: str = ""
    context_window: int = Field(default=0, ge=0)
    history: list[SessionHistoryItem] = Field(default_factory=list)


class CommandRequest(WireModel):
    command: str = ""
    args: list[str] | None = None
    raw: str = ""
    kind: Literal["server", "prompt"] = "server"


class CommandInfo(WireModel):
    name: str
    slash: str
    kind: Literal["client", "server", "prompt"]
    description: str
    usage: str = ""
    examples: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class CommandListResponse(WireModel):
    commands: list[CommandInfo]


class CommandResult(WireModel):
    command: str
    status: Literal["ok", "error"]
    message: str
    data: Any = None
    history: list[SessionHistoryItem] | None = None


class CommandResponse(WireModel):
    type: Literal["command_result"] = "command_result"
    data: CommandResult


class MessageRequest(WireModel):
    content: str = Field(min_length=1, pattern=r".*\S.*")
    request_id: str = ""


class PermissionResponseRequest(WireModel):
    request_id: str = Field(min_length=1)
    decision: Literal["allow", "deny"]
    scope: Literal["once", "session"] = "once"


class UserInputResponseRequest(WireModel):
    request_id: str = Field(min_length=1)
    answer: Any = None


class ErrorResponse(WireModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class ErrorEventData(ErrorResponse):
    stage: str | None = None


ServerEventType = Literal[
    "assistant_message",
    "assistant_message_delta",
    "client_message",
    "end",
    "error",
    "message_queued",
    "permission_denied",
    "permission_request",
    "permission_response_recorded",
    "tool_call_delta",
    "tool_calls_started",
    "tool_result",
    "task_updated",
    "turn_cancelled",
    "turn_finished",
    "turn_started",
    "usage",
    "user_input_recorded",
    "user_input_required",
]

KNOWN_SERVER_EVENT_TYPES: tuple[str, ...] = get_args(ServerEventType)


class PermissionRequestData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call: dict[str, Any]
    decision: Literal["ask"] = "ask"
    reason: str
    resume_supported: bool = False


class PermissionDeniedData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call: dict[str, Any]
    decision: Literal["deny"] = "deny"
    reason: str
    resume_supported: bool = False


class UserInputRequiredData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, ge=0)
    resume_supported: bool = False


class InteractionRecordedData(WireModel):
    request_id: str = Field(min_length=1)
    status: Literal["answered", "timeout", "cancelled"]
    decision: Literal["allow", "deny", ""] = ""
    scope: Literal["once", "session", ""] = ""
    answer: Any = None
    pending_interactions: list[str] = Field(default_factory=list)


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


class ClientMessageData(WireModel):
    message: str
    level: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)


class MessageQueuedData(WireModel):
    message_id: str = Field(min_length=1)
    position: int = Field(ge=1)


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


class TaskUpdatedData(WireModel):
    task_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    cwd: str
    status: Literal["pending", "running", "completed", "failed", "stopped"]
    created_at: float = Field(ge=0)
    started_at: float = Field(ge=0)
    finished_at: float = Field(ge=0)
    output: str = ""
    error: str = ""


class UsageData(WireModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    requests: int = Field(default=1, ge=1)


class TurnData(WireModel):
    turn: int = Field(ge=1)


class TurnCancelledData(TurnData):
    reason: str = Field(min_length=1)


class EndData(WireModel):
    status: str = Field(min_length=1)


_SERVER_EVENT_DATA_MODELS: dict[str, type[WireModel]] = {
    "assistant_message": AssistantMessageData,
    "assistant_message_delta": AssistantMessageDeltaData,
    "client_message": ClientMessageData,
    "end": EndData,
    "error": ErrorEventData,
    "message_queued": MessageQueuedData,
    "permission_denied": PermissionDeniedData,
    "permission_request": PermissionRequestData,
    "permission_response_recorded": InteractionRecordedData,
    "tool_call_delta": ToolCallDeltaData,
    "tool_calls_started": ToolCallsStartedData,
    "tool_result": ToolResultData,
    "task_updated": TaskUpdatedData,
    "turn_cancelled": TurnCancelledData,
    "turn_finished": TurnData,
    "turn_started": TurnData,
    "usage": UsageData,
    "user_input_required": UserInputRequiredData,
    "user_input_recorded": InteractionRecordedData,
}
TYPED_SERVER_EVENT_TYPES: tuple[str, ...] = tuple(_SERVER_EVENT_DATA_MODELS)


class ServerEvent(WireModel):
    protocol_version: str = PROTOCOL_VERSION
    session_id: str = ""
    thread_id: str = "agent"
    request_id: str = ""
    sequence: int = 0
    type: str
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def _validate_known_data(
        cls,
        data: dict[str, Any],
        info: ValidationInfo,
    ) -> dict[str, Any]:
        model = _SERVER_EVENT_DATA_MODELS.get(str(info.data.get("type") or ""))
        if model is None:
            return data
        return model.model_validate(data).model_dump(exclude_unset=True)


def server_event(
    *,
    type: str,
    data: dict[str, Any] | None = None,
    sequence: int = 0,
    session_id: str = "",
    thread_id: str = "agent",
    request_id: str = "",
    protocol_version: str = PROTOCOL_VERSION,
) -> ServerEvent:
    return ServerEvent(
        protocol_version=protocol_version,
        session_id=session_id,
        thread_id=thread_id,
        request_id=request_id,
        sequence=sequence,
        type=type,
        data=dict(data or {}),
    )


SessionMode = Literal["new", "resume"]


__all__ = [
    "AssistantMessageData",
    "AssistantMessageDeltaData",
    "ClientMessageData",
    "CommandInfo",
    "CommandListResponse",
    "CommandRequest",
    "CommandResponse",
    "CommandResult",
    "EndData",
    "ErrorEventData",
    "ErrorResponse",
    "HelloRequest",
    "HelloResponse",
    "InteractionRecordedData",
    "KNOWN_SERVER_EVENT_TYPES",
    "MessageRequest",
    "OpenSessionRequest",
    "OpenSessionResponse",
    "PermissionDeniedData",
    "PermissionRequestData",
    "PermissionResponseRequest",
    "ServerEvent",
    "ServerEventType",
    "SessionHistoryItem",
    "SessionMode",
    "TaskUpdatedData",
    "ToolCallData",
    "ToolCallDeltaData",
    "ToolCallDeltaItemData",
    "ToolCallsStartedData",
    "ToolResultData",
    "TurnCancelledData",
    "TurnData",
    "TYPED_SERVER_EVENT_TYPES",
    "UsageData",
    "UserInputRequiredData",
    "UserInputResponseRequest",
    "WireModel",
    "server_event",
]
