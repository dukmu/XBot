"""One process-owned SQLite thread with bounded asynchronous admission."""

from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
import inspect
from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class StorageLimits:
    # Experimental limits; benchmarks report these rather than claiming a
    # hardware-independent latency or memory target.
    connection_limit: int = 32
    queue_operations: int = 64
    queue_bytes: int = 16 * 1024 * 1024
    replay_records: int = 128
    replay_bytes: int = 1024 * 1024
    record_bytes: int = 8 * 1024 * 1024
    kv_bytes: int = 64 * 1024


class DatabaseWorker:
    def __init__(self, limits: StorageLimits):
        self.limits = limits
        self._queue = queue.Queue()
        self._admission = asyncio.Condition()
        self._operations = self._bytes = 0
        self._closing = False
        self._thread = threading.Thread(
            target=self._run, name="xbot-sqlite", daemon=False
        )
        self._thread.start()

    async def call(
        self,
        path: Path,
        callback: Callable[[sqlite3.Connection], T],
        *,
        size: int = 0,
        create: bool = False,
    ) -> T:
        return await self._submit(path, callback, size=size, create=create)

    async def release(self, path: Path | None = None):
        """Close the connection for path, or all connections when path is None."""
        await self._submit(path, None, size=0, create=False)

    async def _submit(self, path, callback, *, size, create):
        if size < 0 or size > self.limits.queue_bytes:
            raise ValueError(
                "Database operation exceeds the configured queue byte budget"
            )
        async with self._admission:
            await self._admission.wait_for(
                lambda: (
                    self._closing
                    or (
                        self._operations < self.limits.queue_operations
                        and self._bytes + size <= self.limits.queue_bytes
                    )
                )
            )
            if self._closing:
                raise RuntimeError("Persistence is closing")
            self._operations += 1
            self._bytes += size
        loop = asyncio.get_running_loop()
        result = loop.create_future()
        self._queue.put((loop, result, path, callback, create))
        # Once admitted, the real transaction must finish before cancellation
        # can release its owner/lock or budget.
        try:
            while True:
                try:
                    value = await asyncio.shield(result)
                    break
                except asyncio.CancelledError:
                    if result.done():
                        result.result()
                        raise
                    while not result.done():
                        try:
                            await asyncio.shield(result)
                        except asyncio.CancelledError:
                            continue
                    result.result()
                    raise
            return value
        finally:
            async with self._admission:
                self._operations -= 1
                self._bytes -= size
                self._admission.notify_all()

    @staticmethod
    def _complete(future, value, error):
        if not future.done():
            if error is None:
                future.set_result(value)
            else:
                future.set_exception(error)

    @staticmethod
    def _result(value):
        if isinstance(value, (sqlite3.Cursor, sqlite3.Connection)):
            raise TypeError(
                "Database callbacks must return ordinary values, never SQLite objects"
            )
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            raise TypeError("Database transactions must be synchronous and short")
        if isinstance(value, sqlite3.Row):
            return tuple(DatabaseWorker._result(item) for item in value)
        if isinstance(value, tuple):
            return tuple(DatabaseWorker._result(item) for item in value)
        if isinstance(value, list):
            return [DatabaseWorker._result(item) for item in value]
        if isinstance(value, dict):
            return {key: DatabaseWorker._result(item) for key, item in value.items()}
        if is_dataclass(value) and not isinstance(value, type):
            for field in fields(value):
                DatabaseWorker._result(getattr(value, field.name))
        return value

    def _run(self):
        # Only this worker thread touches the connections and their LRU order.
        connections = OrderedDict()
        poisoned = set()

        def discard(path):
            connection, _ = connections[path]
            poisoned.add(path)
            connection.close()
            del connections[path]
            poisoned.discard(path)

        while (work := self._queue.get()) is not None:
            loop, result, path, callback, create = work
            value = error = None
            connection = None
            try:
                if callback is None:
                    if path is None:
                        close_errors = []
                        for target in list(connections):
                            try:
                                discard(target)
                            except BaseException as close_error:
                                close_errors.append(close_error)
                        if close_errors:
                            raise BaseExceptionGroup(
                                "Database connection cleanup failed", close_errors
                            )
                    elif path in connections:
                        discard(path)
                else:
                    current = None
                    try:
                        stat = path.stat()
                        current = (stat.st_dev, stat.st_ino)
                    except FileNotFoundError:
                        pass
                    if path in connections:
                        _, identity = connections[path]
                        if path in poisoned or current != identity:
                            discard(path)
                    if path not in connections:
                        if len(connections) >= self.limits.connection_limit:
                            discard(next(iter(connections)))
                        if create:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            connection = sqlite3.connect(path)
                        else:
                            connection = sqlite3.connect(
                                path.as_uri() + "?mode=rw", uri=True
                            )
                        # Register before initialization so a failure can close it.
                        connections[path] = (connection, None)
                        stat = path.stat()
                        connections[path] = (connection, (stat.st_dev, stat.st_ino))
                        connection.row_factory = sqlite3.Row
                        connection.execute("PRAGMA foreign_keys=ON")
                        connection.execute("PRAGMA busy_timeout=1000")
                    else:
                        connection, _ = connections[path]
                    connections.move_to_end(path)
                    value = self._result(callback(connection))
                    connection.set_authorizer(None)
                    connection.commit()
            except BaseException as exc:
                error = exc
                if connection is not None:
                    cleanup_errors = []
                    poisoned.add(path)
                    try:
                        connection.set_authorizer(None)
                        connection.rollback()
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                    try:
                        discard(path)
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                    if cleanup_errors:
                        error = BaseExceptionGroup(
                            "Database operation cleanup failed", [error, *cleanup_errors]
                        )
            loop.call_soon_threadsafe(self._complete, result, value, error)
            del work, loop, result, path, callback, create, value, error, connection

    async def close(self):
        async with self._admission:
            self._closing = True
            self._admission.notify_all()
            await self._admission.wait_for(lambda: self._operations == 0)
        if self._thread.is_alive():
            loop = asyncio.get_running_loop()
            result = loop.create_future()
            self._queue.put((loop, result, None, None, False))
            await asyncio.shield(result)
            self._queue.put(None)
            await asyncio.to_thread(self._thread.join)


def execute(connection, sql, parameters=()):
    """Execute a write without leaking its SQLite cursor to the event loop."""
    connection.execute(sql, parameters)
