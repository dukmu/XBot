"""Test helpers for the service-driven tool-execution pipeline.

These helpers build an XCore context with the tool execution services
(guards, approval, interactions) registered the way the plugin tree would,
so tests exercise the same wiring as production instead of passing policy
objects as ``execute_tools`` parameters.
"""

from __future__ import annotations

from XBotv2.agentloop.api.contracts import TOOLS
from typing import Any
import xcore
from XBotv2.agentloop.impl.tool_registry import ToolRegistry
from XBotv2.agentloop.impl.tool_service import ToolsService
from XBotv2.permissions.api.models import Denied
from XBotv2.permissions.api.models import Approval, PermissionRequest


class _UnavailableApproval:
    async def request(self, _event: PermissionRequest) -> Approval:
        return Denied(reason="approval unavailable")


async def _ignore_permission_decision(
    _event: PermissionRequest, decision: Approval
) -> Approval:
    return decision


def make_tool_ctx(
    registry: ToolRegistry,
    *,
    sandbox: Any = None,
    permissions: Any = None,
    approval: Any = None,
    interactions: Any = None,
    extra_guards: tuple[Any, ...] = (),
    base: Any = None,
) -> xcore.api.context.Context:
    """Build an XCore context with the tool pipeline services registered.

    Guards are registered in the order a plugin tree would compose them:
    permissions first, then sandbox, then any extra guards.  ``approval``
    and ``interactions`` are mounted when provided (approval is optional —
    without it ``ask`` fails closed).  Pass ``base`` to register onto an
    existing context (e.g. one carrying event listeners).
    """
    ctx = base or xcore.api.context.Context()
    tools_service = ctx.get(TOOLS, strict=False)
    if tools_service is None:
        # The harness composes and owns this context, so registrations made
        # outside a plugin apply are caller-owned here.
        tools_service = ToolsService(registry, events=ctx, ownership="caller")
        ctx.provide(TOOLS, tools_service)
    if permissions is not None:
        from XBotv2.permissions.impl.guard import PermissionGuard

        if ctx.get("permissions", strict=False) is None:
            ctx.set("permissions", permissions)
        guard = PermissionGuard(
            permissions,
            approval or _UnavailableApproval(),
            ctx.emit,
            _ignore_permission_decision,
        )
        tools_service.guard(guard.check, approval=True, cleanup="caller")
    if sandbox is not None:
        if ctx.get("sandbox", strict=False) is None:
            ctx.set("sandbox", sandbox)
        tools_service.guard(sandbox.make_guard(), cleanup="caller")
    for guard in extra_guards:
        tools_service.guard(guard, cleanup="caller")
    if approval is not None:
        ctx.set("approval", approval)
    if interactions is not None:
        ctx.set("interactions", interactions)
    return ctx
