"""Agentloop repositories own conversation, input and working-set semantics."""

from __future__ import annotations
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from XBotv2.agentloop.api.contracts import (
    InboxItem,
    HumanInput,
    Inserted,
    Edited,
    Retargeted,
    Removed,
    Consumed,
    Discarded,
)
from XBotv2.core.api.history import (
    HistoryPage,
    HistoryCursorInvalid,
    HistoryRevisionConflict,
    TrajectoryRead,
    MessageAppended,
    SurfaceReplaced,
    DurableEventRecorded,
    encode_history_cursor,
    decode_history_cursor,
)
from XBotv2.core.api.domain import HistoryRevision, InboxTarget
from XBotv2.core.api.messages import HumanInputMessage, RuntimeNoticeMessage
from XBotv2.core.api.parts import TextPart, ImagePart
from XBotv2.persistence.api.models import StreamCorrupt
from XBotv2.agentloop.impl.inbox import input_fingerprint, content_digest
from XBotv2.agentloop.impl import storage_projection
from XBotv2.agentloop.impl.storage_projection import MESSAGE, _one, _all
from XBotv2.agentloop.impl.storage_schema import TABLES, SCHEMA


class MessageHistoryStore:
    def __init__(self, store):
        self.store = store
        self.stream = store.stream

    async def surface_state(self, after_version=None):
        if not await self.store.has_history():
            return (0, ()) if after_version != 0 else None
        async with self.store.operation():

            def snapshot(sql):
                version = _one(
                    sql,
                    "SELECT surface_version FROM history_state WHERE thread_id=?",
                    (self.store.thread_id,),
                )[0]
                if after_version == version:
                    return version, None
                identities = []
                after = 0
                while True:
                    refs = sql.execute(
                        "SELECT order_key,record_id FROM surface WHERE thread_id=? AND order_key>? ORDER BY order_key LIMIT 256",
                        (self.store.thread_id, after),
                    )
                    if not refs:
                        return version, tuple(identities)
                    identities.extend(row[1] for row in refs)
                    after = refs[-1][0]

            version, identities = await self.store.sql(snapshot)
            if identities is None:
                return None
            messages = tuple(
                [await self.store.message(identity) for identity in identities]
            )
            return version, messages

    async def load_surface(self):
        return (await self.surface_state())[1]

    async def count_turns(self):
        if not await self.store.has_history():
            return 0
        async with self.store.operation():
            return await self.store.sql(
                lambda c: _one(
                    c,
                    "SELECT turn_count FROM history_state WHERE thread_id=?",
                    (self.store.thread_id,),
                )[0]
            )

    async def begin_turn(self, input_id):
        async with self.store.operation(write=True):
            await self.stream.append("turn_started", {"input_id": input_id})
            return await self.store.sql(
                lambda c: _one(
                    c,
                    "SELECT turn_count FROM history_state WHERE thread_id=?",
                    (self.store.thread_id,),
                )[0]
            )

    async def _check_version(self, expected):
        if expected is None or not await self.store.has_history():
            return
        current = await self.store.sql(
            lambda c: _one(
                c,
                "SELECT surface_version FROM history_state WHERE thread_id=?",
                (self.store.thread_id,),
            )[0]
        )
        if current != expected:
            raise HistoryRevisionConflict("Canonical working set changed before commit")

    async def append(self, messages, *, expected_version=None):
        async with self.store.operation(write=True):
            await self._check_version(expected_version)
            for message in messages:
                if isinstance(message, (HumanInputMessage, RuntimeNoticeMessage)):
                    row = (
                        await self.store.sql(
                            lambda c: _one(
                                c,
                                "SELECT current_record,retired FROM input_ids WHERE thread_id=? AND input_id=?",
                                (self.store.thread_id, message.id),
                            )
                        )
                        if await self.store.has_history()
                        else None
                    )
                    if row is not None:
                        if row[1]:
                            raise ValueError(
                                f"Conversation message identity already committed: {message.id}"
                            )
                        item = await self.store.input(row[0])
                        base = input_message(
                            item,
                            {"steering": message.steering}
                            if isinstance(message, HumanInputMessage)
                            else {},
                        )
                        original = base.model_dump(mode="json")
                        actual = message.model_dump(mode="json")
                        projection = {
                            key: value
                            for key, value in actual.items()
                            if original.get(key) != value
                        }
                        if isinstance(message, HumanInputMessage) and message.steering:
                            projection["steering"] = True
                        await self.stream.append(
                            "input_committed",
                            {
                                "input_id": message.id,
                                "body_ref": row[0],
                                "projection": projection,
                            },
                        )
                        continue
                await self.stream.append(
                    "message", {"message": message.model_dump(mode="json")}
                )

            return await self.store.sql(
                lambda c: _one(
                    c,
                    "SELECT surface_version FROM history_state WHERE thread_id=?",
                    (self.store.thread_id,),
                )[0]
            )

    async def replace_surface(
        self,
        source_ids,
        messages,
        *,
        operation,
        preserve_transcript,
        expected_version=None,
    ):
        async with self.store.operation(write=True):
            await self._check_version(expected_version)
            await self.stream.append(
                "surface_replaced",
                {
                    "source_ids": list(source_ids),
                    "messages": [
                        message.model_dump(mode="json") for message in messages
                    ],
                    "operation": operation,
                    "preserve_transcript": preserve_transcript,
                },
            )

            return await self.store.sql(
                lambda c: _one(
                    c,
                    "SELECT surface_version FROM history_state WHERE thread_id=?",
                    (self.store.thread_id,),
                )[0]
            )

    async def record(self, event, *, durable=False):
        async with self.store.operation(write=True):
            await self.stream.append(
                "event",
                {
                    "event": event.model_dump(mode="json"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )

    async def open_transactions(self, transaction_kind):
        if not await self.store.has_history():
            return frozenset()
        async with self.store.operation():
            result = set()
            after = ""
            while True:
                rows = await self.store.sql(
                    lambda sql: sql.execute(
                        "SELECT transaction_id FROM open_transactions WHERE thread_id=? AND kind=? AND transaction_id>? ORDER BY transaction_id LIMIT 256",
                        (self.store.thread_id, transaction_kind, after),
                    )
                )
                if not rows:
                    return frozenset(result)
                result.update(row[0] for row in rows)
                after = rows[-1][0]

    async def page(self, *, limit, cursor=None):
        return await self._page("surface", limit, cursor)

    async def page_transcript(self, *, limit, cursor=None):
        return await self._page("transcript", limit, cursor)

    async def _page(self, table, limit, cursor):
        if limit < 1 or limit > 500:
            raise ValueError("History page limit must be between 1 and 500")
        if not await self.store.has_history():
            if cursor is not None:
                raise HistoryCursorInvalid("Empty history has no cursor")
            return HistoryPage((), None)
        async with self.store.operation():

            def query(c):
                generation = _one(
                    c,
                    f"SELECT {table}_revision FROM history_state WHERE thread_id=?",
                    (self.store.thread_id,),
                )[0]
                revision = HistoryRevision(
                    f"{self.store.session_id}/{self.store.thread_id}:{table}:{generation}"
                )
                end = (
                    _one(
                        c,
                        f"SELECT COALESCE(MAX(order_key),0)+1 FROM {table} WHERE thread_id=?",
                        (self.store.thread_id,),
                    )[0]
                    if cursor is None
                    else decode_history_cursor(cursor, revision)
                )
                if end < 1:
                    raise HistoryCursorInvalid("Invalid history cursor range")
                rows = _all(
                    c,
                    f"SELECT order_key,record_id FROM {table} WHERE thread_id=? AND order_key<? ORDER BY order_key DESC LIMIT ?",
                    (self.store.thread_id, end, limit),
                )
                older = (
                    _one(
                        c,
                        f"SELECT 1 FROM {table} WHERE thread_id=? AND order_key<? LIMIT 1",
                        (self.store.thread_id, rows[-1][0]),
                    )
                    is not None
                    if rows
                    else False
                )
                return revision, [tuple(row) for row in rows], older

            revision, rows, more = await self.store.sql(query)
            rows = list(reversed(rows))
            messages = tuple([await self.store.message(row[1]) for row in rows])
            return HistoryPage(
                messages, encode_history_cursor(revision, rows[0][0]) if more else None
            )

    async def page_trajectory(self, *, limit, cursor=None, before=None):
        if cursor is not None and before is not None:
            raise ValueError("Pass either cursor or before")
        if limit < 1 or limit > 500:
            raise ValueError("Trajectory page limit must be between 1 and 500")
        async with self.store.operation():
            revision = HistoryRevision(
                f"{self.store.session_id}/{self.store.thread_id}:trajectory"
            )
            has_history = await self.store.has_history()
            newest = (await self.stream.head())[0] if has_history else 0
            end = (
                decode_history_cursor(cursor, revision)
                if cursor is not None
                else (before if before is not None else newest + 1)
            )
            if end < 1 or end > newest + 1:
                raise HistoryCursorInvalid("Invalid trajectory anchor")
            if not has_history:
                return TrajectoryRead(HistoryPage((), None), newest)
            newest, rows = await self.stream.records_page(
                before=end,
                limit=limit,
                kinds=("message", "input_committed", "surface_replaced", "event"),
            )
            more = (
                bool(
                    (
                        await self.stream.records_page(
                            before=rows[-1][1],
                            limit=1,
                            kinds=(
                                "message",
                                "input_committed",
                                "surface_replaced",
                                "event",
                            ),
                        )
                    )[1]
                )
                if rows
                else False
            )
            rows = list(reversed(rows))
            entries = []
            for identity, seq in rows:
                record = await self.stream.record(identity)
                kind = record["kind"]
                data = record["data"]
                if kind in ("message", "input_committed"):
                    mid = (
                        data["message"]["id"] if kind == "message" else data["input_id"]
                    )
                    entries.append(
                        MessageAppended(
                            position=seq, message=await self.store.message(mid)
                        )
                    )
                elif kind == "surface_replaced":
                    from XBotv2.agentloop.impl.storage_projection import MESSAGE

                    entries.append(
                        SurfaceReplaced(
                            position=seq,
                            operation=data["operation"],
                            transcript_policy="preserve"
                            if data["preserve_transcript"]
                            else "replace",
                            source_ids=tuple(data["source_ids"]),
                            replacements=tuple(
                                MESSAGE.validate_python(m) for m in data["messages"]
                            ),
                        )
                    )
                elif kind == "event":
                    entries.append(
                        DurableEventRecorded(
                            position=seq,
                            timestamp=data["timestamp"],
                            event=data["event"],
                        )
                    )
            return TrajectoryRead(
                HistoryPage(
                    tuple(entries),
                    encode_history_cursor(revision, rows[0][1]) if more else None,
                ),
                newest,
            )


class InboxStore:
    def __init__(self, store):
        self.store = store
        self.stream = store.stream

    async def load(self):
        if not await self.store.has_history():
            return []
        async with self.store.operation():
            items = []
            delivery = ""
            after = 0
            while True:
                rows = await self.store.sql(
                    lambda sql: sql.execute(
                        "SELECT body_ref,delivery,order_key FROM inbox WHERE thread_id=? AND (delivery>? OR (delivery=? AND order_key>?)) ORDER BY delivery,order_key LIMIT 256",
                        (self.store.thread_id, delivery, delivery, after),
                    )
                )
                if not rows:
                    break
                for identity, target, position in rows:
                    items.append(
                        (await self.store.input(identity)).model_copy(
                            update={"target": InboxTarget(target)}
                        )
                    )
                delivery, after = rows[-1][1], rows[-1][2]
            return items

    async def accepted(self, input_id, fingerprint):
        async with self.store.operation():
            return await self._accepted(input_id, fingerprint)

    async def _accepted(self, input_id, fingerprint):
        if not await self.store.has_history():
            return False
        row = await self.store.sql(
            lambda sql: _one(
                sql,
                "SELECT accepted_record FROM input_ids WHERE thread_id=? AND input_id=?",
                (self.store.thread_id, input_id),
            )
        )
        if row is None:
            return False
        record = await self.stream.record(row[0])
        if record["kind"] == "input_accepted":
            original = record["data"]["request_fingerprint"]
        else:
            original = input_fingerprint((await self.store.input(row[0])).input)
        if original != fingerprint:
            from XBotv2.core.api.errors import OperationError

            raise OperationError(
                "input_id_conflict",
                "Input ID was already accepted with different content",
            )
        return True

    async def current_content_sha256(self, input_id):
        async with self.store.operation():
            row = await self.store.sql(
                lambda sql: _one(
                    sql,
                    "SELECT current_record FROM input_ids WHERE thread_id=? AND input_id=?",
                    (self.store.thread_id, input_id),
                )
            )
            if row is None:
                raise KeyError(input_id)
            record = await self.stream.record(row[0])
            if record["kind"] == "message":
                return content_digest((await self.store.input(row[0])).input.content)
            return record["data"]["content_sha256"]

    async def append(self, change):
        async with self.store.operation(write=True):
            if isinstance(change, Inserted):
                fingerprint = change.request_fingerprint or input_fingerprint(
                    change.item.input
                )
                if await self._accepted(change.item.id, fingerprint):
                    return False
                await self.stream.append(
                    "input_accepted",
                    {
                        "item": change.item.model_dump(mode="json"),
                        "request_fingerprint": fingerprint,
                        "content_sha256": change.content_sha256 or content_digest(
                            change.item.input.content
                        ),
                    },
                    identity=f"input:{change.item.id}",
                )
                return True
            if isinstance(change, Consumed):
                pending = await self.store.sql(
                    lambda c: [
                        identity
                        for identity in change.ids
                        if _one(
                            c,
                            "SELECT 1 FROM inbox WHERE thread_id=? AND input_id=?",
                            (self.store.thread_id, identity),
                        )
                        is not None
                    ]
                )
                if pending:
                    await self.stream.append("inputs_consumed", {"input_ids": pending})
                return True
            if isinstance(change, Edited):
                row = await self.store.sql(
                    lambda c: _one(
                        c,
                        "SELECT body_ref FROM inbox WHERE thread_id=? AND input_id=?",
                        (self.store.thread_id, change.id),
                    )
                )
                if row is None:
                    raise KeyError(change.id)
                await self.stream.append(
                    "input_edited",
                    {
                        "input_id": change.id,
                        "previous": row[0],
                        "content": change.content,
                        "content_sha256": change.content_sha256 or content_digest(
                            change.content
                        ),
                        "artifacts": [
                            ref.model_dump(mode="json") for ref in change.artifacts
                        ],
                    },
                )
            elif isinstance(change, Retargeted):
                await self.stream.append(
                    "input_retargeted",
                    {"input_id": change.id, "target": change.target.value},
                )
            elif isinstance(change, (Removed, Discarded)):
                await self.stream.append(
                    "input_removed"
                    if isinstance(change, Removed)
                    else "inputs_discarded",
                    {
                        "input_ids": [change.id]
                        if isinstance(change, Removed)
                        else list(change.ids)
                    },
                )
            else:
                raise TypeError("Unsupported inbox mutation")
            return True


class ConversationStore:
    def __init__(self, backend, session_id, thread_id, thread_repository):
        self.backend = backend
        self.session_id = session_id
        self.thread_id = thread_id
        self.thread_repository = thread_repository
        self.domain = backend.database(session_id, owner="agentloop", tables=TABLES)
        self.stream = backend.rollout(
            session_id,
            thread_id,
            domain=self.domain,
            project=lambda tx, thread, batch: storage_projection.project(
                tx, thread, batch
            ),
        )
        self.history = MessageHistoryStore(self)
        self.inbox = InboxStore(self)
        self._initialized = False

    async def sql(self, callback, *, size=0, write=False):
        return await self.domain.transaction(callback, size=size, write=write)

    async def has_history(self):
        if await self.stream.present():
            return True
        if not await self.domain.exists():
            return False
        return await self.sql(
            lambda sql: (
                _one(
                    sql,
                    "SELECT 1 FROM history_state WHERE thread_id=?",
                    (self.thread_id,),
                )
                is not None
            )
        )

    @asynccontextmanager
    async def operation(self, *, write=False):
        if write:
            await self.thread_repository.ensure()
        async with self.stream.lease(write=write):
            if write or await self.has_history():
                await self.initialize()
                await self.stream.recover()
            if write:
                yield
                return
        # Confirmed bodies are immutable. Hold a read lifetime, not the
        # serial writer mutex, while decoding a page or loading W.
        async with self.stream.reading():
            yield

    async def initialize(self):
        if self._initialized:
            return
        present = await self.domain.exists()
        row = (
            await self.sql(
                lambda sql: _one(
                    sql,
                    "SELECT 1 FROM history_state WHERE thread_id=?",
                    (self.thread_id,),
                )
            )
            if present
            else None
        )
        if row is None:
            claim = (
                self.backend.acquire(self.session_id)
                if not self.backend.owns(self.session_id)
                else None
            )
            try:
                self.domain = self.backend.database(
                    self.session_id, owner="agentloop", tables=TABLES
                )
                self.stream.bind_projection(self.domain)

                def create(sql):
                    for statement in SCHEMA:
                        sql.execute(statement)
                    for table in TABLES:
                        if table == "history_state":
                            continue
                        column = "stream_id" if table == "message_refs" else "thread_id"
                        sql.execute(
                            "DELETE FROM " + table + " WHERE " + column + "=?",
                            (self.thread_id,),
                        )
                    sql.execute(
                        "INSERT OR REPLACE INTO history_state(thread_id) VALUES(?)",
                        (self.thread_id,),
                    )

                await self.sql(create, write=True)
                await self.stream.initialize()
                await self.stream.reset()
            finally:
                if claim is not None:
                    claim.release()
        else:
            await self.stream.initialize()
        self._initialized = True

    async def pending_ids(self):
        result = []
        after = 0
        while True:
            rows = await self.sql(
                lambda sql: sql.execute(
                    "SELECT input_id,order_key FROM inbox WHERE thread_id=? AND order_key>? ORDER BY order_key LIMIT 256",
                    (self.thread_id, after),
                )
            )
            if not rows:
                return result
            result.extend(row[0] for row in rows)
            after = rows[-1][1]

    async def input(self, identity):
        record = await self.stream.record(identity)
        edited_content = None
        edited_artifacts = None
        if record["kind"] == "input_edited":
            edited_content = record["data"]["content"]
            from XBotv2.core.api.artifacts import ArtifactRef
            edited_artifacts = tuple(ArtifactRef.model_validate(ref) for ref in record["data"]["artifacts"])
            accepted = await self.sql(
                lambda sql: _one(
                    sql,
                    "SELECT accepted_record FROM input_ids WHERE thread_id=? AND input_id=?",
                    (self.thread_id, record["data"]["input_id"]),
                )
            )
            if accepted is None:
                raise StreamCorrupt("Edited input has no original accepted identity")
            record = await self.stream.record(accepted[0])
        if record["kind"] == "message":
            message = MESSAGE.validate_python(record["data"]["message"])
            if not isinstance(message, (HumanInputMessage, RuntimeNoticeMessage)):
                raise StreamCorrupt("Input identity refers to a non-input message")
            from XBotv2.agentloop.api.contracts import RuntimeInput
            from XBotv2.core.api.domain import InboxTarget

            payload = {
                "content": "".join(
                    p.text for p in message.parts if isinstance(p, TextPart)
                ),
                "images": tuple(
                    p.image for p in message.parts if isinstance(p, ImagePart)
                ),
                "artifacts": message.artifacts,
            }
            value = (
                HumanInput(**payload)
                if isinstance(message, HumanInputMessage)
                else RuntimeInput(**payload, source=message.source, event=message.event)
            )
            return InboxItem(id=message.id, target=InboxTarget.NEXT_TURN, input=value)
        if record["kind"] != "input_accepted":
            raise StreamCorrupt("Input body reference is not an accepted input")
        item = InboxItem.model_validate(record["data"]["item"])
        if edited_content is not None:
            item = item.model_copy(
                update={
                    "input": item.input.model_copy(update={"content": edited_content, "artifacts": edited_artifacts})
                }
            )
        return item

    async def message(self, identity):
        row = await self.sql(
            lambda c: _one(
                c,
                "SELECT record_id,ordinal FROM message_refs WHERE stream_id=? AND message_id=?",
                (self.thread_id, identity),
            )
        )
        if row is None:
            raise StreamCorrupt(f"Missing message identity: {identity}")
        record = await self.stream.record(row[0])
        data = record["data"]
        if record["kind"] == "message":
            return MESSAGE.validate_python(data["message"])
        if record["kind"] == "surface_replaced":
            return MESSAGE.validate_python(data["messages"][row[1]])
        if record["kind"] != "input_committed":
            raise StreamCorrupt("Message reference has invalid kind")
        item = await self.input(data["body_ref"])
        return input_message(item, data["projection"])


def input_message(item, projection):
    payload = item.input
    parts = (TextPart(text=payload.content),) + tuple(
        ImagePart(image=image) for image in payload.images
    )
    base = {"id": item.id, "parts": parts, "artifacts": payload.artifacts}
    if isinstance(payload, HumanInput):
        message = HumanInputMessage(**base, steering=projection.get("steering", False))
    else:
        message = RuntimeNoticeMessage(
            **base, source=payload.source, event=payload.event
        )
    return MESSAGE.validate_python({**message.model_dump(mode="json"), **projection})
