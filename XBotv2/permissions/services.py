"""Public service Protocols for permission-policy consumers."""

from __future__ import annotations

from typing import Protocol

from XBotv2.core.tools import ToolCall


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


__all__ = ["PermissionsPort"]
