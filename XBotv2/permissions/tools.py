"""Agent-facing tools owned by the permissions plugin."""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from XBotv2.core.tools import ClientEvent, Tool, ToolResult
from XBotv2.permission_request import ApprovalPort, PermissionRequestData
from XBotv2.permissions import PermissionsPort


async def request_tool_permission(
    tool: str,
    params: dict[str, str],
    reason: str,
    *,
    permissions: PermissionsPort,
    approval: ApprovalPort,
    record_permission_decision: Callable[
        [ClientEvent, str, str], Awaitable[None]
    ],
) -> ToolResult:
    """Ask the human to approve a restricted permission rule for one tool."""
    if not tool.strip():
        raise ValueError("tool must not be empty")
    if not reason.strip():
        raise ValueError("reason must not be empty")
    for name, pattern in params.items():
        if not name.strip():
            raise ValueError("parameter names must not be empty")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regular expression for {name}: {exc}") from exc
    payload = PermissionRequestData(
        request_id=f"permission:{secrets.token_hex(8)}",
        source="request_permission",
        permission={"tool": tool, "params": params},
        decision="ask",
        reason=reason,
        resume_supported=False,
    )
    event = ClientEvent(
        type="permission_request",
        data=payload.model_dump(exclude_none=True),
    )
    result = await approval.request(event)
    decision = str(result.get("decision") or "")
    scope = str(result.get("scope") or "once")
    if decision != "allow":
        return ToolResult.failure(
            "permission_rejected",
            f"Permission was not granted for {tool}.",
        )
    if scope == "once":
        permissions.grant_once(tool, params)
    elif scope == "session":
        await record_permission_decision(event, "allow", scope)
    return ToolResult.success(f"Permission granted for {tool} ({scope}).")


class RequestPermissionTool:
    """Agent Tool handler with explicit permission dependencies."""

    def __init__(
        self,
        permissions: PermissionsPort,
        approval: ApprovalPort,
        record_permission_decision: Callable[
            [ClientEvent, str, str], Awaitable[None]
        ],
    ) -> None:
        self._permissions = permissions
        self._approval = approval
        self._record_permission_decision = record_permission_decision

    async def invoke(
        self,
        tool: str,
        params: dict[str, str],
        reason: str,
    ) -> ToolResult:
        """Ask the human to approve a restricted permission rule for one tool."""
        return await request_tool_permission(
            tool,
            params,
            reason,
            permissions=self._permissions,
            approval=self._approval,
            record_permission_decision=self._record_permission_decision,
        )

    def as_tool(self) -> Tool:
        return Tool.from_function(self.invoke, name="request_permission")
