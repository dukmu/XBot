"""Behavior tests for the built-in TodoList plugin."""

from XBotv2.tests.helpers import make_engine

from pathlib import Path

import pytest
from XBotv2.todolist.plugin import TodolistPlugin, TodolistService
import xcore
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.agentloop.engine import Engine
from XBotv2.config.models import RuntimeConfig
from XBotv2.llm.mock import MockLLM
from XBotv2.persistence.store import ThreadPersistence
from plugin_harness import mount_ctx, mount_plugin
from XBotv2.permissions.system import PermissionSystem
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.sandbox.policy import SandboxPolicy


class SetupContext:
    """Post-apply view of a plugin's registrations on a real XCore context."""

    def __init__(self, plugin) -> None:
        self.ctx = plugin.ctx
        self.tools: dict = {}
        self.options: dict = {}
        self.commands: dict = {}
        for entry in self.ctx.tools._registry.registered_entries():
            self.tools[entry.tool.name] = entry.tool
            self.options[entry.tool.name] = _EntryOptions(
                namespace=entry.namespace,
            )
        for command in self.ctx.commands.all():
            self.commands[command.name] = command


class _EntryOptions:
    def __init__(self, *, namespace) -> None:
        self.namespace = namespace


def _mount(plugin, state_store):
    return mount_plugin(plugin, state_store)


def make_plugin(state_store) -> TodolistService:
    component = _mount(TodolistPlugin(), state_store)
    return component.ctx.todolist


def setup_plugin(state_store) -> tuple[TodolistService, SetupContext]:
    component = _mount(TodolistPlugin(), state_store)
    return component.ctx.todolist, SetupContext(component)


def todo(content: str, status: str) -> dict[str, str]:
    return {"content": content, "status": status}


async def plugin_snapshot(plugin: TodolistService) -> list[dict[str, str]]:
    snapshot = await plugin.snapshot()
    return [item.to_dict() for item in snapshot.items]


def test_todolist_registers_one_atomic_tool(state_store):
    _plugin, setup = setup_plugin(state_store)

    assert list(setup.tools) == ["update_todos"]
    tool = setup.tools["update_todos"]
    assert tool.parameters["required"] == ["todos"]
    item = tool.parameters["properties"]["todos"]["items"]
    assert item["required"] == ["content", "status"]
    assert item["properties"]["status"]["enum"] == [
        "pending", "in_progress", "completed",
    ]


@pytest.mark.asyncio
async def test_update_todos_atomically_replaces_the_complete_list(state_store):
    plugin = make_plugin(state_store)
    initial = [
        todo("inspect API", "in_progress"),
        todo("write tests", "pending"),
    ]

    created = await plugin.update_todos(initial)
    unchanged = await plugin.update_todos(initial)
    replacement = [
        todo("inspect API", "completed"),
        todo("write tests", "in_progress"),
        todo("update docs", "pending"),
    ]
    updated = await plugin.update_todos(replacement)

    assert created.status == "success"
    assert unchanged.status == "success"
    assert updated.status == "success"
    assert updated.data == {
        "kind": "todo_snapshot",
        "schema_version": 1,
        "items": replacement,
    }
    assert await plugin_snapshot(plugin) == replacement


@pytest.mark.asyncio
async def test_invalid_list_never_partially_changes_state(state_store):
    plugin = make_plugin(state_store)
    original = [todo("keep this", "in_progress")]
    await plugin.update_todos(original)

    results = [
        await plugin.update_todos([todo("not started", "pending")]),
        await plugin.update_todos([
            todo("first", "in_progress"),
            todo("second", "in_progress"),
        ]),
        await plugin.update_todos([todo(" ", "in_progress")]),
        await plugin.update_todos([todo("bad status", "blocked")]),
        await plugin.update_todos([{
            "content": "unexpected field",
            "status": "in_progress",
            "id": "todo-1",
        }]),
    ]

    assert [result.error.code for result in results] == [
        "invalid_todo_progress",
        "invalid_todo_progress",
        "invalid_todo",
        "invalid_todo_status",
        "invalid_todos",
    ]
    assert await plugin_snapshot(plugin) == original


@pytest.mark.asyncio
async def test_all_completed_returns_final_list_then_clears_active_state(state_store):
    plugin = make_plugin(state_store)
    await plugin.update_todos([todo("verify behavior", "in_progress")])
    completed = [todo("verify behavior", "completed")]

    result = await plugin.update_todos(completed)

    assert result.status == "success"
    assert "All todos completed" in result.content
    assert await plugin_snapshot(plugin) == []


@pytest.mark.asyncio
async def test_empty_list_clears_without_requiring_progress_item(state_store):
    plugin = make_plugin(state_store)
    await plugin.update_todos([todo("obsolete", "in_progress")])

    result = await plugin.update_todos([])

    assert result.status == "success"
    assert await plugin_snapshot(plugin) == []


@pytest.mark.asyncio
async def test_todolist_rejects_obsolete_id_based_state(state_store):
    plugin = make_plugin(state_store)
    await state_store.state.namespace("todolist").set("snapshot", {
        "schema_version": 1,
        "items": [
        {"id": "todo-2", "content": "resume work", "status": "in_progress"},
        ],
    })

    with pytest.raises(ValueError, match="only content and status"):
        await plugin.snapshot()


@pytest.mark.asyncio
async def test_todolist_rejects_invalid_persisted_state(state_store):
    plugin = make_plugin(state_store)
    invalid = {"schema_version": 1, "items": "not-a-list"}
    store = state_store.state.namespace("todolist")
    await store.set("snapshot", invalid)

    with pytest.raises(TypeError, match="items must be a list"):
        await plugin.snapshot()

    assert await store.get("snapshot") == invalid


@pytest.mark.asyncio
async def test_plugin_dispose_removes_tool_but_retains_todos(tmp_path, state_store):
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
    handles = mount_plugin_tree(ctx, PluginTree.from_dict([
        {"id": "todolist", "name": "todolist"},
    ]))

    await ctx.start()
    validate_mounted_tree(handles)
    assert registry.registered_names() == ["update_todos"]
    active = [todo("survive unload", "in_progress")]
    await registry.get("update_todos").tool.ainvoke({"todos": active})

    await handles["todolist"].dispose()
    assert registry.registered_names() == []
    stored = await ctx.state.namespace("todolist").get("snapshot")
    assert stored["items"] == active
    assert (state_store.paths.plugin_state_dir / "state.json").is_file()



@pytest.mark.asyncio
async def test_engine_keeps_todo_call_and_result_in_next_model_context(
    state_store,
    temp_workspace: Path,
):
    _plugin, setup = setup_plugin(state_store)
    registry = ToolRegistry()
    tool = setup.tools["update_todos"]
    options = setup.options["update_todos"]
    registry.register(
        tool,
        namespace=options.namespace,
    )
    active = [
        todo("verify SSE", "in_progress"),
        todo("write docs", "pending"),
    ]
    llm = MockLLM(responses=[
        {
            "content": "tracking work",
            "tool_calls": [{
                "id": "todo-call-1",
                "name": "update_todos",
                "args": {"todos": active},
            }],
        },
        {"content": "Tracked."},
    ])
    engine = make_engine(
        llm=llm,
        tool_registry=registry,
        plugin_ctx=xcore.Context(),
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

    events = [event async for event in engine.run_turn("track verification")]
    tool_event = next(event for event in events if event["type"] == "tool_result")
    second_context = llm.get_call_messages(1)

    assert tool_event["data"]["status"] == "success"
    assert tool_event["data"]["data"] == {
        "kind": "todo_snapshot",
        "schema_version": 1,
        "items": active,
    }
    assert "Todo list" in tool_event["data"]["content"]
    assert [message.role for message in second_context][-3:] == [
        "user", "assistant", "tool",
    ]
    assistant = second_context[-2]
    result = second_context[-1]
    assert assistant.tool_calls[0].name == "update_todos"
    assert assistant.tool_calls[0].args == {"todos": active}
    assert result.tool_call_id == "todo-call-1"
