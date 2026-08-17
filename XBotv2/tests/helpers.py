"""Test helpers for the service-driven tool-execution pipeline.

These helpers build an XCore context with the tool execution services
(guards, approval, interactions) registered the way the plugin tree would,
so tests exercise the same wiring as production instead of passing policy
objects as ``execute_tools`` parameters.
"""

from __future__ import annotations

from typing import Any

import xcore

from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.tool_service import ToolsService


def make_tool_ctx(
    registry: ToolRegistry,
    *,
    sandbox: Any = None,
    permissions: Any = None,
    approval: Any = None,
    interactions: Any = None,
    extra_guards: tuple[Any, ...] = (),
    base: Any = None,
) -> xcore.Context:
    """Build an XCore context with the tool pipeline services registered.

    Guards are registered in the order a plugin tree would compose them:
    permissions first, then sandbox, then any extra guards.  ``approval``
    and ``interactions`` are mounted when provided (approval is optional —
    without it ``ask`` fails closed).  Pass ``base`` to register onto an
    existing context (e.g. one carrying event listeners).
    """
    ctx = base or xcore.Context()
    tools_service = ToolsService(registry, events=ctx)
    ctx.set("tools", tools_service)
    if permissions is not None:
        from XBotv2.permissions.guard import make_permission_guard

        ctx.set("permissions", permissions)
        tools_service.guard(make_permission_guard(
            permissions,
            approval,
            ctx.emit,
        ))
    if sandbox is not None:
        ctx.set("sandbox", sandbox)
        tools_service.guard(sandbox.make_guard())
    for guard in extra_guards:
        tools_service.guard(guard)
    if approval is not None:
        ctx.set("approval", approval)
    if interactions is not None:
        ctx.set("interactions", interactions)
    return ctx
