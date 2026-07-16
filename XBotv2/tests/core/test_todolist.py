"""Behavior tests for the built-in TodoList plugin."""

from pathlib import Path

import pytest
import yaml

from builtin_plugins.todolist.plugin import TodolistPlugin
from xbotv2.api import PluginManifest
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
        self.tools = {}
        self.options = {}

    def register_tool(self, tool, options=None):
        self.tools[tool.name] = tool
        self.options[tool.name] = options
        return f"plugin:todolist:{tool.name}"


def make_plugin(state_store) -> TodolistPlugin:
    return TodolistPlugin(
        PluginManifest(name="todolist", version="1"),
        PluginStore(state_store, "todolist"),
    )


def setup_plugin(state_store) -> tuple[TodolistPlugin, SetupContext]:
    plugin = make_plugin(state_store)
    setup = SetupContext()
    plugin.setup(setup)
    return plugin, setup


def todo(content: str, status: str) -> dict[str, str]:
    return {"content": content, "status": status}


def test_todolist_registers_one_atomic_host_tool(state_store):
    plugin, setup = setup_plugin(state_store)

    assert list(setup.tools) == ["update_todos"]
    tool = setup.tools["update_todos"]
    options = setup.options["update_todos"]
    assert options.namespace == "plugin:todolist"
    assert options.sandbox_mode == "host"
    assert tool.parameters["required"] == ["todos"]
    item = tool.parameters["properties"]["todos"]["items"]
    assert item["required"] == ["content", "status"]
    assert item["properties"]["status"]["enum"] == [
        "pending", "in_progress", "completed",
    ]
    assert "Call only when" in tool.description
    assert plugin.diagnostics() == {
        "status": "ready",
        "scope": "session",
        "tool": "update_todos",
        "item_statuses": ["completed", "in_progress", "pending"],
    }


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

    assert created.data == {
        "todos": initial,
        "cleared": False,
    }
    assert "unchanged" in unchanged.content
    assert "Do not call update_todos again" in unchanged.content
    assert "updated" in updated.content
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

    assert result.data == {
        "todos": completed,
        "cleared": True,
    }
    assert "All todos completed" in result.content
    assert await plugin.store.get("state") == {"items": []}


@pytest.mark.asyncio
async def test_empty_list_clears_without_requiring_progress_item(state_store):
    plugin = make_plugin(state_store)
    await plugin.update_todos([todo("obsolete", "in_progress")])

    result = await plugin.update_todos([])

    assert result.data["cleared"] is False
    assert result.data["todos"] == []
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
    plugin_dir = plugins_root / "todolist"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "todolist", "version": "1.0.0"}),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    loader = PluginLoader(
        plugin_dirs=[plugins_root],
        state_store=state_store,
        hook_manager=HookManager(),
        tool_registry=registry,
        context_builder=ContextBuilder(),
    )

    loaded = await loader.load()
    assert isinstance(loaded[0], TodolistPlugin)
    assert registry.registered_names() == ["plugin:todolist:update_todos"]
    active = [todo("survive unload", "in_progress")]
    await registry.get("update_todos").tool.ainvoke({"todos": active})

    assert await loader.unload("todolist") is True
    assert registry.registered_names() == []
    assert state_store.get_plugin_state("todolist") == {
        "state": {"items": active},
    }

    await loader.load()
    assert registry.registered_names() == ["plugin:todolist:update_todos"]
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
        sandbox_mode=options.sandbox_mode,
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
    engine = Engine(
        llm=llm,
        tool_registry=registry,
        hook_manager=HookManager(),
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

    events = [event async for event in engine.run_turn("track verification")]
    tool_event = next(event for event in events if event["type"] == "tool_result")
    second_context = llm.get_call_messages(1)

    assert tool_event["data"]["data"] == {
        "todos": active,
        "cleared": False,
    }
    assert [message.role for message in second_context][-3:] == [
        "user", "assistant", "tool",
    ]
    assistant = second_context[-2]
    result = second_context[-1]
    assert assistant.tool_calls[0].name == "update_todos"
    assert assistant.tool_calls[0].args == {"todos": active}
    assert result.tool_call_id == "todo-call-1"
    assert "Todo list updated" in result.content
