"""Protocol-wide HTTP and SSE envelope models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.protocol.version import PROTOCOL_VERSION


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


class HealthResponse(WireModel):
    status: Literal["ok"] = "ok"
    server_name: str
    protocol_version: str = PROTOCOL_VERSION
    uptime_s: int = Field(ge=0)
    sessions: int = Field(ge=0)
    threads: int = Field(ge=0)
    workspace_root: str


class ErrorResponse(WireModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class ErrorEventData(ErrorResponse):
    stage: str | None = None


class EndData(WireModel):
    status: str = Field(min_length=1)


class ServerEvent(WireModel):
    protocol_version: str = PROTOCOL_VERSION
    session_id: str = ""
    thread_id: str = "agent"
    request_id: str = ""
    sequence: int = 0
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


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


__all__ = [
    "EndData",
    "ErrorEventData",
    "ErrorResponse",
    "HealthResponse",
    "HelloRequest",
    "HelloResponse",
    "ServerEvent",
    "WireModel",
    "server_event",
]
