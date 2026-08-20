"""Protocol-wide HTTP and SSE contracts."""

from XBotv2.protocol.models import (
    EndData,
    ErrorEventData,
    ErrorResponse,
    HealthResponse,
    HelloRequest,
    HelloResponse,
    ServerEvent,
    WireModel,
    server_event,
)
from XBotv2.protocol.version import PROTOCOL_VERSION

__all__ = [
    "EndData",
    "ErrorEventData",
    "ErrorResponse",
    "HealthResponse",
    "HelloRequest",
    "HelloResponse",
    "PROTOCOL_VERSION",
    "ServerEvent",
    "WireModel",
    "server_event",
]
