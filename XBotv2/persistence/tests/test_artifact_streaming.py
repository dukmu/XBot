"""Bounded byte-preserving objects and actual publication/I/O lifetimes."""
import asyncio
import hashlib
import threading

import pytest

from XBotv2.core.api.artifacts import ArtifactKind
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.persistence.impl.artifacts import ArtifactStore, CHUNK_BYTES
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.tests.runtime import thread_scope
from XBotv2.llm.impl.mock import MockLLM


async def pieces(payload):
    # Producers may yield larger pieces; storage/read buffers remain bounded.
    for start in range(0, len(payload), 1024 * 1024):
        yield payload[start:start + 1024 * 1024]


async def test_stream_adopt_and_bounded_byte_ranges_preserve_original(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path).session("s").thread("t")
    store = ArtifactStore(paths)
    payload = bytes(range(256)) * 36001
    ref = await store.put_stream(ArtifactKind.TOOL_RESULT, pieces(payload))
    assert ref.size == len(payload)
    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    chunks = [chunk async for chunk in store.read_chunks(ref)]
    assert all(0 < len(chunk) <= CHUNK_BYTES for chunk in chunks)
    assert b"".join(chunks) == payload
    page = b"".join([chunk async for chunk in store.read_chunks(ref, offset=123456, limit=9001)])
    assert page == payload[123456:132457]
    async with store.spool_file() as spool:
        spool.write_bytes(payload)
        adopted = await store.adopt_file(ArtifactKind.TOOL_RESULT, spool)
        assert not spool.exists()
    assert adopted == ref
    assert await store.read(adopted) == payload
    assert list((paths.session.root / ".spool").iterdir()) == []


async def test_source_error_and_cancel_remove_only_after_file_worker_exits(tmp_path, monkeypatch):
    import XBotv2.persistence.impl.artifacts as module
    paths = RuntimePaths.from_data_dir(tmp_path).session("s").thread("t")
    store = ArtifactStore(paths)
    async def broken():
        yield b"prefix"
        raise OSError("source failure")
    with pytest.raises(OSError, match="source failure"):
        await store.put_stream(ArtifactKind.CONTEXT, broken())
    assert list((paths.session.root / ".spool").iterdir()) == []

    started, finish = threading.Event(), threading.Event()
    original = module._append
    def slow_write(path, chunk):
        started.set()
        assert finish.wait(5)
        assert path.exists()
        original(path, chunk)
    monkeypatch.setattr(module, "_append", slow_write)
    task = asyncio.create_task(store.put_stream(ArtifactKind.CONTEXT, pieces(b"cancelled")))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        assert len(list((paths.session.root / ".spool").iterdir())) == 1
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((paths.session.root / ".spool").iterdir()) == []
    assert not paths.artifacts_dir.exists()


@pytest.mark.parametrize("first_fails", [False, True])
async def test_same_object_publications_share_lock_across_store_instances(tmp_path, first_fails):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(paths=paths, session_id="s", llm_override=MockLLM([]))
    resources = scope.root.require(PERSISTENCE)
    store_a = resources.artifacts("s", "main")
    store_b = resources.artifacts("s", "other")
    entered, release = asyncio.Event(), asyncio.Event()
    payload = b"same original\xff\x00" * 10000
    async def publish(store, fail, first=False):
        async with store.prepare_stream(ArtifactKind.CONTEXT, pieces(payload)) as ref:
            if first:
                entered.set()
                await release.wait()
            if fail:
                raise ValueError("caller did not commit")
            return ref
    try:
        first = asyncio.create_task(publish(store_a, first_fails, True))
        await entered.wait()
        second = asyncio.create_task(publish(store_b, not first_fails))
        await asyncio.sleep(.03)
        assert not second.done()
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        ref = next(result for result in results if not isinstance(result, BaseException))
        assert await store_b.read(ref) == payload
        assert len(list(paths.session("s").root.joinpath("objects").iterdir())) == 1
    finally:
        release.set()
        await scope.root.close()
