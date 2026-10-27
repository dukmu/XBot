"""Shared physical-record cache bounds, invalidation and owner lifetime."""
import pytest

from xcore.api.context import Context
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.runtime_logging import DEFAULT_RUNTIME_LOG
from XBotv2.loader.api.contracts import PluginEntry, PluginTree
from XBotv2.loader.api.service import create_loader
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.persistence.api.models import StreamCorrupt
from XBotv2.persistence.impl.record_cache import RecordCache


def test_pins_bound_eviction_and_retire_on_invalidation():
    cache = RecordCache(1100)
    a = ("s", "/a", 0, 10, "a")
    b = ("s", "/b", 0, 10, "b")
    c = ("s", "/c", 0, 10, "c")
    with cache.hold(a, (1,), b"a" * 10):
        with cache.hold(b, (1,), b"b" * 10):
            assert cache.stats["pins"] == 2
            with cache.hold(c, (1,), b"c" * 10):
                assert cache.stats["bypasses"] == 1
            cache.invalidate("/a")
            assert cache.lookup(a) is None
            assert cache.stats["pinned_bytes"] > 0
        assert cache.lookup(b) == b"b" * 10
    assert cache.stats["pins"] == 0
    with cache.hold(c, (1,), b"c" * 10):
        assert cache.stats["resident_bytes"] <= cache.stats["budget_bytes"]
    d = ("s", "/d", 0, 10, "d")
    with cache.hold(d, (1,), b"d" * 10):
        assert cache.stats["evictions"] >= 1
    cache.clear()
    assert cache.stats["resident_bytes"] == cache.stats["entries"] == 0


def test_staging_root_drop_does_not_evict_other_roots():
    cache = RecordCache(4096)
    staged = ("target", "/tmp/stage/threads/main", 0, 4, "one")
    target = ("target", "/tmp/target/threads/main", 0, 4, "one")
    with cache.hold(staged, (1,), b"old\n"):
        with cache.hold(target, (2,), b"new\n"):
            cache.drop_root("/tmp/stage")
            assert cache.lookup(staged) is None
            assert cache.lookup(target) == b"new\n"
            assert cache.stats["pins"] == 2
    assert cache.stats["entries"] == 1
    assert cache.stats["pins"] == 0


def test_zero_and_oversized_records_do_not_reside():
    for budget in (0, 600):
        cache = RecordCache(budget)
        key = ("s", "/a", 0, 1000, "a")
        with cache.hold(key, (1,), b"x" * 1000) as data:
            assert len(data) == 1000
        assert cache.stats["bypasses"] == 1
        assert cache.stats["resident_bytes"] == 0


@pytest.mark.asyncio
async def test_real_rollout_cache_append_rebuild_corruption_and_drain(tmp_path):
    root = Context()
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root.set("runtime_paths", paths)
    root.set("runtime_log", DEFAULT_RUNTIME_LOG)
    loader = create_loader()
    await loader.mount(root, PluginTree([PluginEntry("persistence", "persistence")]),
                       kind="process", required=(PERSISTENCE,))
    backend = root.require(PERSISTENCE)
    claim = backend.acquire("s")
    try:
        domain = backend.database("s", owner="cache_test", tables=("cache_test_values",))
        await domain.transaction(lambda sql: sql.execute(
            "CREATE TABLE cache_test_values(id TEXT PRIMARY KEY)"
        ))
        def project(sql, stream, batch):
            for record, _, _ in batch:
                sql.execute("INSERT INTO cache_test_values VALUES(?)", (record["id"],))
        stream = backend.rollout("s", "thread", domain=domain, project=project)
        async with stream.lease(write=True):
            await stream.initialize()
            await stream.append("event", {"value": "old"}, identity="a")
            first = await stream.record("a")
            first["data"]["value"] = "changed locally"
            assert (await stream.record("a"))["data"]["value"] == "old"
            hits = backend.record_cache_stats["hits"]
            assert hits >= 1
            await stream.append("event", {"value": "second"}, identity="b")
            assert (await stream.record("a"))["data"]["value"] == "old"
            assert backend.record_cache_stats["hits"] > hits
        async with backend.copy_session("s", "fork") as prepared:
            target_domain = backend.database("fork", owner="cache_test", tables=("cache_test_values",))
            staged = backend.rollout("fork", "thread", domain=target_domain, project=project)
            assert (await staged.record("a"))["data"]["value"] == "old"
            staged_count = backend.record_cache_stats["entries"]
            await prepared.publish()
            assert backend.record_cache_stats["entries"] < staged_count
        target_domain = backend.database("fork", owner="cache_test", tables=("cache_test_values",))
        forked = backend.rollout("fork", "thread", domain=target_domain, project=project)
        assert (await forked.record("a"))["data"]["value"] == "old"
        path = paths.session("s").thread("thread").rollout_file
        original = path.read_bytes()
        before = path.stat()
        corrupted = original.replace(b'"value":"old"', b'"value":bad!!', 1)
        assert corrupted != original
        path.write_bytes(corrupted)
        import os
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns - 1_000_000_000))
        after = path.stat()
        assert (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)
        with pytest.raises(StreamCorrupt):
            await stream.record("a")
        path.write_bytes(original)
        async with stream.lease(write=True):
            await domain.transaction(lambda sql: sql.execute("DELETE FROM cache_test_values"))
            await stream.reset()
            await stream.recover()
            assert (await stream.record("a"))["data"]["value"] == "old"
        await backend.drain("s")
        assert backend.record_cache_stats["entries"] == 1  # fork remains independently cached
        backend.record_cache.drop_session("fork")
        assert backend.record_cache_stats["resident_bytes"] == 0
    finally:
        claim.release()
        await root.close()
        loader.close()
    assert backend.record_cache_stats["pins"] == 0


@pytest.mark.asyncio
async def test_real_conversation_and_team_share_cache(tmp_path):
    from XBotv2.agentloop.api.state import LOOP_STATE
    from XBotv2.agentloop.api.storage import ARCHIVE
    from XBotv2.core.api.messages import HumanInputMessage
    from XBotv2.core.api.parts import TextPart
    from XBotv2.llm.impl.mock import MockLLM
    from XBotv2.team.api.service import TEAM
    from XBotv2.tests.runtime import thread_scope

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(
        paths=paths, session_id="shared", llm_override=MockLLM(),
        extra_plugins=[
            {"id": "caption", "config": {"auto": False}},
            {"id": "team", "config": {"enabled": True}},
            {"id": "persistence", "config": {"record_cache_bytes": 8192}},
        ],
    )
    backend = scope.root.require(PERSISTENCE)
    try:
        await scope.require(LOOP_STATE).history.append(
            HumanInputMessage(id="first", parts=(TextPart(text="conversation"),))
        )
        team = scope.require(TEAM)
        await team.add_member("g", "main")
        await team.publish("g", "board-1", "main", "team message", [])
        backend.record_cache.clear()
        archive = scope.root.require(ARCHIVE)
        first_page = await archive.message_page("shared", "main", limit=1)
        assert first_page.items[0].id == "first"
        first_stats = backend.record_cache_stats
        assert first_stats["entries"] > 0 and first_stats["misses"] > 0
        await archive.message_page("shared", "main", limit=1)
        assert backend.record_cache_stats["hits"] > first_stats["hits"]
        board = [record async for record in team.latest("g", 1)]
        board[0]["data"]["content"] = "local mutation"
        after_board = backend.record_cache_stats
        again = [record async for record in team.latest("g", 1)]
        assert again[0]["data"]["content"] == "team message"
        assert backend.record_cache_stats["hits"] > after_board["hits"]
        assert backend.record_cache_stats["resident_bytes"] <= 8192
    finally:
        await scope.root.close()
    assert backend.record_cache_stats["resident_bytes"] == 0
    assert backend.record_cache_stats["pins"] == 0


@pytest.mark.asyncio
async def test_active_decoding_pins_bytes_across_invalidation(tmp_path, monkeypatch):
    import asyncio
    import threading
    import XBotv2.persistence.impl.stream as physical_stream

    root = Context()
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root.set("runtime_paths", paths)
    root.set("runtime_log", DEFAULT_RUNTIME_LOG)
    loader = create_loader()
    await loader.mount(root, PluginTree([PluginEntry("persistence", "persistence")]),
                       kind="process", required=(PERSISTENCE,))
    backend = root.require(PERSISTENCE)
    claim = backend.acquire("s")
    started, release = threading.Event(), threading.Event()
    task = None
    try:
        domain = backend.database("s", owner="cache_test", tables=("cache_test_values",))
        await domain.transaction(lambda sql: sql.execute(
            "CREATE TABLE cache_test_values(id TEXT PRIMARY KEY)"
        ))
        def project(sql, stream, batch):
            for record, _, _ in batch:
                sql.execute("INSERT INTO cache_test_values VALUES(?)", (record["id"],))
        stream = backend.rollout("s", "thread", domain=domain, project=project)
        async with stream.lease(write=True):
            await stream.initialize()
            await stream.append("event", {"value": "held"}, identity="one")
        assert (await stream.record("one"))["data"]["value"] == "held"
        original = physical_stream.decode_record
        def blocked(*args):
            started.set()
            assert release.wait(5)
            return original(*args)
        monkeypatch.setattr(physical_stream, "decode_record", blocked)
        task = asyncio.create_task(stream.record("one"))
        assert await asyncio.to_thread(started.wait, 5)
        assert backend.record_cache_stats["pins"] == 1
        backend.record_cache.clear()
        assert backend.record_cache_stats["entries"] == 0
        assert backend.record_cache_stats["pinned_bytes"] > 0
        release.set()
        assert (await task)["data"]["value"] == "held"
        assert backend.record_cache_stats["pins"] == 0
        assert backend.record_cache_stats["resident_bytes"] == 0
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        claim.release()
        await root.close()
        loader.close()
