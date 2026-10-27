"""Process-owned materialized views, bounded when unpinned and rebuilt on demand."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from XBotv2.persistence.api.files import run_file_io


@dataclass
class CachedArtifact:
    path: Path
    charge: int
    pins: int = 0


class ArtifactCache:
    def __init__(self, resources, budget):
        self.resources = resources
        self.budget = budget
        self.root = resources._paths.data_dir / ".cache" / "artifacts" / uuid4().hex
        self.entries = OrderedDict()
        self.lock = asyncio.Lock()

    def path(self, store, identity):
        return self.root / store._paths.session_id / identity.split("/", 1)[1]

    async def _cached(self, key):
        async with self.lock:
            entry = self.entries.get(key)
            if entry is not None:
                entry.pins += 1
                self.entries.move_to_end(key)
            return entry

    async def _acquire(self, store, ref, key):
        from XBotv2.persistence.impl import record_artifacts
        from XBotv2.persistence.impl.artifacts import _create, _append, _publish, _unlink
        entry = await self._cached(key)
        if entry is not None:
            return entry
        target = self.path(store, ref.id)
        async with self.resources.artifact_lock(target.parent, ref.id):
            entry = await self._cached(key)
            if entry is not None:
                return entry
            temporary = target.with_name("." + target.name + "." + uuid4().hex)
            try:
                try:
                    await run_file_io(_create, temporary)
                    async for chunk in record_artifacts.chunks(store, ref.id):
                        await run_file_io(_append, temporary, chunk)
                    await run_file_io(_publish, temporary, target, self.root.parent)
                finally:
                    await run_file_io(_unlink, temporary)
                entry = CachedArtifact(
                    target, ref.size + len(ref.model_dump_json().encode("utf-8"))
                    + len(str(target).encode("utf-8")) + 2048, pins=1,
                )
                async with self.lock:
                    self.entries[key] = entry
                return entry
            except BaseException:
                await run_file_io(_unlink, target)
                raise

    @asynccontextmanager
    async def pin(self, store, ref):
        key = (store._paths.session_id, ref.id)
        entry = None
        try:
            entry = await self._acquire(store, ref, key)
            await self._evict()
            yield str(entry.path)
        finally:
            if entry is not None:
                async with self.lock:
                    entry.pins -= 1
                    self.entries.move_to_end(key)
                await self._evict()

    async def _evict(self):
        from XBotv2.persistence.impl.artifacts import _unlink
        async with self.lock:
            candidates = [(key, entry) for key, entry in self.entries.items() if not entry.pins]
        for key, entry in candidates:
            async with self.lock:
                unpinned = sum(value.charge for value in self.entries.values() if not value.pins)
                if unpinned <= self.budget:
                    return
            async with self.resources.artifact_lock(entry.path.parent, key[1]):
                async with self.lock:
                    if self.entries.get(key) is not entry or entry.pins:
                        continue
                    del self.entries[key]
                await run_file_io(_unlink, entry.path)

    async def drop_session(self, session_id):
        from XBotv2.persistence.impl.artifacts import _unlink
        async with self.lock:
            selected = [(key, entry) for key, entry in self.entries.items() if key[0] == session_id]
            if any(entry.pins for _, entry in selected):
                raise RuntimeError("Session artifact paths are still in use")
            for key, _ in selected:
                del self.entries[key]
        for _, entry in selected:
            await run_file_io(_unlink, entry.path)

    async def close(self):
        import shutil
        async with self.lock:
            if any(entry.pins for entry in self.entries.values()):
                raise RuntimeError("Artifact cache still has active users")
            self.entries.clear()
        await run_file_io(shutil.rmtree, self.root, True)
