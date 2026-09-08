"""Public service contracts for permission policy and approval."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import JsonValue

from XBotv2.core.tools import ClientEvent, ToolCall
from XBotv2.permissions.protocol import ApprovalDecision


class ApprovalPort(Protocol):
    async def request(self, client_event: ClientEvent) -> ApprovalDecision: ...


class PermissionsPort(Protocol):
    def check(
        self,
        tool_name: str,
        args: dict[str, JsonValue] | None = None,
    ) -> Literal["allow", "deny", "ask"]: ...
    def explicit_allow(
        self,
        tool_name: str,
        args: dict[str, JsonValue] | None = None,
        *,
        constrain_param: str | None = None,
    ) -> bool: ...
    def check_tool_call(self, tool_call: ToolCall) -> tuple[str, str]: ...
    def grant_once(self, tool_name: str, param_patterns: dict[str, str]) -> None: ...
    def consume_once(self, tool_name: str, args: dict[str, JsonValue]) -> None: ...


__all__ = ["ApprovalPort", "PermissionsPort"]
