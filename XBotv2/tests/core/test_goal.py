"""Behavior tests for the built-in Goal plugin."""

import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
import yaml

from XBotv2.goal.plugin import GoalPlugin
from XBotv2.core import ContextComponent, EventContext, Events
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.agentloop.engine import Engine
from XBotv2.config.models import RuntimeConfig
from XBotv2.llm.mock import MockLLM
from XBotv2.agentloop.inbox import InboxMessage
from XBotv2.persistence.store import CoreStateStore
from plugin_harness import mount_ctx, mount_plugin
from XBotv2.permissions.system import PermissionSystem
from XBotv2.tools.registry import ToolRegistry
from XBotv2.sandbox.policy import SandboxPolicy


def _mount(plugin, state_store):
    return mount_plugin(plugin, state_store)


class SetupContext:
    """Post-apply view of a plugin's registrations on a real XCore context."""

    def __init__(self, plugin) -> None:
        self.ctx = plugin.ctx
        self.tools: dict = {}
        self.options: dict = {}
        self.commands: dict = {}
        for entry in self.ctx.tools.registry.registered_entries():
            self.tools[entry.tool.name] = entry.tool
            self.options[entry.tool.name] = _EntryOptions(
                namespace=entry.namespace,
                sandbox_mode=entry.sandbox_mode,
            )
        for command in self.ctx.commands.all():
            self.commands[command.name] = command


class _EntryOptions:
    def __init__(self, *, namespace, sandbox_mode) -> None:
        self.namespace = namespace
        self.sandbox_mode = sandbox_mode


def make_plugin(state_store) -> GoalPlugin:
    from XBotv2.goal.plugin import GoalPlugin

    return _mount(GoalPlugin(), state_store)


def setup_plugin(state_store):
    plugin = make_plugin(state_store)
    return plugin, SetupContext(plugin)


def test_goal_registers_human_command_and_agent_tools(state_store):
    _plugin, setup = setup_plugin(state_store)

    assert setup.ctx._bus.listener_count(Events.TURN_START) > 0
    assert setup.ctx._bus.listener_count(Events.TURN_END) > 0
    assert list(setup.tools) == ["create_goal", "get_goal", "update_goal"]
    assert setup.tools["update_goal"].parameters["properties"]["status"]["enum"] == [
        "complete", "blocked",
    ]
    assert all(
        setup.options[name].sandbox_mode == "host" for name in setup.tools
    )
    assert list(setup.commands) == ["goal"]
    assert setup.commands["goal"].kind == "server"


@pytest.mark.asyncio
async def test_goal_lifecycle_keeps_summary_until_clear(state_store):
    plugin = make_plugin(state_store)
    queued = []

    async def enqueue():
        queued.append(True)

    ctx = SimpleNamespace(request_continuation=enqueue)

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
        SimpleNamespace(request_continuation=None),
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
    requests = []

    async def request_continuation():
        requests.append(True)

    turn_end = EventContext(
        session=SimpleNamespace(),
        stop_reason="completed",
        request_continuation=request_continuation,
    )
    await plugin._on_turn_end(turn_end)
    await plugin._on_turn_end(turn_end)

    assert len(requests) == 1

    # The continuation turn starting resets the pending flag; the next
    # completed turn schedules another continuation.
    await plugin._start_goal_turn(EventContext(
        session=SimpleNamespace(),
        user_input="[goal continuation]",
        continuation=True,
    ))
    await plugin._on_turn_end(turn_end)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_runtime_notification_does_not_drive_active_goal(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_goal("iterate until complete")
    requests = []

    async def request_continuation():
        requests.append(True)

    await plugin._on_turn_end(EventContext(
        session=SimpleNamespace(),
        stop_reason="completed",
        request_continuation=request_continuation,
    ))

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_goal_exposes_compact_status_slot(state_store):
    plugin = make_plugin(state_store)

    assert await plugin.status_slots() == {}
    await plugin.create_goal("show status")
    assert await plugin.status_slots() == {"goal": "active"}
    await plugin.update_goal("complete", "done")
    assert await plugin.status_slots() == {"goal": "complete"}


@pytest.mark.asyncio
async def test_interrupt_pauses_goal_without_scheduling_continuation(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_goal("pause on escape")
    requests = []

    async def request_continuation():
        requests.append(True)

    await plugin._on_turn_end(EventContext(
        session=SimpleNamespace(),
        stop_reason="client_interrupt",
        request_continuation=request_continuation,
    ))

    assert requests == []
    assert (await plugin.get_goal()).data["goal"]["status"] == "paused"


@pytest.mark.asyncio
async def test_goal_snapshot_is_added_only_to_continuation_turn(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_goal("output two greetings")
    active_ctx = EventContext(
        session=SimpleNamespace(),
        user_input="wake",
        continuation=True,
    )

    await plugin._start_goal_turn(active_ctx)

    assert "output two greetings" in active_ctx.user_input

    await plugin.update_goal("complete", "Output both requested greetings.")
    ctx = EventContext(
        session=SimpleNamespace(),
        user_input="wake",
        continuation=False,
    )

    await plugin._start_goal_turn(ctx)

    assert ctx.user_input == "wake"


@pytest.mark.asyncio
async def test_goal_continuation_turn_replaces_prompt_with_goal_context(
    state_store,
    temp_workspace,
):
    plugin, setup = setup_plugin(state_store)
    await plugin.create_goal("finish the audit")
    llm = MockLLM(responses=[
        {"content": "Working on the audit."},
        {"content": "Plain reply."},
    ])
    engine = Engine(
        llm=llm,
        tool_registry=ToolRegistry(),
        plugin_ctx=setup.ctx,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(),
    )

    # A continuation turn gets the active goal context as its prompt.
    engine.continuation = True
    _ = [
        event
        async for event in engine.run_turn("[goal continuation]", request_id="goal")
    ]
    last = llm.get_call_messages(0)[-1]
    assert last.role == "user"
    assert json.loads(last.content) == {
        "objective": "finish the audit",
        "status": "active",
    }
    assert all(message.role != "user" for message in llm.get_call_messages(0)[:-1])

    # A normal turn keeps its own prompt.
    engine.continuation = False
    _ = [event async for event in engine.run_turn("plain wake", request_id="plain")]
    assert llm.get_call_messages(1)[-1].content == "plain wake"


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
    plugins_root.mkdir()
    (plugins_root / "goal").symlink_to(
        Path(__file__).parents[2] / "goal",
        target_is_directory=True,
    )
    from XBotv2.loader import Loader, PluginTree

    ctx = mount_ctx(state_store)
    registry = ctx.tools.registry
    loader = Loader(ctx, tree=PluginTree.from_dict([
        {"id": "goal", "name": "goal"},
    ]))

    await loader.load()
    assert isinstance(loader.get("goal"), GoalPlugin)
    await registry.get("create_goal").tool.ainvoke({"objective": "retain me"})

    assert await loader.unload("goal") is True
    assert registry.registered_names() == []
    import json as _json
    state_path = state_store.paths.state_dir / "state.json"
    data = _json.loads(state_path.read_text(encoding="utf-8"))
    assert data["goal.goal"]["objective"] == "retain me"


@pytest.mark.asyncio
async def test_engine_summarizes_completed_goal_without_persistent_context(
    state_store,
    temp_workspace,
):
    plugin, setup = setup_plugin(state_store)
    await plugin.create_goal("finish this turn")
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
        plugin_ctx=setup.ctx,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(),
    )
    await engine.start_session()

    events = [event async for event in engine.run_turn("finish the goal")]
    second_context = llm.get_call_messages(1)
    tool_event = next(event for event in events if event["type"] == "tool_result")

    assert not any(_is_goal_runtime_event(message) for message in second_context)
    assert tool_event["data"]["data"]["goal"]["summary"] == "All work passed."
    assert llm.call_count == 2

    _ = [event async for event in engine.run_turn("start an unrelated request")]
    third_context = llm.get_call_messages(2)

    assert any(
        "start an unrelated request" in message.content
        for message in third_context
    )
    assert not any(_is_goal_runtime_event(message) for message in third_context)


def _is_goal_runtime_event(message) -> bool:
    if message.role != "user":
        return False
    try:
        event = ET.fromstring(message.content)
    except ET.ParseError:
        return False
    return event.tag == "runtime_event" and event.attrib.get("source") == "goal"
