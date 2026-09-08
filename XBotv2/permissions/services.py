"""Public service Protocols for permission-policy consumers."""

from __future__ import annotations

from typing import Protocol

from XBotv2.core.tools import ClientEvent, ToolCall
from XBotv2.permissions.protocol import ApprovalDecision


class ApprovalPort(Protocol):
    async def request(self, client_event: ClientEvent) -> ApprovalDecision: ...


class PermissionsPort(Protocol):
    def check(self, tool_name: str, args: dict[str, object] | None = None) -> str: ...

    def explicit_allow(
        self,
        tool_name: str,
        args: dict[str, object] | None = None,
        *,
        constrain_param: str | None = None,
    ) -> bool: ...

    def check_tool_call(self, tool_call: ToolCall) -> tuple[str, str]: ...

    def grant_once(self, tool_name: str, param_patterns: dict[str, str]) -> None: ...

    def consume_once(self, tool_name: str, args: dict[str, object]) -> None: ...


__all__ = ["ApprovalPort", "PermissionsPort"]
