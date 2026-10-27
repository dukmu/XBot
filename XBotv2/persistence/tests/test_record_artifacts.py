"""Physical immutable ranges work for arbitrary owners and survive cache eviction."""
import asyncio
import json
import threading
from pathlib import Path

import pytest
import yaml

from XBotv2.agentloop.api.contracts import AGENT_LOOP, HumanInput, InboxItem
from XBotv2.core.api.domain import InboxTarget
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.tests.runtime import thread_scope


@pytest.fixture
async def records(tmp_path, request):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    config = paths.config_dir
    config.mkdir(parents=True)
    (config / "plugins.yaml").write_text(yaml.safe_dump([
        {"id": "persistence", "config": {"artifact_cache_bytes": 16000}},
    ]))
    scope = await thread_scope(
        paths=paths, session_id="s", llm_override=MockLLM([]),
        extra_plugins=[{"id": "persistence", "config": {"artifact_cache_bytes": 16000}}],
    )
    await scope.require(AGENT_LOOP).submit_input(InboxItem(
        id="seed", target=InboxTarget.NEXT_TURN, input=HumanInput(content="seed"),
    ), wake=False)
    backend = scope.root.require(PERSISTENCE)
    domain = backend.database("s", owner="notes", tables=())
    source = backend.rollout("s", "notes", domain=domain, project=lambda sql, stream, batch: None)
    async with source.lease(write=True):
        await source.initialize()
        for index in range(getattr(request, "param", 8)):
            await source.append("note", {"value": f"opaque-note-{index}"}, identity=f"note-{index}")
    try:
        yield scope, source, paths
    finally:
        await scope.root.close()


async def values(store, ref):
    data = b"".join([chunk async for chunk in store.read_chunks(ref, chunk_size=4096)])
    return [json.loads(line) for line in data.splitlines()]


@pytest.mark.parametrize("records", [605], indirect=True)
async def test_explicit_ids_preserve_more_than_query_budget_and_fail_missing(records):
    scope, source, paths = records
    ids = [f"note-{index}" for index in reversed(range(605))] + ["note-1"]
    ref = await scope.artifacts.reference_records("notes", record_ids=ids)
    assert [record["id"] for record in await values(scope.artifacts, ref)] == ids
    async with scope.artifacts.pin(ref) as path:
        assert [json.loads(line)["id"] for line in Path(path).read_bytes().splitlines()] == ids
    with pytest.raises(ValueError, match="Missing selected record"):
        await scope.artifacts.reference_records("notes", record_ids=[*ids, "missing"])
    assert [record["id"] for record in await values(scope.artifacts, ref)] == ids


async def test_fixed_cut_finishes_during_append_and_empty_files_eviction(records):
    scope, source, paths = records
    ref = await scope.artifacts.reference_records("notes", after_seq=3)
    async def append():
        async with source.lease(write=True):
            for index in range(8, 23):
                await source.append("note", {"value": f"later-{index}"}, identity=f"note-{index}")
    producer = asyncio.create_task(append())
    result = await values(scope.artifacts, ref)
    await producer
    assert [record["seq"] for record in result] == [4, 5, 6, 7, 8]
    with pytest.raises(ValueError, match="durable"):
        await scope.artifacts.reference_records("notes", through_seq=10000)
    empty = await scope.artifacts.reference_records("notes", record_ids=[])
    assert empty.size == 0 and await values(scope.artifacts, empty) == []
    scope.root.require(PERSISTENCE).artifact_cache.budget = 0
    async with scope.artifacts.pin(empty) as path:
        assert Path(path).read_bytes() == b""
    assert not Path(path).exists()
    async with scope.artifacts.pin(empty) as rebuilt:
        assert Path(rebuilt).read_bytes() == b""


async def test_shared_atomic_materialization_and_cancelled_io_drains(records, monkeypatch):
    import XBotv2.persistence.impl.artifacts as physical
    scope, source, paths = records
    ref = await scope.artifacts.reference_records("notes")
    started, finish = threading.Event(), threading.Event()
    append = physical._append
    def pause(path, data):
        if ".cache" in path.parts:
            started.set()
            assert finish.wait(5)
        append(path, data)
    monkeypatch.setattr(physical, "_append", pause)
    async def materialize():
        async with scope.artifacts.pin(ref) as path:
            return Path(path).read_bytes()
    task = asyncio.create_task(materialize())
    try:
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(.005)
        target = Path(scope.artifacts.resolve_model_path("artifact:" + ref.id))
        assert not target.exists()
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not target.exists()
    assert list(target.parent.glob(".*")) == []
    monkeypatch.setattr(physical, "_append", append)
    first, second = await asyncio.gather(materialize(), materialize())
    assert first == second
    assert len(first.splitlines()) == 8


async def test_fork_and_selection_exchange_rebind_without_source_session(records, tmp_path):
    from XBotv2.session.api.contracts import SESSIONS
    scope, source, paths = records
    ref = await scope.artifacts.reference_records("notes", record_ids=["note-7", "note-0"])
    backend = scope.root.require(PERSISTENCE)
    expected = await values(scope.artifacts, ref)
    target = await scope.root.require(SESSIONS).fork_session("s")
    copied = backend.artifacts(target, "main")
    assert await values(copied, ref) == expected
    package = tmp_path / "selection"
    await scope.artifacts.export_reference(ref, package)
    async with backend.prepare_session("imported") as prepared:
        imported = backend.artifacts("imported", "main")
        await backend.materialize_database(backend.paths("imported").database_file)
        await imported.import_reference(package, ref)
        assert await values(imported, ref) == expected
        await prepared.publish()
    await scope.root.require(SESSIONS).close_session("s")
    await scope.root.require(SESSIONS).delete_session("s")
    assert await values(copied, ref) == expected
    assert await values(backend.artifacts("imported", "main"), ref) == expected


async def test_progressive_foreign_cuts_exchange_and_readonly_sources(records, tmp_path):
    from XBotv2.agentloop.api.state import LOOP_STATE
    from XBotv2.agentloop.api.storage import ARCHIVE
    from XBotv2.core.api.messages import HumanInputMessage
    from XBotv2.core.api.parts import TextPart
    scope, source, paths = records
    first = await scope.artifacts.reference_records("notes", through_seq=4)
    second = await scope.artifacts.reference_records("notes", through_seq=8)
    await scope.require(LOOP_STATE).history.append(HumanInputMessage(
        id="shared-ranges", parts=(TextPart(text="Read both fixed ranges."),), artifacts=(first, second),
    ))
    archive = scope.root.require(ARCHIVE)
    target = tmp_path / "canonical.jsonl"
    await archive.export_rollout("s", "main", target)
    imported = await archive.import_rollout(target)
    backend = scope.root.require(PERSISTENCE)
    store = backend.artifacts(imported, "main")
    assert await values(store, first) == await values(scope.artifacts, first)
    assert await values(store, second) == await values(scope.artifacts, second)
    claim = backend.acquire(imported)
    try:
        domain = backend.database(imported, owner="notes", tables=())
        writer = backend.rollout(imported, "notes", domain=domain, project=lambda *args: pytest.fail("snapshot reached a business projector"))
        with pytest.raises(ValueError, match="snapshots cannot"):
            async with writer.lease(write=True):
                await writer.append("note", {"value": "forbidden"})
    finally:
        await backend.drain(imported)
        claim.release()


@pytest.mark.parametrize("target_count", [2, 6])
async def test_import_never_replaces_or_reinterprets_an_existing_owner_source(records, tmp_path, target_count):
    scope, source, paths = records
    backend = scope.root.require(PERSISTENCE)
    ref = await scope.artifacts.reference_records("notes", through_seq=4)
    package = tmp_path / "source-package"
    await scope.artifacts.export_reference(ref, package)
    claim = backend.acquire("target")
    try:
        await backend.materialize_database(backend.paths("target").database_file)
        domain = backend.database("target", owner="journal", tables=("journal_state",))
        await domain.transaction(lambda sql: sql.execute("CREATE TABLE journal_state(id TEXT PRIMARY KEY, value TEXT)"))
        def project(sql, stream, batch):
            for record, _, _ in batch:
                sql.execute("INSERT INTO journal_state VALUES(?,?)", (record["id"], record["data"]["value"]))
        target = backend.rollout("target", "notes", domain=domain, project=project)
        async with target.lease(write=True):
            await target.initialize()
            for index in range(target_count):
                await target.append("note", {"value": f"TARGET-{index}"}, identity=f"note-{index}")
        before = backend.paths("target").thread("notes").rollout_file.read_bytes()
        state = await domain.transaction(lambda sql: sql.execute("SELECT * FROM journal_state ORDER BY id"), write=False)
        with pytest.raises(ValueError, match="conflicts"):
            await backend.artifacts("target", "main").import_reference(package, ref)
        assert backend.paths("target").thread("notes").rollout_file.read_bytes() == before
        assert await domain.transaction(lambda sql: sql.execute("SELECT * FROM journal_state ORDER BY id"), write=False) == state
    finally:
        await backend.drain("target")
        claim.release()


async def test_failed_snapshot_extension_restores_old_prefix_before_retry(records, tmp_path, monkeypatch):
    import XBotv2.persistence.impl.record_artifacts as physical
    scope, source, paths = records
    backend = scope.root.require(PERSISTENCE)
    first = await scope.artifacts.reference_records("notes", through_seq=4)
    second = await scope.artifacts.reference_records("notes", through_seq=8)
    package4, package8 = tmp_path / "four", tmp_path / "eight"
    await scope.artifacts.export_reference(first, package4)
    await scope.artifacts.export_reference(second, package8)
    claim = backend.acquire("target")
    try:
        target = backend.artifacts("target", "main")
        await target.import_reference(package4, first)
        path = backend.paths("target").thread("notes").rollout_file
        before = path.read_bytes()
        append = physical._append_source
        def fail_after_write(*args):
            append(*args)
            raise OSError("tail write failed")
        monkeypatch.setattr(physical, "_append_source", fail_after_write)
        with pytest.raises(OSError, match="tail write failed"):
            await target.import_reference(package8, second)
        assert path.read_bytes() == before
        assert len(await values(target, first)) == 4
        with pytest.raises(ValueError, match="durable"):
            await target.reference_records("notes", through_seq=8)
        monkeypatch.setattr(physical, "_append_source", append)
        await target.import_reference(package8, second)
        assert len(await values(target, first)) == 4
        assert len(await values(target, second)) == 8
    finally:
        await backend.drain("target")
        claim.release()
