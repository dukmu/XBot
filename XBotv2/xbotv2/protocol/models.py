"""Versioned HTTP request and response models."""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
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
    agent: str | None = None


class SessionHistoryItem(WireModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: str = ""
    status: str = ""
    data: Any = None
    error: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class UsageData(WireModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    requests: int = Field(default=1, ge=0)
    context_tokens: int = Field(default=0, ge=0)


def _empty_usage() -> UsageData:
    return UsageData(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        requests=0,
    )


class HealthResponse(WireModel):
    status: Literal["ok"] = "ok"
    server_name: str
    protocol_version: str = PROTOCOL_VERSION
    uptime_s: int = Field(ge=0)
    sessions: int = Field(ge=0)
    threads: int = Field(ge=0)
    workspace_root: str


class ProviderInfo(WireModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    max_tokens: int = Field(ge=1)
    reasoning_effort: str = ""
    thinking_enabled: bool = False


class ProviderListResponse(WireModel):
    default: str
    providers: list[ProviderInfo] = Field(default_factory=list)


class AgentInfo(WireModel):
    name: str = Field(min_length=1)
    description: str
    mode: Literal["primary", "subagent", "all"]
    provider: str = ""
    model: str = ""
    context_window: int = Field(default=0, ge=0)


class AgentListResponse(WireModel):
    active: str = ""
    agents: list[AgentInfo] = Field(default_factory=list)


class ToolInfo(WireModel):
    name: str = Field(min_length=1)
    registered_name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    description: str
    parameters: dict[str, Any]
    sandbox_mode: Literal["sandboxed", "host"]
    timeout_seconds: float | None = Field(default=None, gt=0)


class ToolListResponse(WireModel):
    tools: list[ToolInfo] = Field(default_factory=list)


class ThreadSummary(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: Literal["active", "inactive"]
    kind: Literal["main", "subagent"] = "main"
    turn_status: Literal["idle", "running"] = "idle"
    parent_thread_id: str = ""
    agent: str = ""
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    context_window: int = Field(default=0, ge=0)
    message_count: int = Field(default=0, ge=0)
    usage: UsageData = Field(default_factory=_empty_usage)
    pending_interactions: list[str] = Field(default_factory=list)
    status_slots: dict[str, str] = Field(default_factory=dict)


class ThreadListResponse(WireModel):
    session_id: str = Field(min_length=1)
    threads: list[ThreadSummary] = Field(default_factory=list)


class SessionSummary(WireModel):
    session_id: str = Field(min_length=1)
    status: Literal["active", "inactive"]
    active_threads: int = Field(default=0, ge=0)
    thread_count: int = Field(default=0, ge=0)


class SessionListResponse(WireModel):
    sessions: list[SessionSummary] = Field(default_factory=list)


PermissionDecision = Literal["allow", "deny", "ask"]
SandboxAccess = Literal["allow", "deny", "ask", "readonly", "readwrite"]
SandboxKey = Literal[
    "enabled",
    "network",
    "external_read",
    "external_write",
    "workspace_read",
    "workspace_write",
]
SandboxValue = StrictBool | SandboxAccess


class SessionPolicyPatch(WireModel):
    permissions: dict[str, PermissionDecision] = Field(default_factory=dict)
    remove_permissions: list[str] = Field(default_factory=list)
    sandbox: dict[SandboxKey, SandboxValue] = Field(default_factory=dict)
    remove_sandbox: list[SandboxKey] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def _validate_permission_names(
        cls, value: dict[str, PermissionDecision]
    ) -> dict[str, PermissionDecision]:
        if any(not name.strip() for name in value):
            raise ValueError("permission tool names must be non-empty")
        return {name.strip(): decision for name, decision in value.items()}

    @field_validator("remove_permissions")
    @classmethod
    def _validate_removed_permission_names(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("permission tool names must be non-empty")
        return [name.strip() for name in value]

    @model_validator(mode="after")
    def _validate_policy_patch(self) -> "SessionPolicyPatch":
        permission_overlap = set(self.permissions).intersection(
            self.remove_permissions
        )
        sandbox_overlap = set(self.sandbox).intersection(self.remove_sandbox)
        if permission_overlap or sandbox_overlap:
            raise ValueError("policy keys cannot be set and removed together")
        for key, value in self.sandbox.items():
            if key in {"enabled", "network"} and not isinstance(value, bool):
                raise ValueError(f"sandbox.{key} must be a boolean")
            if key not in {"enabled", "network"} and isinstance(value, bool):
                raise ValueError(f"sandbox.{key} must be an access mode")
        return self


class SessionPolicyResponse(WireModel):
    session_id: str = Field(min_length=1)
    permissions: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    sandbox: dict[str, Any] = Field(default_factory=dict)
    effective_sandbox: dict[str, Any] = Field(default_factory=dict)


class OpenSessionResponse(WireModel):
    session_id: str
    thread_id: str
    status: Literal["ready"] = "ready"
    agent_name: str
    workspace_root: str
    provider: str
    model: str = ""
    model_mode: str = ""
    context_window: int = Field(default=0, ge=0)
    usage: UsageData = Field(default_factory=_empty_usage)
    history: list[SessionHistoryItem] = Field(default_factory=list)
    status_slots: dict[str, str] = Field(default_factory=dict)


class OpenThreadRequest(WireModel):
    thread_id: str = Field(min_length=1)
    parent_thread_id: str = Field(default="agent", min_length=1)
    workspace_root: str | None = None
    mode: Literal["new", "resume"] = "new"
    agent: str | None = None


class ThreadMessagesResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    messages: list[SessionHistoryItem] = Field(default_factory=list)


class UndoRequest(WireModel):
    count: int = Field(default=1, ge=1)


class HistoryMutationResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    removed_turns: int = Field(ge=0)
    messages: list[SessionHistoryItem] = Field(default_factory=list)


class ForkResponse(WireModel):
    session_id: str = Field(min_length=1)
    source_session_id: str = Field(min_length=1)
    status: Literal["forked"] = "forked"


class AgentSelectionRequest(WireModel):
    name: str = Field(min_length=1)


class AgentSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str
    model_mode: str = ""
    context_window: int = Field(ge=0)


class ProviderSelectionRequest(WireModel):
    name: str = Field(min_length=1)


class ProviderSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_mode: str = ""


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


class InteractionResponse(WireModel):
    request_id: str = Field(min_length=1)
    recorded: Literal[True] = True
    pending_interactions: list[str] = Field(default_factory=list)


class InterruptResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: Literal["idle", "interrupting"]
    cancelled: bool


class CloseResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = ""
    status: Literal["closed"] = "closed"


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


class RequestedPermissionData(WireModel):
    tool: str = Field(min_length=1)
    params: dict[str, str] = Field(default_factory=dict)


class PermissionRequestData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call: dict[str, Any] | None = None
    permission: RequestedPermissionData | None = None
    decision: Literal["ask"] = "ask"
    reason: str
    resume_supported: bool = False

    @model_validator(mode="after")
    def _require_subject(self) -> "PermissionRequestData":
        if (self.tool_call is None) == (self.permission is None):
            raise ValueError(
                "permission request requires exactly one tool_call or permission"
            )
        return self


class PermissionDeniedData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call: dict[str, Any]
    decision: Literal["deny"] = "deny"
    reason: str
    resume_supported: bool = False


class UserInputOption(WireModel):
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)


class UserInputRequiredData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    options: list[UserInputOption] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, gt=0)
    resume_supported: bool = False

    @model_validator(mode="after")
    def _validate_ask_user_options(self) -> "UserInputRequiredData":
        if self.source == "ask_user" and len(self.options) < 2:
            raise ValueError("ask_user requires at least two options")
        return self


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
    kind: Literal["shell", "agent"] = "shell"
    command: str = Field(min_length=1)
    cwd: str
    status: Literal["pending", "running", "completed", "failed", "stopped"]
    created_at: float = Field(ge=0)
    started_at: float = Field(ge=0)
    finished_at: float = Field(ge=0)
    output: str = ""
    error: str = ""
    agent: str = ""
    thread_id: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)


class TaskListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    tasks: list[TaskUpdatedData] = Field(default_factory=list)


class TaskStopResponse(TaskListResponse):
    matched_count: int = Field(ge=0)


class TurnData(WireModel):
    turn: int = Field(ge=1)
    status_slots: dict[str, str] = Field(default_factory=dict)


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
    "AgentSelectionRequest",
    "AgentSelectionResponse",
    "AgentInfo",
    "AgentListResponse",
    "ClientMessageData",
    "CloseResponse",
    "CommandInfo",
    "CommandListResponse",
    "CommandRequest",
    "CommandResponse",
    "CommandResult",
    "EndData",
    "ErrorEventData",
    "ErrorResponse",
    "ForkResponse",
    "HelloRequest",
    "HelloResponse",
    "HealthResponse",
    "HistoryMutationResponse",
    "InteractionResponse",
    "InteractionRecordedData",
    "InterruptResponse",
    "KNOWN_SERVER_EVENT_TYPES",
    "MessageRequest",
    "OpenSessionRequest",
    "OpenSessionResponse",
    "OpenThreadRequest",
    "PermissionDeniedData",
    "PermissionRequestData",
    "PermissionResponseRequest",
    "RequestedPermissionData",
    "ProviderInfo",
    "ProviderListResponse",
    "ProviderSelectionRequest",
    "ProviderSelectionResponse",
    "ServerEvent",
    "ServerEventType",
    "SessionHistoryItem",
    "SessionListResponse",
    "SessionMode",
    "SessionPolicyPatch",
    "SessionPolicyResponse",
    "PermissionDecision",
    "SandboxKey",
    "SandboxValue",
    "SessionSummary",
    "TaskListResponse",
    "TaskStopResponse",
    "TaskUpdatedData",
    "ToolCallData",
    "ToolCallDeltaData",
    "ToolCallDeltaItemData",
    "ToolCallsStartedData",
    "ToolResultData",
    "ToolInfo",
    "ToolListResponse",
    "TurnCancelledData",
    "TurnData",
    "ThreadListResponse",
    "ThreadMessagesResponse",
    "ThreadSummary",
    "TYPED_SERVER_EVENT_TYPES",
    "UsageData",
    "UndoRequest",
    "UserInputOption",
    "UserInputRequiredData",
    "UserInputResponseRequest",
    "WireModel",
    "server_event",
]
