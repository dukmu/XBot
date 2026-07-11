"""Core-owned domain contracts; public names are re-exported by xbotv2.api."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ClientEvent:
    type: str
    data: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    media_type: str = "application/octet-stream"
    name: str = ""


@dataclass(frozen=True)
class ToolResult:
    status: Literal["success", "error", "denied", "cancelled"] = "success"
    content: str = ""
    data: JsonValue = None
    error: ToolError | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    client_events: tuple[ClientEvent, ...] = ()
    wait_for_user: bool = False
    timeout_seconds: float | None = None

    @classmethod
    def success(cls, content: str = "", *, data: JsonValue = None) -> "ToolResult":
        return cls(content=content, data=data)

    @classmethod
    def failure(
        cls, code: str, message: str, *, retryable: bool = False
    ) -> "ToolResult":
        return cls(
            status="error",
            content=message,
            error=ToolError(code=code, message=message, retryable=retryable),
        )


class HookAction(str, Enum):
    CONTINUE = "continue"
    DENY = "deny"
    STOP = "stop"


@dataclass(frozen=True)
class HookDecision:
    action: HookAction = HookAction.CONTINUE
    reason: str = ""
    value: Any = None


__all__ = [
    "ArtifactRef", "ClientEvent", "HookAction", "HookDecision", "JsonValue",
    "ToolCall", "ToolError", "ToolResult",
]
