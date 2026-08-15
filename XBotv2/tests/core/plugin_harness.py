"""Shared helper: real XCore context with bridge services for plugin tests."""

from __future__ import annotations


def mount_ctx(state_store):
    """Real XCore context with the bridge services, for plugin tests."""
    from xcore import Context
    from xbotv2.plugin.bridge import register_core_services
    from xbotv2.tools.registry import ToolRegistry
    from xbotv2.core.context import ContextBuilder
    from xbotv2.core.agents import AgentRegistry
    from xbotv2.api.jobs import JobRegistry
    from xbotv2.api.variables import RuntimeVariables

    ctx = Context(data_dir=state_store.paths.state_dir)
    register_core_services(
        ctx,
        tool_registry=ToolRegistry(),
        context_builder=ContextBuilder(),
        agent_registry=AgentRegistry(),
        job_registry=JobRegistry(),
        runtime_variables=RuntimeVariables(),
        workspace_root=state_store.workspace_root,
        data_root=state_store.paths.runtime.data_dir,
        session=None,
        runtime_config=None,
        agent_runtime=None,
        paths=state_store.paths,
    )
    return ctx


def mount_plugin(plugin, state_store):
    """Bind a plugin to a real XCore context and run its apply body.

    Mirrors the loader's PluginAdapter binding (ctx + namespaced store) for
    unit tests that drive the plugin directly.
    """
    ctx = mount_ctx(state_store)
    plugin.ctx = ctx
    plugin.store = ctx.state.namespace(plugin.manifest.name)
    plugin.apply(ctx)
    return plugin


def mount_plugin_standalone(plugin):
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
    return mount_plugin(plugin, store)
