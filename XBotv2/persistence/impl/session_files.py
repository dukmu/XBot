"""Consistent session snapshots and explicit atomic deletion publication."""

from __future__ import annotations
import json
import asyncio
import os
import shutil
import sqlite3
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.errors import OperationError
from XBotv2.persistence.impl.stream import file_io, sync_parents


def _copy_prefix(source, target, length):
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("xb") as writer:
        remaining = length
        while remaining:
            chunk = reader.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("Source rollout is shorter than its frozen prefix")
            writer.write(chunk)
            remaining -= len(chunk)
        writer.flush()
        os.fsync(writer.fileno())


def _copy_objects(source, target):
    if not source.exists():
        return
    for directory, _, files in os.walk(source):
        parent = target / Path(directory).relative_to(source)
        parent.mkdir(parents=True, exist_ok=True)
        for name in files:
            src = Path(directory) / name
            dst = parent / name
            if src.is_symlink():
                raise ValueError("Session objects must not be symlinks")
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)
                with dst.open("rb") as copied:
                    os.fsync(copied.fileno())
        sync_parents(parent, target.parent)


def _publish(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    os.rename(source, target)
    source_fd = os.open(source.parent, os.O_RDONLY)
    try:
        os.fsync(source_fd)
    finally:
        os.close(source_fd)
    fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_tree(root):
    # All content and child directory entries must precede publication,
    # including a metadata-only snapshot with no rollout or objects.
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_symlink():
                raise ValueError("Prepared storage must not contain symlinks")
            if entry.is_dir(follow_symlinks=False):
                _sync_tree(Path(entry.path))
            else:
                descriptor = os.open(entry.path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PreparedCopy:
    def __init__(self, resources, session_id, staging, cuts):
        self.resources = resources
        self.session_id = session_id
        self.staging = staging
        self.cuts = tuple(cuts)
        self.published = False

    async def publish(self):
        await file_io(_sync_tree, self.staging.root)
        await file_io(
            _publish,
            self.staging.root,
            self.resources._paths.session(self.session_id).root,
        )
        self.published = True


from contextlib import asynccontextmanager


@asynccontextmanager
async def prepare_copy(resources, source_id, target_id):
    async with resources.use(source_id):
        source_claim = target_claim = staging_claim = None
        prepared = None
        final = resources._paths.session(target_id)
        staging_root = resources._paths.data_dir / ".preparing" / uuid4().hex
        staging = RuntimePaths.from_data_dir(staging_root).session(target_id)
        try:
            source = resources.paths(source_id) if source_id is not None else None
            if source is not None and not resources.owns(source_id):
                source_claim = resources.acquire(source_id)
            target_claim = resources.acquire(target_id)
            if final.root.exists():
                raise FileExistsError(final.root)
            resources._preparations[target_id] = (staging, asyncio.current_task())
            staging_claim = resources.acquire(target_id)
            cuts = []
            if source is not None:

                def snapshot(c):
                    staging.root.mkdir(parents=True, exist_ok=True)
                    target = sqlite3.connect(staging.database_file)
                    try:
                        c.backup(target)
                    finally:
                        target.close()
                    tables = {
                        row[0]
                        for row in c.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    return (
                        [
                            tuple(row)
                            for row in c.execute(
                                "SELECT stream_id,indexed_seq,indexed_offset FROM stream_heads ORDER BY stream_id"
                            )
                        ]
                        if "stream_heads" in tables
                        else []
                    )

                cuts = await resources.worker.call(source.database_file, snapshot)
                for thread, seq, offset in cuts:
                    if offset:
                        await file_io(
                            _copy_prefix,
                            source.thread(thread).rollout_file,
                            staging.thread(thread).rollout_file,
                            offset,
                        )
                await file_io(
                    _copy_objects, source.root / "objects", staging.root / "objects"
                )
            prepared = PreparedCopy(resources, target_id, staging, cuts)
            yield prepared
        finally:
            if (
                prepared is not None
                and not prepared.published
                and not staging.root.exists()
                and final.root.exists()
            ):
                await file_io(shutil.rmtree, final.root, True)
            if staging_claim is not None:
                staging_claim.release()
            resources._preparations.pop(target_id, None)
            if target_claim is not None:
                target_claim.release()
            if source_claim is not None:
                source_claim.release()
            await file_io(shutil.rmtree, staging_root, True)


def _delete(paths, session_id):
    source = paths.session(session_id).root
    if source.is_symlink():
        raise OperationError(
            "invalid_session_storage", "Cannot delete symlinked session storage"
        )
    if not source.exists():
        return
    container = paths.data_dir / ".deleting" / uuid4().hex
    container.mkdir(parents=True)
    marker = container / "DELETE"
    marker.write_text(session_id, encoding="utf-8")
    with marker.open("rb") as stream:
        os.fsync(stream.fileno())
    sync_parents(container, paths.data_dir.parent)
    os.rename(source, container / "session")
    sync_parents(container, paths.data_dir.parent)
    fd = os.open(source.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    shutil.rmtree(container)


async def delete_persisted_session(resources, session_id):
    await file_io(_delete, resources._paths, session_id)


def cleanup_deletions(paths):
    root = paths.data_dir / ".deleting"
    if not root.exists():
        return
    for container in root.iterdir():
        if (
            container.is_dir()
            and not container.is_symlink()
            and (container / "DELETE").is_file()
        ):
            # Only explicit deletion tombstones are eligible, never malformed
            # ordinary session directories or unfinished preparation trees.
            paths.session((container / "DELETE").read_text(encoding="utf-8"))
            shutil.rmtree(container)
