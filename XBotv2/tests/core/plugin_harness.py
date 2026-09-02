"""Shared helper: real XCore context with capability services for plugin tests."""

from __future__ import annotations


def mount_ctx(state_store):
    """Real XCore context with the capability services, for plugin tests."""
    from xcore import Context
    from XBotv2.agentloop.tool_service import ToolsService
    from XBotv2.agents.catalog import AgentCatalog
    from XBotv2.commands.plugin import CommandsService
    from XBotv2.prompts.plugin import PromptsService
    from XBotv2.agentloop.tool_registry import ToolRegistry
    from XBotv2.context_builder.builder import ContextBuilder
    from XBotv2.jobs.registry import JobRegistry
    from XBotv2.core.variables import RuntimeVariables

    class TestInteractions:
        async def request_user_input(self, *_args, **_kwargs):
            raise AssertionError("test interaction was not configured")

    class TestUsage:
        def __init__(self) -> None:
            self.records = []
            self.context_updates = []

        async def add(self, usage, *, update_context=True):
            self.records.append((dict(usage), update_context))
            if not usage:
                return None
            from XBotv2.core.usage import UsageData

            event = UsageData.from_provider(usage).to_event_dict()
            if not update_context:
                event["context_tokens"] = 0
            return event

        async def update_context(self, context_tokens):
            self.context_updates.append(context_tokens)
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
                "context_tokens": context_tokens,
            }

    ctx = Context(
        data_dir=state_store.paths.plugin_state_dir,
        state_service=state_store.state,
    )
    ctx.set("tools", ToolsService(ToolRegistry()))
    ctx.set("commands", CommandsService())
    ctx.set("prompts", PromptsService(ContextBuilder()))
    ctx.set("agent_catalog", AgentCatalog())
    ctx.set("jobs", JobRegistry())
    ctx.set("interactions", TestInteractions())
    ctx.set("usage", TestUsage())
    ctx.set("variables", RuntimeVariables())
    ctx.set("workspace_root", state_store.workspace_root)
    ctx.set("data_root", state_store.paths.runtime.data_dir)
    ctx.set("runtime_paths", state_store.paths.runtime)
    ctx.set("session", None)
    ctx.set("runtime", None)
    ctx.set("paths", state_store.paths)
    from XBotv2.agentloop import LoopState
    from XBotv2.session import SessionInfo
    from XBotv2.llm.service import ModelService
    from XBotv2.sandbox.policy import SandboxPolicy

    ctx.set("model", ModelService())
    ctx.set("artifacts", state_store.artifacts)
    ctx.set("thread_persistence", state_store)
    ctx.set("thread_paths", state_store.paths)
    ctx.set("loop_state", LoopState(
        session=SessionInfo(
            session_id=state_store.session_id,
            thread_id=state_store.thread_id,
            workspace_root=str(state_store.workspace_root),
            provider="default",
        ),
    ))
    ctx.set("session", ctx.loop_state.session)
    ctx.set("sandbox", SandboxPolicy(
        {"enabled": False, "resources": []},
        data_root=state_store.paths.runtime.data_dir,
        workspace_root=state_store.workspace_root,
        session_root=state_store.paths.state_dir,
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
    from XBotv2.persistence.store import ThreadPersistence

    tmp = Path(tempfile.mkdtemp())
    store = ThreadPersistence.create(
        RuntimePaths.from_data_dir(tmp).session("s"),
        thread_id="t",
        workspace_root=str(tmp),
        provider="default",
    )
    return mount_plugin(plugin, store, config)
