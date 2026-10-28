"""Asynchronous immutable objects; file work never occupies the SQL worker."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

from XBotv2.core.api.artifacts import ArtifactKind, ArtifactRef
from XBotv2.persistence.api.files import run_file_io
from XBotv2.core.api.paths import ThreadPaths
from XBotv2.core.api.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog

CHUNK_BYTES = 256 * 1024
_ACTIVE_ARTIFACTS = ContextVar("xbot_artifact_request", default=None)


class ArtifactStore:
    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
        *,
        resources=None,
        binding=None,
    ) -> None:
        self._paths = paths
        self._resources = resources
        self._binding = binding
        self._local_lock = asyncio.Lock()
        self._local_owner = None
        self._log = runtime_log.bind(
            "persistence", session_id=paths.session_id, thread_id=paths.thread_id,
        )

    @asynccontextmanager
    async def _use(self, *, write=False):
        if self._resources is None:
            yield
        else:
            async with self._resources.handle_use(
                self._paths.session_id, self._binding, write=write
            ):
                yield

    async def reference_records(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
        through_seq: int | None = None,
        record_ids: Sequence[str] | None = None,
        kind: ArtifactKind = ArtifactKind.CONTEXT,
        name: str = "records.jsonl",
    ) -> ArtifactRef:
        from XBotv2.persistence.impl.record_artifacts import create

        return await create(
            self, stream_id, after_seq=after_seq, through_seq=through_seq,
            record_ids=record_ids, kind=kind, name=name,
        )

    async def reference(self, identity):
        from XBotv2.persistence.impl.record_artifacts import is_selection, metadata
        async with self._use():
            if is_selection(identity):
                return ArtifactRef.model_validate_json((await metadata(self, identity))[4])
            path = self._paths.artifact_file(identity)
            size = await run_file_io(lambda: path.stat().st_size)
            return ArtifactRef(id=identity, kind=ArtifactKind(identity.split("/", 1)[0]),
                               size=size, sha256=identity.rsplit("/", 1)[1].split(".", 1)[0])

    @asynccontextmanager
    async def using(self):
        """One request's exposed paths; ownership stays out of provider values."""
        token = _ACTIVE_ARTIFACTS.set({})
        try:
            yield
        finally:
            _ACTIVE_ARTIFACTS.reset(token)

    @asynccontextmanager
    async def pin(self, artifact):
        from XBotv2.persistence.impl.record_artifacts import is_selection
        async with self._use():
            ref = await self.reference(_artifact_id(artifact))
            if is_selection(ref.id):
                async with self._resources.artifact_cache.pin(self, ref) as path:
                    yield path
            else:
                yield self.model_path(ref)

    async def expose(self, artifact, resources):
        path = await resources.enter_async_context(self.pin(artifact))
        active = _ACTIVE_ARTIFACTS.get()
        if active is not None:
            identity = _artifact_id(artifact)
            active[(self._paths.session_id, identity)] = (self, identity)
        return path

    async def handoff(self):
        """Acquire independent rights for the paths exposed by this request."""
        resources = AsyncExitStack()
        try:
            for store, ref in list((_ACTIVE_ARTIFACTS.get() or {}).values()):
                await resources.enter_async_context(store.pin(ref))
            return resources
        except BaseException:
            await resources.aclose()
            raise

    def _managed_identity(self, value):
        from XBotv2.persistence.impl.record_artifacts import is_selection
        if value.startswith("artifact:"):
            return value[len("artifact:"):]
        root = self._paths.runtime.data_dir / ".cache" / "artifacts"
        try:
            relative = Path(value).relative_to(root)
        except ValueError:
            return None
        if len(relative.parts) != 3 or relative.parts[1] != self._paths.session_id:
            raise ValueError("Managed artifact path belongs to another session")
        identity = "records/" + relative.parts[2]
        is_selection(identity)
        return identity

    def resolve_model_path(self, value):
        from XBotv2.persistence.impl.record_artifacts import is_selection
        identity = self._managed_identity(value)
        if identity is None:
            return value
        if is_selection(identity):
            return str(self._resources.artifact_cache.path(self, identity))
        return self.model_path(identity)

    async def reference_path(self, value):
        identity = self._managed_identity(value)
        return await self.reference(identity) if identity is not None else None

    def managed_roots(self):
        roots = [str(self._paths.artifacts_dir)]
        if self._resources is not None:
            roots.append(str(self._resources.artifact_cache.root / self._paths.session_id))
        return tuple(roots)

    @asynccontextmanager
    async def resolve_path(self, path):
        identity = self._managed_identity(path)
        if identity is None:
            yield path
            return
        async with self.pin(identity) as pinned:
            yield pinned

    async def selection_stream(self, artifact):
        from XBotv2.persistence.impl.record_artifacts import is_selection, metadata
        if not is_selection(_artifact_id(artifact)):
            return None
        async with self._use():
            return (await metadata(self, _artifact_id(artifact)))[0]

    async def read_records(self, artifact):
        from XBotv2.persistence.impl.record_artifacts import records
        async with self._use():
            async for record in records(self, _artifact_id(artifact)):
                yield record

    @asynccontextmanager
    async def spool_file(self):
        """Own a temporary output file until its producer has stopped."""
        async with self._use(write=True):
            path = self._paths.session.root / ".spool" / uuid4().hex
            try:
                await run_file_io(_create, path)
                yield path
            finally:
                await run_file_io(_unlink, path)

    async def put(self, kind, payload: bytes, **metadata) -> ArtifactRef:
        async def chunks():
            for start in range(0, len(payload), CHUNK_BYTES):
                yield payload[start : start + CHUNK_BYTES]

        return await self.put_stream(kind, chunks(), **metadata)

    async def put_stream(
        self, kind: ArtifactKind, chunks: AsyncIterable[bytes], **metadata,
    ) -> ArtifactRef:
        async with self.prepare_stream(kind, chunks, **metadata) as ref:
            return ref

    @asynccontextmanager
    async def prepare_stream(
        self,
        kind: ArtifactKind,
        chunks: AsyncIterable[bytes],
        *,
        media_type="application/octet-stream",
        name="",
        suffix="",
    ):
        """Publish before the caller commits; undo a new object if its scope fails."""
        _validate_metadata(name, suffix)
        async with self.spool_file() as temporary:
            digest = hashlib.sha256()
            size = 0
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("Artifact chunks must be bytes")
                for start in range(0, len(chunk), CHUNK_BYTES):
                    piece = chunk[start : start + CHUNK_BYTES]
                    await run_file_io(_append, temporary, piece)
                    digest.update(piece)
                    size += len(piece)
            ref = _ref(kind, digest.hexdigest(), size, media_type, name, suffix)
            async with self._publication(temporary, ref):
                yield ref

    @asynccontextmanager
    async def publication(self):
        if self._resources is not None:
            async with self._resources.publication(self._paths.session.root):
                yield
        elif self._local_owner is asyncio.current_task():
            yield
        else:
            async with self._local_lock:
                self._local_owner = asyncio.current_task()
                try:
                    yield
                finally:
                    self._local_owner = None

    @asynccontextmanager
    async def _publication(self, temporary, ref):
        async with self.publication():
            target = self._paths.artifact_file(ref.id)
            created = not await run_file_io(target.exists)
            try:
                if created:
                    await run_file_io(
                        _publish, temporary, target,
                        self._paths.session.runtime.data_dir.parent,
                    )
                yield
            except BaseException:
                if created:
                    await run_file_io(_unlink, target)
                raise

    async def adopt_file(
        self,
        kind: ArtifactKind,
        source: Path,
        *,
        media_type="application/octet-stream",
        name="",
        suffix="",
    ) -> ArtifactRef:
        """Consume a producer-owned file, hash original bytes, and publish it."""
        _validate_metadata(name, suffix)
        async with self._use(write=True):
            # External files are first moved/copied to a controlled temporary;
            # a cancelled worker is drained before that temporary is removed.
            async with self.spool_file() as temporary:
                digest, size = await run_file_io(_adopt, Path(source), temporary)
                ref = _ref(kind, digest, size, media_type, name, suffix)
                async with self._publication(temporary, ref):
                    return ref

    async def read_chunks(
        self,
        artifact: ArtifactRef | str,
        *,
        offset=0,
        limit=None,
        chunk_size=CHUNK_BYTES,
    ):
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("Artifact offset and limit must be non-negative")
        if not 0 < chunk_size <= CHUNK_BYTES:
            raise ValueError(f"Artifact chunk_size must be between 1 and {CHUNK_BYTES}")
        from XBotv2.persistence.impl.record_artifacts import is_selection, chunks
        identity = _artifact_id(artifact)
        if is_selection(identity):
            async with self._use():
                async for chunk in chunks(self, identity, offset=offset, limit=limit, chunk_size=chunk_size):
                    yield chunk
            return
        path = self._paths.artifact_file(identity)
        async with self._use():
            remaining = limit
            while remaining is None or remaining:
                count = chunk_size if remaining is None else min(chunk_size, remaining)
                chunk = await run_file_io(_read_range, path, offset, count)
                if not chunk:
                    break
                offset += len(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk

    async def read(self, artifact: ArtifactRef | str) -> bytes:
        return b"".join([chunk async for chunk in self.read_chunks(artifact)])

    async def exists(self, artifact: ArtifactRef | str) -> bool:
        from XBotv2.persistence.impl.record_artifacts import is_selection
        if is_selection(_artifact_id(artifact)):
            await self.reference(_artifact_id(artifact))
            return True
        async with self._use():
            path = self._paths.artifact_file(_artifact_id(artifact))
            return await run_file_io(path.is_file)

    def model_path(self, artifact: ArtifactRef | str) -> str:
        from XBotv2.persistence.impl.record_artifacts import is_selection
        if is_selection(_artifact_id(artifact)):
            key = (self._paths.session_id, _artifact_id(artifact))
            entry = self._resources.artifact_cache.entries.get(key)
            if entry is None or not entry.pins:
                raise RuntimeError("A record artifact path requires an active pin")
            return str(entry.path)
        return str(self._paths.artifact_file(_artifact_id(artifact)).absolute())

    async def export_reference(self, artifact, destination, *, supplied_streams=()):
        from XBotv2.persistence.impl.record_artifacts import is_selection, export_reference
        if is_selection(artifact.id):
            async with self._use():
                await export_reference(self, artifact, destination, supplied_streams)
        else:
            await self.export_file(artifact, destination)

    async def import_reference(self, source, artifact):
        from XBotv2.persistence.impl.record_artifacts import is_selection, import_reference
        if is_selection(artifact.id):
            async with self._use(write=True):
                await self._resources.materialize_database(self._paths.session.database_file)
                async with self._resources.artifact_lock(self._paths.session.root, artifact.id):
                    await import_reference(self, source, artifact)
        else:
            await self.import_file(source, artifact)

    async def export_file(self, artifact: ArtifactRef, destination: Path) -> None:
        from XBotv2.persistence.impl.record_artifacts import is_selection
        if is_selection(artifact.id):
            destination = Path(destination)
            temporary = destination.with_name("." + destination.name + "." + uuid4().hex)
            async with self._use():
                try:
                    await run_file_io(_create, temporary)
                    async for chunk in self.read_chunks(artifact):
                        await run_file_io(_append, temporary, chunk)
                    await run_file_io(_publish, temporary, destination, destination.parent)
                finally:
                    await run_file_io(_unlink, temporary)
            return
        async with self._use():
            await run_file_io(
                _copy_verified, self._paths.artifact_file(artifact.id),
                Path(destination), _expected_digest(artifact), artifact.size,
            )

    async def import_file(self, source: Path, artifact: ArtifactRef) -> None:
        async with self._use(write=True), self.publication():
            await run_file_io(
                _copy_verified, Path(source), self._paths.artifact_file(artifact.id),
                _expected_digest(artifact), artifact.size,
            )


def _artifact_id(artifact):
    return artifact.id if isinstance(artifact, ArtifactRef) else artifact


def _validate_metadata(name, suffix):
    if suffix and (not suffix.startswith(".") or "/" in suffix or "\\" in suffix):
        raise ValueError(f"Invalid artifact suffix: {suffix!r}")
    if name and Path(name).name != name:
        raise ValueError("artifact name must be a file name")


def _ref(kind, digest, size, media_type, name, suffix):
    return ArtifactRef(
        id=f"{kind.value}/{digest}{suffix}", kind=kind, media_type=media_type,
        name=name, size=size, sha256=digest,
    )


def _create(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=False)


def _append(path, chunk):
    with path.open("ab") as writer:
        writer.write(chunk)


def _read_range(path, offset, count):
    with path.open("rb") as reader:
        reader.seek(offset)
        return reader.read(count)


def _publish(temporary, target, durable_root):
    from XBotv2.persistence.impl.stream import sync_parents

    target.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    sync_parents(target.parent, durable_root)


def _adopt(source, temporary):
    import errno
    import shutil

    if source.is_symlink():
        raise ValueError("Artifact source must not be a symlink")
    try:
        os.replace(source, temporary)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        with source.open("rb") as reader, temporary.open("wb") as writer:
            shutil.copyfileobj(reader, writer, CHUNK_BYTES)
        source.unlink()
    digest = hashlib.sha256()
    size = 0
    with temporary.open("rb") as reader:
        while chunk := reader.read(CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _expected_digest(artifact):
    digest = artifact.id.rsplit("/", 1)[-1].split(".", 1)[0]
    if artifact.sha256 and artifact.sha256 != digest:
        raise ValueError("Artifact identity and hash mismatch")
    return digest


def _copy_verified(source, target, expected_digest, size):
    if source.is_symlink():
        raise ValueError("Artifact source must not be a symlink")
    durable_root = target.parent
    while not durable_root.exists():
        durable_root = durable_root.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(descriptor, "wb") as writer, source.open("rb") as reader:
            while chunk := reader.read(CHUNK_BYTES):
                digest.update(chunk)
                count += len(chunk)
                writer.write(chunk)
        if digest.hexdigest() != expected_digest or count != size:
            raise ValueError("Artifact hash or size mismatch")
        _publish(temporary, target, durable_root)
    finally:
        temporary.unlink(missing_ok=True)


def _unlink(path):
    path.unlink(missing_ok=True)
