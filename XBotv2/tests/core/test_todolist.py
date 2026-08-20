"""Behavior tests for the built-in TodoList plugin."""

from XBotv2.tests.helpers import make_engine

import json
from pathlib import Path

import pytest
from XBotv2.todolist.plugin import TodolistPlugin
import xcore
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.agentloop.engine import Engine
from XBotv2.config.models import RuntimeConfig
from XBotv2.llm.mock import MockLLM
from XBotv2.persistence.store import CoreStateStore
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
        for entry in self.ctx.tools.registry.registered_entries():
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


def make_plugin(state_store) -> TodolistPlugin:
    from XBotv2.todolist.plugin import TodolistPlugin

    return _mount(TodolistPlugin(), state_store)


def setup_plugin(state_store) -> tuple[TodolistPlugin, SetupContext]:
    plugin = make_plugin(state_store)
    return plugin, SetupContext(plugin)


def todo(content: str, status: str) -> dict[str, str]:
    return {"content": content, "status": status}


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
    assert await plugin.store.get("state") == {"items": replacement}


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
    assert await plugin.store.get("state") == {"items": original}


@pytest.mark.asyncio
async def test_all_completed_returns_final_list_then_clears_active_state(state_store):
    plugin = make_plugin(state_store)
    await plugin.update_todos([todo("verify behavior", "in_progress")])
    completed = [todo("verify behavior", "completed")]

    result = await plugin.update_todos(completed)

    assert result.status == "success"
    assert "All todos completed" in result.content
    assert await plugin.store.get("state") == {"items": []}


@pytest.mark.asyncio
async def test_empty_list_clears_without_requiring_progress_item(state_store):
    plugin = make_plugin(state_store)
    await plugin.update_todos([todo("obsolete", "in_progress")])

    result = await plugin.update_todos([])

    assert result.status == "success"
    assert await plugin.store.get("state") == {"items": []}


@pytest.mark.asyncio
async def test_old_id_based_state_is_read_without_exposing_ids(state_store):
    plugin = make_plugin(state_store)
    await plugin.store.set("state", {
        "next_id": 3,
        "items": [
            {"id": "todo-2", "content": "resume work", "status": "in_progress"},
        ],
    })

    assert await plugin._read_items() == [todo("resume work", "in_progress")]


@pytest.mark.asyncio
async def test_todolist_rejects_invalid_persisted_state(state_store):
    plugin = make_plugin(state_store)
    invalid = {"items": "not-a-list"}
    await plugin.store.set("state", invalid)

    with pytest.raises(ValueError, match="Todo list state is invalid"):
        await plugin._read_items()

    assert await plugin.store.get("state") == invalid


@pytest.mark.asyncio
async def test_loader_unload_removes_tool_but_retains_todos(tmp_path, state_store):
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    (plugins_root / "todolist").symlink_to(
        Path(__file__).parents[2] / "todolist",
        target_is_directory=True,
    )
    from XBotv2.loader import PluginTree
    from XBotv2.loader.runtime import Loader

    ctx = mount_ctx(state_store)
    registry = ctx.tools.registry
    loader = Loader(ctx, tree=PluginTree.from_dict([
        {"id": "todolist", "name": "todolist"},
    ]))

    await loader.load()
    assert isinstance(loader.get("todolist"), TodolistPlugin)
    assert registry.registered_names() == ["update_todos"]
    active = [todo("survive unload", "in_progress")]
    await registry.get("update_todos").tool.ainvoke({"todos": active})

    assert await loader.unload("todolist") is True
    assert registry.registered_names() == []
    state_file = state_store.paths.state_dir / "state.json"
    assert json.loads(state_file.read_text(encoding="utf-8"))["todolist.state"] == {
        "items": active,
    }

    await loader.load()
    assert registry.registered_names() == ["update_todos"]
    await loader.unload_all()


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
    assert "Todo list" in tool_event["data"]["content"]
    assert [message.role for message in second_context][-3:] == [
        "user", "assistant", "tool",
    ]
    assistant = second_context[-2]
    result = second_context[-1]
    assert assistant.tool_calls[0].name == "update_todos"
    assert assistant.tool_calls[0].args == {"todos": active}
    assert result.tool_call_id == "todo-call-1"
