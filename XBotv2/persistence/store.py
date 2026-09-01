"""Filesystem adapters composed as one thread persistence service."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.filesystem.atomic import write_text_atomic
from XBotv2.core.history import (
    ConversationPage,
    HistoryNode,
    HistoryCursorInvalid,
    decode_history_cursor,
    encode_history_cursor,
)
from XBotv2.core.messages import Message
from XBotv2.core.tools import JsonObject
from XBotv2.core.paths import SessionPaths, ThreadPaths
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.agentloop.inbox import InboxInput
from XBotv2.persistence.models import (
    InboxSnapshot,
    MessagePayloadRecord,
    MessageRecord,
    SurfaceReplaceRecord,
    TrajectoryEventRecord,
    ThreadLifecycleRecord,
    ThreadMetadata,
    utc_now,
)
from xcore.state import StateService


class MessageHistoryStore:
    """Append-only trajectory store with one deterministic message surface."""

    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._path = paths.messages_file
        self._cursor_scope = f"{paths.session_id}/{paths.thread_id}"
        self._log = runtime_log
        self._next_position = 1

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[Message]:
        started = time.perf_counter()
        nodes = self.load_surface()
        messages = [node.message for node in nodes]
        self._log.debug(
            "persistence.history.loaded",
            messages=len(messages),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return messages

    def load_transcript(self) -> list[Message]:
        """Derive the human transcript without hiding compacted conversation."""
        return [node.message for node in _fold_transcript(self._records())]

    def load_surface(self) -> tuple[HistoryNode, ...]:
        records = self._records()
        self._next_position = len(records) + 1
        return _fold_surface(records)

    def append(self, messages: Sequence[Message]) -> tuple[HistoryNode, ...]:
        if not messages:
            return ()
        self._ensure_loaded_id()
        records = [
            MessageRecord.from_message(message, self._next_position + index)
            for index, message in enumerate(messages)
        ]
        self._append_records(records)
        self._next_position += len(records)
        self._log.debug(
            "persistence.history.appended",
            messages=len(records),
            next_position=self._next_position,
        )
        return tuple(
            HistoryNode(str(record.position), message)
            for record, message in zip(records, messages, strict=True)
        )

    def replace(self, messages: Sequence[Message]) -> None:
        surface = self.load_surface()
        if not surface:
            self.append(messages)
            return
        self.replace_surface(
            tuple(node.node_id for node in surface),
            messages,
            operation="replace",
            preserve_transcript=False,
        )

    def replace_surface(
        self,
        source_node_ids: Sequence[str],
        messages: Sequence[Message],
        *,
        operation: str,
        preserve_transcript: bool,
    ) -> tuple[HistoryNode, ...]:
        records = self._records()
        self._next_position = len(records) + 1
        record = SurfaceReplaceRecord(
            position=self._next_position,
            operation=operation,
            transcript="preserve" if preserve_transcript else "replace",
            source_node_ids=tuple(source_node_ids),
            messages=tuple(
                MessagePayloadRecord.from_message(message) for message in messages
            ),
        )
        # Both projections must accept the transition before it becomes durable.
        prospective = [*records, record]
        _fold_surface(prospective)
        _fold_transcript(prospective)
        self._append_records((record,))
        self._next_position = record.position + 1
        self._log.info(
            "persistence.surface.replaced",
            operation=operation,
            source_nodes=len(source_node_ids),
            replacement_nodes=len(messages),
        )
        return tuple(
            HistoryNode(f"{record.position}:{index}", message)
            for index, message in enumerate(messages)
        )

    def record(self, event: str, data: JsonObject) -> None:
        self.load_surface()
        record = TrajectoryEventRecord(
            position=self._next_position,
            event=event,
            data=data,
            timestamp=utc_now(),
        )
        self._append_records((record,))
        self._next_position += 1
        self._log.debug("persistence.trajectory.event", trajectory_event=event)

    def count(self) -> int:
        return len(self.load_surface())

    def page(self, *, limit: int, cursor: str | None = None) -> ConversationPage:
        return self._page_nodes(
            _fold_surface(self._records()),
            limit=limit,
            cursor=cursor,
            projection="surface",
        )

    def page_transcript(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage:
        return self._page_nodes(
            _fold_transcript(self._records()),
            limit=limit,
            cursor=cursor,
            projection="transcript",
        )

    def _page_nodes(
        self,
        nodes: Sequence[HistoryNode],
        *,
        limit: int,
        cursor: str | None,
        projection: str,
    ) -> ConversationPage:
        if limit < 1:
            raise ValueError("History page limit must be positive")
        records = self._records()
        if not nodes:
            if cursor is not None:
                raise HistoryCursorInvalid(
                    "History cursor is outside the current history"
                )
            return ConversationPage(())
        generation = max((
            record.position
            for record in records
            if isinstance(record, SurfaceReplaceRecord)
            and (
                projection == "surface"
                or record.transcript == "replace"
            )
        ), default=0)
        revision = f"{self._cursor_scope}:{projection}:{generation}"
        end = len(nodes) if cursor is None else decode_history_cursor(cursor, revision)
        if end < 0 or end > len(nodes):
            raise HistoryCursorInvalid("History cursor is outside the current history")
        if end == 0:
            return ConversationPage(())
        start = max(0, end - limit)
        messages = tuple(node.message for node in nodes[start:end])
        return ConversationPage(
            messages,
            encode_history_cursor(revision, start) if start else None,
        )

    def has_history(self) -> bool:
        return self._path.exists() and self._path.stat().st_size > 0

    def _ensure_loaded_id(self) -> None:
        if (
            self._next_position == 1
            and self._path.exists()
            and self._path.stat().st_size
        ):
            self.load_surface()

    def _records(self) -> list[MessageRecord | SurfaceReplaceRecord | TrajectoryEventRecord]:
        records = [
            _trajectory_record(raw)
            for raw in _read_jsonl(self._path, "messages.jsonl")
        ]
        positions = [record.position for record in records]
        if positions != list(range(1, len(records) + 1)):
            raise ValueError("Trajectory positions must be contiguous and start at 1")
        return records

    def _append_records(
        self,
        records: Sequence[MessageRecord | SurfaceReplaceRecord | TrajectoryEventRecord],
    ) -> None:
        payload = "".join(
            json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
            for record in records
        ).encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        original_size = os.fstat(descriptor).st_size
        try:
            if original_size:
                with self._path.open("rb") as stream:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        raise ValueError("messages.jsonl ends with an incomplete record")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written == 0:
                    raise OSError("Trajectory append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            os.ftruncate(descriptor, original_size)
            os.fsync(descriptor)
            raise
        finally:
            os.close(descriptor)


TrajectoryRecord = MessageRecord | SurfaceReplaceRecord | TrajectoryEventRecord


def _trajectory_record(value: Mapping[str, object]) -> TrajectoryRecord:
    record_type = value.get("record_type")
    if record_type is None:
        return MessageRecord.from_dict(value)
    if record_type == "surface_replace":
        return SurfaceReplaceRecord.from_dict(value)
    if record_type == "event":
        return TrajectoryEventRecord.from_dict(value)
    raise ValueError(f"Unknown trajectory record type: {record_type!r}")


def _fold_surface(records: Sequence[TrajectoryRecord]) -> tuple[HistoryNode, ...]:
    surface: list[HistoryNode] = []
    for record in records:
        if isinstance(record, MessageRecord):
            surface.append(HistoryNode(str(record.position), record.to_message()))
        elif isinstance(record, SurfaceReplaceRecord):
            _apply_surface_record(surface, record)
    for node in surface:
        node.message.seal()
    return tuple(surface)


def _fold_transcript(records: Sequence[TrajectoryRecord]) -> tuple[HistoryNode, ...]:
    """Fold only explicit user history edits; compaction remains model-only."""
    transcript: list[HistoryNode] = []
    lineage: dict[str, tuple[str, ...]] = {}
    for record in records:
        if isinstance(record, MessageRecord):
            node_id = str(record.position)
            transcript.append(HistoryNode(node_id, record.to_message()))
            lineage[node_id] = (node_id,)
        elif isinstance(record, SurfaceReplaceRecord):
            sources = tuple(
                origin
                for source in record.source_node_ids
                for origin in lineage.get(source, (source,))
            )
            replacements = [
                HistoryNode(f"{record.position}:{index}", payload.to_message())
                for index, payload in enumerate(record.messages)
            ]
            if record.transcript == "preserve":
                if len(replacements) != 1:
                    raise ValueError(
                        "Transcript-preserving replacement must produce one surface node"
                    )
                lineage[replacements[0].node_id] = sources
                continue
            _replace_transcript_nodes(transcript, sources, replacements, record.position)
            for node in replacements:
                lineage[node.node_id] = (node.node_id,)
    for node in transcript:
        node.message.seal()
    return tuple(transcript)


def _replace_transcript_nodes(
    transcript: list[HistoryNode],
    source_ids: Sequence[str],
    replacements: Sequence[HistoryNode],
    position: int,
) -> None:
    if not source_ids:
        raise ValueError(f"Transcript replacement at {position} has no sources")
    try:
        start = next(
            index
            for index, node in enumerate(transcript)
            if node.node_id == source_ids[0]
        )
    except StopIteration as exc:
        raise ValueError(
            f"Transcript replacement at {position} sources are not current"
        ) from exc
    current = [node.node_id for node in transcript[start:start + len(source_ids)]]
    if current != list(source_ids):
        raise ValueError(f"Transcript replacement at {position} sources are not current")
    transcript[start:start + len(source_ids)] = replacements


def _apply_surface_record(
    surface: list[HistoryNode],
    record: SurfaceReplaceRecord,
) -> None:
    source_ids = list(record.source_node_ids)
    try:
        start = next(
            index
            for index, node in enumerate(surface)
            if node.node_id == source_ids[0]
        )
    except StopIteration as exc:
        raise ValueError(
            f"Surface replacement at {record.position} source nodes are not current"
        ) from exc
    current = [
        node.node_id for node in surface[start:start + len(source_ids)]
    ]
    if current != source_ids:
        raise ValueError(
            f"Surface replacement at {record.position} source nodes are not current"
        )
    replacements = [
        HistoryNode(f"{record.position}:{index}", payload.to_message())
        for index, payload in enumerate(record.messages)
    ]
    surface[start:start + len(source_ids)] = replacements


class ThreadMetadataStore:
    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._path = paths.metadata_file
        self._log = runtime_log

    def load(self) -> ThreadMetadata:
        raw = _read_json(self._path, "thread metadata")
        if raw is None:
            return ThreadMetadata()
        return ThreadMetadata.from_dict(raw)

    def save(self, metadata: ThreadMetadata) -> None:
        write_text_atomic(
            self._path,
            json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        self._log.debug("persistence.metadata.saved")


class InboxStore:
    """Atomic projection of inputs not yet committed to conversation history."""

    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._path = paths.inbox_file
        self._log = runtime_log

    def load(self) -> list[InboxInput]:
        raw = _read_json(self._path, "inbox snapshot")
        if raw is None:
            return []
        return InboxSnapshot.from_dict(raw).to_inputs()

    def replace(self, items: Sequence[InboxInput]) -> None:
        snapshot = InboxSnapshot.from_inputs(items)
        write_text_atomic(
            self._path,
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        self._log.debug("persistence.inbox.replaced", items=len(items))

    def reconcile(self, committed_input_ids: set[str]) -> list[InboxInput]:
        stored = self.load()
        pending = [
            item for item in stored if item.message_id not in committed_input_ids
        ]
        if len(pending) != len(stored):
            self.replace(pending)
        self._log.debug(
            "persistence.inbox.reconciled",
            stored=len(stored),
            committed=len(stored) - len(pending),
            pending=len(pending),
        )
        return pending


class ThreadLifecycleStore:
    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._path = paths.session.threads_log
        self._log = runtime_log

    def append(self, record: ThreadLifecycleRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            self._path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o644,
        )
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError(
                    f"Incomplete lifecycle append: {written}/{len(payload)} bytes"
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._log.debug(
            "persistence.lifecycle.appended",
            bytes=len(payload),
        )

    def load(self) -> list[ThreadLifecycleRecord]:
        return [
            ThreadLifecycleRecord.from_dict(raw)
            for raw in _read_jsonl(self._path, "thread lifecycle")
        ]


def _read_json(path: Path, name: str) -> Mapping[str, object] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {name} JSON") from exc
    if not isinstance(value, Mapping):
        raise TypeError(f"{name.capitalize()} must be an object")
    return value


def _read_jsonl(path: Path, name: str) -> list[Mapping[str, object]]:
    if not path.exists():
        return []
    records: list[Mapping[str, object]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid {name} record at line {line_number}"
                ) from exc
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} line {line_number} must be an object")
            records.append(value)
    return records


class ThreadPersistence:
    """Typed persistence composition for one session thread."""

    def __init__(
        self,
        paths: ThreadPaths,
        *,
        state: StateService,
        workspace_root: str = "",
        provider: str = "",
        artifacts: ArtifactStorePort | None = None,
    ) -> None:
        self.paths = paths
        self.session_id = paths.session_id
        self.thread_id = paths.thread_id
        self.workspace_root = workspace_root
        self.provider = provider
        runtime_log = DEFAULT_RUNTIME_LOG.bind(
            "persistence",
            session_id=self.session_id,
            thread_id=self.thread_id,
        )
        self.history = MessageHistoryStore(paths, runtime_log)
        self.artifacts: ArtifactStorePort = (
            artifacts
            if artifacts is not None
            else ArtifactStore(paths, runtime_log)
        )
        self.metadata = ThreadMetadataStore(paths, runtime_log)
        self.inbox = InboxStore(paths, runtime_log)
        self.lifecycle = ThreadLifecycleStore(paths, runtime_log)
        self.state = state

    def has_persisted_state(self) -> bool:
        return (
            self.history.has_history()
            or self.paths.metadata_file.exists()
            or self.paths.inbox_file.exists()
            or self.paths.plugin_state_file.exists()
        )

    @classmethod
    def create(
        cls,
        paths: SessionPaths | ThreadPaths,
        *,
        thread_id: str,
        workspace_root: str,
        provider: str,
        artifacts: ArtifactStorePort | None = None,
    ) -> "ThreadPersistence":
        thread_paths = (
            paths.thread(thread_id)
            if isinstance(paths, SessionPaths)
            else paths
        )
        thread_paths.state_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            thread_paths,
            state=StateService(path=thread_paths.plugin_state_file),
            workspace_root=workspace_root,
            provider=provider,
            artifacts=artifacts,
        )

    @classmethod
    def open(
        cls,
        paths: SessionPaths | ThreadPaths,
        *,
        thread_id: str,
        workspace_root: str = "",
        provider: str = "",
    ) -> "ThreadPersistence":
        """Open an inactive thread with one private StateService instance."""
        thread_paths = (
            paths.thread(thread_id)
            if isinstance(paths, SessionPaths)
            else paths
        )
        return cls(
            thread_paths,
            state=StateService(path=thread_paths.plugin_state_file),
            workspace_root=workspace_root,
            provider=provider,
        )


__all__ = [
    "InboxStore",
    "MessageHistoryStore",
    "ThreadMetadataStore",
    "ThreadLifecycleStore",
    "ThreadPersistence",
]
