"""Conversation archive business and authorization over the physical backend."""

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

    async def message_page(self, session_id, thread_id, *, cursor=None, limit=160):
        async with self.backend.use(session_id):
            try:
                return await self._store(session_id, thread_id).history.page_transcript(
                    limit=limit or 160, cursor=cursor
                )
            except HistoryCursorInvalid as error:
                raise OperationError("invalid_cursor", str(error)) from error

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

    async def artifact(self, session_id, thread_id, artifact_id):
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
                artifacts.read_chunks(ref), ref.media_type, ref.name, ref.size,
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
