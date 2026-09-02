"""Typed notifications owned by the permissions plugin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from XBotv2.core import ClientEvent, ToolCall


PERMISSION_DECIDED = "permissions/decided"
PERMISSION_REQUESTED = "permission/request"


@dataclass(frozen=True, slots=True)
class PermissionDecided:
    decision: Literal["allow", "deny"]
    scope: str
    rule: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class PermissionRequested:
    tool_call: ToolCall
    client_event: ClientEvent


__all__ = [
    "PERMISSION_DECIDED",
    "PERMISSION_REQUESTED",
    "PermissionDecided",
    "PermissionRequested",
]
