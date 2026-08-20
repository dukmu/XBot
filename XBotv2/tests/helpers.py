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
    tools_service = ctx.get("tools", strict=False)
    if tools_service is None:
        tools_service = ToolsService(registry, events=ctx)
        ctx.set("tools", tools_service)
    if permissions is not None:
        from XBotv2.permissions.guard import make_permission_guard

        if ctx.get("permissions", strict=False) is None:
            ctx.set("permissions", permissions)
        tools_service.guard(make_permission_guard(
            permissions,
            approval,
            ctx.emit,
        ))
    if sandbox is not None:
        if ctx.get("sandbox", strict=False) is None:
            ctx.set("sandbox", sandbox)
        tools_service.guard(sandbox.make_guard())
    for guard in extra_guards:
        tools_service.guard(guard)
    if approval is not None:
        ctx.set("approval", approval)
    if interactions is not None:
        ctx.set("interactions", interactions)
    return ctx


def make_engine(
    *,
    llm,
    tool_registry,
    plugin_ctx,
    state_store,
    sandbox_policy=None,
    permission_system=None,
    config=None,
    context_builder=None,
):
    """Build the current Engine from the pre-refactor test composition.

    The concrete loop driver now receives only provider-neutral core ports:
    a model client, a tool service, an event port, loop state, and settings.
    This helper maps the old test inputs (LLM, registry, hook context, store,
    policy objects, runtime config) onto those ports the way application
    composition does.
    """
    from XBotv2.agentloop.engine import Engine
    from XBotv2.config.models import RuntimeConfig
    from XBotv2.agentloop import LoopSettings, LoopState
    from XBotv2.core.runtime import SessionInfo
    from XBotv2.permissions.system import PermissionSystem
    from XBotv2.sandbox.policy import SandboxPolicy

    if sandbox_policy is None:
        sandbox_policy = SandboxPolicy(
            enabled=False,
            workspace_root=str(state_store.workspace_root),
        )
    if permission_system is None:
        permission_system = PermissionSystem(default_decision="allow")
    events = make_tool_ctx(
        tool_registry,
        sandbox=sandbox_policy,
        permissions=permission_system,
        base=plugin_ctx,
    )
    runtime_config = config or RuntimeConfig()
    state = LoopState(
        session=SessionInfo(
            session_id=state_store.session_id,
            thread_id=state_store.thread_id,
            workspace_root=str(state_store.workspace_root),
            provider="default",
        ),
        media_root=str(state_store.root),
    )
    settings = LoopSettings(
        provider="default",
        model="mock",
        context_window=runtime_config.max_context_tokens,
        max_output_tokens=runtime_config.max_output_tokens or 0,
        agent_name=runtime_config.agent_name,
        agent_role=runtime_config.agent_role,
        developer_instructions=runtime_config.instructions,
        agent_instructions=runtime_config.agent_instructions,
        memory=runtime_config.memory,
        workspace=str(state_store.workspace_root),
    )
    from XBotv2.context_builder.builder import ContextBuilder
    from XBotv2.agentloop import EventContext, Events

    builder = ContextBuilder()

    async def _build_context(event: Any) -> None:
        components = builder.build_components(**dict(event.context_kwargs or {}))
        component_event = EventContext(
            messages=event.messages,
            session=event.session,
            context_components=components,
        )
        await events.emit(
            Events.AFTER_CONTEXT_COMPONENTS_BUILD,
            component_event,
        )
        components = component_event.context_components or components
        event.context_components = components
        event.context_messages = builder.messages_from_components(components)

    events.on(Events.CONTEXT_BUILD, _build_context)
    return Engine(
        model_client=llm,
        tools=events.tools,
        events=events,
        state=state,
        settings=settings,
    )
