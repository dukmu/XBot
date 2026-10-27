"""Real SQLite worker lifetime, thread confinement and bounded admission."""
import asyncio
import gc
import sqlite3
import threading
import weakref
from dataclasses import dataclass
import pytest
from XBotv2.persistence.impl.database import DatabaseWorker, StorageLimits


async def test_idle_worker_releases_result_and_callback_capture(tmp_path):
    @dataclass
    class Payload:
        data: str

    worker = DatabaseWorker(StorageLimits())
    try:
        async def request():
            captured = Payload('a' * 1024 * 1024)
            output = Payload('b' * 1024 * 1024)
            callback = lambda connection, held=captured: output
            references = weakref.ref(captured), weakref.ref(output)
            await worker.call(tmp_path / 'state.sqlite3', callback, create=True)
            return references

        captured_ref, output_ref = await request()
        await asyncio.sleep(0)
        gc.collect()
        assert worker._thread.is_alive()
        assert captured_ref() is None and output_ref() is None
    finally:
        await worker.close()


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
        close_failed = False

        def rollback(self):
            super().rollback()
            raise RuntimeError('rollback fault')

        def close(self):
            super().close()
            if not self.close_failed:
                self.close_failed = True
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


async def test_connection_reuse_release_and_replaced_database(tmp_path, monkeypatch):
    connect = sqlite3.connect
    opened = []

    def counted(*args, **kwargs):
        opened.append(threading.get_ident())
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', counted)
    worker = DatabaseWorker(StorageLimits())
    path = tmp_path / 'state.sqlite3'
    replacement = tmp_path / 'replacement.sqlite3'
    try:
        await worker.call(path, lambda c: c.execute('CREATE TABLE data(value)'), create=True)
    except TypeError:
        # Returning a cursor is prohibited even when the statement succeeded.
        pass
    try:
        def initialize(c):
            c.execute('CREATE TABLE IF NOT EXISTS data(value)')
            c.execute('INSERT INTO data VALUES(1)')
        await worker.call(path, initialize)
        await worker.call(path, lambda c: c.execute('SELECT * FROM data').fetchall())
        assert len(opened) == 2  # failed callback discarded its connection
        with connect(replacement) as other:
            other.execute('CREATE TABLE data(value)')
            other.execute('INSERT INTO data VALUES(2)')
        replacement.replace(path)
        assert await worker.call(path, lambda c: c.execute('SELECT value FROM data').fetchone()[0]) == 2
        assert len(opened) == 3
        await worker.release(path)
        assert await worker.call(path, lambda c: c.execute('SELECT value FROM data').fetchone()[0]) == 2
        assert len(opened) == 4
        assert all(ident != threading.get_ident() for ident in opened)
    finally:
        await worker.close()


async def test_failed_transaction_does_not_leak_authorizer_or_writes(tmp_path):
    worker = DatabaseWorker(StorageLimits())
    path = tmp_path / 'state.sqlite3'
    try:
        def create(c):
            c.execute('CREATE TABLE data(value)')
        await worker.call(path, create, create=True)

        def fail(c):
            c.execute('INSERT INTO data VALUES(1)')
            c.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
            raise ValueError('failed')
        with pytest.raises(ValueError, match='failed'):
            await worker.call(path, fail)
        assert await worker.call(path, lambda c: c.execute('SELECT count(*) FROM data').fetchone()[0]) == 0
    finally:
        await worker.close()


async def test_release_close_error_reaches_caller_and_worker_continues(tmp_path, monkeypatch):
    connect = sqlite3.connect
    count = 0
    allow_close = threading.Event()

    class BrokenClose(sqlite3.Connection):
        def close(self):
            if not allow_close.is_set():
                raise RuntimeError('close fault')
            super().close()

    def controlled(*args, **kwargs):
        nonlocal count
        count += 1
        return connect(*args, **kwargs, factory=BrokenClose)

    monkeypatch.setattr(sqlite3, 'connect', controlled)
    worker = DatabaseWorker(StorageLimits())
    path = tmp_path / 'state.sqlite3'
    try:
        assert await worker.call(path, lambda c: 1, create=True) == 1
        with pytest.raises(RuntimeError, match='close fault'):
            await worker.release(path)
        assert await worker.call(tmp_path / 'other.sqlite3', lambda c: 2, create=True) == 2
        assert count == 2
        allow_close.set()
        assert await worker.call(path, lambda c: 2) == 2
        allow_close.clear()
        with pytest.raises(ExceptionGroup, match='connection cleanup failed'):
            await worker.close()
        assert worker._thread.is_alive()
        allow_close.set()
    finally:
        allow_close.set()
        await worker.close()


async def test_connection_lru_and_targeted_release(tmp_path, monkeypatch):
    connect = sqlite3.connect
    opened = []
    closed = []

    class Tracked(sqlite3.Connection):
        def close(self):
            closed.append(self.name)
            super().close()

    def counted(path, *args, **kwargs):
        kwargs['factory'] = Tracked
        connection = connect(path, *args, **kwargs)
        connection.name = str(path).split('?')[0].removeprefix('file://')
        opened.append(connection.name)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', counted)
    worker = DatabaseWorker(StorageLimits(connection_limit=2))
    a, b, c = (tmp_path / f'{name}.sqlite3' for name in 'abc')
    try:
        for path in (a, b, a, c):
            await worker.call(path, lambda connection: 1, create=True)
        assert opened == [str(a), str(b), str(c)]
        assert closed == [str(b)]
        await worker.release(a)
        assert closed == [str(b), str(a)]
        await worker.call(c, lambda connection: 1)
        assert len(opened) == 3
        await worker.release()
        assert closed == [str(b), str(a), str(c)]
    finally:
        await worker.close()


async def test_release_all_closes_other_connections_after_failure(tmp_path, monkeypatch):
    connect = sqlite3.connect
    allow_close = threading.Event()
    closed = []
    broken = tmp_path / 'broken.sqlite3'
    healthy = tmp_path / 'healthy.sqlite3'

    class Tracked(sqlite3.Connection):
        def close(self):
            if self.name == str(broken) and not allow_close.is_set():
                raise RuntimeError('close fault')
            super().close()
            closed.append(self.name)

    def controlled(path, *args, **kwargs):
        kwargs['factory'] = Tracked
        connection = connect(path, *args, **kwargs)
        connection.name = str(path)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', controlled)
    worker = DatabaseWorker(StorageLimits())
    try:
        await worker.call(broken, lambda c: 1, create=True)
        await worker.call(healthy, lambda c: 1, create=True)
        with pytest.raises(ExceptionGroup, match='connection cleanup failed') as failure:
            await worker.release()
        assert len(failure.value.exceptions) == 1
        assert 'close fault' in str(failure.value.exceptions[0])
        assert closed == [str(healthy)]
        allow_close.set()
        await worker.release()
        assert closed == [str(healthy), str(broken)]
    finally:
        allow_close.set()
        await worker.close()
