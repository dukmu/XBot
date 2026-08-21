"""Agent-facing tools owned by the permissions plugin."""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from XBotv2.core.tools import Tool, ToolResult
from XBotv2.permission_request import PermissionRequestData
from XBotv2.permissions import PermissionsPort


async def request_tool_permission(
    tool: str,
    params: dict[str, str],
    reason: str,
    *,
    permissions: Any = None,
    approval: Any = None,
    record_permission_decision: Any = None,
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
    if approval is None:
        return ToolResult.failure(
            "approval_unavailable",
            "Permission approval is unavailable in this session.",
        )
    payload = PermissionRequestData(
        request_id=f"permission:{secrets.token_hex(8)}",
        source="request_permission",
        permission={"tool": tool, "params": params},
        decision="ask",
        reason=reason,
        resume_supported=False,
    )
    event = {
        "type": "permission_request",
        "data": payload.model_dump(exclude_none=True),
    }
    result = await approval.request(event)
    decision = str(result.get("decision") or "")
    scope = str(result.get("scope") or "once")
    if decision != "allow":
        return ToolResult.failure(
            "permission_rejected",
            f"Permission was not granted for {tool}.",
        )
    if scope == "once" and permissions is not None:
        permissions.grant_once(tool, params)
    elif scope == "session" and record_permission_decision is not None:
        await record_permission_decision(event, "allow", scope)
    return ToolResult.success(f"Permission granted for {tool} ({scope}).")


def build_request_permission_tool(
    permissions: PermissionsPort,
    approval: Any,
    record_permission_decision: Callable[
        [dict[str, Any], str, str], Awaitable[None]
    ],
) -> Tool:
    """Bind permission-policy services to the Agent-facing request Tool."""

    async def invoke(
        tool: str,
        params: dict[str, str],
        reason: str,
    ) -> ToolResult:
        return await request_tool_permission(
            tool,
            params,
            reason,
            permissions=permissions,
            approval=approval,
            record_permission_decision=record_permission_decision,
        )

    invoke.__doc__ = request_tool_permission.__doc__
    return Tool.from_function(invoke, name="request_permission")
