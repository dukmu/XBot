"""Conversation archive business and authorization over the physical backend."""

import json
from XBotv2.agentloop.api.storage import ExecutionRecord, InputRecord
from XBotv2.agentloop.impl.storage import ConversationStore
from XBotv2.core.api.artifacts import ArtifactRef
from XBotv2.core.api.history import HistoryCursorInvalid
from XBotv2.core.api.errors import OperationError
from XBotv2.persistence.api.models import ArtifactPayload


class ArchiveService:
    def __init__(self, backend, sessions):
        self.backend = backend
        self.sessions = sessions

    def _store(self, session_id, thread_id):
        return ConversationStore(
            self.backend,
            session_id,
            thread_id,
            self.sessions.thread_repository(session_id, thread_id),
        )

    async def flush(self, session_id, thread_id):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            async with store.operation():
                return (
                    await store.stream.flush()
                    if await store.has_history()
                    else (0, 0, 0)
                )

    async def copy_state(self, event):
        event.handled.add("agentloop")

    async def finalize_fork(self, event):
        cuts = {thread: (seq, offset) for thread, seq, offset in event.cuts}
        for thread in event.thread_ids:
            store = self._store(event.target_session_id, thread)
            async with store.operation(write=True):
                pending = await store.pending_ids()
                seq, offset = cuts.get(thread, (0, 0))
                await store.stream.append(
                    "fork_boundary",
                    {
                        "source_session": event.source_session_id,
                        "source_stream": thread,
                        "source_seq": seq,
                        "source_offset": offset,
                        "input_ids": pending,
                        "origins": event.origins.get(thread, {}),
                    },
                )

    async def _execution_start(self, session_id, thread_id, record):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            async with store.operation(write=True):
                await store.stream.append("execution_started", record.model_dump(mode="json"))

    async def _execution_end(self, session_id, thread_id, record):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            async with store.operation(write=True):
                await store.stream.append("execution_ended", record.model_dump(mode="json"))

    async def execution_for_turn(self, session_id, thread_id, turn_id):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            if not await store.has_history():
                return None
            async with store.operation():
                rows = await store.sql(lambda sql: sql.execute(
                    "SELECT turn_id,input_ids,status,final_message_id FROM executions WHERE thread_id=? AND turn_id=?", (thread_id, turn_id)))
                if not rows:
                    return None
                turn_id, input_ids, status, final_message_id = rows[0]
                return ExecutionRecord(turn_id=turn_id, input_ids=json.loads(input_ids), status=status, final_message_id=final_message_id)

    async def execution_for_input(self, session_id, thread_id, input_id):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            if not await store.has_history():
                return None
            async with store.operation():
                rows = await store.sql(lambda sql: sql.execute(
                    "SELECT e.turn_id,e.input_ids,e.status,e.final_message_id FROM executions e "
                    "JOIN execution_inputs i ON i.thread_id=e.thread_id AND i.turn_id=e.turn_id "
                    "WHERE e.thread_id=? AND i.input_id=? ORDER BY e.order_key DESC LIMIT 1", (thread_id, input_id)))
                if not rows:
                    return None
                turn_id, input_ids, status, final_message_id = rows[0]
                return ExecutionRecord(turn_id=turn_id, input_ids=json.loads(input_ids), status=status, final_message_id=final_message_id)

    async def latest_input(self, session_id, thread_id, *, source=None, event=None):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            if not await store.has_history():
                return None
            async with store.operation():
                query = "SELECT input_id,status,source,event,target FROM input_facts WHERE thread_id=?"
                parameters = [thread_id]
                if source is not None:
                    query += " AND source=?"
                    parameters.append(source)
                if event is not None:
                    query += " AND event=?"
                    parameters.append(event)
                query += " ORDER BY order_key DESC LIMIT 1"
                rows = await store.sql(lambda sql: sql.execute(query, parameters))
                if not rows:
                    return None
                input_id, status, source, event, target = rows[0]
                return InputRecord(input_id=input_id, status=status, source=source, event=event, target=target)

    async def latest_execution(self, session_id, thread_id):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            if not await store.has_history():
                return None
            async with store.operation():
                rows = await store.sql(lambda sql: sql.execute(
                    "SELECT turn_id,input_ids,status,final_message_id FROM executions WHERE thread_id=? ORDER BY order_key DESC LIMIT 1", (thread_id,)))
                if not rows:
                    return None
                turn_id, input_ids, status, final_message_id = rows[0]
                return ExecutionRecord(turn_id=turn_id, input_ids=json.loads(input_ids), status=status, final_message_id=final_message_id)

    async def message(self, session_id, thread_id, message_id):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            async with store.operation():
                return await store.message(message_id)

    async def surface(self, session_id, thread_id):
        async with self.backend.use(session_id):
            return await self._store(session_id, thread_id).history.load_surface()

    async def message_count(self, session_id, thread_id):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            if not await store.has_history():
                return 0
            async with store.operation():
                return await store.sql(
                    lambda sql: sql.execute(
                        "SELECT COUNT(*) FROM transcript WHERE thread_id=?",
                        (thread_id,),
                    )[0][0]
                )

    async def turn_count(self, session_id, thread_id):
        async with self.backend.use(session_id):
            return await self._store(session_id, thread_id).history.count_turns()

    async def message_page(self, session_id, thread_id, *, cursor=None, limit=160, before_id=None, after_id=None):
        async with self.backend.use(session_id):
            try:
                return await self._store(session_id, thread_id).history.page_transcript(
                    limit=limit or 160, cursor=cursor, before_id=before_id, after_id=after_id
                )
            except HistoryCursorInvalid as error:
                raise OperationError("invalid_cursor", str(error)) from error

    async def _display_args(self, store, message):
        from XBotv2.core.api.messages import AssistantMessage
        from XBotv2.core.api.tools import ToolCall

        rows = await store.sql(lambda sql: sql.execute(
            "SELECT owners.assistant_id FROM tool_call_owners owners "
            "JOIN transcript assistant ON assistant.thread_id=owners.thread_id "
            "AND assistant.record_id=owners.assistant_id "
            "JOIN transcript result ON result.thread_id=owners.thread_id "
            "AND result.record_id=? WHERE owners.thread_id=? AND owners.call_id=? "
            "AND assistant.order_key<result.order_key ORDER BY assistant.order_key DESC LIMIT 1",
            (str(message.id), store.thread_id, str(message.call.id)),
        ))
        if not rows:
            raise OperationError("tool_call_not_found", f"No assistant call for {message.call.id}")
        assistant = await store.message(rows[0][0])
        if isinstance(assistant, AssistantMessage):
            for part in assistant.parts:
                if isinstance(part, ToolCall) and part.id == message.call.id:
                    return part.args
        raise OperationError("tool_call_not_found", f"No assistant call for {message.call.id}")

    async def display_page(self, session_id, thread_id, *, cursor=None, limit=160, before_id=None, after_id=None):
        from XBotv2.agentloop.impl.display import display_record
        from XBotv2.core.api.history import HistoryPage
        from XBotv2.core.api.messages import ToolMessage

        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            try:
                async with store.operation():
                    page = await store.history.page_refs("transcript", limit or 160, cursor, before_id, after_id)
                    records = []
                    for identity in page.items:
                        message = await store.message(identity)
                        args = await self._display_args(store, message) if isinstance(message, ToolMessage) else None
                        records.append(display_record(message, args))
                    return HistoryPage(tuple(records), page.older_cursor, page.newer_cursor)
            except HistoryCursorInvalid as error:
                raise OperationError("invalid_cursor", str(error)) from error

    async def display_body(self, session_id, thread_id, message_id, *, field, offset=0, limit=4096):
        import codecs
        from XBotv2.agentloop.impl.display import body_fields
        from XBotv2.core.api.messages import ToolMessage
        from XBotv2.core.api.tools import ToolSucceeded, ToolFailed
        from XBotv2.session.api.records import DisplayBodyPage

        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            async with store.operation():
                message = await store.message(message_id)
                if field.startswith("artifact:"):
                    identity = field[len("artifact:"):]
                    refs = message.outcome.output.artifacts if isinstance(message, ToolMessage) and isinstance(message.outcome, (ToolSucceeded, ToolFailed)) else ()
                    ref = next((item for item in refs if item.id == identity and item.media_type.lower().startswith("text/")), None)
                    if ref is None:
                        raise OperationError("field_not_found", "Text artifact is not referenced by this tool result")
                    if offset > ref.size:
                        raise OperationError("invalid_offset", "Body offset exceeds field length")
                    prefix = min(offset, 3)
                    artifact = await self.artifact(session_id, thread_id, identity, offset=offset-prefix, limit=prefix+limit)
                    data = b"".join([chunk async for chunk in artifact.content])
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    decoder.decode(data[:prefix], final=False)
                    content = decoder.decode(data[prefix:], final=offset + len(data) - prefix >= ref.size)
                    end = min(offset + limit, ref.size)
                    return DisplayBodyPage(content=content, offset=offset, total=ref.size, next_offset=end if end < ref.size else None)
                args = await self._display_args(store, message) if isinstance(message, ToolMessage) and field == "args" else None
                fields = body_fields(message, args)
                if field not in fields:
                    raise OperationError("field_not_found", f"Unknown field {field!r}")
                text = fields[field]
                if offset > len(text):
                    raise OperationError("invalid_offset", "Body offset exceeds field length")
                end = min(offset + limit, len(text))
                return DisplayBodyPage(content=text[offset:end], offset=offset, total=len(text), next_offset=end if end < len(text) else None)

    async def trajectory_page(
        self, session_id, thread_id, *, cursor=None, limit=160, before=None
    ):
        async with self.backend.use(session_id):
            try:
                return await self._store(session_id, thread_id).history.page_trajectory(
                    limit=limit, cursor=cursor, before=before
                )
            except HistoryCursorInvalid as error:
                raise OperationError("invalid_cursor", str(error)) from error

    async def record_selection(
        self, session_id, thread_id, *, after_seq=0, through_seq=None, record_ids=None,
    ):
        """Reference this conversation's fixed canonical JSONL record selection."""
        async with self.backend.use(session_id):
            claim = None if self.backend.owns(session_id) else self.backend.acquire(session_id)
            try:
                store = self._store(session_id, thread_id)
                async with store.operation(write=True):
                    await store.stream.flush()
                    ref = await self.backend.artifacts(session_id, thread_id).reference_records(
                        thread_id, after_seq=after_seq, through_seq=through_seq,
                        record_ids=record_ids, name="conversation-records.jsonl",
                    )
                    await store.stream.append("artifact_referenced", {"artifact": ref.model_dump(mode="json")})
                    return ref
            finally:
                if claim is not None:
                    claim.release()

    async def artifact(self, session_id, thread_id, artifact_id, *, offset=0, limit=None):
        async with self.backend.use(session_id):
            store = self._store(session_id, thread_id)
            metadata = None
            if await store.has_history():
                async with store.operation():
                    rows = await store.sql(lambda sql: sql.execute(
                        "SELECT metadata FROM conversation_artifacts WHERE thread_id=? AND artifact_id=?",
                        (thread_id, artifact_id),
                    ))
                    metadata = rows[0][0] if rows else None
            if metadata is None:
                raise OperationError("artifact_not_found", "Artifact is not referenced by this conversation")
            ref = ArtifactRef.model_validate_json(metadata)
            artifacts = self.backend.artifacts(session_id, thread_id)
            if not await artifacts.exists(ref):
                raise OperationError("artifact_not_found", "Referenced artifact is missing")
            return ArtifactPayload(
                artifacts.read_chunks(ref, offset=offset, limit=limit), ref.media_type, ref.name,
                min(ref.size - min(offset, ref.size), limit) if limit is not None else ref.size - min(offset, ref.size),
            )

    async def export_rollout(self, session_id, thread_id, destination):
        from XBotv2.agentloop.impl.exchange import export_rollout

        async with self.backend.use(session_id):
            return await export_rollout(self, session_id, thread_id, destination)

    async def import_rollout(
        self, source, *, workspace_root=None, artifact_source=None
    ):
        from XBotv2.agentloop.impl.exchange import import_rollout

        async with self.backend.use():
            return await import_rollout(
                self,
                source,
                workspace_root=workspace_root,
                artifact_source=artifact_source,
            )
