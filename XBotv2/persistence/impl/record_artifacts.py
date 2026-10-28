"""Immutable physical record selections; record data stays opaque to storage."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from XBotv2.core.api.artifacts import ArtifactKind, ArtifactRef
from XBotv2.persistence.api.files import run_file_io
from XBotv2.persistence.impl.stream import read_record, sync_parents

TABLES = frozenset({"artifact_selections", "artifact_record_ids", "artifact_import_sources"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_import_sources(stream_id TEXT PRIMARY KEY, through_seq INTEGER NOT NULL, offset INTEGER NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_selections(artifact_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL, after_seq INTEGER NOT NULL, through_seq INTEGER NOT NULL, mode TEXT NOT NULL, metadata TEXT NOT NULL, selection_key TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS artifact_selection_key ON artifact_selections(selection_key);
CREATE TABLE IF NOT EXISTS artifact_record_ids(artifact_id TEXT NOT NULL, ordinal INTEGER NOT NULL, record_id TEXT NOT NULL, PRIMARY KEY(artifact_id,ordinal));
"""


class SelectionPackage(BaseModel):
    stream_id: str
    after_seq: int = Field(ge=0)
    through_seq: int = Field(ge=0)
    mode: Literal["range", "ids"]
    metadata: ArtifactRef
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_size: int = Field(ge=0)
    source_provided: bool
    model_config = ConfigDict(extra="forbid", frozen=True)


def _package_header(path, budget):
    with path.open("rb") as reader:
        payload = reader.read(budget + 1)
    if len(payload) > budget:
        raise ValueError("Selection package metadata exceeds the byte budget")
    return SelectionPackage.model_validate_json(payload).model_dump(mode="json")


def is_selection(identity):
    if identity.startswith("records/"):
        if not re.fullmatch(r"records/[a-f0-9]{64}", identity):
            raise ValueError("Invalid record artifact identity")
        return True
    return False


def _id_batches(values, byte_budget):
    start = 0
    while start < len(values):
        page, encoded = [], []
        charge = 0
        for value in values[start:start + 128]:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            if page and charge + len(payload) > byte_budget:
                break
            page.append(value)
            encoded.append(payload)
            charge += len(payload)
        yield start, tuple(page), tuple(encoded)
        start += len(page)


async def create(store, stream_id, *, after_seq=0, through_seq=None, record_ids=None,
                 kind=ArtifactKind.CONTEXT, name="records.jsonl"):
    resources = store._resources
    if resources is None:
        raise RuntimeError("Record artifacts require a persistence owner")
    resources.paths(store._paths.session_id).thread(stream_id)
    if after_seq < 0 or (through_seq is not None and through_seq < 0):
        raise ValueError("Record bounds must be non-negative")
    if record_ids is not None and (after_seq or through_seq is not None):
        raise ValueError("Choose explicit IDs or sequence bounds")
    async with store._use(write=True):
        worker, database = resources.worker, store._paths.session.database_file
        def head(c):
            row = c.execute("SELECT h.indexed_seq,s.through_seq FROM stream_heads h LEFT JOIN artifact_import_sources s ON s.stream_id=h.stream_id WHERE h.stream_id=?", (stream_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown physical record stream")
            return min(row[0], row[1]) if row[1] is not None else row[0]
        confirmed = await worker.call(database, head)
        cut = confirmed if through_seq is None else through_seq
        if cut > confirmed:
            raise ValueError("Selection exceeds the confirmed durable stream head")
        digest = hashlib.sha256()
        digest.update(json.dumps([stream_id, after_seq, cut, record_ids is not None]).encode())
        temporary_key = uuid4().hex
        try:
            size = 0
            if record_ids is None:
                size = await worker.call(database, lambda c: c.execute(
                    "SELECT COALESCE(SUM(length),0) FROM records WHERE stream_id=? AND seq>? AND seq<=?",
                    (stream_id, after_seq, cut),
                ).fetchone()[0])
            else:
                for start, page, encoded in _id_batches(record_ids, resources.limits.replay_bytes):
                    for value in encoded:
                        digest.update(value)
                    def add(c):
                        count = 0
                        for ordinal, identity in enumerate(page, start):
                            row = c.execute(
                                "SELECT length FROM records WHERE stream_id=? AND record_id=? AND seq<=?",
                                (stream_id, identity, cut),
                            ).fetchone()
                            if row is None:
                                raise ValueError(f"Missing selected record: {identity}")
                            count += row[0]
                            c.execute("INSERT INTO artifact_record_ids VALUES(?,?,?)", (temporary_key, ordinal, identity))
                        return count
                    size += await worker.call(database, add, size=sum(map(len, encoded)))
            identity = "records/" + digest.hexdigest()
            ref = ArtifactRef(id=identity, kind=kind, name=name,
                              media_type="application/x-ndjson", size=size)
            def publish(c):
                c.execute("INSERT OR IGNORE INTO artifact_selections VALUES(?,?,?,?,?,?,?)",
                          (identity, stream_id, after_seq, cut,
                           "ids" if record_ids is not None else "range", ref.model_dump_json(), temporary_key))
                row = c.execute("SELECT metadata FROM artifact_selections WHERE artifact_id=?", (identity,)).fetchone()
                return ArtifactRef.model_validate_json(row[0])
            return await worker.call(database, publish, size=len(ref.model_dump_json().encode()))
        finally:
            # The final header is the only publication point. Cancellation can
            # arrive after its SQL commit; retain rows if that header owns them.
            def unused(c):
                return c.execute("SELECT 1 FROM artifact_selections WHERE selection_key=?", (temporary_key,)).fetchone() is None
            if await worker.call(database, unused):
                while True:
                    def discard(c):
                        return c.execute("DELETE FROM artifact_record_ids WHERE artifact_id=? AND ordinal IN (SELECT ordinal FROM artifact_record_ids WHERE artifact_id=? LIMIT 128)", (temporary_key, temporary_key)).rowcount
                    if not await worker.call(database, discard):
                        break


async def metadata(store, identity):
    def read(connection):
        row = connection.execute(
            "SELECT stream_id,after_seq,through_seq,mode,metadata,selection_key FROM artifact_selections WHERE artifact_id=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown record artifact: {identity}")
        return tuple(row)
    return await store._resources.worker.call(store._paths.session.database_file, read)


async def ranges(store, identity):
    """Yield bounded offset pages in sequence or explicit caller ID order."""
    stream, after, cut, mode, _, selection_key = await metadata(store, identity)
    position = after if mode == "range" else -1
    while True:
        def query(connection):
            if mode == "range":
                return [tuple(row) for row in connection.execute(
                    "SELECT seq,offset,length FROM records WHERE stream_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT 128",
                    (stream, position, cut),
                )]
            rows = connection.execute(
                "SELECT s.ordinal,r.offset,r.length FROM artifact_record_ids s LEFT JOIN records r ON r.stream_id=? AND r.record_id=s.record_id AND r.seq<=? WHERE s.artifact_id=? AND s.ordinal>? ORDER BY s.ordinal LIMIT 128",
                (stream, cut, selection_key, position),
            ).fetchall()
            if any(row[1] is None for row in rows):
                raise ValueError("Selected source record is missing")
            return [tuple(row) for row in rows]
        page = await store._resources.worker.call(store._paths.session.database_file, query)
        if not page:
            break
        for ordinal, offset, length in page:
            yield store._paths.session.thread(stream).rollout_file, offset, length
        position = page[-1][0]


async def chunks(store, identity, *, offset=0, limit=None, chunk_size=256 * 1024):
    from XBotv2.persistence.impl.artifacts import _read_range
    remaining = limit
    async for path, start, length in ranges(store, identity):
        if offset >= length:
            offset -= length
            continue
        start += offset
        length -= offset
        offset = 0
        while length and remaining != 0:
            count = min(chunk_size, length, remaining if remaining is not None else length)
            chunk = await run_file_io(_read_range, path, start, count)
            if len(chunk) != count:
                raise ValueError("Selected source record is incomplete")
            yield chunk
            start += count
            length -= count
            if remaining is not None:
                remaining -= count
        if remaining == 0:
            break


async def records(store, identity):
    async for path, offset, length in ranges(store, identity):
        yield await run_file_io(read_record, path, offset, length)


async def id_pages(store, identity):
    selection_key = (await metadata(store, identity))[5]
    after = -1
    while True:
        def read(c):
            result = []
            used = 0
            for row in c.execute(
                "SELECT ordinal,record_id FROM artifact_record_ids WHERE artifact_id=? AND ordinal>? ORDER BY ordinal LIMIT 128",
                (selection_key, after),
            ):
                charge = len(row[1].encode("utf-8"))
                if result and used + charge > store._resources.limits.replay_bytes:
                    break
                result.append(tuple(row))
                used += charge
            return result
        rows = await store._resources.worker.call(store._paths.session.database_file, read)
        if not rows:
            return
        yield tuple(row[1] for row in rows)
        after = rows[-1][0]


async def export_reference(store, ref, destination, supplied_streams):
    import shutil
    destination = Path(destination)
    temporary = destination.with_name("." + destination.name + "." + uuid4().hex)
    try:
        durable_root = await _write_package(store, ref, temporary, supplied_streams)
        await run_file_io(_publish_package, temporary, destination, durable_root)
    finally:
        await run_file_io(shutil.rmtree, temporary, True)


def _publish_package(source, target, durable_root):
    import os
    from XBotv2.persistence.impl.session_files import _sync_tree
    if target.exists():
        raise FileExistsError(target)
    _sync_tree(source)
    os.rename(source, target)
    sync_parents(target.parent, durable_root)


async def _write_package(store, ref, destination, supplied_streams):
    from XBotv2.persistence.impl.artifacts import _create, _append
    from XBotv2.persistence.api.files import write_text_atomic
    stream, after, cut, mode, metadata_json, _ = await metadata(store, ref.id)
    destination = Path(destination)
    durable_root = await run_file_io(_package_directory, destination)
    header = dict(stream_id=stream, after_seq=after, through_seq=cut,
                  mode=mode, metadata=json.loads(metadata_json))
    if mode == "ids":
        ids = destination / "ids.jsonl"
        await run_file_io(_create, ids)
        async for page in id_pages(store, ref.id):
            data = b"".join((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8") for value in page)
            await run_file_io(_append, ids, data)
    end = await store._resources.worker.call(
        store._paths.session.database_file,
        lambda c: c.execute(
            "SELECT COALESCE(MAX(offset+length),0) FROM records WHERE stream_id=? AND seq<=?",
            (stream, cut),
        ).fetchone()[0],
    )
    header["source_sha256"] = await run_file_io(
        _copy_source, store._paths.session.thread(stream).rollout_file,
        None if stream in supplied_streams else destination / "source.jsonl", end,
    )
    header["source_size"] = end
    header["source_provided"] = stream in supplied_streams
    await run_file_io(write_text_atomic, destination / "selection.json", json.dumps(header, ensure_ascii=False))
    return durable_root


def _package_directory(path):
    ancestor = path.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    path.mkdir(parents=True, exist_ok=False)
    return ancestor


def _copy_source(source, target, size):
    import os
    from contextlib import nullcontext
    digest = hashlib.sha256()
    with (
        source.open("rb") if size else nullcontext(None)
    ) as reader, (
        target.open("xb") if target is not None else nullcontext(None)
    ) as writer:
        while size:
            chunk = reader.read(min(size, 256 * 1024))
            if not chunk:
                raise ValueError("Selected source prefix is incomplete")
            if writer is not None:
                writer.write(chunk)
            digest.update(chunk)
            size -= len(chunk)
        if writer is not None:
            writer.flush()
            os.fsync(writer.fileno())
    return digest.hexdigest()


def _ids_page(path, offset, record_bytes, batch_bytes):
    values = []
    with path.open("rb") as reader:
        reader.seek(offset)
        used = 0
        for _ in range(128):
            start = reader.tell()
            line = reader.readline(record_bytes + 1)
            if not line:
                break
            if len(line) > record_bytes or not line.endswith(b"\n"):
                raise ValueError("Invalid exported selection ID")
            if values and used + len(line) > batch_bytes:
                reader.seek(start)
                break
            used += len(line)
            value = json.loads(line)
            if not isinstance(value, str):
                raise ValueError("Selected record ID must be a string")
            values.append(value)
        return values, reader.tell()


def _append_source(source, target, offset, size):
    import os
    with source.open("rb") as reader, target.open("ab") as writer:
        if writer.tell() != offset:
            raise ValueError("Imported snapshot source changed outside its owner")
        reader.seek(offset)
        remaining = size - offset
        while remaining:
            chunk = reader.read(min(remaining, 256 * 1024))
            if not chunk:
                raise ValueError("Imported snapshot tail is incomplete")
            writer.write(chunk)
            remaining -= len(chunk)
        writer.flush()
        os.fsync(writer.fileno())


def _truncate_source(path, offset):
    import os
    with path.open("rb+") as writer:
        writer.truncate(offset)
        writer.flush()
        os.fsync(writer.fileno())


async def _import_source(store, package, header):
    from XBotv2.persistence.impl.artifacts import _copy_verified, _unlink
    from XBotv2.persistence.impl.schema import ROLLOUT_SCHEMA
    from XBotv2.persistence.impl.stream import replay_batch
    stream, cut = header["stream_id"], header["through_seq"]
    paths = store._paths.session.thread(stream)
    worker, database = store._resources.worker, store._paths.session.database_file
    def state(c):
        c.executescript(ROLLOUT_SCHEMA)
        row = c.execute("SELECT indexed_seq,indexed_offset FROM stream_heads WHERE stream_id=?", (stream,)).fetchone()
        snapshot = c.execute("SELECT through_seq,offset,sha256 FROM artifact_import_sources WHERE stream_id=?", (stream,)).fetchone()
        return (tuple(row) if row else None, tuple(snapshot) if snapshot else None)
    current, previous = await worker.call(database, state)
    if current is not None and current[0] >= cut:
        end = await worker.call(database, lambda c: c.execute(
            "SELECT COALESCE(MAX(offset+length),0) FROM records WHERE stream_id=? AND seq<=?", (stream, cut),
        ).fetchone()[0])
        digest = await run_file_io(_copy_source, paths.rollout_file, None, end)
        if end != header["source_size"] or digest != header["source_sha256"]:
            raise ValueError("Imported record source conflicts with the existing stream prefix")
        return
    if current is not None and (previous is None or current != previous[:2]):
        raise ValueError("Imported record source conflicts with an existing owner stream")
    if header["source_provided"]:
        raise ValueError("Referenced canonical source prefix is missing")
    incoming = package / "source.jsonl"
    if await run_file_io(lambda: incoming.stat().st_size) != header["source_size"]:
        raise ValueError("Imported source has an invalid byte length")
    digest = await run_file_io(_copy_source, incoming, None, header["source_size"])
    if digest != header["source_sha256"]:
        raise ValueError("Imported source hash mismatch")
    if previous is not None:
        old_digest = await run_file_io(_copy_source, incoming, None, previous[1])
        actual_digest = await run_file_io(_copy_source, paths.rollout_file, None, previous[1])
        if old_digest != previous[2] or actual_digest != previous[2]:
            raise ValueError("Imported source does not extend its immutable prefix")
    elif await run_file_io(paths.rollout_file.exists):
        raise ValueError("Imported record source conflicts with an unindexed stream")
    old_seq, old_offset = previous[:2] if previous is not None else (0, 0)
    def begin(c):
        if previous is None:
            c.execute("INSERT INTO stream_heads VALUES(?,?,?,?)", (stream, 0, 0, 0))
            c.execute("INSERT INTO artifact_import_sources VALUES(?,?,?,?)",
                      (stream, 0, 0, hashlib.sha256(b"").hexdigest()))
    await worker.call(database, begin)
    try:
        if previous is None:
            await run_file_io(_copy_verified, incoming, paths.rollout_file,
                              header["source_sha256"], header["source_size"])
        else:
            await run_file_io(_append_source, incoming, paths.rollout_file, old_offset, header["source_size"])
        offset, seq = old_offset, old_seq
        while seq < cut:
            batch, end = await run_file_io(replay_batch, paths.rollout_file, offset, seq,
                                          store._resources.limits, header["source_size"])
            if not batch:
                raise ValueError("Exported record source is incomplete")
            def index(c):
                for record, start, length in batch:
                    c.execute("INSERT INTO records VALUES(?,?,?,?,?,?)",
                              (stream, record["seq"], record["id"], record["kind"], start, length))
                c.execute("UPDATE stream_heads SET indexed_seq=?,indexed_offset=? WHERE stream_id=?",
                          (batch[-1][0]["seq"], end, stream))
            await worker.call(database, index, size=sum(length for _, _, length in batch))
            offset, seq = end, batch[-1][0]["seq"]
        if seq != cut or offset != header["source_size"]:
            raise ValueError("Exported source does not match its fixed cut")
        await worker.call(database, lambda c: c.execute(
            "UPDATE artifact_import_sources SET through_seq=?,offset=?,sha256=? WHERE stream_id=?",
            (cut, offset, header["source_sha256"], stream),
        ).rowcount)
    except BaseException:
        published = await worker.call(database, lambda c: c.execute(
            "SELECT through_seq FROM artifact_import_sources WHERE stream_id=?", (stream,),
        ).fetchone()[0] == cut)
        if not published:
            if previous is None:
                await run_file_io(_unlink, paths.rollout_file)
            else:
                await run_file_io(_truncate_source, paths.rollout_file, old_offset)
            def restore(c):
                c.execute("DELETE FROM records WHERE stream_id=? AND seq>?", (stream, old_seq))
                if previous is None:
                    c.execute("DELETE FROM stream_heads WHERE stream_id=?", (stream,))
                    c.execute("DELETE FROM artifact_import_sources WHERE stream_id=?", (stream,))
                else:
                    c.execute("UPDATE stream_heads SET indexed_seq=?,indexed_offset=? WHERE stream_id=?",
                              (old_seq, old_offset, stream))
            await worker.call(database, restore)
        raise


async def import_reference(store, source, ref):
    source = Path(source)
    header = await run_file_io(
        _package_header, source / "selection.json", store._resources.limits.queue_bytes + 4096,
    )
    if ArtifactRef.model_validate(header["metadata"]) != ref:
        raise ValueError("Exported selection metadata does not match its reference")
    stream, after, cut, mode = (header[name] for name in ("stream_id", "after_seq", "through_seq", "mode"))
    worker, database = store._resources.worker, store._paths.session.database_file
    if header["source_provided"]:
        await _import_source(store, source, header)
    else:
        async with store._resources.operation(
            store._paths.session.thread(stream), write=True, binding=store._binding,
        ):
            await _import_source(store, source, header)
    digest = hashlib.sha256()
    digest.update(json.dumps([stream, after, cut, mode == "ids"]).encode())
    def register(c):
        c.execute("INSERT INTO artifact_selections VALUES(?,?,?,?,?,?,?)",
                  (ref.id, stream, after, cut, mode, ref.model_dump_json(), ref.id))
    # Each reference is imported once by the exchange owner. Repeated refs
    # validate the same immutable physical descriptor instead of rewriting it.
    existing = await worker.call(database, lambda c: c.execute(
        "SELECT metadata FROM artifact_selections WHERE artifact_id=?", (ref.id,),
    ).fetchone())
    if existing is not None:
        if existing[0] != ref.model_dump_json():
            raise ValueError("Conflicting record artifact metadata")
        return
    try:
        size = 0
        if mode == "ids":
            offset = ordinal = 0
            while True:
                page, end = await run_file_io(
                    _ids_page, source / "ids.jsonl", offset,
                    store._resources.limits.record_bytes, store._resources.limits.replay_bytes,
                )
                if not page:
                    break
                for value in page:
                    digest.update(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                def add(c):
                    count = 0
                    for index, value in enumerate(page, ordinal):
                        row = c.execute("SELECT length FROM records WHERE stream_id=? AND record_id=? AND seq<=?",
                                        (stream, value, cut)).fetchone()
                        if row is None:
                            raise ValueError(f"Missing selected record: {value}")
                        count += row[0]
                        c.execute("INSERT INTO artifact_record_ids VALUES(?,?,?)", (ref.id, index, value))
                    return count
                size += await worker.call(database, add, size=sum(len(value.encode("utf-8")) for value in page))
                offset, ordinal = end, ordinal + len(page)
        elif mode == "range":
            size = await worker.call(database, lambda c: c.execute(
                "SELECT COALESCE(SUM(length),0) FROM records WHERE stream_id=? AND seq>? AND seq<=?",
                (stream, after, cut),
            ).fetchone()[0])
        else:
            raise ValueError("Unsupported selection mode")
        if ref.id != "records/" + digest.hexdigest() or ref.size != size:
            raise ValueError("Selection identity or size does not match its source")
        await worker.call(database, register, size=len(ref.model_dump_json().encode("utf-8")))
    except BaseException:
        def discard(c):
            c.execute("DELETE FROM artifact_record_ids WHERE artifact_id=?", (ref.id,))
            c.execute("DELETE FROM artifact_selections WHERE artifact_id=?", (ref.id,))
        await worker.call(database, discard)
        raise
