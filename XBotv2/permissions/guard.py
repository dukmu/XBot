"""Final tool guard owned by the permissions plugin."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from XBotv2.core.tools import ClientEvent, GuardDecision, ToolCall
from XBotv2.permission_request import ApprovalPort, PermissionRequestData
from XBotv2.permissions import PermissionsPort
from XBotv2.permissions.events import PERMISSION_REQUESTED, PermissionRequested


class PermissionGuard:
    """Resolve tri-state policy through explicit permission dependencies."""

    def __init__(
        self,
        permissions: PermissionsPort,
        approval: ApprovalPort,
        emit: Callable[[str, Any], Awaitable[Any]],
        record_decision: Callable[[ClientEvent, str, str], Awaitable[None]],
    ) -> None:
        self._permissions = permissions
        self._approval = approval
        self._emit = emit
        self._record_decision = record_decision

    async def check(self, tool_call: ToolCall, _entry: Any) -> GuardDecision | None:
        decision, reason = self._permissions.check_tool_call(tool_call)
        if decision == "allow":
            return None
        if decision == "deny":
            return GuardDecision("deny", reason, source="permissions")
        payload = PermissionRequestData(
            request_id=f"permission:{tool_call.id}",
            source="permission_system",
            tool_call=tool_call.model_dump(mode="json"),
            decision="ask",
            reason=reason,
            resume_supported=False,
        )
        event = ClientEvent(
            type="permission_request",
            data=payload.model_dump(exclude_none=True),
        )
        await self._emit(
            PERMISSION_REQUESTED,
            PermissionRequested(
                tool_call=tool_call,
                client_event=event,
            ),
        )
        result = await self._approval.request(event)
        if str(result.get("decision") or "") != "allow":
            return GuardDecision(
                "deny",
                reason or f"Permission denied for tool: {tool_call.name}",
                source="permissions",
            )
        scope = str(result.get("scope") or "once")
        if scope == "session":
            await self._record_decision(event, "allow", scope)
        return None
