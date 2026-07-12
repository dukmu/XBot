"""Contract tests for per-plugin persistent state."""

import asyncio

import pytest
import yaml

from xbotv2.api.paths import RuntimePaths
from xbotv2.persistence.store import CoreStateStore
from xbotv2.plugin.store import PluginStore


def _core_store(tmp_path) -> CoreStateStore:
    return CoreStateStore.create(
        RuntimePaths.from_data_dir(tmp_path).session("s"),
        thread_id="t",
        workspace_root="/workspace",
        provider="default",
    )


@pytest.mark.asyncio
async def test_mutations_are_persisted_immediately(tmp_path) -> None:
    core = _core_store(tmp_path)
    store = PluginStore(core, "sample")

    await store.set("enabled", True)
    assert core.get_plugin_state("sample") == {"enabled": True}

    await store.delete("enabled")
    assert core.get_plugin_state("sample") == {}

    await store.set("value", 1)
    await store.clear()
    assert core.get_plugin_state("sample") == {}


@pytest.mark.asyncio
async def test_store_instances_do_not_lose_sequential_updates(tmp_path) -> None:
    core = _core_store(tmp_path)
    first = PluginStore(core, "shared")
    second = PluginStore(core, "shared")

    assert await first.all() == {}
    assert await second.all() == {}
    await first.set("first", 1)
    await second.set("second", 2)

    assert await first.all() == {"first": 1, "second": 2}


@pytest.mark.asyncio
async def test_event_loop_tasks_preserve_all_updates(tmp_path) -> None:
    core = _core_store(tmp_path)

    await asyncio.gather(*(
        PluginStore(core, "shared").set(f"key_{index}", index)
        for index in range(10)
    ))

    assert core.get_plugin_state("shared") == {
        f"key_{index}": index for index in range(10)
    }


@pytest.mark.asyncio
async def test_read_values_cannot_mutate_store_without_set(tmp_path) -> None:
    store = PluginStore(_core_store(tmp_path), "sample")
    await store.set("nested", {"count": 1})

    value = await store.get("nested")
    value["count"] = 99

    assert await store.get("nested") == {"count": 1}


@pytest.mark.asyncio
async def test_failed_serialization_preserves_previous_state(tmp_path) -> None:
    core = _core_store(tmp_path)
    store = PluginStore(core, "sample")
    await store.set("valid", True)

    with pytest.raises(yaml.representer.RepresenterError):
        await store.set("invalid", object())

    assert core.get_plugin_state("sample") == {"valid": True}
    assert list(core.plugin_states_dir.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_plugin_namespaces_remain_isolated(tmp_path) -> None:
    core = _core_store(tmp_path)
    first = PluginStore(core, "first")
    second = PluginStore(core, "second")

    await first.set("value", 1)
    await second.set("value", 2)

    assert await first.get("value") == 1
    assert await second.get("value") == 2


@pytest.mark.asyncio
async def test_non_mapping_state_file_fails_at_read_boundary(tmp_path) -> None:
    core = _core_store(tmp_path)
    path = core.plugin_states_dir / "sample.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must contain a mapping"):
        await PluginStore(core, "sample").all()
