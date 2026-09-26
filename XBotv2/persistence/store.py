"""Filesystem adapters composed as one thread persistence service."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.filesystem.atomic import write_text_atomic
from XBotv2.core.history import (
    DurableEvent,
    HistoryPage,
    HistoryCursorInvalid,
    DurableEventRecorded,
    MessageAppended,
    TrajectoryRead,
    SurfaceReplaced,
    decode_history_cursor,
    encode_history_cursor,
    page_messages,
)
from XBotv2.core.domain import Cursor, HistoryRevision, TransactionEnded, TransactionStarted
from XBotv2.core.messages import ConversationMessage, HumanInputMessage
from XBotv2.core.metadata import ThreadMetadata
from XBotv2.persistence.contracts import (
    HistoryPort,
    InboxPersistencePort,
    MetadataPort,
    ThreadLifecyclePort,
    ThreadPersistencePort,
)
from pydantic import JsonValue
from XBotv2.core.paths import SessionPaths, ThreadPaths
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.agentloop.contracts import InboxItem
from XBotv2.persistence.models import (
    InboxSnapshot,
    StoredTrajectoryRecord,
)
from XBotv2.persistence.contracts import (
    ThreadLifecycleRecord,
)
from xcore.state import StateService

# Ordinary telemetry is flushed with a bounded delay; callers that declare a
# durable record get an immediate flush instead.
_SYNC_INTERVAL_SECONDS = 0.25

TrajectoryRecord = StoredTrajectoryRecord


class _SurfaceState:
    """Incrementally folded current conversation surface."""

    def __init__(self) -> None:
        self.messages: list[ConversationMessage] = []
        self.seen_ids: set[str] = set()
        self.revision = 0

    def apply(self, record: TrajectoryRecord) -> None:
        entry = record.entry
        if isinstance(entry, MessageAppended):
            message = entry.message
            self._claim((message,), entry.position)
            self.messages.append(message)
        elif isinstance(entry, SurfaceReplaced):
            replacements = list(entry.replacements)
            self._claim(replacements, entry.position)
            _replace_messages(
                self.messages,
                entry.source_ids,
                replacements,
                position=entry.position,
                scope="Surface",
                source_term="source nodes",
            )
            self.revision = max(self.revision, entry.position)

    def _claim(self, messages: Sequence[ConversationMessage], position: int) -> None:
        identities = [message.id for message in messages]
        if len(identities) != len(set(identities)) or self.seen_ids.intersection(identities):
            raise ValueError(f"Surface record at {position} reuses a message identity")
        self.seen_ids.update(identities)

    def view(self) -> tuple[ConversationMessage, ...]:
        return tuple(self.messages)


class _TranscriptState:
    """Incrementally folded human transcript; compaction stays model-only."""

    def __init__(self) -> None:
        self.messages: list[ConversationMessage] = []
        self.lineage: dict[str, tuple[str, ...]] = {}
        self.revision = 0

    def apply(self, record: TrajectoryRecord) -> None:
        entry = record.entry
        if isinstance(entry, MessageAppended):
            message = entry.message
            self.messages.append(message)
            self.lineage[message.id] = (message.id,)
        elif isinstance(entry, SurfaceReplaced):
            sources = tuple(
                origin
                for source in entry.source_ids
                for origin in self.lineage.get(source, (source,))
            )
            replacements = list(entry.replacements)
            if entry.transcript_policy == "preserve":
                if len(replacements) != 1:
                    raise ValueError(
                        "Transcript-preserving replacement must produce one surface record"
                    )
                self.lineage[replacements[0].id] = sources
                return
            _replace_messages(
                self.messages,
                sources,
                replacements,
                position=entry.position,
                scope="Transcript",
                source_term="sources",
            )
            for message in replacements:
                self.lineage[message.id] = (message.id,)
            self.revision = max(self.revision, entry.position)

    def view(self) -> tuple[ConversationMessage, ...]:
        return tuple(self.messages)


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
        self.users = 0
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
            if record.entry.position != index:
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
        state.users += 1
        _evict_idle_states(keep=path)
    state.lock.acquire()
    try:
        yield state
    finally:
        state.lock.release()
        with _trajectory_guard:
            state.users -= 1


def _evict_idle_states(*, keep: Path) -> None:
    """Drop least recently used caches that no operation is using.

    A borrowed state (``users`` above zero) is never evicted, so an operation
    keeps working on the same object even while the cache shrinks around it.
    Neither the registry guard nor another state's lock blocks the caller.
    """
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
        idle = [
            (candidate, state)
            for candidate, state in _trajectory_states.items()
            if candidate != keep and state.users == 0
        ]
        if not idle:
            return
        del _trajectory_states[idle[0][0]]


class MessageHistoryStore(HistoryPort):
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

    def load(self) -> list[ConversationMessage]:
        started = time.perf_counter()
        messages = list(self.load_surface())
        self._log.debug(
            "persistence.history.loaded",
            messages=len(messages),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return messages

    def load_transcript(self) -> list[ConversationMessage]:
        """Derive the human transcript without hiding compacted conversation."""
        with _trajectory_use(self._path) as state:
            return list(state.transcript_state().view())

    def load_surface(self) -> tuple[ConversationMessage, ...]:
        with _trajectory_use(self._path) as state:
            return state.surface_state().view()

    def count_turns(self) -> int:
        """Count accepted human turns in the append-only canonical trajectory.

        Surface replacement may fold or clear messages, while the runtime turn
        counter is lifetime state. Counting MessageAppended records restores
        that counter without adding a second persisted field.
        """
        with _trajectory_use(self._path) as state:
            return sum(
                isinstance(record.entry, MessageAppended)
                and isinstance(record.entry.message, HumanInputMessage)
                for record in state.recorded()
            )

    def append(self, messages: Sequence[ConversationMessage]) -> tuple[ConversationMessage, ...]:
        if not messages:
            return ()
        with _trajectory_use(self._path) as state:
            state.prepare_write(self._log)
            existing_ids = state.surface_state().seen_ids
            added_ids = [message.id for message in messages]
            if len(added_ids) != len(set(added_ids)) or existing_ids.intersection(added_ids):
                raise ValueError("Trajectory messages must have unique identities")
            added = [
                StoredTrajectoryRecord(entry=MessageAppended(
                    position=state.next_position + index,
                    message=message,
                ))
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
        return tuple(messages)

    def replace(self, messages: Sequence[ConversationMessage]) -> None:
        surface = self.load_surface()
        if not surface:
            self.append(messages)
            return
        self.replace_surface(
            tuple(message.id for message in surface),
            messages,
            operation="replace",
            preserve_transcript=False,
        )

    def replace_surface(
        self,
        source_ids: Sequence[str],
        messages: Sequence[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool,
    ) -> tuple[ConversationMessage, ...]:
        with _trajectory_use(self._path) as state:
            records = state.prepare_write(self._log)
            record = StoredTrajectoryRecord(
                entry=SurfaceReplaced(
                    position=state.next_position,
                    operation=operation,
                    transcript_policy=(
                        "preserve" if preserve_transcript else "replace"
                    ),
                    source_ids=tuple(source_ids),
                    replacements=tuple(messages),
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
            source_nodes=len(source_ids),
            replacement_nodes=len(messages),
        )
        return tuple(messages)

    def record(self, event: DurableEvent, *, durable: bool = False) -> None:
        with _trajectory_use(self._path) as state:
            state.prepare_write(self._log)
            record = StoredTrajectoryRecord(
                entry=DurableEventRecorded(
                    position=state.next_position,
                    timestamp=datetime.now(timezone.utc),
                    event=event,
                ),
            )
            self._append_records((record,), state=state, sync=durable)
            state.wrote((record,))
        self._log.debug("persistence.trajectory.event", trajectory_event=event.kind)

    def open_transactions(
        self,
        transaction_kind: str,
    ) -> frozenset[str]:
        """Fold correlated start/end records without changing the surface."""
        open_ids: set[str] = set()
        with _trajectory_use(self._path) as state:
            records = state.recorded()
        for record in records:
            entry = record.entry
            if not isinstance(entry, DurableEventRecorded):
                continue
            event = entry.event
            if event.transaction.kind != transaction_kind:
                continue
            if isinstance(event, TransactionStarted):
                open_ids.add(event.transaction.id)
            elif isinstance(event, TransactionEnded):
                open_ids.discard(event.transaction.id)
        return frozenset(open_ids)

    def count(self) -> int:
        with _trajectory_use(self._path) as state:
            return len(state.surface_state().messages)

    def page(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
    ) -> HistoryPage[ConversationMessage]:
        with _trajectory_use(self._path) as state:
            surface = state.surface_state()
            return page_messages(
                surface.messages,
                revision=self._revision(surface.revision, "surface"),
                limit=limit,
                cursor=cursor,
                out_of_range="History cursor is outside the current history",
            )

    def page_transcript(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
    ) -> HistoryPage[ConversationMessage]:
        with _trajectory_use(self._path) as state:
            transcript = state.transcript_state()
            return page_messages(
                transcript.messages,
                revision=self._revision(transcript.revision, "transcript"),
                limit=limit,
                cursor=cursor,
                out_of_range="History cursor is outside the current history",
            )

    def page_trajectory(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
        before: int | None = None,
    ) -> TrajectoryRead:
        """Return one page ending before ``before`` (exclusive 1-based position).

        ``before`` anchors a window that already dropped its oldest entries:
        the opaque cursor chain only walks backwards from the newest page, so a
        client that evicted its front could not otherwise re-fetch from where it
        now starts. ``before`` is validated against the current record count, and
        the page reports ``newest_position`` so the client knows the tail.
        """
        if cursor is not None and before is not None:
            raise ValueError("pass either cursor or before, not both")
        with _trajectory_use(self._path) as state:
            records = state.recorded()
            revision = HistoryRevision(f"{self._cursor_scope}:trajectory")
            if cursor is None:
                end = len(records) if before is None else before - 1
            else:
                end = decode_history_cursor(cursor, revision)
            if end < 0 or end > len(records):
                raise HistoryCursorInvalid(
                    "Trajectory anchor is outside the current history"
                )
            start = max(0, end - limit)
            items = tuple(
                _trajectory_item(record) for record in records[start:end]
            )
            newest = len(records)
        return TrajectoryRead(
            page=HistoryPage(
                items=items,
                older_cursor=encode_history_cursor(revision, start) if start else None,
            ),
            newest_position=newest,
        )

    def _revision(self, generation: int, projection: str) -> HistoryRevision:
        return HistoryRevision(f"{self._cursor_scope}:{projection}:{generation}")

    def has_history(self) -> bool:
        return self._path.exists() and self._path.stat().st_size > 0

    def _append_records(
        self,
        records: Sequence[StoredTrajectoryRecord],
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
    MessageAppended | SurfaceReplaced | DurableEventRecorded
):
    return record.entry


def _trajectory_record(value: Mapping[str, JsonValue]) -> TrajectoryRecord:
    return StoredTrajectoryRecord.model_validate(value)


def _replace_messages(
    messages: list[ConversationMessage],
    source_ids: Sequence[str],
    replacements: Sequence[ConversationMessage],
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
            for index, message in enumerate(messages)
            if message.id == source_ids[0]
        )
    except StopIteration as exc:
        raise ValueError(f"{scope} replacement at {position} {source_term} are not current") from exc
    current = [message.id for message in messages[start:start + len(source_ids)]]
    if current != list(source_ids):
        raise ValueError(f"{scope} replacement at {position} {source_term} are not current")
    messages[start:start + len(source_ids)] = replacements


class ThreadMetadataStore(MetadataPort):
    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._path = paths.metadata_file
        self._session_id = paths.session_id
        self._thread_id = paths.thread_id
        self._log = runtime_log

    def load(self) -> ThreadMetadata | None:
        raw = _read_json(self._path, "thread metadata")
        if raw is None:
            return None
        metadata = ThreadMetadata.model_validate(raw)
        return metadata.with_default_title(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )

    def save(self, metadata: ThreadMetadata) -> None:
        write_text_atomic(
            self._path,
            json.dumps(metadata.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
        self._log.debug("persistence.metadata.saved")


class DeferredThreadMetadataStore(MetadataPort):
    """Metadata that stays off disk until the thread has a durable record.

    ``save`` buffers while the messages file is empty and writes through once
    it exists; ``flush`` is the explicit materialization call used by the
    persistence component after the first committed turn.
    """

    def __init__(
        self,
        real: ThreadMetadataStore,
        messages_file: Path,
    ) -> None:
        self._real = real
        self._messages_file = messages_file
        self._flushed = False
        self._pending: ThreadMetadata | None = None

    def _has_records(self) -> bool:
        return self._messages_file.exists() and self._messages_file.stat().st_size > 0

    def load(self) -> ThreadMetadata | None:
        return self._real.load()

    def save(self, metadata: ThreadMetadata) -> None:
        if self._flushed or self._has_records():
            # The thread is durable: write through and drop any earlier
            # buffered snapshot — the file is now the authoritative state, so
            # a later flush must not resurrect the pre-write value.
            self._real.save(metadata)
            self._pending = None
        else:
            self._pending = metadata

    def flush(self) -> None:
        self._flushed = True
        if self._pending is not None:
            self._real.save(self._pending)
            self._pending = None


class InboxStore(InboxPersistencePort):
    """Atomic projection of inputs not yet committed to conversation history."""

    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._path = paths.inbox_file
        self._log = runtime_log

    def load(self) -> list[InboxItem]:
        raw = _read_json(self._path, "inbox snapshot")
        if raw is None:
            return []
        return list(InboxSnapshot.model_validate(raw).items)

    def replace(self, items: Sequence[InboxItem]) -> None:
        snapshot = InboxSnapshot(items=tuple(items))
        write_text_atomic(
            self._path,
            json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
        self._log.debug("persistence.inbox.replaced", items=len(items))

    def reconcile(self, committed_input_ids: set[str]) -> list[InboxItem]:
        stored = self.load()
        pending = [
            item for item in stored if item.id not in committed_input_ids
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


class ThreadLifecycleStore(ThreadLifecyclePort):
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


class ThreadPersistence(ThreadPersistencePort):
    """Typed persistence composition for one session thread."""

    def __init__(
        self,
        paths: ThreadPaths,
        *,
        state: StateService,
        artifacts: ArtifactStorePort | None = None,
        metadata: ThreadMetadataStore | DeferredThreadMetadataStore | None = None,
    ) -> None:
        self.paths = paths
        self.session_id = paths.session_id
        self.thread_id = paths.thread_id
        runtime_log = DEFAULT_RUNTIME_LOG.bind(
            "persistence",
            session_id=self.session_id,
            thread_id=self.thread_id,
        )
        self.metadata = metadata or ThreadMetadataStore(paths, runtime_log)
        self.history = MessageHistoryStore(paths, runtime_log)
        self.artifacts: ArtifactStorePort = (
            artifacts
            if artifacts is not None
            else ArtifactStore(paths, runtime_log)
        )
        self.inbox = InboxStore(paths, runtime_log)
        self.lifecycle = ThreadLifecycleStore(paths, runtime_log)
        self.state = state

    def materialize(self) -> None:
        """Persist buffered state now that the thread has a durable record.

        A new session defers metadata until it actually has a message; the
        persistence component calls this once the first turn commits. It is a
        no-op for every later turn and for resumed sessions.
        """
        if isinstance(self.metadata, DeferredThreadMetadataStore):
            self.metadata.flush()

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
        artifacts: ArtifactStorePort | None = None,
        defer_metadata: bool = False,
    ) -> "ThreadPersistence":
        thread_paths = _thread_paths(paths, thread_id)
        # A manager-opened new session is not durable until its first record.
        # Direct/resumed runtimes keep their eager persistence layout.
        if not defer_metadata:
            thread_paths.state_dir.mkdir(parents=True, exist_ok=True)
        metadata = (
            DeferredThreadMetadataStore(
                ThreadMetadataStore(thread_paths),
                messages_file=thread_paths.messages_file,
            )
            if defer_metadata
            else None
        )
        return cls(
            thread_paths,
            state=StateService(path=thread_paths.plugin_state_file),
            artifacts=artifacts,
            metadata=metadata,
        )

    @classmethod
    def open(
        cls,
        paths: SessionPaths | ThreadPaths,
        *,
        thread_id: str,
    ) -> "ThreadPersistence":
        """Open an inactive thread with one private StateService instance."""
        thread_paths = _thread_paths(paths, thread_id)
        return cls(
            thread_paths,
            state=StateService(path=thread_paths.plugin_state_file),
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
