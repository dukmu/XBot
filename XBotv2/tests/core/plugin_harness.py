"""Shared helper: real XCore context with capability services for plugin tests."""

from __future__ import annotations


def mount_ctx(state_store):
    """Real XCore context with the capability services, for plugin tests."""
    from xcore import Context
    from xbotv2.plugins.tools import (
        AgentsService,
        CommandsService,
        PromptsService,
        ToolsService,
    )
    from xbotv2.tools.registry import ToolRegistry
    from xbotv2.core.context import ContextBuilder
    from xbotv2.core.agents import AgentRegistry
    from xbotv2.api.jobs import JobRegistry
    from xbotv2.api.variables import RuntimeVariables

    ctx = Context(data_dir=state_store.paths.state_dir)
    ctx.set("tools", ToolsService(ToolRegistry()))
    ctx.set("commands", CommandsService())
    ctx.set("prompts", PromptsService(ContextBuilder()))
    ctx.set("agents", AgentsService(AgentRegistry()))
    ctx.set("job_registry", JobRegistry())
    ctx.set("variables", RuntimeVariables())
    ctx.set("workspace_root", state_store.workspace_root)
    ctx.set("data_root", state_store.paths.runtime.data_dir)
    ctx.set("session", None)
    ctx.set("runtime", None)
    ctx.set("agent_runtime", None)
    ctx.set("paths", state_store.paths)
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

    from xbotv2.api.paths import RuntimePaths
    from xbotv2.persistence.store import CoreStateStore

    tmp = Path(tempfile.mkdtemp())
    store = CoreStateStore.create(
        RuntimePaths.from_data_dir(tmp).session("s"),
        thread_id="t",
        workspace_root=str(tmp),
        provider="default",
    )
    return mount_plugin(plugin, store, config)
