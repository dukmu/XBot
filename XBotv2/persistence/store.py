"""Filesystem adapters composed as one thread persistence service."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.filesystem.atomic import write_text_atomic
from XBotv2.core.history import (
    ConversationPage,
    HistoryNode,
    HistoryCursorInvalid,
    TrajectoryEvent,
    TrajectoryMessage,
    TrajectoryPage,
    TrajectorySurfaceReplace,
    decode_history_cursor,
    encode_history_cursor,
    page_messages,
)
from XBotv2.core.messages import Message
from XBotv2.core.metadata import ThreadMetadata
from pydantic import JsonValue
from XBotv2.core.paths import SessionPaths, ThreadPaths
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.agentloop.contracts import InboxInput
from XBotv2.persistence.models import (
    InboxSnapshot,
    MessagePayloadRecord,
    MessageRecord,
    SurfaceReplaceRecord,
    TrajectoryEventRecord,
    utc_now,
)
from XBotv2.persistence.contracts import (
    ThreadLifecycleRecord,
    TrajectoryTransaction,
)
from xcore.state import StateService

# Ordinary telemetry is flushed with a bounded delay; transaction markers are
# durability boundaries and are forced immediately.
TrajectoryRecord = MessageRecord | SurfaceReplaceRecord | TrajectoryEventRecord

_SYNC_EVENT_PREFIXES: tuple[str, ...] = ("compaction/",)
_SYNC_INTERVAL_SECONDS = 0.25

class _SurfaceState:
    """Incrementally folded current conversation surface."""

    def __init__(self) -> None:
        self.nodes: list[HistoryNode] = []
        self.revision = 0

    def apply(self, record: TrajectoryRecord) -> None:
        if isinstance(record, MessageRecord):
            self.nodes.append(_sealed_node(str(record.position), record.to_message()))
        elif isinstance(record, SurfaceReplaceRecord):
            _replace_nodes(
                self.nodes,
                record.source_node_ids,
                _replacement_nodes(record),
                position=record.position,
                scope="Surface",
                source_term="source nodes",
            )
            self.revision = max(self.revision, record.position)

    def view(self) -> tuple[HistoryNode, ...]:
        return tuple(self.nodes)


class _TranscriptState:
    """Incrementally folded human transcript; compaction stays model-only."""

    def __init__(self) -> None:
        self.nodes: list[HistoryNode] = []
        self.lineage: dict[str, tuple[str, ...]] = {}
        self.revision = 0

    def apply(self, record: TrajectoryRecord) -> None:
        if isinstance(record, MessageRecord):
            node_id = str(record.position)
            self.nodes.append(_sealed_node(node_id, record.to_message()))
            self.lineage[node_id] = (node_id,)
        elif isinstance(record, SurfaceReplaceRecord):
            sources = tuple(
                origin
                for source in record.source_node_ids
                for origin in self.lineage.get(source, (source,))
            )
            replacements = _replacement_nodes(record)
            if record.transcript == "preserve":
                if len(replacements) != 1:
                    raise ValueError(
                        "Transcript-preserving replacement must produce one surface node"
                    )
                self.lineage[replacements[0].node_id] = sources
                return
            _replace_nodes(
                self.nodes,
                sources,
                replacements,
                position=record.position,
                scope="Transcript",
                source_term="sources",
            )
            for node in replacements:
                self.lineage[node.node_id] = (node.node_id,)
            self.revision = max(self.revision, record.position)

    def view(self) -> tuple[HistoryNode, ...]:
        return tuple(self.nodes)


def _sealed_node(node_id: str, message: Message) -> HistoryNode:
    node = HistoryNode(node_id, message)
    node.message.seal()
    return node


def _apply_records(state: _SurfaceState | _TranscriptState, records: Sequence[TrajectoryRecord]) -> None:
    for record in records:
        state.apply(record)


class _TrajectoryState:
    """Process-wide cache and position allocator for one trajectory file.

    The parsed trajectory and the folded projections of the current file
    version are reused by every reader of the path; this process's own writes
    extend them incrementally, and a file change made elsewhere drops them.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.records: list[TrajectoryRecord] | None = None
        self.surface: _SurfaceState | None = None
        self.transcript: _TranscriptState | None = None
        self.next_position = 1
        self.file_size = -1
        self.read_size = -1
        self.last_sync = 0.0

    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def recorded(self) -> list[TrajectoryRecord]:
        """The parsed trajectory, re-read only when the file version changed."""
        size = self.size()
        if self.records is None or self.read_size != size:
            self.surface = None
            self.transcript = None
            self.records = self._parse(size)
        return self.records

    def surface_state(self) -> _SurfaceState:
        records = self.recorded()
        if self.surface is None:
            self.surface = _SurfaceState()
            _apply_records(self.surface, records)
        return self.surface

    def transcript_state(self) -> _TranscriptState:
        records = self.recorded()
        if self.transcript is None:
            self.transcript = _TranscriptState()
            _apply_records(self.transcript, records)
        return self.transcript

    def prepare_write(self, log: RuntimeLog) -> list[TrajectoryRecord]:
        """Make positions and the file tail current before this process appends."""
        size = self.size()
        if (
            self.records is not None
            and self.read_size == size
            and self.file_size == size
        ):
            self.next_position = len(self.records) + 1
            return self.records
        size = _drop_incomplete_tail(self.path, size, log)
        self.surface = None
        self.transcript = None
        self.records = self._parse(size)
        self.file_size = size
        self.next_position = len(self.records) + 1
        return self.records

    def wrote(self, added: Sequence[TrajectoryRecord]) -> None:
        """Extend the cache after this process appended records durably."""
        self.records = [*(self.records or ()), *added]
        self.next_position = len(self.records) + 1
        self.file_size = self.read_size = self.size()
        if self.surface is not None:
            _apply_records(self.surface, added)
        if self.transcript is not None:
            _apply_records(self.transcript, added)

    def replaced(
        self,
        prospective: list[TrajectoryRecord],
        surface: _SurfaceState,
        transcript: _TranscriptState,
    ) -> None:
        """Adopt one validated surface replacement as the cached trajectory."""
        self.records = prospective
        self.surface = surface
        self.transcript = transcript
        self.next_position = len(prospective) + 1
        self.file_size = self.read_size = self.size()

    def _parse(self, size: int) -> list[TrajectoryRecord]:
        parsed: list[TrajectoryRecord] = []
        for index, raw in enumerate(
            _read_jsonl(self.path, "messages.jsonl"),
            start=1,
        ):
            record = _trajectory_record(raw)
            if record.position != index:
                raise ValueError(
                    "Trajectory positions must be contiguous and start at 1"
                )
            parsed.append(record)
        self.read_size = size
        return parsed


# Bounded cache: a long-lived server must not keep every visited session's
# trajectory and projections in memory.
_CACHE_LIMIT = 8
_CACHE_RECORD_BUDGET = 60_000

_trajectory_states: "OrderedDict[Path, _TrajectoryState]" = OrderedDict()
_trajectory_guard = threading.Lock()


@contextmanager
def _trajectory_use(path: Path) -> Iterator[_TrajectoryState]:
    """Borrow the shared state for one operation while holding its lock."""
    with _trajectory_guard:
        state = _trajectory_states.get(path)
        if state is None:
            state = _TrajectoryState(path)
            _trajectory_states[path] = state
        else:
            _trajectory_states.move_to_end(path)
        state.lock.acquire()
        _evict_idle_states(keep=path)
    try:
        yield state
    finally:
        state.lock.release()


def _evict_idle_states(*, keep: Path) -> None:
    """Drop least recently used caches that no operation is using."""
    while True:
        cached = sum(
            len(state.records or ()) for state in _trajectory_states.values()
        )
        within_limit = (
            len(_trajectory_states) <= _CACHE_LIMIT
            and cached <= _CACHE_RECORD_BUDGET
        )
        if within_limit or len(_trajectory_states) <= 1:
            return
        for candidate, state in list(_trajectory_states.items()):
            if candidate == keep:
                continue
            if state.lock.acquire(blocking=False):
                state.lock.release()
                del _trajectory_states[candidate]
                break
        else:
            return


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
        with _trajectory_use(self._path) as state:
            nodes = state.transcript_state().view()
        return [node.message for node in nodes]

    def load_surface(self) -> tuple[HistoryNode, ...]:
        with _trajectory_use(self._path) as state:
            return state.surface_state().view()

    def append(self, messages: Sequence[Message]) -> tuple[HistoryNode, ...]:
        if not messages:
            return ()
        with _trajectory_use(self._path) as state:
            state.prepare_write(self._log)
            added = [
                MessageRecord.from_message(message, state.next_position + index)
                for index, message in enumerate(messages)
            ]
            self._append_records(added, state=state)
            state.wrote(added)
            next_position = state.next_position
        self._log.debug(
            "persistence.history.appended",
            messages=len(added),
            next_position=next_position,
        )
        return tuple(
            HistoryNode(str(record.position), message)
            for record, message in zip(added, messages, strict=True)
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
        with _trajectory_use(self._path) as state:
            records = state.prepare_write(self._log)
            record = SurfaceReplaceRecord(
                position=state.next_position,
                operation=operation,
                transcript="preserve" if preserve_transcript else "replace",
                source_node_ids=tuple(source_node_ids),
                messages=tuple(
                    MessagePayloadRecord.from_message(message) for message in messages
                ),
            )
            # Both projections must accept the transition before it becomes durable.
            prospective = [*records, record]
            surface = _SurfaceState()
            _apply_records(surface, prospective)
            transcript = _TranscriptState()
            _apply_records(transcript, prospective)
            self._append_records((record,), state=state)
            state.replaced(prospective, surface, transcript)
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

    def record(self, event: str, data: dict[str, JsonValue]) -> None:
        with _trajectory_use(self._path) as state:
            state.prepare_write(self._log)
            record = TrajectoryEventRecord(
                position=state.next_position,
                event=event,
                data=data,
                timestamp=utc_now(),
            )
            self._append_records(
                (record,),
                state=state,
                sync=event.startswith(_SYNC_EVENT_PREFIXES),
            )
            state.wrote((record,))
        self._log.debug("persistence.trajectory.event", trajectory_event=event)

    def open_transactions(
        self,
        transaction: TrajectoryTransaction,
    ) -> frozenset[str]:
        """Fold correlated start/end records without changing the surface."""
        open_ids: set[str] = set()
        with _trajectory_use(self._path) as state:
            records = state.recorded()
        for record in records:
            if not isinstance(record, TrajectoryEventRecord):
                continue
            correlation_id = str(record.data.get(transaction.id_field) or "")
            if not correlation_id:
                continue
            if record.event == transaction.start_event:
                open_ids.add(correlation_id)
            elif record.event == transaction.end_event:
                open_ids.discard(correlation_id)
        return frozenset(open_ids)

    def count(self) -> int:
        with _trajectory_use(self._path) as state:
            return len(state.surface_state().nodes)

    def page(self, *, limit: int, cursor: str | None = None) -> ConversationPage:
        with _trajectory_use(self._path) as state:
            surface = state.surface_state()
            return page_messages(
                tuple(node.message for node in surface.nodes),
                revision=self._revision(surface.revision, "surface"),
                limit=limit,
                cursor=cursor,
                out_of_range="History cursor is outside the current history",
            )

    def page_transcript(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage:
        with _trajectory_use(self._path) as state:
            transcript = state.transcript_state()
            return page_messages(
                tuple(node.message for node in transcript.nodes),
                revision=self._revision(transcript.revision, "transcript"),
                limit=limit,
                cursor=cursor,
                out_of_range="History cursor is outside the current history",
            )

    def page_trajectory(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> TrajectoryPage:
        with _trajectory_use(self._path) as state:
            records = state.recorded()
            revision = f"{self._cursor_scope}:trajectory"
            end = len(records) if cursor is None else decode_history_cursor(cursor, revision)
            if end < 0 or end > len(records):
                raise HistoryCursorInvalid("Trajectory cursor is outside the current history")
            start = max(0, end - limit)
            items = tuple(
                _trajectory_item(record) for record in records[start:end]
            )
        return TrajectoryPage(
            items=items,
            next_cursor=encode_history_cursor(revision, start) if start else None,
        )

    def _revision(self, generation: int, projection: str) -> str:
        return f"{self._cursor_scope}:{projection}:{generation}"

    def has_history(self) -> bool:
        return self._path.exists() and self._path.stat().st_size > 0

    def _append_records(
        self,
        records: Sequence[MessageRecord | SurfaceReplaceRecord | TrajectoryEventRecord],
        *,
        state: _TrajectoryState,
        sync: bool = True,
    ) -> None:
        payload = "".join(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n"
            for record in records
        ).encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        original_size = os.fstat(descriptor).st_size
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written == 0:
                    raise OSError("Trajectory append made no progress")
                view = view[written:]
            now = time.monotonic()
            if sync or now - state.last_sync >= _SYNC_INTERVAL_SECONDS:
                os.fsync(descriptor)
                state.last_sync = now
        except BaseException:
            os.ftruncate(descriptor, original_size)
            os.fsync(descriptor)
            raise
        finally:
            os.close(descriptor)




def _trajectory_item(record: TrajectoryRecord) -> (
    TrajectoryMessage | TrajectorySurfaceReplace | TrajectoryEvent
):
    if isinstance(record, MessageRecord):
        return TrajectoryMessage(record.position, record.to_message())
    if isinstance(record, SurfaceReplaceRecord):
        return TrajectorySurfaceReplace(
            record.position,
            record.operation,
            record.transcript,
            record.source_node_ids,
            tuple(message.to_message() for message in record.messages),
        )
    return TrajectoryEvent(
        record.position,
        record.event,
        record.data,
        record.timestamp,
    )


def _trajectory_record(value: Mapping[str, JsonValue]) -> TrajectoryRecord:
    record_type = value.get("record_type")
    if record_type is None:
        return MessageRecord.model_validate(value)
    if record_type == "surface_replace":
        return SurfaceReplaceRecord.model_validate(value)
    if record_type == "event":
        return TrajectoryEventRecord.model_validate(value)
    raise ValueError(f"Unknown trajectory record type: {record_type!r}")


def _fold_surface(records: Sequence[TrajectoryRecord]) -> tuple[HistoryNode, ...]:
    state = _SurfaceState()
    _apply_records(state, records)
    return state.view()


def _fold_transcript(records: Sequence[TrajectoryRecord]) -> tuple[HistoryNode, ...]:
    """Fold only explicit user history edits; compaction remains model-only."""
    state = _TranscriptState()
    _apply_records(state, records)
    return state.view()


def _replacement_nodes(record: SurfaceReplaceRecord) -> list[HistoryNode]:
    return [
        HistoryNode(f"{record.position}:{index}", payload.to_message())
        for index, payload in enumerate(record.messages)
    ]


def _replace_nodes(
    nodes: list[HistoryNode],
    source_ids: Sequence[str],
    replacements: Sequence[HistoryNode],
    *,
    position: int,
    scope: str,
    source_term: str,
) -> None:
    if not source_ids:
        raise ValueError(f"{scope} replacement at {position} has no {source_term}")
    try:
        start = next(
            index
            for index, node in enumerate(nodes)
            if node.node_id == source_ids[0]
        )
    except StopIteration as exc:
        raise ValueError(f"{scope} replacement at {position} {source_term} are not current") from exc
    current = [node.node_id for node in nodes[start:start + len(source_ids)]]
    if current != list(source_ids):
        raise ValueError(f"{scope} replacement at {position} {source_term} are not current")
    nodes[start:start + len(source_ids)] = replacements


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
        return ThreadMetadata.model_validate(raw)

    def save(self, metadata: ThreadMetadata) -> None:
        write_text_atomic(
            self._path,
            json.dumps(metadata.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
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
        return InboxSnapshot.model_validate(raw).to_inputs()

    def replace(self, items: Sequence[InboxInput]) -> None:
        snapshot = InboxSnapshot.from_inputs(items)
        write_text_atomic(
            self._path,
            json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
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
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n"
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
            ThreadLifecycleRecord.model_validate(raw)
            for raw in _read_jsonl(self._path, "thread lifecycle")
        ]


def _read_json(path: Path, name: str) -> Mapping[str, JsonValue] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {name} JSON") from exc
    if not isinstance(value, Mapping):
        raise TypeError(f"{name.capitalize()} must be an object")
    return value


def _drop_incomplete_tail(
    path: Path,
    size: int,
    log: RuntimeLog,
) -> int:
    """Truncate a final record a crash left without its newline.

    The writer never continues an unacknowledged fragment: it removes the bytes
    first, then appends from the last durable record.
    """
    if size == 0 or not path.exists():
        return size
    with path.open("rb") as stream:
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) == b"\n":
            return size
        offset = size
        while offset > 0:
            chunk = min(4096, offset)
            offset -= chunk
            stream.seek(offset)
            found = stream.read(chunk).rfind(b"\n")
            if found >= 0:
                offset += found + 1
                break
    os.truncate(path, offset)
    log.warning(
        "persistence.trajectory.tail_dropped",
        dropped_bytes=size - offset,
    )
    return offset


def _read_jsonl(path: Path, name: str) -> list[Mapping[str, JsonValue]]:
    if not path.exists():
        return []
    records: list[Mapping[str, JsonValue]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.endswith("\n"):
                # The owning runtime appends whole lines, so a final fragment
                # without its newline is an append still in flight rather than
                # a durable record.
                break
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
        thread_paths = _thread_paths(paths, thread_id)
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
        thread_paths = _thread_paths(paths, thread_id)
        return cls(
            thread_paths,
            state=StateService(path=thread_paths.plugin_state_file),
            workspace_root=workspace_root,
            provider=provider,
        )


def _thread_paths(paths: SessionPaths | ThreadPaths, thread_id: str) -> ThreadPaths:
    return paths.thread(thread_id) if isinstance(paths, SessionPaths) else paths


__all__ = [
    "InboxStore",
    "MessageHistoryStore",
    "ThreadMetadataStore",
    "ThreadLifecycleStore",
    "ThreadPersistence",
]
