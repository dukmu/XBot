"""Shared helper: real XCore context with capability services for plugin tests."""

from __future__ import annotations


def mount_ctx(state_store):
    """Real XCore context with the capability services, for plugin tests."""
    from xcore import Context
    from XBotv2.agentloop.tool_service import ToolsService
    from XBotv2.agents.service import AgentRegistry, AgentsService
    from XBotv2.commands.plugin import CommandsService
    from XBotv2.prompts.plugin import PromptsService
    from XBotv2.agentloop.tool_registry import ToolRegistry
    from XBotv2.context_builder.builder import ContextBuilder
    from XBotv2.jobs import JobRegistry
    from XBotv2.core.variables import RuntimeVariables

    ctx = Context(data_dir=state_store.paths.state_dir)
    ctx.set("tools", ToolsService(ToolRegistry()))
    ctx.set("commands", CommandsService())
    ctx.set("prompts", PromptsService(ContextBuilder()))
    ctx.set("agents", AgentsService(ctx, AgentRegistry()))
    ctx.set("jobs", JobRegistry())
    ctx.set("variables", RuntimeVariables())
    ctx.set("workspace_root", state_store.workspace_root)
    ctx.set("data_root", state_store.paths.runtime.data_dir)
    ctx.set("session", None)
    ctx.set("runtime", None)
    ctx.set("paths", state_store.paths)
    from XBotv2.core.loop import LoopState
    from XBotv2.core.runtime import SessionInfo
    from XBotv2.llm.service import ModelService
    from XBotv2.sandbox.policy import SandboxPolicy

    ctx.set("model", ModelService())
    ctx.set("storage", state_store)
    ctx.set("thread_paths", state_store.paths)
    ctx.set("loop_state", LoopState(
        session=SessionInfo(
            session_id=state_store.session_id,
            thread_id=state_store.thread_id,
            workspace_root=str(state_store.workspace_root),
            provider="default",
        ),
        media_root=str(state_store.root),
    ))
    ctx.set("sandbox", SandboxPolicy(
        {"enabled": False, "resources": []},
        data_root=state_store.paths.runtime.data_dir,
        workspace_root=state_store.workspace_root,
        session_root=state_store.root,
        variables=ctx.variables,
    ))
    return ctx


def mount_plugin(plugin, state_store, config=None):
    """Bind a plugin to a real XCore context and run its apply body.

    Mirrors the loader's adapter binding (ctx + namespaced store) for unit
    tests that drive the plugin directly.  The plugin registers its event
    listeners and capability registrations on ``ctx``, so tests drive them
    through ``plugin.ctx`` (``ctx.on`` listeners, ``ctx.serial``/``ctx.emit``
    dispatches).
    """
    ctx = mount_ctx(state_store)
    plugin.ctx = ctx
    plugin.apply(ctx, config)
    return plugin


def mount_plugin_standalone(plugin, config=None):
    """Mount a plugin on a fresh temporary XCore context (unit tests)."""
    import tempfile
    from pathlib import Path

    from XBotv2.core.paths import RuntimePaths
    from XBotv2.persistence.store import CoreStateStore

    tmp = Path(tempfile.mkdtemp())
    store = CoreStateStore.create(
        RuntimePaths.from_data_dir(tmp).session("s"),
        thread_id="t",
        workspace_root=str(tmp),
        provider="default",
    )
    return mount_plugin(plugin, store, config)
