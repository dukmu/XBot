"""Behavior tests for the built-in Goal plugin."""

from types import SimpleNamespace

import pytest
import yaml

from builtin_plugins.goal.plugin import GoalPlugin
from xbotv2.api import ContextComponent, HookContext, HookStage, PluginManifest
from xbotv2.core.context import ContextBuilder
from xbotv2.core.engine import Engine
from xbotv2.hooks.manager import HookManager
from xbotv2.llm.mock import MockLLM
from xbotv2.persistence.store import CoreStateStore
from xbotv2.plugin.loader import PluginLoader
from xbotv2.plugin.store import PluginStore
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.sandbox import SandboxPolicy


class SetupContext:
    def __init__(self) -> None:
        self.hooks = {}
        self.tools = {}
        self.options = {}

    def register_hook(self, stage, callback):
        self.hooks[stage] = callback

    def register_tool(self, tool, options=None):
        self.tools[tool.name] = tool
        self.options[tool.name] = options
        return f"plugin:goal:{tool.name}"


def make_plugin(state_store) -> GoalPlugin:
    return GoalPlugin(
        PluginManifest(name="goal", version="1"),
        PluginStore(state_store, "goal"),
    )


def setup_plugin(state_store) -> tuple[GoalPlugin, SetupContext]:
    plugin = make_plugin(state_store)
    setup = SetupContext()
    plugin.setup(setup)
    return plugin, setup


def test_goal_registers_context_hook_and_five_host_tools(state_store):
    plugin, setup = setup_plugin(state_store)

    assert list(setup.hooks) == [HookStage.AFTER_CONTEXT_COMPONENTS_BUILD]
    assert list(setup.tools) == [
        "create_goal",
        "inspect_goal",
        "update_goal",
        "complete_goal",
        "abandon_goal",
    ]
    assert all(
        options.namespace == "plugin:goal" and options.sandbox_mode == "host"
        for options in setup.options.values()
    )
    assert plugin.diagnostics() == {
        "status": "ready",
        "scope": "session",
        "goal_statuses": ["abandoned", "active", "completed"],
        "automatic_continuation": False,
    }


@pytest.mark.asyncio
async def test_goal_lifecycle_requires_explicit_terminal_transition(state_store):
    plugin = make_plugin(state_store)

    empty = await plugin.inspect_goal()
    created = await plugin.create_goal("stabilize the API")
    duplicate = await plugin.create_goal("replace implicitly")
    updated = await plugin.update_goal("document the public API")
    completed = await plugin.complete_goal()
    terminal_update = await plugin.update_goal("should fail")
    inspected = await plugin.inspect_goal()
    replacement = await plugin.create_goal("ship Goal plugin")
    abandoned = await plugin.abandon_goal()

    assert empty.data == {"goal": None}
    assert created.data["goal"]["status"] == "active"
    assert duplicate.error.code == "goal_exists"
    assert updated.data["goal"]["objective"] == "document the public API"
    assert completed.data["goal"]["status"] == "completed"
    assert terminal_update.error.code == "no_active_goal"
    assert inspected.data == completed.data
    assert replacement.data["goal"] == {
        "objective": "ship Goal plugin",
        "status": "active",
    }
    assert abandoned.data["goal"]["status"] == "abandoned"


@pytest.mark.asyncio
async def test_invalid_goal_mutations_leave_state_unchanged(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_goal("keep this objective")
    before = await plugin.store.all()

    blank_create = await plugin.create_goal(" ")
    blank_update = await plugin.update_goal("")
    long_update = await plugin.update_goal("x" * 2_001)

    assert blank_create.error.code == "invalid_objective"
    assert blank_update.error.code == "invalid_objective"
    assert long_update.error.code == "objective_too_long"
    assert await plugin.store.all() == before


@pytest.mark.asyncio
async def test_only_active_goal_enters_context(state_store):
    plugin = make_plugin(state_store)
    components = [
        ContextComponent(role="system", source="system_prefix", content="base")
    ]
    ctx = HookContext(
        stage=HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
        session=SimpleNamespace(),
        context_components=components,
    )

    await plugin._add_active_goal_context(ctx)
    assert ctx.context_components == components

    await plugin.create_goal("preserve concise context")
    await plugin._add_active_goal_context(ctx)
    assert ctx.context_components[-1] == ContextComponent(
        role="system",
        source="plugin_fragment",
        content="## Active Goal\n\npreserve concise context",
        plugin_name="goal",
        stage="context_suffix",
    )

    await plugin.complete_goal()
    terminal_ctx = HookContext(
        stage=HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
        session=SimpleNamespace(),
        context_components=components,
    )
    await plugin._add_active_goal_context(terminal_ctx)
    assert terminal_ctx.context_components == components


@pytest.mark.asyncio
async def test_goal_survives_state_store_recreation(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_goal("survive restart")

    restored_store = CoreStateStore(
        paths=state_store.paths,
        thread_id=state_store.thread_id,
        workspace_root=state_store.workspace_root,
        provider=state_store.provider,
    )
    restored = make_plugin(restored_store)

    assert (await restored.inspect_goal()).data == {
        "goal": {"objective": "survive restart", "status": "active"}
    }


@pytest.mark.asyncio
async def test_goal_rejects_invalid_persisted_state(state_store):
    plugin = make_plugin(state_store)
    invalid = {"objective": "broken", "status": "unknown"}
    await plugin.store.set("goal", invalid)

    with pytest.raises(ValueError, match="Goal state is invalid"):
        await plugin.inspect_goal()

    assert await plugin.store.get("goal") == invalid


@pytest.mark.asyncio
async def test_loader_unload_removes_goal_resources_but_retains_state(
    tmp_path,
    state_store,
):
    plugins_root = tmp_path / "plugins"
    plugin_dir = plugins_root / "goal"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "goal", "version": "1.0.0"}),
        encoding="utf-8",
    )
    hooks = HookManager()
    registry = ToolRegistry()
    loader = PluginLoader(
        plugin_dirs=[plugins_root],
        state_store=state_store,
        hook_manager=hooks,
        tool_registry=registry,
        context_builder=ContextBuilder(),
    )

    loaded = await loader.load()
    assert isinstance(loaded[0], GoalPlugin)
    await registry.get("create_goal").tool.ainvoke({"objective": "retain me"})

    assert await loader.unload("goal") is True
    assert registry.registered_names() == []
    assert hooks._hooks.get(HookStage.AFTER_CONTEXT_COMPONENTS_BUILD, []) == []
    assert state_store.get_plugin_state("goal")["goal"] == {
        "objective": "retain me",
        "status": "active",
    }


@pytest.mark.asyncio
async def test_engine_removes_completed_goal_from_same_turn_context(
    state_store,
    temp_workspace,
):
    plugin, setup = setup_plugin(state_store)
    await plugin.create_goal("finish this turn")
    hooks = HookManager()
    hooks.register(
        HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
        setup.hooks[HookStage.AFTER_CONTEXT_COMPONENTS_BUILD],
    )
    registry = ToolRegistry()
    for name, tool in setup.tools.items():
        options = setup.options[name]
        registry.register(
            tool,
            sandbox_mode=options.sandbox_mode,
            namespace=options.namespace,
        )
    llm = MockLLM(
        responses=[
            {
                "content": "finishing",
                "tool_calls": [
                    {"id": "goal-call-1", "name": "complete_goal", "args": {}}
                ],
            },
            {"content": "Goal completed."},
        ]
    )
    engine = Engine(
        llm=llm,
        tool_registry=registry,
        hook_manager=hooks,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=None,
    )
    await engine.start_session()

    events = [event async for event in engine.run_turn("finish the goal")]
    first_context = [message.content for message in llm.get_call_messages(0)]
    second_context = [message.content for message in llm.get_call_messages(1)]
    tool_event = next(event for event in events if event["type"] == "tool_result")

    assert any("## Active Goal\n\nfinish this turn" in text for text in first_context)
    assert not any("## Active Goal" in text for text in second_context)
    assert tool_event["data"]["data"] == {
        "goal": {"objective": "finish this turn", "status": "completed"}
    }
