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


async def test_memory_state_namespaces_share_values_without_creating_files(tmp_path):
    state = StateService.memory()
    ctx = Context(data_dir=tmp_path / "unused", state_service=state)
    await ctx.start()
    try:
        source = {"items": ["one"]}
        await ctx.state.namespace("goal").set("value", source)
        source["items"].append("outside")
        assert await state.namespace("goal").get("value") == {"items": ["one"]}
        await asyncio.gather(*(
            state.namespace("todo").set(str(index), index) for index in range(10)
        ))
        assert await ctx.state.namespace("todo").all() == {
            str(index): index for index in range(10)
        }
        await state.namespace("todo").delete("0")
        assert "0" not in await state.namespace("todo").keys()
        await state.namespace("todo").clear()
        assert await state.namespace("todo").all() == {}
        await ctx.stop()
        await ctx.start()
        assert await ctx.state.namespace("goal").get("value") == {"items": ["one"]}
        with pytest.raises(TypeError):
            await state.set("bad", object())
    finally:
        await ctx.destroy()
    assert list(tmp_path.iterdir()) == []
    assert await StateService.memory().all() == {}


async def test_persisted_across_instances(tmp_path):
    path = _path(tmp_path)
    await StateService(path=path).set("key", "value")
    # a fresh instance (simulating a restart) reads the same file
    recovered = StateService(path=path)
    assert await recovered.get("key") == "value"


@pytest.mark.parametrize("count", [20, 40])
async def test_growing_state_appends_only_changed_keys(tmp_path, count):
    path = _path(tmp_path)
    state = StateService(path=path)
    await state.set("key-0", "payload" * 100)
    prefix = path.read_bytes()
    for index in range(1, count):
        await state.set(f"key-{index}", "payload" * 100)
    assert path.read_bytes().startswith(prefix)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[0] == {"version": 1}
    assert len(records) == count + 1
    assert records[-1] == {"op": "set", "key": f"key-{count - 1}", "value": "payload" * 100}
    assert len(await StateService(path=path).all()) == count


async def test_append_leaves_no_temp_file(tmp_path):
    state = StateService(path=_path(tmp_path))
    await state.set("a", 1)
    files = [p.name for p in tmp_path.iterdir()]
    assert files == ["state.json"]


async def test_state_operations_emit_content_safe_metadata_logs(tmp_path, caplog):
    caplog.set_level("DEBUG", logger="xcore.state")
    state = StateService(path=_path(tmp_path)).namespace("todo")

    await state.set("snapshot", {"secret": "must-not-be-logged"})
    await state.delete("snapshot")
    await state.clear()

    text = caplog.text
    assert "state.loaded" in text
    assert "state.persisted operation=set" in text
    assert "state.persisted operation=delete" in text
    assert "state.persisted operation=clear" in text
    assert "namespace=todo." in text
    assert "must-not-be-logged" not in text


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
    path = _path(tmp_path)
    state = StateService(path=path)
    await state.set("k", "v")
    prefix = path.read_bytes()
    # A crash can split a UTF-8 code point, not just a JSON token.
    with path.open("ab") as stream:
        stream.write(b'{"op":"set","key":"k","value":"\xe4')
    recovered = StateService(path=path)
    assert await recovered.get("k") == "v"
    await recovered.set("next", 2)
    assert path.read_bytes().startswith(prefix)
    assert await StateService(path=path).all() == {"k": "v", "next": 2}


@pytest.mark.parametrize("payload", [b'', b'{"old":1}', b'{"old":1}\n',
    b'{"version":true}\n', b'{"version":1}\n{broken}\n',
    b'{"version":1}\n{"op":"delete","key":"missing"}\n'])
async def test_invalid_committed_state_is_not_silently_replaced(tmp_path, payload):
    path = _path(tmp_path)
    path.write_bytes(payload)
    with pytest.raises(RuntimeError, match="corrupted or unsupported"):
        await StateService(path=path).set("new", 2)
    assert path.read_bytes() == payload


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["write", "fsync"])
async def test_append_failure_preserves_disk_and_cache(tmp_path, monkeypatch, existing, failure):
    path = _path(tmp_path)
    state = StateService(path=path)
    if existing:
        await state.set("stable", 1)
    prefix = path.read_bytes() if existing else b""
    write = os.write
    fsync = os.fsync
    calls = 0

    def fail_after_partial(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return write(fd, data[:7])
        raise OSError("disk full")

    def fail_first_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk full")
        return fsync(fd)

    with monkeypatch.context() as patch:
        if failure == "write":
            patch.setattr(os, "write", fail_after_partial)
        else:
            patch.setattr(os, "fsync", fail_first_sync)
        with pytest.raises(OSError, match="disk full"):
            await state.set("new", 2)
    assert path.exists() == existing
    if existing:
        assert path.read_bytes() == prefix
    assert await state.all() == ({"stable": 1} if existing else {})
    await state.set("new", 2)
    assert await StateService(path=path).all() == await state.all()


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


async def test_context_state_resolves_plugin_owned_service(tmp_path):
    ctx = Context(data_dir=tmp_path)
    state = StateService.memory()

    def provider(scope, config):
        scope.set("state", state)

    async def consumer(scope, config):
        assert scope.state is state
        await scope.state.set("value", "shared")

    consumer.inject = ["state"]
    ctx.plugin(consumer)
    ctx.plugin(provider)
    try:
        await ctx.start()
        assert ctx.state is state
        assert await ctx.state.get("value") == "shared"
        assert not (tmp_path / "state.json").exists()
    finally:
        await ctx.destroy()


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
    raw = _path(tmp_path).read_text(encoding="utf-8")
    assert "你好，世界" in raw
    assert await StateService(path=_path(tmp_path)).get("greeting") == "你好，世界"
