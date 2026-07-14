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


def test_todolist_registers_four_host_tools(state_store):
    plugin, setup = setup_plugin(state_store)

    assert list(setup.tools) == [
        "list_todos",
        "create_todo",
        "update_todo",
        "remove_todo",
    ]
    assert all(
        options.namespace == "plugin:todolist"
        and options.sandbox_mode == "host"
        for options in setup.options.values()
    )
    assert setup.tools["create_todo"].parameters["required"] == ["content"]
    assert setup.tools["update_todo"].parameters["required"] == ["todo_id"]
    assert plugin.diagnostics() == {
        "status": "ready",
        "scope": "session",
        "item_statuses": ["completed", "in_progress", "pending"],
    }


@pytest.mark.asyncio
async def test_todolist_crud_preserves_order_and_never_reuses_ids(state_store):
    plugin = make_plugin(state_store)

    first = await plugin.create_todo("inspect API")
    second = await plugin.create_todo("write tests")
    updated = await plugin.update_todo(
        "todo-1",
        content="inspect public API",
        status="in_progress",
    )
    listed = await plugin.list_todos()
    removed = await plugin.remove_todo("todo-1")
    third = await plugin.create_todo("update docs")

    assert first.data["item"]["id"] == "todo-1"
    assert second.data["item"]["id"] == "todo-2"
    assert updated.data["item"] == {
        "id": "todo-1",
        "content": "inspect public API",
        "status": "in_progress",
    }
    assert [item["id"] for item in listed.data["items"]] == ["todo-1", "todo-2"]
    assert removed.data["item"]["id"] == "todo-1"
    assert third.data["item"]["id"] == "todo-3"


@pytest.mark.asyncio
async def test_invalid_mutations_leave_persisted_state_unchanged(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_todo("keep this")
    before = await plugin.store.all()

    results = [
        await plugin.create_todo("   "),
        await plugin.update_todo("todo-1"),
        await plugin.update_todo("todo-1", content=""),
        await plugin.update_todo("todo-1", status="blocked"),
        await plugin.update_todo("todo-99", status="completed"),
        await plugin.remove_todo("todo-99"),
    ]

    assert [result.error.code for result in results] == [
        "invalid_todo",
        "invalid_update",
        "invalid_todo",
        "invalid_status",
        "todo_not_found",
        "todo_not_found",
    ]
    assert await plugin.store.all() == before


@pytest.mark.asyncio
async def test_todolist_survives_state_store_recreation(state_store):
    plugin = make_plugin(state_store)
    await plugin.create_todo("persist across restart")
    await plugin.update_todo("todo-1", status="completed")

    restored_store = CoreStateStore(
        paths=state_store.paths,
        thread_id=state_store.thread_id,
        workspace_root=state_store.workspace_root,
        provider=state_store.provider,
    )
    restored = make_plugin(restored_store)

    result = await restored.list_todos()
    assert result.data["items"] == [
        {
            "id": "todo-1",
            "content": "persist across restart",
            "status": "completed",
        }
    ]
    assert (await restored.create_todo("next")).data["item"]["id"] == "todo-2"


@pytest.mark.asyncio
async def test_todolist_rejects_invalid_persisted_state(state_store):
    plugin = make_plugin(state_store)
    invalid = {"next_id": 1, "items": "not-a-list"}
    await plugin.store.set("state", invalid)

    with pytest.raises(ValueError, match="Todo list state is invalid"):
        await plugin.list_todos()

    assert await plugin.store.get("state") == invalid


@pytest.mark.asyncio
async def test_loader_unload_removes_tools_but_retains_todos(tmp_path, state_store):
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
    assert registry.registered_names() == [
        "plugin:todolist:list_todos",
        "plugin:todolist:create_todo",
        "plugin:todolist:update_todo",
        "plugin:todolist:remove_todo",
    ]
    await registry.get("create_todo").tool.ainvoke({"content": "survive unload"})

    assert await loader.unload("todolist") is True
    assert registry.registered_names() == []
    assert state_store.get_plugin_state("todolist")["state"]["items"][0][
        "content"
    ] == "survive unload"

    await loader.load()
    listed = await registry.get("list_todos").tool.ainvoke({})
    assert listed.data["items"][0]["content"] == "survive unload"
    await loader.unload_all()


@pytest.mark.asyncio
async def test_engine_exposes_structured_todo_result(
    state_store,
    temp_workspace: Path,
):
    _plugin, setup = setup_plugin(state_store)
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
                "content": "tracking work",
                "tool_calls": [
                    {
                        "id": "todo-call-1",
                        "name": "create_todo",
                        "args": {"content": "verify SSE"},
                    }
                ],
            },
            {"content": "Tracked."},
        ]
    )
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

    events = [event async for event in engine.run_turn("track SSE verification")]
    tool_event = next(event for event in events if event["type"] == "tool_result")

    assert tool_event["data"]["status"] == "success"
    assert tool_event["data"]["data"] == {
        "item": {
            "id": "todo-1",
            "content": "verify SSE",
            "status": "pending",
        }
    }
    stored = await PluginStore(state_store, "todolist").get("state")
    assert stored["items"] == [
        {
            "id": "todo-1",
            "content": "verify SSE",
            "status": "pending",
        }
    ]
