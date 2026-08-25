"""Permission request component: the approval seam as an XCore service.

Provides ``ctx.approval`` — the live one-shot approval channel for tool
``ask`` decisions. Permissions declares it as a required dependency, so XCore
activates the policy guard only after the channel is available. Answerers are
registered by protocol/UI plugins that own the client event and human response.
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
        ctx.on(Events.SESSION_CLOSE, service.session_closed)


plugin = PermissionRequestComponent()
