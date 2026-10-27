"""Real SQLite worker lifetime, thread confinement and bounded admission."""
import asyncio
import sqlite3
import threading
import pytest
from XBotv2.persistence.impl.database import DatabaseWorker, StorageLimits


async def test_worker_confinement_budget_and_cancelled_call_waits_for_real_work(tmp_path):
    worker = DatabaseWorker(StorageLimits(queue_operations=2, queue_bytes=128))
    started = threading.Event()
    release = threading.Event()
    path = tmp_path / 'state.sqlite3'

    def blocked(c):
        started.set()
        assert release.wait(5)
        return threading.get_ident(), c.execute('SELECT 1').fetchone()[0]
    first = asyncio.create_task(worker.call(path, blocked, create=True, size=64))
    others = []
    try:
        assert await asyncio.to_thread(started.wait, 5)
        others = [
            asyncio.create_task(
                worker.call(
                    path,
                    lambda c: threading.get_ident(),
                    size=64)) for _ in range(2)]
        await asyncio.sleep(.01)
        assert worker._operations == 2 and worker._bytes == 128
        assert not others[-1].done()
        first.cancel()
        await asyncio.sleep(.01)
        assert not first.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        ids = await asyncio.gather(*others)
        assert ids[0] == ids[1] and ids[0] != threading.get_ident()
        with pytest.raises(ValueError, match='byte budget'):
            await worker.call(path, lambda c: None, size=129)
        with pytest.raises(TypeError, match='SQLite objects'):
            await worker.call(path, lambda c: c.execute('SELECT 1'))

        async def wrong(c):
            return 1
        with pytest.raises(TypeError, match='synchronous'):
            await worker.call(path, wrong)
        assert await worker.call(path, lambda c: c.execute('SELECT 2').fetchone()[0]) == 2
    finally:
        release.set()
        await asyncio.gather(first, *others, return_exceptions=True)
        await worker.close()
    assert not worker._thread.is_alive()


async def test_rollback_and_close_errors_are_delivered_without_killing_worker(
        tmp_path, monkeypatch):
    connect = sqlite3.connect

    class BrokenCleanup(sqlite3.Connection):
        def rollback(self):
            super().rollback()
            raise RuntimeError('rollback fault')

        def close(self):
            super().close()
            raise RuntimeError('close fault')
    count = 0

    def controlled(*args, **kwargs):
        nonlocal count
        count += 1
        return connect(*args, **kwargs, **({'factory': BrokenCleanup} if count == 1 else {}))
    monkeypatch.setattr(sqlite3, 'connect', controlled)
    worker = DatabaseWorker(StorageLimits())
    try:
        def fail(c):
            raise ValueError('transaction fault')
        with pytest.raises(ExceptionGroup) as error:
            await worker.call(tmp_path / 'state.sqlite3', fail, create=True)
        assert len(error.value.exceptions) == 3
        assert await asyncio.wait_for(worker.call(tmp_path / 'state.sqlite3', lambda c: 3), 5) == 3
    finally:
        await worker.close()
