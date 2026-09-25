"""Protocol-wide HTTP and SSE envelope models."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.protocol.version import PROTOCOL_VERSION
from XBotv2.core.domain import EventScope


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def wire_payload(self) -> dict[str, JsonValue]:
        """Encode a concrete event model without its routing discriminator."""
        return self.model_dump(mode="json", exclude={"kind"})


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
    details: dict[str, JsonValue] = Field(default_factory=dict)
    retryable: bool = False


T = TypeVar("T")


class ResourceResponse(WireModel, Generic[T]):
    """HTTP envelope for one owner-defined resource value."""

    data: T


class EndData(WireModel):
    status: str = Field(min_length=1)


class ServerEvent(WireModel):
    protocol_version: str = PROTOCOL_VERSION
    session_id: str
    thread_id: str
    sequence: int = Field(ge=0)
    scope: EventScope
    kind: str = Field(min_length=1)
    payload: dict[str, JsonValue]


def server_event(
    *,
    kind: str,
    payload: dict[str, JsonValue],
    sequence: int,
    session_id: str,
    thread_id: str,
    scope: EventScope,
    protocol_version: str = PROTOCOL_VERSION,
) -> ServerEvent:
    return ServerEvent(
        protocol_version=protocol_version,
        session_id=session_id,
        thread_id=thread_id,
        sequence=sequence,
        scope=scope,
        kind=kind,
        payload=payload,
    )


__all__ = [
    "EndData",
    "ErrorResponse",
    "HealthResponse",
    "HelloRequest",
    "HelloResponse",
    "ResourceResponse",
    "ServerEvent",
    "WireModel",
    "server_event",
]
