"""Small namespaced KV and table-scoped transactions on the owning DB worker."""

from __future__ import annotations
from XBotv2.persistence.impl.database import execute

_BIND_NOW = object()
import json
import re
import sqlite3


class SqlTransaction:
    def __init__(self, connection, limit):
        self._connection = connection
        self._limit = limit

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        self.changed = max(0, cursor.rowcount)
        if cursor.description is None:
            return ()
        rows = cursor.fetchmany(self._limit + 1)
        if len(rows) > self._limit:
            raise ValueError(
                "Domain transaction query exceeds row budget; use bounded pages"
            )
        return tuple(tuple(row) for row in rows)


class DomainStore:
    def __init__(self, state, owner, tables):
        if not re.fullmatch("[a-z][a-z0-9_]*", owner) or any(
            not re.fullmatch("[a-z][a-z0-9_]*", table)
            or table in {"records", "stream_heads", "plugin_kv", "persistence_tables"}
            for table in tables
        ):
            raise ValueError("Invalid or reserved domain table declaration")
        self.state = state
        self.owner = owner
        self.tables = frozenset(tables)

    async def exists(self):
        async with self.state.resources.handle_use(
            self.state.session_id, self.state.binding, write=False
        ):
            return await self._exists()

    async def _exists(self):
        if not self.state.path.exists():
            return False
        return await self.state.resources.worker.call(
            self.state.path,
            lambda c: all(
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                    ("table", table),
                ).fetchone()
                is not None
                for table in self.tables
            ),
        )

    def contribute(self, c, callback, *, write=True):
        for table in self.tables:
            row = c.execute(
                "SELECT owner FROM persistence_tables WHERE table_name=?", (table,)
            ).fetchone()
            if row is not None and row[0] != self.owner:
                raise ValueError("Table belongs to another storage owner: " + table)
            if write:
                c.execute(
                    "INSERT OR IGNORE INTO persistence_tables VALUES(?,?)",
                    (table, self.owner),
                )

        def authorize(action, table, column, database, trigger):
            if not write and action in (
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_CREATE_INDEX,
                sqlite3.SQLITE_DROP_INDEX,
            ):
                return sqlite3.SQLITE_DENY
            if action in (
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            ):
                return (
                    sqlite3.SQLITE_OK
                    if table in self.tables or table == "sqlite_master"
                    else sqlite3.SQLITE_DENY
                )
            if action in (
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_CREATE_INDEX,
                sqlite3.SQLITE_DROP_INDEX,
            ):
                return (
                    sqlite3.SQLITE_OK
                    if table in self.tables or column in self.tables
                    else sqlite3.SQLITE_DENY
                )
            if action in (
                sqlite3.SQLITE_ATTACH,
                sqlite3.SQLITE_DETACH,
                sqlite3.SQLITE_PRAGMA,
                sqlite3.SQLITE_TRANSACTION,
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        c.set_authorizer(authorize)
        try:
            return callback(SqlTransaction(c, 500))
        finally:
            c.set_authorizer(None)

    async def transaction(self, callback, *, size=0, write=True):
        async with self.state.resources.handle_use(
            self.state.session_id, self.state.binding, write=write
        ):
            if write:
                await self.state.materialize()

            def run(c):
                c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                return self.contribute(c, callback, write=write)

            return await self.state.resources.worker.call(
                self.state.path, run, size=size
            )


class SqlState:
    def __init__(
        self,
        resources,
        path,
        thread="",
        prefix="",
        *,
        session_id=None,
        binding=_BIND_NOW,
    ):
        self.resources = resources
        self._path = path
        self.thread = thread
        self.prefix = prefix
        self.session_id = session_id
        self.binding = (
            (resources._claims.get(path.parent) if session_id is not None else None)
            if binding is _BIND_NOW
            else binding
        )

    @property
    def path(self):
        return (
            self.resources.paths(self.session_id).database_file
            if self.session_id is not None
            else self._path
        )

    async def materialize(self):
        await self.resources.materialize_database(self.path)

    def namespace(self, prefix):
        namespace = self.prefix + "." + prefix if self.prefix else prefix
        return SqlState(
            self.resources,
            self.path,
            self.thread,
            namespace,
            session_id=self.session_id,
            binding=self.binding,
        )

    def domain(self, owner, tables):
        return DomainStore(self, owner, tables)

    async def get(self, key, default=None):
        async with self.resources.handle_use(
            self.session_id, self.binding, write=False
        ):
            return await self._get(key, default)

    async def _get(self, key, default=None):
        if not self.path.exists():
            return default
        row = await self.resources.worker.call(
            self.path,
            lambda c: c.execute(
                "SELECT value FROM plugin_kv WHERE thread_id=? AND namespace=? AND key=?",
                (self.thread, self.prefix, key),
            ).fetchone(),
        )
        return json.loads(row[0]) if row is not None else default

    async def set(self, key, value):
        async with self.resources.handle_use(self.session_id, self.binding, write=True):
            return await self._set(key, value)

    async def _set(self, key, value):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        size = len(data.encode("utf-8"))
        if size > self.resources.limits.kv_bytes:
            raise ValueError(
                "Small-state KV value exceeds byte budget; use an owned structured table"
            )
        if await self.get(key) == value:
            return
        await self.materialize()
        await self.resources.worker.call(
            self.path,
            lambda c: execute(
                c,
                "INSERT INTO plugin_kv VALUES(?,?,?,?) ON CONFLICT(thread_id,namespace,key) DO UPDATE SET value=excluded.value",
                (self.thread, self.prefix, key, data),
            ),
            size=size,
        )

    async def delete(self, key):
        async with self.resources.handle_use(self.session_id, self.binding, write=True):
            return await self._delete(key)

    async def _delete(self, key):
        if self.path.exists():
            await self.resources.worker.call(
                self.path,
                lambda c: execute(
                    c,
                    "DELETE FROM plugin_kv WHERE thread_id=? AND namespace=? AND key=?",
                    (self.thread, self.prefix, key),
                ),
            )

    async def clear(self):
        async with self.resources.handle_use(self.session_id, self.binding, write=True):
            return await self._clear()

    async def _clear(self):
        if self.path.exists():
            await self.resources.worker.call(
                self.path,
                lambda c: execute(
                    c,
                    "DELETE FROM plugin_kv WHERE thread_id=? AND namespace=?",
                    (self.thread, self.prefix),
                ),
            )

    async def keys(self):
        async with self.resources.handle_use(
            self.session_id, self.binding, write=False
        ):
            return await self._keys()

    async def _keys(self):
        if not self.path.exists():
            return []
        return await self.resources.worker.call(
            self.path,
            lambda c: [
                row[0]
                for row in c.execute(
                    "SELECT key FROM plugin_kv WHERE thread_id=? AND namespace=?",
                    (self.thread, self.prefix),
                )
            ],
        )
