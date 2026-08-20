"""Final tool guard owned by the permissions plugin."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from XBotv2.core.events import EventContext, Events
from XBotv2.core.tools import ClientEvent, GuardDecision
from XBotv2.permission_request import PermissionRequestData


def make_permission_guard(
    permissions: Any,
    approval: Any,
    emit: Callable[[str, Any], Awaitable[Any]],
    *,
    record_decision: Callable[[dict[str, Any], str, str], Awaitable[None]] | None = None,
) -> Any:
    """Build a guard that resolves tri-state policy inside this plugin."""

    async def guard(tool_call: Any, _entry: Any) -> GuardDecision | None:
        decision, reason = permissions.check_tool_call(tool_call)
        if decision == "allow":
            return None
        if decision == "deny":
            return GuardDecision("deny", reason, source="permissions")
        payload = PermissionRequestData(
            request_id=f"permission:{tool_call.id}",
            source="permission_system",
            tool_call=tool_call.to_dict(),
            decision="ask",
            reason=reason,
            resume_supported=False,
        )
        event = {
            "type": "permission_request",
            "data": payload.model_dump(exclude_none=True),
        }
        await emit(
            Events.PERMISSION_REQUEST,
            EventContext(
                tool_call=tool_call,
                client_event=ClientEvent.from_mapping(event),
            ),
        )
        result = await approval.request(event) if approval is not None else {
            "status": "unavailable",
            "decision": "",
            "scope": "once",
        }
        if str(result.get("decision") or "") != "allow":
            return GuardDecision(
                "deny",
                reason or f"Permission denied for tool: {tool_call.name}",
                source="permissions",
            )
        scope = str(result.get("scope") or "once")
        if scope == "session" and record_decision is not None:
            await record_decision(event, "allow", scope)
        return None

    return guard
