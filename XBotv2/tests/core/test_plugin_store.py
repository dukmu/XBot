"""Contract tests for per-plugin persistent state (ctx.state.namespace).

The pre-migration ``PluginStore`` class was removed; plugins now persist
through ``ctx.state.namespace(manifest.name)`` (an async
``get/set/delete/all/clear`` store).  These tests pin the store contract on
the real StateService implementation.
"""

import asyncio
import json
from pathlib import Path

import pytest

from XBotv2.persistence.store import CoreStateStore
from XBotv2.core.paths import RuntimePaths
from plugin_harness import mount_ctx


def _state_file(tmp_path) -> Path:
    return (
        RuntimePaths.from_data_dir(tmp_path).session("s").thread("t").state_dir
        / "state.json"
    )


def _core_store(tmp_path) -> CoreStateStore:
    return CoreStateStore.create(
        RuntimePaths.from_data_dir(tmp_path).session("s"),
        thread_id="t",
        workspace_root="/workspace",
        provider="default",
    )


@pytest.mark.asyncio
async def test_mutations_are_persisted_immediately(tmp_path) -> None:
    ctx = mount_ctx(_core_store(tmp_path))
    store = ctx.state.namespace("sample")

    await store.set("enabled", True)
    state = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    assert state["sample.enabled"] is True

    await store.delete("enabled")
    state = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    assert "sample.enabled" not in state

    await store.set("value", 1)
    await store.clear()
    state = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    assert not any(key.startswith("sample.") for key in state)


@pytest.mark.asyncio
async def test_store_instances_do_not_lose_sequential_updates(tmp_path) -> None:
    ctx = mount_ctx(_core_store(tmp_path))
    first: PluginStore = ctx.state.namespace("shared")
    second: PluginStore = ctx.state.namespace("shared")

    assert await first.all() == {}
    assert await second.all() == {}
    await first.set("first", 1)
    await second.set("second", 2)

    assert await first.all() == {"first": 1, "second": 2}


@pytest.mark.asyncio
async def test_event_loop_tasks_preserve_all_updates(tmp_path) -> None:
    ctx = mount_ctx(_core_store(tmp_path))
    store = ctx.state.namespace("shared")

    await asyncio.gather(*(
        store.set(f"key_{index}", index) for index in range(10)
    ))

    assert await store.all() == {f"key_{index}": index for index in range(10)}


@pytest.mark.asyncio
async def test_plugin_namespaces_are_isolated(tmp_path) -> None:
    ctx = mount_ctx(_core_store(tmp_path))
    goal: PluginStore = ctx.state.namespace("goal")
    todo: PluginStore = ctx.state.namespace("todolist")

    await goal.set("state", {"active": True})
    assert await todo.all() == {}
    assert await goal.get("state") == {"active": True}


@pytest.mark.asyncio
async def test_read_values_cannot_mutate_store_without_set(tmp_path) -> None:
    ctx = mount_ctx(_core_store(tmp_path))
    store = ctx.state.namespace("sample")
    await store.set("nested", {"count": 1})

    value = await store.get("nested")
    value["count"] = 99

    assert await store.get("nested") == {"count": 1}
