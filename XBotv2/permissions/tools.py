"""Agent-facing tools owned by the permissions plugin."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from XBotv2.core.tools import ClientEvent, Tool, ToolResult
from XBotv2.permissions import ApprovalDecision, ApprovalPort, PermissionRequestData
from XBotv2.permissions.approval import request_decision
from XBotv2.permissions.patterns import compile_pattern


async def request_tool_permission(
    tool: str,
    params: dict[str, str],
    reason: str,
    *,
    approval: ApprovalPort,
    apply_permission_decision: Callable[
        [ClientEvent, ApprovalDecision], Awaitable[ApprovalDecision]
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
        compile_pattern(pattern)
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
    result = await request_decision(approval, event, apply_permission_decision)
    scope = result.scope
    if result.decision != "allow":
        return ToolResult.failure(
            "permission_rejected",
            f"Permission was not granted for {tool}.",
        )
    return ToolResult.success(f"Permission granted for {tool} ({scope}).")


class RequestPermissionTool:
    """Agent Tool handler with explicit permission dependencies."""

    def __init__(
        self,
        approval: ApprovalPort,
        apply_permission_decision: Callable[
            [ClientEvent, ApprovalDecision], Awaitable[ApprovalDecision]
        ],
    ) -> None:
        self._approval = approval
        self._apply_permission_decision = apply_permission_decision

    async def invoke(
        self,
        tool: str,
        params: dict[str, str],
        reason: str,
    ) -> ToolResult:
        """Request a permission rule for future calls; never execute the target tool.

        tool: Exact registered tool name.
        params: Parameter names mapped to full-match regular expressions.
            Omitted parameters are unconstrained. Constrain command and cwd
            for shell access. Sandbox escape also requires an explicit
            sandbox_permissions pattern matching require_escalated.
        reason: Explain why subsequent calls need this scope.

        Approval grants one matching future call or this Agent thread's session.
        Session grants survive resume; once grants are not persisted.
        It does not change sandbox policy or override deny rules.
        """
        return await request_tool_permission(
            tool,
            params,
            reason,
            approval=self._approval,
            apply_permission_decision=self._apply_permission_decision,
        )

    def as_tool(self) -> Tool:
        return Tool.from_function(self.invoke, name="request_permission")
