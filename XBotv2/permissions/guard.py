"""Final tool guard owned by the permissions plugin."""

from __future__ import annotations

from typing import Awaitable, Callable

from XBotv2.agentloop.contracts import ToolRegistration
from XBotv2.core.tools import GuardDecision, ToolCall
from XBotv2.permissions import Allowed, Approval, ApprovalPort
from XBotv2.permissions.contracts import PermissionRequest, ToolPermission
from XBotv2.permissions import PermissionsPort
from XBotv2.permissions.events import PERMISSION_REQUESTED, PermissionRequested
from XBotv2.permissions.approval import request_decision
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG


class PermissionGuard:
    """Resolve tri-state policy through explicit permission dependencies."""

    def __init__(
        self,
        permissions: PermissionsPort,
        approval: ApprovalPort,
        emit: Callable[[str, object], Awaitable[object]],
        apply_decision: Callable[[PermissionRequest, Approval], Awaitable[Approval]],
    ) -> None:
        self._permissions = permissions
        self._approval = approval
        self._emit = emit
        self._apply_decision = apply_decision

    async def check(self, tool_call: ToolCall, _entry: ToolRegistration) -> GuardDecision | None:
        decision, reason = self._permissions.check_tool_call(tool_call)
        DEFAULT_RUNTIME_LOG.bind("permissions").info(
            "permission.checked", call_id=tool_call.id, tool=tool_call.name, decision=decision,
        )
        if decision == "allow":
            return None
        if decision == "deny":
            return GuardDecision("deny", reason, source="permissions")
        event = PermissionRequest(
            interaction_id=f"permission:{tool_call.id}",
            source="permission_system",
            subject=ToolPermission(tool_call=tool_call),
            reason=reason,
            resume_supported=True,
        )
        await self._emit(
            PERMISSION_REQUESTED,
            PermissionRequested(
                request=event,
            ),
        )
        result = await request_decision(self._approval, event, self._apply_decision)
        if not isinstance(result, Allowed) or self._permissions.check(tool_call.name, tool_call.args) == "deny":
            return GuardDecision(
                "deny",
                reason or f"Permission denied for tool: {tool_call.name}",
                source="permissions",
            )
        return None
