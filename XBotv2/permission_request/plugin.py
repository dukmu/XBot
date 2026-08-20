"""Permission request component: the approval seam as an XCore service.

Provides ``ctx.approval`` — the live one-shot approval channel for tool
``ask`` decisions.  The tool pipeline resolves ``ask`` through this service
opportunistically (``ctx.get("approval")``); a deployment without it keeps
the fail-closed deny degrade.  Answerers are registered by protocol/UI
plugins that own the client event and the human response.
"""

from __future__ import annotations

from typing import Any

from XBotv2.agentloop import Events
from XBotv2.permission_request.service import ApprovalService


class PermissionRequestComponent:
    """Register the approval service as ``ctx.approval``."""

    name = "xbot.permission_request"
    inject = ["client_events"]

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = ApprovalService(ctx, ctx.client_events)
        ctx.set("approval", service)
        ctx.dispose(ctx.client_events.register_waiter(
            "permission_request", service.waiter
        ))
        ctx.on(
            Events.SESSION_CLOSE,
            lambda _event: service.cancel_all("session_closed"),
        )


plugin = PermissionRequestComponent()
