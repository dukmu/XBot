"""Shared approval validation and terminal audit for both permission entrypoints."""

import asyncio
from collections.abc import Awaitable, Callable

from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG
from XBotv2.core.tools import ClientEvent
from XBotv2.permissions.protocol import ApprovalDecision
from XBotv2.permissions.contracts import ApprovalPort
from XBotv2.agentloop import EventContext, Events
from XBotv2.application.contracts import ApplicationEventsPort, ClientEventsPort
from XBotv2.interactions.interactions import InteractionWaiter


class ApprovalService(ApprovalPort):
    """Permissions-owned transport and validation for live approvals."""

    def __init__(self, events: ApplicationEventsPort, client_events: ClientEventsPort) -> None:
        self._events = events
        self._client_events = client_events
        self.waiter = InteractionWaiter()

    def session_closed(self, _event: EventContext) -> None:
        self.waiter.cancel_all("session_closed")

    async def request(self, client_event: ClientEvent) -> ApprovalDecision:
        await self._events.emit(Events.CLIENT_EVENT, EventContext(client_event=client_event))
        result = await self._client_events.request(client_event)
        status = result.get("status", "answered") if result is not None else "unavailable"
        DEFAULT_RUNTIME_LOG.bind("permissions").info(
            "permission.response", request_id=client_event.data["request_id"], status=status,
        )
        if status in {"unavailable", "cancelled", "timeout"}:
            return ApprovalDecision(decision="deny")
        return ApprovalDecision.model_validate({
            "decision": result.get("decision"), "scope": result.get("scope", "once"),
        })


async def request_decision(
    approval: ApprovalPort,
    event: ClientEvent,
    apply: Callable[[ClientEvent, ApprovalDecision], Awaitable[ApprovalDecision]],
) -> ApprovalDecision:
    log = DEFAULT_RUNTIME_LOG.bind(
        "permissions", request_id=event.data["request_id"], source=event.data["source"],
    )
    log.info("permission.requested")
    outcome = "error"
    decision = ApprovalDecision(decision="deny")
    try:
        decision = await approval.request(event)
        decision = await apply(event, decision)
        outcome = "answered"
        return decision
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        log.info("permission.finished", outcome=outcome, decision=decision.decision, scope=decision.scope)
