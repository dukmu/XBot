"""Versioned HTTP request and response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from xbotv2.protocol.frames import PROTOCOL_VERSION


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


class OpenSessionResponse(WireModel):
    session_id: str
    thread_id: str
    status: Literal["ready"] = "ready"
    agent_name: str
    workspace_root: str
    provider: str


class CommandRequest(WireModel):
    command: str = ""
    args: list[str] | None = None
    raw: str = ""
    kind: Literal["server", "tool", "skill", "mcp"] = "server"


class CommandInfo(WireModel):
    name: str
    slash: str
    kind: Literal["client", "server", "tool", "skill", "mcp"]
    description: str
    examples: list[str] = Field(default_factory=list)
    parameters: dict[str, str] = Field(default_factory=dict)


class CommandListResponse(WireModel):
    commands: list[CommandInfo]


class CommandResult(WireModel):
    command: str
    status: Literal["ok", "error"]
    message: str
    data: Any = None


class CommandResponse(WireModel):
    type: Literal["command_result"] = "command_result"
    data: CommandResult


class MessageRequest(WireModel):
    content: str = Field(min_length=1, pattern=r".*\S.*")
    request_id: str = ""


class InteractionResponseRequest(WireModel):
    request_id: str
    decision: str | None = None
    scope: str | None = None
    answer: Any = None


class ErrorResponse(WireModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class Event(WireModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


SessionMode = Literal["new", "resume"]


__all__ = [
    "CommandInfo",
    "CommandListResponse",
    "CommandRequest",
    "CommandResponse",
    "CommandResult",
    "ErrorResponse",
    "Event",
    "HelloRequest",
    "HelloResponse",
    "InteractionResponseRequest",
    "MessageRequest",
    "OpenSessionRequest",
    "OpenSessionResponse",
    "SessionMode",
    "WireModel",
]
