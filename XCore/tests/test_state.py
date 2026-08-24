"""StateService tests: persistence, atomicity, namespaces, recovery."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from xcore import Context, StateService


def _path(tmp_path):
    return tmp_path / "state.json"


async def test_roundtrip_and_defaults(tmp_path):
    state = StateService(path=_path(tmp_path))
    assert await state.get("missing") is None
    assert await state.get("missing", default="d") == "d"
    await state.set("k", {"nested": [1, 2], "ok": True})
    assert await state.get("k") == {"nested": [1, 2], "ok": True}
    await state.delete("k")
    assert await state.get("k") is None


async def test_persisted_across_instances(tmp_path):
    path = _path(tmp_path)
    await StateService(path=path).set("key", "value")
    # a fresh instance (simulating a restart) reads the same file
    recovered = StateService(path=path)
    assert await recovered.get("key") == "value"


async def test_atomic_write_leaves_no_temp_file(tmp_path):
    state = StateService(path=_path(tmp_path))
    await state.set("a", 1)
    files = [p.name for p in tmp_path.iterdir()]
    assert files == ["state.json"]


async def test_rejects_non_json_values(tmp_path):
    state = StateService(path=_path(tmp_path))
    with pytest.raises(TypeError):
        await state.set("bad", object())


async def test_corrupt_file_fails_loudly(tmp_path):
    path = _path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    state = StateService(path=path)
    with pytest.raises(RuntimeError, match="corrupted"):
        await state.get("k")


async def test_namespace_isolation(tmp_path):
    state = StateService(path=_path(tmp_path))
    goal = state.namespace("goal")
    todo = state.namespace("todo")
    await goal.set("title", "finish")
    await todo.set("items", [1])
    assert await goal.get("title") == "finish"
    assert await todo.get("title") is None
    assert await todo.get("items") == [1]
    assert await state.get("goal.title") == "finish"  # prefixed in the root view
    assert await goal.all() == {"title": "finish"}
    await goal.clear()
    assert await goal.get("title") is None
    assert await todo.get("items") == [1]


async def test_concurrent_namespace_writes_lose_no_keys(tmp_path):
    state = StateService(path=_path(tmp_path))
    namespaces = [state.namespace(f"ns{i}") for i in range(20)]

    async def writer(index: int):
        ns = namespaces[index]
        for j in range(10):
            await ns.set(f"k{j}", index)

    await asyncio.gather(*(writer(i) for i in range(20)))
    for index, ns in enumerate(namespaces):
        for j in range(10):
            assert await ns.get(f"k{j}") == index


async def test_crash_residue_does_not_corrupt_state(tmp_path):
    # simulate a crash mid-write: a stale .tmp file must be ignored
    path = _path(tmp_path)
    state = StateService(path=path)
    await state.set("k", "v")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("{garbage", encoding="utf-8")
    recovered = StateService(path=path)
    assert await recovered.get("k") == "v"


async def test_failed_write_does_not_change_cached_state(tmp_path, monkeypatch):
    state = StateService(path=_path(tmp_path))
    await state.set("stable", {"value": 1})

    async def fail(_data):
        raise OSError("disk full")

    monkeypatch.setattr(state, "_persist", fail)

    with pytest.raises(OSError, match="disk full"):
        await state.set("stable", {"value": 2})
    assert await state.get("stable") == {"value": 1}

    with pytest.raises(OSError, match="disk full"):
        await state.delete("stable")
    assert await state.get("stable") == {"value": 1}

    with pytest.raises(OSError, match="disk full"):
        await state.clear()
    assert await state.get("stable") == {"value": 1}

    recovered = StateService(path=_path(tmp_path))
    assert await recovered.get("stable") == {"value": 1}


async def test_state_service_via_context(tmp_path):
    ctx = Context(data_dir=tmp_path)
    await ctx.start()
    # ctx.state is lazily created and registered as the root service "state"
    svc = ctx.state
    assert ctx.has("state")
    assert svc is ctx.get("state")
    assert ctx.state is svc  # singleton
    await svc.set("session", {"turns": 3})
    await ctx.stop()
    await ctx.start()
    assert await ctx.state.get("session") == {"turns": 3}


async def test_context_uses_explicit_state_service(tmp_path):
    state = StateService(path=tmp_path / "thread" / "state.json")
    ctx = Context(data_dir=tmp_path / "unrelated", state_service=state)

    assert ctx.state is state
    assert ctx.get("state") is state
    await ctx.state.namespace("todo").set("items", ["one"])

    assert await state.namespace("todo").get("items") == ["one"]
    assert not (tmp_path / "unrelated" / "state.json").exists()


async def test_json_file_is_valid_utf8(tmp_path):
    state = StateService(path=_path(tmp_path))
    await state.set("greeting", "你好，世界")
    raw = json.loads(_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["greeting"] == "你好，世界"
