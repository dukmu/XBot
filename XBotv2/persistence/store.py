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
from weakref import WeakValueDictionary

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
    resolve_transcript_sources,
)
from XBotv2.core.domain import Cursor, HistoryRevision, TransactionEnded, TransactionStarted
from XBotv2.core.messages import ConversationMessage, HumanInputMessage, RuntimeNoticeMessage
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
from XBotv2.agentloop.contracts import (
    InboxItem, InboxMutation, InboxTarget, HumanInput,
    Inserted, Edited, Removed, Retargeted, Consumed, Discarded,
)
from XBotv2.persistence.models import (
    StoredInboxRecord,
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
            self._validate_claim((message,), entry.position)
            self.seen_ids.add(message.id)
            self.messages.append(message)
        elif isinstance(entry, SurfaceReplaced):
            self.validate_replacement(entry)
            replacements = list(entry.replacements)
            _replace_messages(
                self.messages,
                entry.source_ids,
                replacements,
                position=entry.position,
                scope="Surface",
                source_term="source nodes",
            )
            self.seen_ids.update(message.id for message in replacements)
            self.revision = max(self.revision, entry.position)

    def validate_replacement(self, entry: SurfaceReplaced) -> None:
        self._validate_claim(entry.replacements, entry.position)
        _replacement_start(
            self.messages, entry.source_ids, position=entry.position,
            scope="Surface", source_term="source nodes",
        )

    def _validate_claim(self, messages: Sequence[ConversationMessage], position: int) -> None:
        identities = [message.id for message in messages]
        if len(identities) != len(set(identities)) or self.seen_ids.intersection(identities):
            raise ValueError(f"Surface record at {position} reuses a message identity")

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
        elif isinstance(entry, SurfaceReplaced):
            self.validate_replacement(entry)
            replacements = list(entry.replacements)
            if entry.transcript_policy == "preserve":
                # Keep edges to prior summaries; repeatedly flattening their
                # entire ancestry would copy old transcript IDs at every compact.
                self.lineage[replacements[0].id] = entry.source_ids
                return
            _replace_messages(
                self.messages,
                resolve_transcript_sources(entry.source_ids, self.lineage),
                replacements,
                position=entry.position,
                scope="Transcript",
                source_term="sources",
            )
            self.revision = max(self.revision, entry.position)

    def validate_replacement(self, entry: SurfaceReplaced) -> None:
        if entry.transcript_policy == "preserve":
            if len(entry.replacements) != 1:
                raise ValueError(
                    "Transcript-preserving replacement must produce one surface record"
                )
            return
        _replacement_start(
            self.messages, resolve_transcript_sources(entry.source_ids, self.lineage), position=entry.position,
            scope="Transcript", source_term="sources",
        )

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
        self.turn_count = 0
        self.records: list[TrajectoryRecord] | None = None
        self.surface: _SurfaceState | None = None
        self.transcript: _TranscriptState | None = None
        self.next_position = 1
        self.file_size = -1
        self.read_size = -1
        self.observed_size = -1
        self.last_sync = 0.0

    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def recorded(self) -> list[TrajectoryRecord]:
        """Return parsed records, folding only a newly appended file suffix."""
        size = self.size()
        if self.records is None:
            self.records = self._parse(size)
        elif size != self.observed_size:
            if size < self.read_size:
                raise ValueError("Trajectory file truncated after it was loaded")
            self._extend_from_disk(size)
        return self.records

    def surface_state(self) -> _SurfaceState:
        records = self.recorded()
        if self.surface is None:
            surface = _SurfaceState()
            _apply_records(surface, records)
            self.surface = surface
        return self.surface

    def transcript_state(self) -> _TranscriptState:
        records = self.recorded()
        if self.transcript is None:
            # Validate identity and source transitions before interpreting
            # summary ancestry; a malformed trace must not form lineage cycles.
            self.surface_state()
            transcript = _TranscriptState()
            _apply_records(transcript, records)
            self.transcript = transcript
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
        if self.records is not None and size < self.read_size:
            raise ValueError("Trajectory file truncated after it was loaded")
        size = _drop_incomplete_tail(self.path, size, log)
        if self.records is None:
            self.records = self._parse(size)
        elif size != self.read_size:
            self.observed_size = -1
            self._extend_from_disk(size)
        else:
            self.observed_size = size
        self.file_size = size
        self.next_position = len(self.records) + 1
        return self.records

    def wrote(self, added: Sequence[TrajectoryRecord]) -> None:
        """Extend the cache after this process appended records durably."""
        if self.records is None:
            raise RuntimeError("Trajectory append was not prepared")
        self.records.extend(added)
        self.turn_count += sum(
            isinstance(record.entry, MessageAppended)
            and isinstance(record.entry.message, HumanInputMessage)
            for record in added
        )
        self.next_position = len(self.records) + 1
        self.file_size = self.read_size = self.observed_size = self.size()
        if self.surface is not None:
            _apply_records(self.surface, added)
        if self.transcript is not None:
            _apply_records(self.transcript, added)

    def _parse(self, size: int) -> list[TrajectoryRecord]:
        parsed: list[TrajectoryRecord] = []
        turn_count = 0
        raw_records, durable_size = _read_jsonl_from(
            self.path, "messages.jsonl", offset=0, first_line=1,
        )
        for index, raw in enumerate(raw_records, start=1):
            record = _trajectory_record(raw)
            if record.entry.position != index:
                raise ValueError(
                    "Trajectory positions must be contiguous and start at 1"
                )
            parsed.append(record)
            if isinstance(record.entry, MessageAppended) and isinstance(
                record.entry.message, HumanInputMessage
            ):
                turn_count += 1
        self.turn_count = turn_count
        self.read_size = durable_size
        self.observed_size = size
        return parsed

    def _extend_from_disk(self, size: int) -> None:
        """Validate a foreign append completely before publishing any of it."""
        assert self.records is not None
        raw_records, durable_size = _read_jsonl_from(
            self.path,
            "messages.jsonl",
            offset=self.read_size,
            first_line=len(self.records) + 1,
        )
        added: list[TrajectoryRecord] = []
        for index, raw in enumerate(raw_records, start=len(self.records) + 1):
            record = _trajectory_record(raw)
            if record.entry.position != index:
                raise ValueError(
                    "Trajectory positions must be contiguous and start at 1"
                )
            added.append(record)

        if self.surface is None:
            surface = _SurfaceState()
            _apply_records(surface, self.records)
            self.surface = surface
        if self.transcript is None:
            transcript = _TranscriptState()
            _apply_records(transcript, self.records)
            self.transcript = transcript
        try:
            _apply_records(self.surface, added)
            _apply_records(self.transcript, added)
        except (TypeError, ValueError):
            # A failed suffix may have advanced one projection. Discard both;
            # the next read rebuilds the valid prefix and retries this suffix.
            self.surface = None
            self.transcript = None
            raise

        self.records.extend(added)
        self.turn_count += sum(
            isinstance(record.entry, MessageAppended)
            and isinstance(record.entry.message, HumanInputMessage)
            for record in added
        )
        self.read_size = durable_size
        self.observed_size = size

# Live stores own their state. The weak registry preserves one lock/cache per
# path even when a live store is evicted from the bounded recent-reader cache.
_CACHE_LIMIT = 8
_CACHE_RECORD_BUDGET = 60_000

_trajectory_states: WeakValueDictionary[Path, _TrajectoryState] = WeakValueDictionary()
_recent_trajectories: "OrderedDict[Path, _TrajectoryState]" = OrderedDict()
_trajectory_guard = threading.Lock()


def _trajectory_for(path: Path) -> _TrajectoryState:
    with _trajectory_guard:
        state = _trajectory_states.get(path)
        if state is None:
            state = _TrajectoryState(path)
            _trajectory_states[path] = state
        return state


@contextmanager
def _trajectory_use(state: _TrajectoryState) -> Iterator[_TrajectoryState]:
    """Keep recent readers warm without dropping a live store's ownership."""
    with _trajectory_guard:
        _recent_trajectories[state.path] = state
        _recent_trajectories.move_to_end(state.path)
        _evict_recent_states()
    with state.lock:
        yield state


def _evict_recent_states() -> None:
    """Bound retained offline readers; live owners retain their own state."""
    while True:
        cached = sum(
            len(state.records or ()) for state in _recent_trajectories.values()
        )
        within_limit = (
            len(_recent_trajectories) <= _CACHE_LIMIT
            and cached <= _CACHE_RECORD_BUDGET
        )
        if within_limit or len(_recent_trajectories) <= 1:
            return
        _recent_trajectories.popitem(last=False)


class MessageHistoryStore(HistoryPort):
    """Append-only trajectory store with one deterministic message surface."""

    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._path = paths.messages_file
        self._state = _trajectory_for(self._path)
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
        with _trajectory_use(self._state) as state:
            return list(state.transcript_state().view())

    def load_surface(self) -> tuple[ConversationMessage, ...]:
        with _trajectory_use(self._state) as state:
            return state.surface_state().view()

    def count_turns(self) -> int:
        """Count accepted human turns in the append-only canonical trajectory.

        Surface replacement may fold or clear messages, while the runtime turn
        counter is lifetime state. Counting MessageAppended records restores
        that counter without adding a second persisted field.
        """
        with _trajectory_use(self._state) as state:
            state.recorded()
            return state.turn_count

    def committed_input_ids(self) -> set[str]:
        """Read commit evidence even when compact/clear removed its surface."""
        committed: set[str] = set()
        with _trajectory_use(self._state) as state:
            for record in state.recorded():
                entry = record.entry
                if not isinstance(entry, MessageAppended):
                    continue
                message = entry.message
                if isinstance(message, (HumanInputMessage, RuntimeNoticeMessage)):
                    committed.add(message.id)
        return committed

    def append(self, messages: Sequence[ConversationMessage]) -> None:
        if not messages:
            return
        with _trajectory_use(self._state) as state:
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
    ) -> None:
        with _trajectory_use(self._state) as state:
            state.prepare_write(self._log)
            entry = SurfaceReplaced(
                position=state.next_position,
                operation=operation,
                transcript_policy=(
                    "preserve" if preserve_transcript else "replace"
                ),
                source_ids=tuple(source_ids),
                replacements=tuple(messages),
            )
            record = StoredTrajectoryRecord(entry=entry)
            # Both projections must accept the transition before it becomes durable.
            state.surface_state().validate_replacement(entry)
            state.transcript_state().validate_replacement(entry)
            self._append_records((record,), state=state)
            state.wrote((record,))
        self._log.info(
            "persistence.surface.replaced",
            operation=operation,
            source_nodes=len(source_ids),
            replacement_nodes=len(messages),
        )

    def record(self, event: DurableEvent, *, durable: bool = False) -> None:
        with _trajectory_use(self._state) as state:
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
        with _trajectory_use(self._state) as state:
            for record in state.recorded():
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
        with _trajectory_use(self._state) as state:
            return len(state.surface_state().messages)

    def page(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
    ) -> HistoryPage[ConversationMessage]:
        with _trajectory_use(self._state) as state:
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
        with _trajectory_use(self._state) as state:
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
        with _trajectory_use(self._state) as state:
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
        now = time.monotonic()
        flush = sync or now - state.last_sync >= _SYNC_INTERVAL_SECONDS
        _append_bytes(self._path, payload, sync=flush)
        if flush:
            state.last_sync = now




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
    start = _replacement_start(
        messages, source_ids, position=position, scope=scope, source_term=source_term,
    )
    messages[start:start + len(source_ids)] = replacements


def _replacement_start(
    messages: Sequence[ConversationMessage],
    source_ids: Sequence[str],
    *,
    position: int,
    scope: str,
    source_term: str,
) -> int:
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
    return start


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
        self._items: dict[str, InboxItem] = {}
        self._size = -1

    def load(self) -> list[InboxItem]:
        self._refresh()
        return sorted(self._items.values(), key=lambda item: item.target is InboxTarget.NEXT_TURN)

    def _refresh(self) -> None:
        if self._path.with_suffix(".json").exists():
            raise ValueError("Unsupported inbox snapshot; expected append-only inbox.jsonl")
        size = self._path.stat().st_size if self._path.exists() else 0
        if size == self._size:
            return
        items: dict[str, InboxItem] = {}
        for raw in _read_jsonl(self._path, "inbox"):
            change = StoredInboxRecord.model_validate(raw).change
            removed, added = _inbox_transition(items, change)
            for identity in removed:
                del items[identity]
            items.update((item.id, item) for item in added)
        self._items = items
        self._size = size

    def append(self, change: InboxMutation) -> None:
        self._refresh()
        removed, added = _inbox_transition(self._items, change)
        payload = (StoredInboxRecord(change=change).model_dump_json() + "\n").encode("utf-8")
        self._size = _drop_incomplete_tail(self._path, max(0, self._size), self._log)
        _append_bytes(self._path, payload)
        for identity in removed:
            del self._items[identity]
        self._items.update((item.id, item) for item in added)
        self._size += len(payload)

    def reconcile(self, committed_input_ids: set[str]) -> list[InboxItem]:
        stored = self.load()
        pending = [
            item for item in stored if item.id not in committed_input_ids
        ]
        if len(pending) != len(stored):
            self.append(Consumed(ids=tuple(item.id for item in stored if item.id in committed_input_ids)))
        self._log.debug(
            "persistence.inbox.reconciled",
            stored=len(stored),
            committed=len(stored) - len(pending),
            pending=len(pending),
        )
        return pending


def _inbox_transition(
    items: Mapping[str, InboxItem], change: InboxMutation,
) -> tuple[tuple[str, ...], tuple[InboxItem, ...]]:
    """Validate a delta before writing; return only its affected entries."""
    if isinstance(change, Inserted):
        if change.item.id in items:
            raise ValueError(f"Duplicate inbox input id: {change.item.id}")
        return (), (change.item,)
    if isinstance(change, (Consumed, Discarded)):
        if len(set(change.ids)) != len(change.ids) or any(identity not in items for identity in change.ids):
            raise ValueError("Inbox retirement must name distinct pending inputs")
        if isinstance(change, Discarded) and set(change.ids) != set(items):
            raise ValueError("Inbox discard must retire all pending inputs")
        return change.ids, ()
    if not isinstance(change, (Edited, Retargeted, Removed)):
        raise TypeError("Only durable inbox mutations can be stored")
    current = items.get(change.id)
    if current is None:
        raise ValueError(f"Unknown inbox input id: {change.id}")
    if isinstance(change, Removed):
        return (change.id,), ()
    if isinstance(change, Edited):
        if not isinstance(current.input, HumanInput) or not change.content.strip():
            raise ValueError("Inbox edit requires human input and non-empty content")
        return (), (current.model_copy(update={
            "input": current.input.model_copy(update={"content": change.content}),
        }),)
    if change.target is current.target:
        return (), ()
    return (change.id,), (current.model_copy(update={"target": change.target}),)


def _append_bytes(path: Path, payload: bytes, *, sync: bool = True) -> None:
    """Append a batch; a failed write leaves the previous committed prefix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    original_size = os.fstat(descriptor).st_size
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("Append made no progress")
            view = view[written:]
        if sync:
            os.fsync(descriptor)
    except BaseException:
        os.ftruncate(descriptor, original_size)
        os.fsync(descriptor)
        if not existed:
            path.unlink()
        raise
    finally:
        os.close(descriptor)


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
    records, _ = _read_jsonl_from(path, name, offset=0, first_line=1)
    return records


def _read_jsonl_from(
    path: Path,
    name: str,
    *,
    offset: int,
    first_line: int,
) -> tuple[list[Mapping[str, JsonValue]], int]:
    """Read complete JSONL records after a previously validated byte offset."""
    if not path.exists():
        return [], 0
    records: list[Mapping[str, JsonValue]] = []
    durable_size = offset
    with path.open("rb") as stream:
        stream.seek(offset)
        for line_number, raw_line in enumerate(stream, start=first_line):
            if not raw_line.endswith(b"\n"):
                # The owning runtime appends whole lines, so a final fragment
                # without its newline is an append still in flight rather than
                # a durable record.
                break
            try:
                line = raw_line.decode("utf-8")
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"Invalid {name} record at line {line_number}"
                ) from exc
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} line {line_number} must be an object")
            records.append(value)
            durable_size += len(raw_line)
    return records, durable_size


class ThreadPersistence(ThreadPersistencePort):
    """Typed persistence composition for one session thread."""

    def __init__(
        self,
        paths: ThreadPaths,
        *,
        state: StateService,
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
        self.inbox = InboxStore(paths, runtime_log)
        self.lifecycle = ThreadLifecycleStore(paths, runtime_log)
        self.state = state

    def materialize(self) -> None:
        """Flush deferred metadata only when the thread has durable state.

        Turn completion and plugin teardown both call this: plugin state can
        be written before the first turn, but an unused session stays off disk.
        """
        if (
            isinstance(self.metadata, DeferredThreadMetadataStore)
            and self.has_persisted_state()
        ):
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
        state: StateService,
        defer_metadata: bool = False,
    ) -> "ThreadPersistence":
        """Bind runtime persistence to the owning Context's state service."""
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
            state=state,
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
