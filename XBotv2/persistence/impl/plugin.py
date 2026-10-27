"""Process-owned storage backend; no conversation or session-domain policy."""

from __future__ import annotations
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from pydantic import BaseModel, ConfigDict, Field
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.persistence.impl.database import DatabaseWorker, StorageLimits
from XBotv2.persistence.impl.schema import COMMON_SCHEMA
from XBotv2.persistence.impl.state import SqlState
from XBotv2.persistence.impl.stream import Stream, file_io, sync_parents
from XBotv2.persistence.impl.artifacts import ArtifactStore
from XBotv2.persistence.impl.session_lock import acquire_session


class PersistenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_cache_bytes: int = Field(default=64 * 1024 * 1024, ge=0)
    queue_operations: int = Field(default=64, ge=1)
    queue_bytes: int = Field(default=16 * 1024 * 1024, ge=4096)
    replay_records: int = Field(default=128, ge=1, le=1000)
    replay_bytes: int = Field(default=1024 * 1024, ge=4096)
    record_bytes: int = Field(default=8 * 1024 * 1024, ge=4096)
    kv_bytes: int = Field(default=64 * 1024, ge=1024)


class PersistenceResources:
    def __init__(self, paths, runtime_log, config, owner):
        self._paths = paths
        self._log = runtime_log
        self._owner = owner
        self.limits = StorageLimits(**config.model_dump(exclude={"artifact_cache_bytes"}))
        if (
            self.limits.record_bytes + self.limits.replay_bytes
            > self.limits.queue_bytes
        ):
            raise ValueError(
                "Replay and record byte budgets must fit the database queue"
            )
        self.worker = DatabaseWorker(self.limits)
        self._operations = Counter()
        self._uses = {}
        self._session_draining = set()
        self._locks = {}
        self._publication_owners = {}
        self._claims = {}
        self._preparations = {}
        self._preparation_lock = asyncio.Lock()
        self._failed_preparation = None
        self._closing = False
        self._draining = False
        self._shutdown_drained = False
        from XBotv2.persistence.impl.artifact_cache import ArtifactCache
        self.artifact_cache = ArtifactCache(self, config.artifact_cache_bytes)

    def _admit(self, session_id=None):
        if (
            not self._owner.is_active
            or self._closing
            or self._draining
            or session_id in self._session_draining
        ):
            raise RuntimeError("Persistence owner is closing")

    @asynccontextmanager
    async def _track(self, session_id):
        key = (asyncio.current_task(), session_id)
        token = object()
        done = asyncio.Event()
        self._operations[key] += 1
        self._uses[token] = (key[0], session_id, done)
        try:
            yield
        finally:
            self._operations[key] -= 1
            if not self._operations[key]:
                del self._operations[key]
            self._uses.pop(token, None)
            done.set()

    @asynccontextmanager
    async def use(self, session_id=None):
        self._admit(session_id)
        async with self._track(session_id):
            yield

    async def drain(self, session_id=None):
        if session_id is not None:
            self._session_draining.add(session_id)
        else:
            self._draining = True
        accepted = [
            (task, done)
            for task, sid, done in self._uses.values()
            if (session_id is None or sid == session_id)
            and task is not asyncio.current_task()
        ]
        for task in {task for task, _ in accepted}:
            task.cancel()
        if accepted:
            await asyncio.gather(*(done.wait() for _, done in accepted))
        if session_id is not None:
            await self.artifact_cache.drop_session(session_id)
        await self.worker.release(
            self.paths(session_id).database_file if session_id is not None else None
        )
        if session_id is None:
            async with self._preparation_lock:
                await self.cleanup_preparation()

    async def cleanup_preparation(self):
        if self._failed_preparation is not None:
            import shutil

            path, root = self._failed_preparation
            await self.worker.release(path)
            if root.exists():
                await file_io(shutil.rmtree, root)
            self._failed_preparation = None

    @asynccontextmanager
    async def handle_use(self, session_id, binding, *, write=False):
        existing = self._operations[(asyncio.current_task(), session_id)] > 0
        if self._closing or (
            (self._draining or session_id in self._session_draining) and not existing
        ):
            raise RuntimeError("Storage scope is closing")
        if session_id is None:
            self._admit()
        elif write:
            current = self._claims.get(self.paths(session_id).root)
            if binding is None or current is not binding:
                raise RuntimeError("Storage writer lease is no longer owned")
        elif not self._owner.is_active and binding is None and not existing:
            raise RuntimeError("Persistence owner is closing")
        async with self._track(session_id):
            yield

    @property
    def record_bytes(self):
        return self.limits.record_bytes

    def paths(self, session_id):
        prepared = self._preparations.get(session_id)
        if prepared is not None:
            if prepared[1] is not asyncio.current_task():
                raise RuntimeError("Storage target is still being prepared")
            return prepared[0]
        return self._paths.session(session_id)

    def database(self, session_id, *, owner, tables):
        self._admit(session_id)
        return SqlState(
            self, self.paths(session_id).database_file, session_id=session_id
        ).domain(owner, tables)

    def state(self, session_id=None, thread_id=""):
        self._admit(session_id)
        path = (
            self.paths(session_id).database_file
            if session_id is not None
            else self._paths.data_dir / "plugin-state.sqlite3"
        )
        return SqlState(self, path, thread_id, session_id=session_id)

    async def database_exists(self, session_id):
        async with self.use(session_id):
            return self.paths(session_id).database_file.exists()

    async def materialize_database(self, path):
        if not path.exists():

            def create(c):
                c.executescript("PRAGMA journal_mode=WAL;" + COMMON_SCHEMA)

            await self.worker.call(path, create, create=True)
            await file_io(sync_parents, path.parent, self._paths.data_dir.parent)

    def rollout(self, session_id, thread_id, *, domain, project):
        self._admit(session_id)
        return Stream(
            self,
            self.paths(session_id).thread(thread_id),
            domain=domain,
            project=project,
        )

    @asynccontextmanager
    async def operation(self, paths, *, write=False, binding=None):
        async with self.handle_use(paths.session_id, binding, write=write):
            key = (paths.session.root, paths.thread_id)
            lock, count = self._locks.get(key, (asyncio.Lock(), 0))
            self._locks[key] = (lock, count + 1)
            try:
                async with lock:
                    yield
            finally:
                lock, count = self._locks[key]
                if count == 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (lock, count - 1)

    @asynccontextmanager
    async def artifact_lock(self, object_root, identity):
        key = (object_root, "artifact:" + identity)
        lock, count = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            lock, count = self._locks[key]
            if count == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, count - 1)

    @asynccontextmanager
    async def publication(self, root):
        task = asyncio.current_task()
        if self._publication_owners.get(root) is task:
            yield
            return
        async with self.artifact_lock(root, "publication"):
            self._publication_owners[root] = task
            try:
                yield
            finally:
                del self._publication_owners[root]

    def require_writer(self, session):
        if session.root not in self._claims:
            raise RuntimeError("Storage mutations require their actual owner claim")

    def acquire(self, session_id):
        self._admit(session_id)
        paths = self.paths(session_id)
        claim = acquire_session(paths.root, label=session_id)
        resources = self

        class Ownership:
            def release(self):
                if resources._claims.get(paths.root) is self:
                    resources._claims.pop(paths.root)
                claim.release()
                resources._session_draining.discard(session_id)

        owner = Ownership()
        self._claims[paths.root] = owner
        return owner

    def owns(self, session_id):
        return self.paths(session_id).root in self._claims

    def artifacts(self, session_id, thread_id):
        self._admit(session_id)
        return ArtifactStore(
            self.paths(session_id).thread(thread_id), self._log, resources=self,
            binding=self._claims.get(self.paths(session_id).root),
        )

    async def owners(self, session_id):
        async with self.use(session_id):
            path = self.paths(session_id).database_file
            if not path.exists():
                return frozenset()
            return await self.worker.call(
                path,
                lambda c: frozenset(
                    [
                        row[0]
                        for row in c.execute(
                            "SELECT DISTINCT owner FROM persistence_tables"
                        )
                    ]
                    + [
                        row[0]
                        for row in c.execute("SELECT DISTINCT namespace FROM plugin_kv")
                    ]
                ),
            )

    def copy_session(self, source_id, target_id):
        from XBotv2.persistence.impl.session_files import prepare_copy

        return prepare_copy(self, source_id, target_id)

    def prepare_session(self, target_id):
        from XBotv2.persistence.impl.session_files import prepare_copy

        return prepare_copy(self, None, target_id)

    async def copy_document(self, source_id, target_id, name):
        from pathlib import Path

        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("Document name must be a basename")
        self.require_writer(self.paths(target_id))
        source = self.paths(source_id).root / name
        if source.exists():
            from XBotv2.persistence.api.files import write_bytes_atomic

            await file_io(
                write_bytes_atomic,
                self.paths(target_id).root / name,
                await file_io(source.read_bytes),
            )

    async def delete_session(self, session_id):
        from XBotv2.persistence.impl.session_files import delete_persisted_session

        async with self.use(session_id):
            self.require_writer(self.paths(session_id))
            await self.drain(session_id)
            try:
                await delete_persisted_session(self, session_id)
            finally:
                self._session_draining.discard(session_id)

    async def close(self):
        self._closing = True
        if not self._shutdown_drained:
            await self.drain()
            self._shutdown_drained = True
        await self.artifact_cache.close()
        await self.worker.close()


async def mount_process(ctx, config):
    resources = PersistenceResources(ctx.runtime_paths, ctx.runtime_log, config, ctx)
    ctx.provide(PERSISTENCE, resources)
    ctx.set("state", resources.state())
    remove = ctx.root.pre_stop(resources.drain)
    ctx.dispose(remove)
    ctx.dispose(resources.close)
    from XBotv2.persistence.impl.session_files import cleanup_deletions

    await file_io(cleanup_deletions, ctx.runtime_paths)


mount_process.inject = ("runtime_paths", "runtime_log")
mount_process.provide = (PERSISTENCE, "state")
mount_process.Config = PersistenceConfig


def mount_thread(ctx, config):
    from XBotv2.session.api.contracts import THREAD_INFO

    info = ctx.require(THREAD_INFO)
    ctx.set(
        "artifacts", ctx.require(PERSISTENCE).artifacts(info.session_id, info.thread_id)
    )


mount_thread.inject = (PERSISTENCE, "thread_info")
mount_thread.provide = ("artifacts",)
