"""Behavior tests for the built-in task-list plugin."""

from XBotv2.tests.helpers import make_engine

from pathlib import Path

import pytest
from XBotv2.todolist.plugin import TaskService, TodolistPlugin
import xcore
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.config.contracts import RuntimeConfig
from XBotv2.llm.mock import MockLLM
from XBotv2.permissions.system import PermissionSystem
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.todolist.contracts import (
    TASK_SCHEMA_VERSION,
    Task,
    TaskConfig,
    TaskList,
)
from plugin_harness import mount_ctx


class SetupContext:
    """Post-apply view of a plugin's registrations on a real XCore context."""

    def __init__(self, plugin) -> None:
        self.ctx = plugin.ctx
        self.tools: dict = {}
        self.options: dict = {}
        for entry in self.ctx.tools._registry.registered_entries():
            self.tools[entry.tool.name] = entry.tool
            self.options[entry.tool.name] = _EntryOptions(namespace=entry.namespace)

    @property
    def service(self) -> TaskService:
        return self.ctx.todolist


class _EntryOptions:
    def __init__(self, *, namespace) -> None:
        self.namespace = namespace


class RecordingEngine:
    """Stand-in driver recording reminders and continuations."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.injected: list[tuple[str, dict[str, object]]] = []

    async def followup(self, content: str, **kwargs: object) -> None:
        self.requests.append((content, kwargs))

    async def inject(self, content: str, **kwargs: object) -> None:
        self.injected.append((content, kwargs))


async def _mount(state_store, *, config=None, engine=None):
    ctx = mount_ctx(state_store)
    if engine is not None:
        ctx.set("engine", engine)
    plugin = TodolistPlugin()
    plugin.ctx = ctx
    handle = ctx.plugin(plugin, config)
    await ctx.start()
    await handle
    return plugin


async def make_plugin(state_store, *, config=None, engine=None) -> TaskService:
    component = await _mount(state_store, config=config, engine=engine)
    return component.ctx.todolist


async def setup_plugin(
    state_store, *, config=None, engine=None
) -> tuple[TaskService, SetupContext]:
    component = await _mount(state_store, config=config, engine=engine)
    return component.ctx.todolist, SetupContext(component)


async def tasks_of(plugin: TaskService) -> tuple[Task, ...]:
    return (await plugin.snapshot()).tasks


@pytest.mark.asyncio
async def test_registers_the_four_task_tools(state_store):
    _plugin, setup = await setup_plugin(state_store)

    assert sorted(setup.tools) == [
        "task_create", "task_get", "task_list", "task_update",
    ]
    item = setup.tools["task_create"].parameters
    assert item["required"] == ["subject"]
    assert set(item["properties"]) == {"subject", "description", "activeForm"}
    update = setup.tools["task_update"].parameters
    assert update["required"] == ["taskId"]
    assert update["properties"]["status"]["anyOf"][0]["enum"] == [
        "pending", "in_progress", "completed", "deleted",
    ]
    assert setup.tools["task_update"].kind == "think"


@pytest.mark.asyncio
async def test_create_assigns_incrementing_ids_and_get_reads_one(state_store):
    plugin = await make_plugin(state_store)

    first = await plugin.task_create("Fix the auth bug", "Sessions expire early.")
    second = await plugin.task_create("Add tests", activeForm="Adding tests")

    assert first.status == "success"
    assert "#1" in first.content
    assert "#2" in second.content
    fetched = await plugin.task_get("1")
    assert fetched.status == "success"
    assert "Fix the auth bug" in fetched.content
    assert "Sessions expire early." in fetched.content
    assert fetched.data["tasks"][0]["id"] == "1"
    assert fetched.data["tasks"][0]["status"] == "pending"

    missing = await plugin.task_get("99")
    assert missing.error.code == "task_not_found"


@pytest.mark.asyncio
async def test_status_transitions_claim_owner_and_complete(state_store):
    plugin = await make_plugin(state_store)
    plugin._agent_name = "default"
    await plugin.task_create("Investigate the crash")

    started = await plugin.task_update("1", status="in_progress")
    completed = await plugin.task_update("1", status="completed")

    assert started.status == "success"
    assert completed.status == "success"
    task = (await plugin.snapshot()).find("1")
    assert task is not None
    assert task.status == "completed"
    assert task.owner == "default"


@pytest.mark.asyncio
async def test_deleted_status_removes_the_task_and_its_edges(state_store):
    plugin = await make_plugin(state_store)
    await plugin.task_create("First")
    await plugin.task_create("Second")
    linked = await plugin.task_update("2", addBlockedBy=["1"])
    assert linked.status == "success"

    deleted = await plugin.task_update("1", status="deleted")

    assert deleted.status == "success"
    remaining = await tasks_of(plugin)
    assert [task.id for task in remaining] == ["2"]
    assert remaining[0].blockedBy == ()
    # Ids are not reused after deletion.
    await plugin.task_create("Third")
    assert [task.id for task in await tasks_of(plugin)] == ["2", "3"]
    assert (await plugin.snapshot()).next_id == 4


@pytest.mark.asyncio
async def test_blocked_task_cannot_start_until_its_blocker_completes(state_store):
    plugin = await make_plugin(state_store)
    await plugin.task_create("Migrate the schema")
    await plugin.task_create("Ship the release")
    await plugin.task_update("2", addBlockedBy=["1"])

    blocked = await plugin.task_update("2", status="in_progress")
    assert blocked.status == "error"
    assert blocked.error.code == "blocked"
    assert "#1" in blocked.error.message

    await plugin.task_update("1", status="completed")
    unblocked = await plugin.task_update("2", status="in_progress")
    assert unblocked.status == "success"


@pytest.mark.asyncio
async def test_dependencies_are_maintained_on_both_sides(state_store):
    plugin = await make_plugin(state_store)
    await plugin.task_create("First")
    await plugin.task_create("Second")

    await plugin.task_update("1", addBlocks=["2"])

    tasks = (await plugin.snapshot())
    assert tasks.find("1").blocks == ("2",)
    assert tasks.find("2").blockedBy == ("1",)
    unknown = await plugin.task_update("1", addBlocks=["9"])
    assert unknown.error.code == "task_not_found"


@pytest.mark.asyncio
async def test_task_list_and_projection_expose_the_task_entity(state_store):
    plugin = await make_plugin(state_store)
    await plugin.task_create("First", "Do the first thing", activeForm="Doing first")
    await plugin.task_create("Second")
    await plugin.task_update("1", status="in_progress")
    await plugin.task_update("2", addBlockedBy=["1"])

    listed = await plugin.task_list()

    assert listed.status == "success"
    assert listed.data["schema_version"] == TASK_SCHEMA_VERSION
    assert listed.data["kind"] == "todo_snapshot"
    first, second = listed.data["tasks"]
    assert first == {
        "id": "1",
        "subject": "First",
        "description": "Do the first thing",
        "activeForm": "Doing first",
        "owner": "",
        "status": "in_progress",
        "blocks": ["2"],
        "blockedBy": [],
        "metadata": {},
    }
    assert second["blockedBy"] == ["1"]
    assert "blocked by #1" in listed.content
    assert "blocks #2" in listed.content
    assert "#1 [>] First (Doing first)" in listed.content


@pytest.mark.asyncio
async def test_mutations_emit_the_client_projection(state_store):
    from XBotv2.application import RUNTIME_EVENT, RuntimeEvent

    plugin = await make_plugin(state_store)
    published: list[RuntimeEvent] = []
    ctx = plugin._store
    del ctx  # the service writes through the store; events come from ToolResult

    created = await plugin.task_create("First")
    assert created.client_events
    event = created.client_events[0]
    assert event.type == "todo_updated"
    assert event.data["kind"] == "todo_snapshot"
    assert event.data["tasks"][0]["subject"] == "First"

    read = await plugin.task_list()
    assert read.client_events == ()


@pytest.mark.asyncio
async def test_verification_nudge_fires_when_a_plan_closes_without_verification(state_store):
    plugin = await make_plugin(state_store)
    await plugin.task_create("Implement the fix")
    await plugin.task_create("Update the docs")
    await plugin.task_create("Land the change")
    for task_id in ("1", "2"):
        await plugin.task_update(task_id, status="completed")

    final = await plugin.task_update("3", status="completed")

    assert final.status == "success"
    assert "verification step" in final.content


@pytest.mark.asyncio
async def test_verification_nudge_stays_silent_when_a_task_verifies(state_store):
    plugin = await make_plugin(state_store)
    await plugin.task_create("Implement the fix")
    await plugin.task_create("Verify the fix")
    await plugin.task_create("Land the change")
    for task_id in ("1", "2"):
        await plugin.task_update(task_id, status="completed")

    final = await plugin.task_update("3", status="completed")

    assert "verification step" not in final.content


@pytest.mark.asyncio
async def test_invalid_updates_never_partially_change_state(state_store):
    plugin = await make_plugin(state_store)
    await plugin.task_create("Keep this")

    results = [
        await plugin.task_create(" "),
        await plugin.task_create("x" * 501),
        await plugin.task_update("1", status="blocked"),
        await plugin.task_update("1", status="deleted", subject="renamed"),
        await plugin.task_update("1"),
        await plugin.task_update("1", addBlocks=["1"]),
        await plugin.task_update("9", status="completed"),
    ]

    assert [result.status for result in results] == ["error"] * len(results)
    assert [result.error.code for result in results] == [
        "invalid_task",
        "invalid_task",
        "invalid_task_status",
        "invalid_task_update",
        "invalid_task_update",
        "invalid_task_dependency",
        "task_not_found",
    ]
    tasks = await tasks_of(plugin)
    assert [task.subject for task in tasks] == ["Keep this"]


@pytest.mark.asyncio
async def test_configured_bounds_drive_validation_and_the_tool_schema(state_store):
    component = await _mount(
        state_store,
        config={
            "max_tasks": 2,
            "max_subject_chars": 8,
            "max_description_chars": 5,
            "max_active_form_chars": 4,
            "reminder_after_turns": 1,
            "verification_nudge": False,
        },
    )
    plugin = component.ctx.todolist
    tools = {
        entry.tool.name: entry.tool
        for entry in component.ctx.tools._registry.registered_entries()
    }

    create = tools["task_create"].parameters["properties"]
    assert create["subject"]["maxLength"] == 8
    assert create["description"]["maxLength"] == 5
    assert create["activeForm"]["maxLength"] == 4
    update = tools["task_update"].parameters["properties"]
    assert update["subject"]["anyOf"][0]["maxLength"] == 8
    assert update["description"]["anyOf"][0]["maxLength"] == 5

    assert (await plugin.task_create("12345678")).status == "success"
    too_long = await plugin.task_create("123456789")
    assert too_long.error.code == "invalid_task"
    assert (await plugin.task_create("second")).status == "success"
    over_limit = await plugin.task_create("third")
    assert over_limit.error.code == "task_limit"

    # The reminder threshold is configurable and fires on the configured turn.
    from XBotv2.agentloop import EventContext

    engine = RecordingEngine()
    plugin._engine = engine
    await plugin.task_update("1", status="deleted")
    await plugin.task_update("2", status="deleted")
    await plugin.task_create("probe")
    await plugin.on_turn_start(EventContext())
    assert len(engine.injected) == 1


@pytest.mark.asyncio
async def test_disabled_verification_nudge_stays_silent(state_store):
    component = await _mount(state_store, config={"verification_nudge": False})
    plugin = component.ctx.todolist
    for subject in ("one", "two", "three"):
        await plugin.task_create(subject)
    for task_id in ("1", "2"):
        await plugin.task_update(task_id, status="completed")

    final = await plugin.task_update("3", status="completed")

    assert final.status == "success"
    assert "verification step" not in final.content


@pytest.mark.asyncio
async def test_task_limit_is_enforced(state_store):
    plugin = await make_plugin(state_store)
    for index in range(TaskConfig().max_tasks):
        assert (await plugin.task_create(f"task {index}")).status == "success"

    overflow = await plugin.task_create("one too many")

    assert overflow.error.code == "task_limit"


@pytest.mark.asyncio
async def test_v1_snapshot_is_read_as_an_id_addressable_list(state_store):
    plugin = await make_plugin(state_store)
    await state_store.state.namespace("todolist").set("snapshot", {
        "schema_version": 1,
        "items": [
            {"content": "legacy one", "status": "in_progress",
             "activeForm": "Doing legacy one"},
            {"content": "legacy two", "status": "pending"},
        ],
    })

    snapshot = await plugin.snapshot()

    assert snapshot.schema_version == TASK_SCHEMA_VERSION
    assert [task.id for task in snapshot.tasks] == ["1", "2"]
    assert snapshot.tasks[0].subject == "legacy one"
    assert snapshot.tasks[0].activeForm == "Doing legacy one"
    assert snapshot.next_id == 3


@pytest.mark.asyncio
async def test_todolist_rejects_invalid_persisted_state(state_store):
    plugin = await make_plugin(state_store)
    store = state_store.state.namespace("todolist")
    await store.set("snapshot", {"schema_version": 2, "tasks": "not-a-list"})

    with pytest.raises(ValueError):
        await plugin.snapshot()

    assert (await store.get("snapshot"))["tasks"] == "not-a-list"


@pytest.mark.asyncio
async def test_stale_reminder_is_injected_once_per_threshold(state_store):
    from XBotv2.agentloop import EventContext

    engine = RecordingEngine()
    plugin = await make_plugin(
        state_store, config={"reminder_after_turns": 3}, engine=engine
    )
    await plugin.task_create("long running step")

    for _ in range(2):
        await plugin.on_turn_start(EventContext())
    assert engine.injected == []

    await plugin.on_turn_start(EventContext())
    assert len(engine.injected) == 1
    content, kwargs = engine.injected[0]
    assert content.startswith('<system_reminder source="todo" event="reminder">')
    assert "task tools haven't been used recently" in content
    # The reminder never restates the list: the tool calls already carry it.
    assert "long running step" not in content
    assert kwargs == {"source": "todo", "metadata": {"kind": "reminder"}}
    # A reminder is injected context, never a new turn.
    assert engine.requests == []

    # The counter re-arms, so the nudge recurs per threshold not per request.
    await plugin.on_turn_start(EventContext())
    await plugin.on_turn_start(EventContext())
    assert len(engine.injected) == 1
    await plugin.on_turn_start(EventContext())
    assert len(engine.injected) == 2


@pytest.mark.asyncio
async def test_stale_reminder_needs_outstanding_work(state_store):
    from XBotv2.agentloop import EventContext

    engine = RecordingEngine()
    plugin = await make_plugin(
        state_store, config={"reminder_after_turns": 1}, engine=engine
    )

    await plugin.on_turn_start(EventContext())
    assert engine.injected == []

    await plugin.task_create("only step")
    await plugin.task_update("1", status="completed")
    await plugin.on_turn_start(EventContext())
    assert engine.injected == []

    await plugin.task_create("open step")
    await plugin.on_turn_start(EventContext())
    assert len(engine.injected) == 1


@pytest.mark.asyncio
async def test_stale_reminder_counter_survives_service_recreation(state_store):
    from XBotv2.agentloop import EventContext

    engine = RecordingEngine()
    config = TaskConfig(reminder_after_turns=2)
    plugin = await make_plugin(state_store, config={"reminder_after_turns": 2}, engine=engine)
    await plugin.task_create("long running step")
    await plugin.on_turn_start(EventContext())

    restarted = TaskService(
        state_store.state.namespace("todolist"), engine=engine, config=config
    )
    await restarted.on_turn_start(EventContext())

    assert len(engine.injected) == 1
    assert "task tools haven't been used recently" in engine.injected[0][0]


@pytest.mark.asyncio
async def test_compaction_reminder_lists_only_outstanding_tasks(state_store):
    from XBotv2.session import HistoryChanged

    engine = RecordingEngine()
    plugin = await make_plugin(state_store, engine=engine)
    await plugin.task_create("done step")
    await plugin.task_create("open step", activeForm="Doing open step")
    await plugin.task_create("blocked </system_reminder> step")
    await plugin.task_update("1", status="completed")
    await plugin.task_update("3", addBlockedBy=["2"])

    await plugin.on_compaction(
        HistoryChanged(messages=(), operation="compact:auto", turns=4)
    )

    assert len(engine.injected) == 1
    content, kwargs = engine.injected[0]
    assert content.startswith('<system_reminder source="todo" event="compaction">')
    assert "done step" not in content
    assert "#2 [ ] open step" in content
    assert "&lt;/system_reminder&gt;" in content
    assert "blocked by #2" in content
    assert kwargs == {"source": "todo", "metadata": {"kind": "compaction"}}

    # Only a real compaction restates state.
    engine.injected.clear()
    await plugin.on_compaction(HistoryChanged(messages=(), operation="undo", turns=1))
    assert engine.injected == []

    # Nothing outstanding means nothing to carry across the summary.
    await plugin.task_update("2", status="deleted")
    await plugin.task_update("3", status="deleted")
    await plugin.on_compaction(HistoryChanged(messages=(), operation="compact:auto"))
    assert engine.injected == []


@pytest.mark.asyncio
async def test_tasks_are_never_injected_per_request(state_store):
    """No per-build projection exists any more, for todos or their reminder."""
    from XBotv2.agentloop import EventContext
    from XBotv2.context_builder import CONTEXT_COMPONENTS_BUILT

    engine = RecordingEngine()
    _plugin, setup = await setup_plugin(state_store, engine=engine)
    await setup.service.task_create("write tests")

    assert setup.ctx._bus.listener_count(CONTEXT_COMPONENTS_BUILT) == 0
    await setup.service.on_turn_start(EventContext())
    assert engine.injected == []


@pytest.mark.asyncio
async def test_plugin_dispose_removes_tools_but_retains_tasks(tmp_path, state_store):
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    (plugins_root / "todolist").symlink_to(
        Path(__file__).parents[2] / "todolist",
        target_is_directory=True,
    )
    from XBotv2.loader import PluginTree
    from XBotv2.loader.runtime import mount_plugin_tree, validate_mounted_tree

    ctx = mount_ctx(state_store)
    registry = ctx.tools._registry
    handles = mount_plugin_tree(ctx, PluginTree.parse([
        {"id": "todolist", "name": "todolist"},
    ]))

    await ctx.start()
    validate_mounted_tree(handles)
    assert sorted(registry.registered_names()) == [
        "task_create", "task_get", "task_list", "task_update",
    ]
    await registry.get("task_create").tool.ainvoke({"subject": "survive unload"})

    await handles["todolist"].dispose()
    assert registry.registered_names() == []
    stored = await ctx.state.namespace("todolist").get("snapshot")
    assert stored["tasks"][0]["subject"] == "survive unload"
    assert (state_store.paths.plugin_state_dir / "state.json").is_file()


@pytest.mark.asyncio
async def test_engine_exposes_task_tools_without_a_projection(
    state_store,
    temp_workspace: Path,
):
    _plugin, setup = await setup_plugin(state_store)
    registry = ToolRegistry()
    for name in ("task_create", "task_update"):
        registry.register(
            setup.tools[name],
            namespace=setup.options[name].namespace,
        )
    await setup.service.task_create("verify SSE", activeForm="Verifying SSE")
    llm = MockLLM(responses=[{
        "content": "tracking",
        "tool_calls": [{
            "id": "task-call-1",
            "name": "task_update",
            "args": {"taskId": "1", "status": "in_progress"},
        }],
    }, {"content": "Tracked."}])
    engine = make_engine(
        llm=llm,
        tool_registry=registry,
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
    await engine.start_session()

    events = [event async for event in engine.run_turn("start the task")]
    tool_event = next(event for event in events if event["type"] == "tool_result")
    request = llm.get_call_messages(0)

    assert tool_event["data"]["status"] == "success"
    assert tool_event["data"]["data"]["tasks"][0]["status"] == "in_progress"
    # The list travels in the tool call and its result; nothing is injected
    # into the prompt on the side.
    assert "<system_reminder" not in request[0].content
    assert all(
        "system_reminder" not in message.content for message in request
    )
    todo_events = [event for event in events if event["type"] == "todo_updated"]
    assert todo_events and todo_events[-1]["data"]["tasks"][0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_http_projection_returns_the_task_list(state_store):
    snapshot = TaskList.from_items([Task(id="1", subject="one")])
    assert snapshot.projection()["tasks"][0]["subject"] == "one"
