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
from xbotv2.core.mailbox import MailboxMessage
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
        self.commands = {}

    def register_hook(self, stage, callback):
        self.hooks[stage] = callback

    def register_tool(self, tool, options=None):
        self.tools[tool.name] = tool
        self.options[tool.name] = options
        return f"plugin:goal:{tool.name}"

    def register_command(self, command):
        self.commands[command.name] = command
        return command.name



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


def test_goal_registers_human_command_and_agent_tools(state_store):
    plugin, setup = setup_plugin(state_store)

    assert list(setup.hooks) == [
        HookStage.ON_TURN_START,
        HookStage.ON_TURN_END,
        HookStage.BEFORE_TOOL_CALL,
        HookStage.BEFORE_MAILBOX_DELIVERY,
    ]
    assert list(setup.tools) == ["create_goal", "get_goal", "update_goal"]
    assert setup.tools["update_goal"].parameters["properties"]["status"]["enum"] == [
        "complete", "blocked",
    ]
    assert all(
        setup.options[name].namespace == "plugin:goal" for name in setup.tools
    )
    assert list(setup.commands) == ["goal"]
    assert setup.commands["goal"].kind == "server"
    assert plugin.diagnostics() == {
        "status": "ready",
        "scope": "session",
        "goal_statuses": ["active", "blocked", "complete", "paused"],
        "automatic_continuation": True,
    }


@pytest.mark.asyncio
async def test_goal_lifecycle_keeps_summary_until_clear(state_store):
    plugin = make_plugin(state_store)
    queued = []

    async def enqueue(message):
        queued.append(message)

    ctx = SimpleNamespace(enqueue_general=enqueue)

    empty = await plugin.get_goal()
    created = await plugin.create_goal("stabilize the API", token_budget=8000)
    duplicate = await plugin.create_goal("replace implicitly")
    updated = await plugin._goal_command(ctx, "document the API")
    missing_summary = await plugin.update_goal("complete", "")
    completed = await plugin.update_goal("complete", "Documented and tested the API.")
    inspected = await plugin.get_goal()
    resumed = await plugin._goal_command(ctx, "resume")
    blocked = await plugin.update_goal("blocked", "Waiting for human review.")
    viewed_blocked = await plugin.get_goal()
    cleared = await plugin._goal_command(ctx, "clear")

    assert empty.data == {"goal": None}
    assert created.data["goal"]["token_budget"] == 8000
    assert duplicate.error.code == "goal_exists"
    assert updated.data["goal"]["objective"] == "document the API"
    assert updated.data["goal"]["summary"] == ""
    assert missing_summary.error.code == "invalid_summary"
    assert completed.data["goal"] == {
        "objective": "document the API",
        "status": "complete",
        "summary": "Documented and tested the API.",
        "token_budget": None,
    }
    assert inspected.data == completed.data
    assert resumed.data["goal"]["status"] == "active"
    assert blocked.data["goal"]["status"] == "blocked"
    assert viewed_blocked.data == blocked.data
    assert cleared.data == {"goal": None}
    assert (await plugin.get_goal()).data == {"goal": None}


@pytest.mark.asyncio
async def test_goal_rejects_invalid_transitions_without_mutating_state(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_goal("keep this objective")
    before = await plugin.store.all()

    invalid_status = await plugin.update_goal("paused", "not allowed")
    blank_create = await plugin.create_goal(" ")
    missing_summary = await plugin.update_goal("complete", "")
    long_summary = await plugin.update_goal("complete", "x" * 2_001)
    bad_budget = await plugin.create_goal("another", token_budget=0)
    bad_command_budget = await plugin._goal_command(
        SimpleNamespace(enqueue_general=None),
        "--token-budget nope another objective",
    )

    assert invalid_status.error.code == "invalid_status"
    assert blank_create.error.code == "invalid_objective"
    assert missing_summary.error.code == "invalid_summary"
    assert long_summary.error.code == "summary_too_long"
    assert bad_budget.error.code == "invalid_token_budget"
    assert bad_command_budget.status == "error"
    assert "positive integer" in bad_command_budget.message
    assert await plugin.store.all() == before


@pytest.mark.asyncio
async def test_active_goal_schedules_one_continuation_at_a_time(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_goal("iterate until complete")
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
    await plugin.create_goal("pause on escape")
    queued = []

    await plugin._on_turn_end(HookContext(
        stage=HookStage.ON_TURN_END,
        session=SimpleNamespace(),
        stop_reason="client_interrupt",
        enqueue_mailbox=queued.append,
    ))

    assert queued == []
    assert (await plugin.get_goal()).data["goal"]["status"] == "paused"


@pytest.mark.asyncio
async def test_goal_snapshot_is_added_only_to_goal_mailbox_turn(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_goal("output two greetings")
    active_ctx = HookContext(
        stage=HookStage.ON_TURN_START,
        session=SimpleNamespace(),
        user_input="wake",
        mailbox_message=SimpleNamespace(
            kind="general",
            message={"source": "goal", "event": "continue"},
        ),
    )

    await plugin._start_goal_turn(active_ctx)

    assert "## Session Goal" in active_ctx.user_input
    assert "output two greetings" in active_ctx.user_input

    await plugin.update_goal("complete", "Output both requested greetings.")
    ctx = HookContext(
        stage=HookStage.ON_TURN_START,
        session=SimpleNamespace(),
        user_input="wake",
        mailbox_message=active_ctx.mailbox_message,
    )

    await plugin._start_goal_turn(ctx)

    assert ctx.user_input == "wake"


@pytest.mark.asyncio
async def test_goal_mailbox_snapshot_is_turn_scoped_and_delivery_is_journaled(
    state_store,
    temp_workspace,
):
    plugin, setup = setup_plugin(state_store)
    await plugin.create_goal("finish the audit")
    hooks = HookManager()
    hooks.register(HookStage.ON_TURN_START, setup.hooks[HookStage.ON_TURN_START])
    llm = MockLLM(responses=[{"content": "Working on the audit."}])
    engine = Engine(
        llm=llm,
        tool_registry=ToolRegistry(),
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
    item = MailboxMessage.create(
        "general",
        {"source": "goal", "event": "continue"},
    )

    _ = [
        event
        async for event in engine.run_turn(
            "Mailbox wake",
            mailbox_message=item,
        )
    ]

    request = llm.get_call_messages(0)
    assert "## Session Goal" in request[0].content
    assert "finish the audit" in request[0].content
    assert all(message.role != "user" for message in request)
    assert all("Session Goal" not in message.content for message in engine.messages)
    records = [
        yaml.safe_load(line)
        for line in state_store.messages_path.read_text(encoding="utf-8").splitlines()
    ]
    delivery = next(
        record for record in records
        if record.get("record_type") == "mailbox_delivery"
    )
    assert delivery["kind"] == "general"
    assert delivery["message"] == {"source": "goal", "event": "continue"}
    assert all(
        message.content != str(delivery["message"])
        for message in state_store.read_messages()
    )


@pytest.mark.asyncio
async def test_goal_survives_state_store_recreation(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_goal("survive restart")
    await plugin.update_goal("complete", "Restart behavior verified.")

    restored_store = CoreStateStore(
        paths=state_store.paths,
        thread_id=state_store.thread_id,
        workspace_root=state_store.workspace_root,
        provider=state_store.provider,
    )
    restored = make_plugin(restored_store)

    assert (await restored.get_goal()).data["goal"] == {
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
        await plugin.get_goal()

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
    assert hooks._hooks.get(HookStage.ON_TURN_START, []) == []
    assert state_store.get_plugin_state("goal")["goal"]["objective"] == "retain me"


@pytest.mark.asyncio
async def test_engine_summarizes_completed_goal_without_persistent_context(
    state_store,
    temp_workspace,
):
    plugin, setup = setup_plugin(state_store)
    await plugin.create_goal("finish this turn")
    hooks = HookManager()
    hooks.register(
        HookStage.ON_TURN_START,
        setup.hooks[HookStage.ON_TURN_START],
    )
    registry = ToolRegistry()
    registry.register(
        setup.tools["update_goal"],
        sandbox_mode="host",
        namespace="plugin:goal",
    )
    llm = MockLLM(responses=[
        {
            "content": "Finished the requested work.",
            "tool_calls": [{
                "id": "goal-call-1",
                "name": "update_goal",
                "args": {"status": "complete", "summary": "All work passed."},
            }],
        },
        {"content": "The goal is complete; all required work passed."},
        {"content": "Starting the unrelated request."},
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

    assert not any("## Session Goal" in text for text in second_context)
    assert any("Goal completed." in text for text in second_context)
    assert tool_event["data"]["data"]["goal"]["summary"] == "All work passed."
    assert llm.call_count == 2

    _ = [event async for event in engine.run_turn("start an unrelated request")]
    third_context = [message.content for message in llm.get_call_messages(2)]

    assert any("start an unrelated request" in text for text in third_context)
    assert not any("## Session Goal" in text for text in third_context)
