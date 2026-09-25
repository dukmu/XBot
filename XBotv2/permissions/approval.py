"""Shared approval validation and terminal audit for both permission entrypoints."""

import asyncio
from collections.abc import Awaitable, Callable

from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG
from XBotv2.permissions.contracts import (
    Allowed,
    Approval,
    ApprovalPort,
    Denied,
    PermissionRequest,
)
from XBotv2.agentloop import Events
from XBotv2.agentloop.events import SessionLifecycle
from XBotv2.application.contracts import ApplicationEventsPort, ClientEventsPort
from XBotv2.interactions import InteractionWaiterPort


class ApprovalService(ApprovalPort):
    """Permissions-owned transport and validation for live approvals."""

    def __init__(
        self,
        events: ApplicationEventsPort,
        client_events: ClientEventsPort,
        waiter: InteractionWaiterPort,
    ) -> None:
        self._events = events
        self._client_events = client_events
        self.waiter = waiter

    def session_closed(self, _event: SessionLifecycle) -> None:
        self.waiter.cancel_all("session_closed")

    async def request(self, request: PermissionRequest) -> Approval:
        result = await self._client_events.request(request)
        if result is None:
            status = "unavailable"
        elif isinstance(result, (Allowed, Denied)):
            status = result.kind
        else:
            raise TypeError(
                f"Permission request received {type(result).__name__} resolution"
            )
        DEFAULT_RUNTIME_LOG.bind("permissions").info(
            "permission.response", request_id=request.interaction_id, status=status,
        )
        if result is None:
            return Denied(reason="unavailable")
        return result


async def request_decision(
    approval: ApprovalPort,
    event: PermissionRequest,
    apply: Callable[[PermissionRequest, Approval], Awaitable[Approval]],
) -> Approval:
    log = DEFAULT_RUNTIME_LOG.bind(
        "permissions", request_id=event.interaction_id, source=event.source,
    )
    log.info("permission.requested")
    outcome = "error"
    decision: Approval = Denied(reason="permission request failed")
    try:
        decision = await approval.request(event)
        decision = await apply(event, decision)
        outcome = "answered"
        return decision
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        log.info(
            "permission.finished",
            outcome=outcome,
            decision=decision.kind,
            scope=decision.scope if isinstance(decision, Allowed) else "",
        )
