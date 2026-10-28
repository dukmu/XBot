"""Persistence state namespace contract through the public storage service."""
import asyncio

import pytest

from XBotv2.core.api.paths import RuntimePaths
from XBotv2.main import open_runtime
from XBotv2.persistence.api.contracts import PERSISTENCE


@pytest.fixture
async def state(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path))
    try:
        yield root.require(PERSISTENCE).state()
    finally:
        await root.close()


async def test_mutations_are_persisted_immediately(state, tmp_path):
    store = state.namespace("sample")
    await store.set("enabled", True)
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path))
    try:
        reader = root.require(PERSISTENCE).state().namespace("sample")
        assert await reader.get("enabled") is True
        await store.delete("enabled")
        assert await reader.get("enabled") is None
        await store.set("value", 1)
        await store.clear()
        assert await reader.keys() == []
    finally:
        await root.close()


async def test_store_instances_do_not_lose_sequential_updates(state):
    first = state.namespace("shared")
    second = state.namespace("shared")
    await first.set("first", 1)
    await second.set("second", 2)
    assert set(await first.keys()) == {"first", "second"}
    assert await second.get("first") == 1
    assert await first.get("second") == 2


async def test_event_loop_tasks_preserve_all_updates(state):
    store = state.namespace("shared")
    await asyncio.gather(*(store.set(f"key_{index}", index) for index in range(10)))
    assert set(await store.keys()) == {f"key_{index}" for index in range(10)}
    assert [await store.get(f"key_{index}") for index in range(10)] == list(range(10))


async def test_plugin_namespaces_are_isolated(state):
    goal = state.namespace("goal")
    todo = state.namespace("todolist")
    await goal.set("state", {"active": True})
    assert await todo.keys() == []
    assert await goal.get("state") == {"active": True}


async def test_read_values_cannot_mutate_store_without_set(state):
    store = state.namespace("sample")
    await store.set("nested", {"count": 1})
    value = await store.get("nested")
    value["count"] = 99
    assert await store.get("nested") == {"count": 1}
