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


def test_goal_registers_one_state_machine_tool_and_context_hook(state_store):
    plugin, setup = setup_plugin(state_store)

    assert list(setup.hooks) == [
        HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
        HookStage.ON_TURN_END,
        HookStage.BEFORE_MAILBOX_DELIVERY,
    ]
    assert list(setup.tools) == ["goal"]
    tool = setup.tools["goal"]
    assert tool.parameters["properties"]["action"]["enum"] == [
        "block", "clear", "complete", "create", "get", "pause", "resume", "update",
    ]
    assert tool.parameters["additionalProperties"] is False
    assert setup.options["goal"].namespace == "plugin:goal"
    assert plugin.diagnostics() == {
        "status": "ready",
        "scope": "session",
        "goal_statuses": ["active", "blocked", "complete", "paused"],
        "automatic_continuation": True,
    }


@pytest.mark.asyncio
async def test_goal_lifecycle_keeps_summary_until_clear(state_store):
    plugin = make_plugin(state_store)

    empty = await plugin.goal("get")
    created = await plugin.goal("create", objective="stabilize the API", token_budget=8000)
    duplicate = await plugin.goal("create", objective="replace implicitly")
    updated = await plugin.goal(
        "update",
        objective="document the API",
        summary="Implementation complete; documentation remains.",
    )
    missing_summary = await plugin.goal("complete")
    completed = await plugin.goal("complete", summary="Documented and tested the API.")
    inspected = await plugin.goal("get")
    resumed = await plugin.goal("resume")
    blocked = await plugin.goal("block", summary="Waiting for human review.")
    viewed_blocked = await plugin.goal("get")
    cleared = await plugin.goal("clear")

    assert empty.data == {"goal": None}
    assert created.data["goal"]["token_budget"] == 8000
    assert duplicate.error.code == "goal_exists"
    assert updated.data["goal"]["objective"] == "document the API"
    assert updated.data["goal"]["summary"] == (
        "Implementation complete; documentation remains."
    )
    assert missing_summary.error.code == "invalid_summary"
    assert completed.data["goal"] == {
        "objective": "document the API",
        "status": "complete",
        "summary": "Documented and tested the API.",
        "token_budget": 8000,
    }
    assert inspected.data == completed.data
    assert resumed.data["goal"]["status"] == "active"
    assert blocked.data["goal"]["status"] == "blocked"
    assert viewed_blocked.data == blocked.data
    assert cleared.data == {"goal": None}
    assert (await plugin.goal("get")).data == {"goal": None}


@pytest.mark.asyncio
async def test_goal_rejects_invalid_transitions_without_mutating_state(state_store):
    plugin = make_plugin(state_store)
    await plugin.goal("create", objective="keep this objective")
    before = await plugin.store.all()

    invalid_action = await plugin.goal("restart")
    blank_update = await plugin.goal("update", objective=" ")
    missing_update = await plugin.goal("update")
    long_summary = await plugin.goal("complete", summary="x" * 2_001)
    bad_budget = await plugin.goal("create", objective="another", token_budget=0)
    update_budget = await plugin.goal("update", token_budget=4096)

    assert invalid_action.error.code == "invalid_action"
    assert blank_update.error.code == "invalid_objective"
    assert missing_update.error.code == "invalid_update"
    assert long_summary.error.code == "summary_too_long"
    assert bad_budget.error.code == "invalid_token_budget"
    assert update_budget.error.code == "invalid_arguments"
    assert await plugin.store.all() == before


@pytest.mark.asyncio
async def test_active_goal_schedules_one_continuation_at_a_time(state_store):
    plugin = make_plugin(state_store)
    await plugin.goal("create", objective="iterate until complete")
    queued = []

    async def enqueue(message):
        queued.append(message)

    turn_end = HookContext(
        stage=HookStage.ON_TURN_END,
        session=SimpleNamespace(),
        stop_reason="completed",
        enqueue_mailbox=enqueue,
    )
    await plugin._on_turn_end(turn_end)
    await plugin._on_turn_end(turn_end)

    assert len(queued) == 1
    assert queued[0]["source"] == "goal"
    assert queued[0]["event"] == "continue"

    await plugin._on_mailbox_delivery(HookContext(
        stage=HookStage.BEFORE_MAILBOX_DELIVERY,
        session=SimpleNamespace(),
        mailbox_message=SimpleNamespace(kind="general", message=queued[0]),
    ))
    await plugin._on_turn_end(turn_end)
    assert len(queued) == 2


@pytest.mark.asyncio
async def test_interrupt_pauses_goal_without_scheduling_continuation(state_store):
    plugin = make_plugin(state_store)
    await plugin.goal("create", objective="pause on escape")
    queued = []

    await plugin._on_turn_end(HookContext(
        stage=HookStage.ON_TURN_END,
        session=SimpleNamespace(),
        stop_reason="client_interrupt",
        enqueue_mailbox=queued.append,
    ))

    assert queued == []
    assert (await plugin.goal("get")).data["goal"]["status"] == "paused"


@pytest.mark.asyncio
async def test_complete_goal_remains_in_context_for_final_summary(state_store):
    plugin = make_plugin(state_store)
    components = [ContextComponent(role="system", source="system_prefix", content="base")]
    await plugin.goal("create", objective="output two greetings")
    await plugin.goal("complete", summary="Output both requested greetings.")
    ctx = HookContext(
        stage=HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
        session=SimpleNamespace(),
        context_components=components,
    )

    await plugin._add_goal_context(ctx)

    content = ctx.context_components[-1].content
    assert "Status: complete" in content
    assert "Execution summary: Output both requested greetings." in content
    assert "Do not restart or continue its work" in content
    assert "concise final summary" in content


@pytest.mark.asyncio
async def test_goal_survives_state_store_recreation(state_store):
    plugin = make_plugin(state_store)
    await plugin.goal("create", objective="survive restart")
    await plugin.goal("complete", summary="Restart behavior verified.")

    restored_store = CoreStateStore(
        paths=state_store.paths,
        thread_id=state_store.thread_id,
        workspace_root=state_store.workspace_root,
        provider=state_store.provider,
    )
    restored = make_plugin(restored_store)

    assert (await restored.goal("get")).data["goal"] == {
        "objective": "survive restart",
        "status": "complete",
        "summary": "Restart behavior verified.",
        "token_budget": None,
    }


@pytest.mark.asyncio
async def test_goal_rejects_invalid_persisted_state(state_store):
    plugin = make_plugin(state_store)
    invalid = {"objective": "broken", "status": "unknown"}
    await plugin.store.set("goal", invalid)

    with pytest.raises(ValueError, match="Goal state is invalid"):
        await plugin.goal("get")

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
    await registry.get("goal").tool.ainvoke({
        "action": "create", "objective": "retain me",
    })

    assert await loader.unload("goal") is True
    assert registry.registered_names() == []
    assert hooks._hooks.get(HookStage.AFTER_CONTEXT_COMPONENTS_BUILD, []) == []
    assert state_store.get_plugin_state("goal")["goal"]["objective"] == "retain me"


@pytest.mark.asyncio
async def test_engine_sees_completed_goal_after_tool_call(
    state_store,
    temp_workspace,
):
    plugin, setup = setup_plugin(state_store)
    await plugin.goal("create", objective="finish this turn")
    hooks = HookManager()
    hooks.register(
        HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
        setup.hooks[HookStage.AFTER_CONTEXT_COMPONENTS_BUILD],
    )
    registry = ToolRegistry()
    registry.register(
        setup.tools["goal"],
        sandbox_mode="host",
        namespace="plugin:goal",
    )
    llm = MockLLM(responses=[
        {
            "content": "Finished the requested work.",
            "tool_calls": [{
                "id": "goal-call-1",
                "name": "goal",
                "args": {"action": "complete", "summary": "All work passed."},
            }],
        },
        {"content": "The goal is complete; all required work passed."},
    ])
    engine = Engine(
        llm=llm,
        tool_registry=registry,
        hook_manager=hooks,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
        permission_system=PermissionSystem(default_decision="allow"),
        config=None,
    )
    await engine.start_session()

    events = [event async for event in engine.run_turn("finish the goal")]
    second_context = [message.content for message in llm.get_call_messages(1)]
    tool_event = next(event for event in events if event["type"] == "tool_result")

    assert any("Status: complete" in text for text in second_context)
    assert any("Do not restart or continue its work" in text for text in second_context)
    assert tool_event["data"]["data"]["goal"]["summary"] == "All work passed."
    assert llm.call_count == 2
