"""Physical rollout order/durability and caller-provided SQL projection."""

from __future__ import annotations
from XBotv2.persistence.api.files import run_file_io as file_io
import json
import os
import sqlite3
from uuid import uuid4
import asyncio
from contextlib import asynccontextmanager
from XBotv2.persistence.api.models import StreamCorrupt, CommitUncertain
from XBotv2.persistence.impl.database import execute


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def read_record(path, offset, length):
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(length)
    if len(data) != length or not data.endswith(b"\n"):
        raise StreamCorrupt(f"Invalid indexed range at {path}:{offset}")
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise StreamCorrupt(f"Invalid rollout record at {path}:{offset}") from exc


def sync_parents(directory, stop):
    while True:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if directory == stop:
            break
        directory = directory.parent


def append_file(path, payload, expected, durable_parent):
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    with path.open("ab", buffering=0) as stream:
        if stream.tell() != expected:
            raise StreamCorrupt("Rollout size changed outside its writer")
        view = memoryview(payload)
        while view:
            count = stream.write(view)
            if not count:
                raise OSError("Rollout append made no progress")
            view = view[count:]
        os.fsync(stream.fileno())
    if created:
        # Publish directory entries as well as the first record.
        sync_parents(path.parent, durable_parent)


def replay_batch(path, offset, seq, limits, confirmed_end=None):
    if not path.exists():
        if offset:
            raise StreamCorrupt("Indexed rollout is missing")
        return [], offset
    if path.stat().st_size < offset:
        raise StreamCorrupt("Rollout is shorter than its confirmed prefix")
    batch = []
    size = 0
    with path.open("rb+") as stream:
        stream.seek(offset)
        while len(batch) < limits.replay_records and size < limits.replay_bytes:
            start = stream.tell()
            if confirmed_end is not None and start >= confirmed_end:
                break
            data = stream.readline(limits.record_bytes + 1)
            if not data:
                break
            if len(data) > limits.record_bytes:
                raise StreamCorrupt(f"Rollout record exceeds byte budget at {start}")
            if not data.endswith(b"\n"):
                if confirmed_end is not None:
                    raise StreamCorrupt("Confirmed rollout prefix is incomplete")
                # Only the final unfinished line can be discarded. Complete
                # malformed lines, including the final one, are corruption.
                stream.truncate(start)
                stream.flush()
                os.fsync(stream.fileno())
                break
            try:
                record = json.loads(data)
                if (
                    set(record) != {"seq", "id", "kind", "data"}
                    or type(record["seq"]) is not int
                    or record["seq"] != seq + len(batch) + 1
                ):
                    raise ValueError("Non-contiguous record sequence")
                if not isinstance(record["id"], str) or not isinstance(
                    record["data"], dict
                ):
                    raise ValueError("Invalid record envelope")
            except (ValueError, UnicodeDecodeError, TypeError) as exc:
                raise StreamCorrupt(
                    f"Invalid rollout record at {path}:{start}"
                ) from exc
            batch.append((record, start, len(data)))
            size += len(data)
    return batch, (batch[-1][1] + batch[-1][2] if batch else offset)


class Stream:
    def __init__(self, resources, paths, *, domain, project):
        self.resources = resources
        self.paths = paths
        self.thread = paths.thread_id
        self._checked_schema = False
        self.domain = domain
        self.project = project
        self.binding = resources._claims.get(paths.session.root)
        self._lease_task = None
        self._lease_write = False

    async def _sql(self, callback, *, size=0, create=False):
        return await self.resources.worker.call(
            self.paths.session.database_file, callback, size=size, create=create
        )

    async def recover(self):
        self._require_lease()
        if not self.paths.session.database_file.exists():
            return
        if not self._checked_schema:
            await self.initialize()
            self._checked_schema = True
        row = await self._sql(
            lambda c: c.execute(
                "SELECT indexed_seq,indexed_offset,indexed_mtime_ns FROM stream_heads WHERE stream_id=?",
                (self.thread,),
            ).fetchone()
        )
        if row is None:
            return
        seq, offset, mtime = row
        current_mtime = (
            self.paths.rollout_file.stat().st_mtime_ns
            if self.paths.rollout_file.exists()
            else 0
        )
        size = (
            self.paths.rollout_file.stat().st_size
            if self.paths.rollout_file.exists()
            else 0
        )
        if size == offset and current_mtime == mtime:
            return
        claim = None
        if self.paths.session.root not in self.resources._claims:
            from XBotv2.core.api.errors import OperationError

            try:
                claim = self.resources.acquire(self.paths.session_id)
            except OperationError as error:
                if error.code == "session_in_use":
                    # A concurrent reader sees only that writer's confirmed
                    # projection prefix; it must not project its in-flight log.
                    return
                raise
        try:
            await self._replay(seq, offset, mtime, current_mtime)
        finally:
            if claim is not None:
                claim.release()

    async def _replay(self, seq, offset, mtime, current_mtime):
        if offset and current_mtime != mtime:
            checked_seq = checked_offset = 0
            while checked_offset < offset:
                prefix, checked_offset = await file_io(
                    replay_batch,
                    self.paths.rollout_file,
                    checked_offset,
                    checked_seq,
                    self.resources.limits,
                    offset,
                )
                if not prefix or checked_offset > offset:
                    raise StreamCorrupt(
                        "Confirmed rollout prefix no longer matches its index"
                    )
                expected = await self._sql(
                    lambda c: [
                        tuple(r)
                        for r in c.execute(
                            "SELECT seq,record_id,kind,offset,length FROM records WHERE stream_id=? AND seq>? AND seq<=? ORDER BY seq",
                            (self.thread, checked_seq, prefix[-1][0]["seq"]),
                        )
                    ]
                )
                actual = [
                    (r["seq"], r["id"], r["kind"], start, length)
                    for r, start, length in prefix
                ]
                if actual != expected:
                    raise StreamCorrupt(
                        "Confirmed rollout prefix no longer matches its index"
                    )
                checked_seq = prefix[-1][0]["seq"]
        while True:
            batch, end = await file_io(
                replay_batch,
                self.paths.rollout_file,
                offset,
                seq,
                self.resources.limits,
            )
            if not batch:
                break
            try:

                def apply(c):
                    self._apply(c, batch)
                    c.execute(
                        "UPDATE stream_heads SET indexed_mtime_ns=? WHERE stream_id=?",
                        (current_mtime, self.thread),
                    )

                await self._sql(apply, size=sum(value[2] for value in batch))
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                raise StreamCorrupt(
                    f"Invalid rollout reference for {self.thread}"
                ) from exc
            seq, offset = batch[-1][0]["seq"], end
        final_mtime = (
            self.paths.rollout_file.stat().st_mtime_ns
            if self.paths.rollout_file.exists()
            else 0
        )
        if final_mtime != mtime:
            await self._sql(
                lambda c: execute(
                    c,
                    "UPDATE stream_heads SET indexed_mtime_ns=? WHERE stream_id=? AND indexed_mtime_ns!=?",
                    (final_mtime, self.thread, final_mtime),
                )
            )

    def bind_projection(self, domain):
        self.domain = domain

    @asynccontextmanager
    async def lease(self, *, write=False):
        async with self.resources.operation(
            self.paths, write=write, binding=self.binding
        ):
            if self.paths.session.database_file.exists():
                snapshot = await self.resources.worker.call(
                    self.paths.session.database_file,
                    lambda c: c.execute(
                        "SELECT 1 FROM artifact_import_sources WHERE stream_id=?", (self.thread,),
                    ).fetchone() is not None,
                )
                if snapshot:
                    raise ValueError(
                        "Imported record snapshots cannot be bound to a domain rollout projector"
                    )
            self._lease_task = asyncio.current_task()
            self._lease_write = write
            try:
                yield
            finally:
                self._lease_task = None
                self._lease_write = False

    @asynccontextmanager
    async def reading(self):
        async with self.resources.handle_use(
            self.paths.session_id, self.binding, write=False
        ):
            yield

    def _require_lease(self, *, write=False):
        if self._lease_task is not asyncio.current_task() or (
            write and not self._lease_write
        ):
            raise RuntimeError(
                "Rollout operation requires its active writer/reader lease"
            )

    async def initialize(self):
        self._require_lease()
        from XBotv2.persistence.impl.schema import ROLLOUT_SCHEMA

        if not self.paths.session.database_file.exists():
            return
        tables = await self._sql(
            lambda c: {
                row[0]
                for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        )
        if not {"records", "stream_heads"} <= tables:
            self.resources.require_writer(self.paths.session)

            def create(c):
                c.executescript(ROLLOUT_SCHEMA)

            await self._sql(create)
        row = await self._sql(
            lambda c: c.execute(
                "SELECT 1 FROM stream_heads WHERE stream_id=?", (self.thread,)
            ).fetchone()
        )
        if row is None:
            self.resources.require_writer(self.paths.session)
            await self._sql(
                lambda c: execute(
                    c, "INSERT INTO stream_heads(stream_id) VALUES(?)", (self.thread,)
                )
            )

    async def reset(self):
        self._require_lease()
        self.resources.require_writer(self.paths.session)

        def reset(c):
            c.execute("DELETE FROM records WHERE stream_id=?", (self.thread,))
            c.execute(
                "INSERT INTO stream_heads(stream_id) VALUES(?) ON CONFLICT(stream_id) DO UPDATE SET indexed_seq=0,indexed_offset=0,indexed_mtime_ns=0",
                (self.thread,),
            )

        await self._sql(reset)

    def _apply(self, c, batch):
        seq, offset = c.execute(
            "SELECT indexed_seq,indexed_offset FROM stream_heads WHERE stream_id=?",
            (self.thread,),
        ).fetchone()
        for record, start, length in batch:
            if record["seq"] != seq + 1 or start != offset:
                raise StreamCorrupt(
                    "Projection does not advance a contiguous physical prefix"
                )
            c.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?)",
                (
                    self.thread,
                    record["seq"],
                    record["id"],
                    record["kind"],
                    start,
                    length,
                ),
            )
            seq, offset = record["seq"], start + length
        self.domain.contribute(c, lambda sql: self.project(sql, self.thread, batch))
        c.execute(
            "UPDATE stream_heads SET indexed_seq=?,indexed_offset=? WHERE stream_id=?",
            (seq, offset, self.thread),
        )

    async def append(self, kind, data, *, identity=None):
        self._require_lease(write=True)
        # Caller holds the full stream operation lock, including validation.
        self.resources.require_writer(self.paths.session)
        await self.resources.materialize_database(self.paths.session.database_file)
        await self.initialize()
        await self.recover()
        seq, offset = await self._sql(
            lambda c: tuple(
                c.execute(
                    "SELECT indexed_seq,indexed_offset FROM stream_heads WHERE stream_id=?",
                    (self.thread,),
                ).fetchone()
            )
        )
        record = {
            "seq": seq + 1,
            "id": identity or uuid4().hex,
            "kind": kind,
            "data": data,
        }
        payload = encoded(record)
        if len(payload) > self.resources.limits.record_bytes:
            raise ValueError("Rollout record exceeds the configured byte budget")

        def validate(c):
            c.execute("SAVEPOINT rollout_validation")
            try:
                self._apply(c, [(record, offset, len(payload))])
            finally:
                c.execute("ROLLBACK TO rollout_validation")
                c.execute("RELEASE rollout_validation")

        await self._sql(validate, size=len(payload))
        try:
            await file_io(
                append_file,
                self.paths.rollout_file,
                payload,
                offset,
                self.paths.runtime.data_dir.parent,
            )
        except asyncio.CancelledError as exc:
            # File I/O has drained, so the complete record may now be durable.
            # The owner must retain objects until same-ID recovery decides.
            raise CommitUncertain(
                f"Rollout {self.thread}:{seq + 1} append completed during cancellation; retry its identity"
            ) from exc
        mtime = self.paths.rollout_file.stat().st_mtime_ns
        try:

            def apply(c):
                self._apply(c, [(record, offset, len(payload))])
                c.execute(
                    "UPDATE stream_heads SET indexed_mtime_ns=? WHERE stream_id=?",
                    (mtime, self.thread),
                )

            await self._sql(apply, size=len(payload))
        except (Exception, asyncio.CancelledError) as exc:
            raise CommitUncertain(
                f"Rollout {self.thread}:{seq + 1} is durable; retry the original record identity"
            ) from exc
        return record

    async def record(self, identity):
        async with self.resources.handle_use(
            self.paths.session_id, self.binding, write=False
        ):
            return await self._record(identity)

    async def _record(self, identity):
        row = await self._sql(
            lambda c: c.execute(
                "SELECT offset,length FROM records WHERE stream_id=? AND record_id=?",
                (self.thread, identity),
            ).fetchone()
        )
        if row is None:
            raise StreamCorrupt(f"Missing rollout reference: {identity}")
        return await file_io(read_record, self.paths.rollout_file, *row)

    async def present(self):
        async with self.resources.handle_use(
            self.paths.session_id, self.binding, write=False
        ):
            return await self._present()

    async def _present(self):
        return (
            self.paths.rollout_file.exists()
            and self.paths.rollout_file.stat().st_size > 0
        )

    async def head(self):
        async with self.resources.handle_use(
            self.paths.session_id, self.binding, write=False
        ):
            return await self._head()

    async def _head(self):
        if not self.paths.session.database_file.exists():
            return (0, 0, 0)
        return await self._sql(
            lambda c: tuple(
                c.execute(
                    "SELECT indexed_seq,indexed_offset,indexed_mtime_ns FROM stream_heads WHERE stream_id=?",
                    (self.thread,),
                ).fetchone()
            )
        )

    async def flush(self):
        async with self.resources.handle_use(
            self.paths.session_id, self.binding, write=False
        ):
            return await self._flush()

    async def _flush(self):
        # append only confirms after fsync and the consumer transaction.
        return await self.head()

    async def records_page(self, *, before=None, limit=128, kinds=()):
        async with self.resources.handle_use(
            self.paths.session_id, self.binding, write=False
        ):
            return await self._records_page(before=before, limit=limit, kinds=kinds)

    async def _records_page(self, *, before=None, limit=128, kinds=()):
        newest = (await self.head())[0]
        end = newest + 1 if before is None else before
        marks = ",".join("?" for _ in kinds)
        condition = " AND kind IN (" + marks + ")" if kinds else ""

        def read(c):
            return [
                tuple(row)
                for row in c.execute(
                    "SELECT record_id,seq FROM records WHERE stream_id=? AND seq<?"
                    + condition
                    + " ORDER BY seq DESC LIMIT ?",
                    (self.thread, end, *kinds, limit),
                )
            ]

        return newest, await self._sql(read)

    async def read_range(
        self, *, after_seq=0, through_seq=None, limit=128, max_bytes=1024 * 1024
    ):
        async with self.resources.handle_use(
            self.paths.session_id, self.binding, write=False
        ):
            return await self._read_range(
                after_seq=after_seq,
                through_seq=through_seq,
                limit=limit,
                max_bytes=max_bytes,
            )

    async def _read_range(
        self, *, after_seq=0, through_seq=None, limit=128, max_bytes=1024 * 1024
    ):
        cut = (await self.head())[0] if through_seq is None else through_seq
        refs = await self._sql(
            lambda c: [
                tuple(row)
                for row in c.execute(
                    "SELECT record_id,seq,length FROM records WHERE stream_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                    (self.thread, after_seq, cut, limit),
                )
            ]
        )
        result = []
        used = 0
        for identity, seq, length in refs:
            if used + length > max_bytes:
                if not result:
                    raise ValueError("Record exceeds this read byte budget")
                break
            result.append(await self.record(identity))
            used += length
        return tuple(result), (result[-1]["seq"] if result else after_seq), cut
