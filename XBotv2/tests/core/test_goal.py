"""Behavior tests for the built-in Goal plugin."""

from XBotv2.tests.helpers import make_engine

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
import yaml

from XBotv2.goal.plugin import GoalPlugin, GoalService
from XBotv2.application import COLLECT_STATUS_SLOTS, StatusSlots
from XBotv2.agentloop import EventContext, Events
from XBotv2.context_builder import ContextComponent
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.agentloop.engine import Engine
from XBotv2.config.models import RuntimeConfig
from XBotv2.llm.mock import MockLLM
from XBotv2.persistence.store import ThreadPersistence
from plugin_harness import mount_ctx, mount_plugin
from XBotv2.permissions.system import PermissionSystem
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.sandbox.policy import SandboxPolicy


class RecordingDriver:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def followup(self, content: str, **kwargs: object) -> None:
        self.requests.append((content, kwargs))


@dataclass
class GoalHarness:
    service: GoalService
    ctx: object
    driver: RecordingDriver
    store: object


class SetupContext:
    """Post-apply view of a plugin's registrations on a real XCore context."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.tools: dict = {}
        self.options: dict = {}
        self.commands: dict = {}
        for entry in self.ctx.tools.registrations():
            self.tools[entry.tool.name] = entry.tool
            self.options[entry.tool.name] = _EntryOptions(
                namespace=entry.namespace,
            )
        for command in self.ctx.commands.all():
            self.commands[command.name] = command


class _EntryOptions:
    def __init__(self, *, namespace) -> None:
        self.namespace = namespace


def make_plugin(state_store) -> GoalHarness:
    ctx = mount_ctx(state_store)
    driver = RecordingDriver()
    ctx.set("engine", driver)
    GoalPlugin().apply(ctx)
    return GoalHarness(
        service=ctx.goal,
        ctx=ctx,
        driver=driver,
        store=ctx.state.namespace("goal"),
    )


def setup_plugin(state_store):
    plugin = make_plugin(state_store)
    return plugin, SetupContext(plugin.ctx)


def test_goal_registers_human_command_and_agent_tools(state_store):
    _plugin, setup = setup_plugin(state_store)

    assert setup.ctx._bus.listener_count(Events.TURN_START) > 0
    assert setup.ctx._bus.listener_count(Events.TURN_END) > 0
    assert list(setup.tools) == ["create_goal", "get_goal", "update_goal"]
    assert setup.tools["update_goal"].parameters["properties"]["status"]["enum"] == [
        "complete", "blocked",
    ]
    assert list(setup.commands) == ["goal"]
    assert setup.commands["goal"].kind == "server"


@pytest.mark.asyncio
async def test_goal_lifecycle_keeps_summary_until_clear(state_store):
    plugin = make_plugin(state_store)

    empty = await plugin.service.get_goal()
    created = await plugin.service.create_goal("stabilize the API", token_budget=8000)
    duplicate = await plugin.service.create_goal("replace implicitly")
    updated = await plugin.service.command("document the API")
    missing_summary = await plugin.service.update_goal("complete", "")
    completed = await plugin.service.update_goal("complete", "Documented and tested the API.")
    inspected = await plugin.service.get_goal()
    resumed = await plugin.service.command("resume")
    blocked = await plugin.service.update_goal("blocked", "Waiting for human review.")
    viewed_blocked = await plugin.service.get_goal()
    cleared = await plugin.service.command("clear")

    assert empty.status == "success"
    assert created.status == "success"
    assert duplicate.error.code == "goal_exists"
    assert updated.status == "ok"
    assert updated.message
    assert "document the API" in updated.message
    assert missing_summary.error.code == "invalid_summary"
    assert completed.status == "success"
    assert inspected.status == "success"
    assert resumed.status == "ok"
    assert blocked.status == "success"
    assert viewed_blocked.status == "success"
    assert cleared.status == "ok"
    assert (await plugin.service.get_goal()).status == "success"


@pytest.mark.asyncio
async def test_goal_rejects_invalid_transitions_without_mutating_state(state_store):
    plugin = make_plugin(state_store)
    await plugin.service.create_goal("keep this objective")
    before = await plugin.store.all()

    invalid_status = await plugin.service.update_goal("paused", "not allowed")
    blank_create = await plugin.service.create_goal(" ")
    missing_summary = await plugin.service.update_goal("complete", "")
    long_summary = await plugin.service.update_goal("complete", "x" * 2_001)
    bad_budget = await plugin.service.create_goal("another", token_budget=0)
    bad_command_budget = await plugin.service.command(
        "--token-budget nope another objective"
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
    await plugin.service.create_goal("iterate until complete")
    turn_end = EventContext(
        session=SimpleNamespace(),
        stop_reason="completed",
    )
    await plugin.service.on_turn_end(turn_end)
    await plugin.service.on_turn_end(turn_end)

    assert len(plugin.driver.requests) == 1

    # The continuation turn starting resets the pending flag; the next
    # completed turn schedules another continuation.
    await plugin.service.start_goal_turn(EventContext(
        session=SimpleNamespace(),
        user_input="[goal continuation]",
        continuation=True,
    ))
    await plugin.service.on_turn_end(turn_end)
    assert len(plugin.driver.requests) == 2


@pytest.mark.asyncio
async def test_runtime_notification_does_not_drive_active_goal(state_store):
    plugin = make_plugin(state_store)
    await plugin.service.create_goal("iterate until complete")
    await plugin.service.on_turn_end(EventContext(
        session=SimpleNamespace(),
        stop_reason="completed",
    ))

    assert len(plugin.driver.requests) == 1


@pytest.mark.asyncio
async def test_goal_exposes_compact_status_slot(state_store):
    plugin = make_plugin(state_store)

    async def status_slots() -> dict[str, str]:
        slots = StatusSlots()
        await plugin.ctx.emit(COLLECT_STATUS_SLOTS, slots)
        return slots.values

    assert await status_slots() == {}
    await plugin.service.create_goal("show status")
    assert await status_slots() == {"goal": "active"}
    await plugin.service.update_goal("complete", "done")
    assert await status_slots() == {"goal": "complete"}


@pytest.mark.asyncio
async def test_interrupt_pauses_goal_without_scheduling_continuation(state_store):
    plugin = make_plugin(state_store)
    await plugin.service.create_goal("pause on escape")
    await plugin.service.on_turn_end(EventContext(
        session=SimpleNamespace(),
        stop_reason="client_interrupt",
    ))

    assert plugin.driver.requests == []
    goal = await plugin.service.snapshot()
    assert goal is not None
    assert goal.status == "paused"


@pytest.mark.asyncio
async def test_goal_snapshot_is_added_only_to_continuation_turn(state_store):
    plugin = make_plugin(state_store)
    await plugin.service.create_goal("output two greetings")
    active_ctx = EventContext(
        session=SimpleNamespace(),
        user_input="wake",
        continuation=True,
    )

    await plugin.service.start_goal_turn(active_ctx)

    assert "output two greetings" in active_ctx.user_input

    await plugin.service.update_goal("complete", "Output both requested greetings.")
    ctx = EventContext(
        session=SimpleNamespace(),
        user_input="wake",
        continuation=False,
    )

    await plugin.service.start_goal_turn(ctx)

    assert ctx.user_input == "wake"


@pytest.mark.asyncio
async def test_goal_continuation_turn_replaces_prompt_with_goal_context(
    state_store,
    temp_workspace,
):
    plugin, setup = setup_plugin(state_store)
    await plugin.service.create_goal("finish the audit")
    llm = MockLLM(responses=[
        {"content": "Working on the audit."},
        {"content": "Plain reply."},
    ])
    engine = make_engine(
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
    # Continuation is carried by the agent-inbox input metadata, matching the
    # goal plugin's production ``send_input(..., metadata={"continuation": True})``.
    await engine.inject(
        "[goal continuation]",
        source="goal",
        metadata={"continuation": True},
    )
    _ = [
        event
        async for event in engine.run_pending(request_id="goal")
    ]
    last = llm.get_call_messages(0)[-1]
    assert last.role == "user"
    assert json.loads(last.content) == {
        "objective": "finish the audit",
        "status": "active",
    }
    assert all(message.role != "user" for message in llm.get_call_messages(0)[:-1])

    # A normal turn keeps its own prompt. The goal plugin schedules the next
    # active-goal continuation at TURN_END; drain it so the explicit input is
    # the sole next-turn claim.
    await engine.discard_inputs()
    _ = [event async for event in engine.run_turn("plain wake", request_id="plain")]
    assert llm.get_call_messages(1)[-1].content == "plain wake"


@pytest.mark.asyncio
async def test_goal_survives_state_store_recreation(state_store):
    plugin = make_plugin(state_store)
    await plugin.service.create_goal("survive restart")
    await plugin.service.update_goal("complete", "Restart behavior verified.")

    restored_store = ThreadPersistence.open(
        state_store.paths,
        thread_id=state_store.thread_id,
        workspace_root=state_store.workspace_root,
        provider=state_store.provider,
    )
    restored = make_plugin(restored_store)

    goal = await restored.service.snapshot()
    assert goal is not None
    assert goal.model_dump(mode="json") == {
        "schema_version": 1,
        "objective": "survive restart",
        "status": "complete",
        "summary": "Restart behavior verified.",
        "token_budget": None,
    }


@pytest.mark.asyncio
async def test_goal_rejects_invalid_persisted_state(state_store):
    plugin = make_plugin(state_store)
    invalid = {
        "schema_version": 1,
        "objective": "broken",
        "status": "unknown",
        "summary": "",
        "token_budget": None,
    }
    await plugin.store.set("snapshot", invalid)

    with pytest.raises(ValueError, match="Input should be"):
        await plugin.service.get_goal()

    assert await plugin.store.get("snapshot") == invalid


@pytest.mark.asyncio
async def test_plugin_dispose_removes_goal_resources_but_retains_state(
    tmp_path,
    state_store,
):
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    (plugins_root / "goal").symlink_to(
        Path(__file__).parents[2] / "goal",
        target_is_directory=True,
    )
    from XBotv2.loader import PluginTree
    from XBotv2.loader.runtime import mount_plugin_tree, validate_mounted_tree

    ctx = mount_ctx(state_store)
    ctx.set("engine", object())
    tools = ctx.tools
    handles = mount_plugin_tree(ctx, PluginTree.parse([
        {"id": "goal", "name": "goal"},
    ]))

    await ctx.start()
    validate_mounted_tree(handles)
    tool = tools.resolve("create_goal")
    assert tool is not None
    await tool.ainvoke({"objective": "retain me"})

    await handles["goal"].dispose()
    assert tools.registered_names() == ()
    import json as _json
    state_path = state_store.paths.plugin_state_dir / "state.json"
    data = _json.loads(state_path.read_text(encoding="utf-8"))
    assert data["goal.snapshot"]["objective"] == "retain me"


@pytest.mark.asyncio
async def test_engine_summarizes_completed_goal_without_persistent_context(
    state_store,
    temp_workspace,
):
    plugin, setup = setup_plugin(state_store)
    await plugin.service.create_goal("finish this turn")
    registry = ToolRegistry()
    registry.register(
        setup.tools["update_goal"],
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
    engine = make_engine(
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
    # The tool result no longer carries a ``data`` field; the summary is in content.
    assert "All work passed." in tool_event["data"]["content"]
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
