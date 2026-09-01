"""Filesystem adapters composed as one thread persistence service."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.filesystem.atomic import write_bytes_atomic, write_text_atomic
from XBotv2.core.history import (
    ConversationPage,
    HistoryCheckpoint,
    HistoryCursorInvalid,
    decode_history_cursor,
    encode_history_cursor,
)
from XBotv2.core.messages import Message
from XBotv2.core.paths import SessionPaths, ThreadPaths
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.agentloop.inbox import InboxInput
from XBotv2.persistence.models import (
    HistoryCheckpointRecord,
    HistoryRestoreRecord,
    InboxSnapshot,
    MessageRecord,
    ThreadLifecycleRecord,
    ThreadMetadata,
    utc_now,
)
from xcore.state import StateService


class MessageHistoryStore:
    """Durable current-history store implementing the HistorySink contract."""

    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._paths = paths
        self._path = paths.messages_file
        self._revision_path = paths.history_revision_file
        self._cursor_scope = f"{paths.session_id}/{paths.thread_id}"
        self._log = runtime_log
        self._next_position = 1

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[Message]:
        started = time.perf_counter()
        records = self._records()
        positions = [record.position for record in records]
        if positions != list(range(1, len(positions) + 1)):
            raise ValueError("MessageRecord positions must be contiguous and start at 1")
        self._next_position = len(records) + 1
        messages = [record.to_message() for record in records]
        for message in messages:
            message.seal()
        self._log.debug(
            "persistence.history.loaded",
            messages=len(messages),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return messages

    def append(self, messages: Sequence[Message]) -> None:
        if not messages:
            return
        self._ensure_revision()
        self._ensure_loaded_id()
        records = [
            MessageRecord.from_message(message, self._next_position + index)
            for index, message in enumerate(messages)
        ]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists() and self._path.stat().st_size:
            with self._path.open("rb") as stream:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    raise ValueError("messages.jsonl ends with an incomplete record")
        with self._path.open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._next_position += len(records)
        self._log.debug(
            "persistence.history.appended",
            messages=len(records),
            next_position=self._next_position,
        )

    def replace(self, messages: Sequence[Message]) -> None:
        content = self._serialize(messages)
        write_text_atomic(self._revision_path, uuid4().hex + "\n")
        write_text_atomic(self._path, content)
        self._next_position = len(messages) + 1
        self._log.info(
            "persistence.history.replaced",
            messages=len(messages),
            bytes=len(content.encode("utf-8")),
        )

    def replace_recoverable(
        self,
        messages: Sequence[Message],
        *,
        operation: str,
        reason: str,
    ) -> HistoryCheckpoint:
        """Preserve the exact current JSONL before atomically replacing it."""
        self._ensure_loaded_id()
        before = self._path.read_bytes() if self._path.exists() else b""
        after_text = self._serialize(messages)
        after = after_text.encode("utf-8")
        checkpoint_id = f"{int(time.time() * 1_000_000)}-{uuid4().hex[:12]}"
        archive = self._paths.history_checkpoint_messages(checkpoint_id)
        metadata_path = self._paths.history_checkpoint_metadata(checkpoint_id)
        archive.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            os.link(self._path, archive)
        else:
            write_bytes_atomic(archive, b"")
        record = HistoryCheckpointRecord(
            checkpoint_id=checkpoint_id,
            operation=operation,
            reason=reason,
            created_at=utc_now(),
            messages_before=self._record_count(before),
            messages_after=len(messages),
            before_sha256=hashlib.sha256(before).hexdigest(),
            after_sha256=hashlib.sha256(after).hexdigest(),
        )
        write_text_atomic(
            metadata_path,
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        self.replace(messages)
        self._log.info(
            "persistence.history.checkpointed",
            checkpoint_id=checkpoint_id,
            operation=operation,
            messages_before=record.messages_before,
            messages_after=record.messages_after,
        )
        return record.to_checkpoint()

    def checkpoints(self, *, operation: str) -> tuple[HistoryCheckpoint, ...]:
        current = self._path.read_bytes() if self._path.exists() else b""
        records = sorted(
            self._checkpoint_records(operation),
            key=lambda record: record.created_at,
        )
        return tuple(
            record.to_checkpoint(status=self._checkpoint_status(record, current))
            for record in records
        )

    def restore(
        self,
        checkpoint_id: str,
        *,
        operation: str,
    ) -> tuple[list[Message], HistoryCheckpoint]:
        record = self._checkpoint_record(checkpoint_id)
        if record.operation != operation:
            raise ValueError(
                f"Checkpoint {checkpoint_id!r} belongs to {record.operation!r}"
            )
        current = self._path.read_bytes() if self._path.exists() else b""
        status = self._checkpoint_status(record, current)
        if status != "active":
            raise ValueError(
                f"Checkpoint {checkpoint_id!r} is {status}; restore the latest "
                "active checkpoint first"
            )
        current_records = self._records_from_bytes(current, "messages.jsonl")
        archived = self._paths.history_checkpoint_messages(checkpoint_id).read_bytes()
        if hashlib.sha256(archived).hexdigest() != record.before_sha256:
            raise ValueError(
                f"History checkpoint {checkpoint_id!r} content hash does not match"
            )
        before_records = self._records_from_bytes(
            archived,
            f"history checkpoint {checkpoint_id}",
        )
        tail = current_records[record.messages_after:]
        restored = [
            item.to_message()
            for item in (*before_records, *tail)
        ]
        self.replace(restored)
        restore = HistoryRestoreRecord(
            checkpoint_id=checkpoint_id,
            restored_at=utc_now(),
            messages_restored=len(restored),
        )
        write_text_atomic(
            self._paths.history_checkpoint_restore(checkpoint_id),
            json.dumps(restore.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        self._log.info(
            "persistence.history.restored",
            checkpoint_id=checkpoint_id,
            operation=operation,
            messages=len(restored),
        )
        return restored, record.to_checkpoint(status="restored")

    def count(self) -> int:
        return len(self._records())

    def page(self, *, limit: int, cursor: str | None = None) -> ConversationPage:
        if limit < 1:
            raise ValueError("History page limit must be positive")
        size = self._path.stat().st_size if self._path.exists() else 0
        if not size:
            if cursor is not None:
                raise HistoryCursorInvalid(
                    "History cursor is outside the current history"
                )
            return ConversationPage(())
        revision = f"{self._cursor_scope}:{self._ensure_revision()}"
        end = size if cursor is None else decode_history_cursor(cursor, revision)
        if end < 0 or end > size:
            raise HistoryCursorInvalid("History cursor is outside the current history")
        if end == 0:
            return ConversationPage(())
        lines, start = self._read_page_lines(end, limit)
        records = [MessageRecord.from_dict(_decode_json_line(line)) for line in lines]
        positions = [record.position for record in records]
        if positions != list(range(positions[0], positions[0] + len(positions))):
            raise ValueError("MessageRecord page positions must be contiguous")
        if start == 0 and positions[0] != 1:
            raise ValueError("MessageRecord positions must start at 1")
        messages = tuple(record.to_message() for record in records)
        for message in messages:
            message.seal()
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
            self.load()

    def _records(self) -> list[MessageRecord]:
        return [
            MessageRecord.from_dict(raw)
            for raw in _read_jsonl(self._path, "messages.jsonl")
        ]

    def _checkpoint_records(self, operation: str) -> list[HistoryCheckpointRecord]:
        directory = self._paths.history_checkpoints_dir
        if not directory.exists():
            return []
        records: list[HistoryCheckpointRecord] = []
        for path in directory.glob("*.json"):
            if path.name.endswith(".restored.json"):
                continue
            record = HistoryCheckpointRecord.from_dict(
                _read_json(path, "history checkpoint") or {}
            )
            if record.operation == operation:
                records.append(record)
        return records

    def _checkpoint_record(self, checkpoint_id: str) -> HistoryCheckpointRecord:
        raw = _read_json(
            self._paths.history_checkpoint_metadata(checkpoint_id),
            "history checkpoint",
        )
        if raw is None:
            raise ValueError(f"Unknown history checkpoint: {checkpoint_id!r}")
        return HistoryCheckpointRecord.from_dict(raw)

    def _checkpoint_status(
        self,
        record: HistoryCheckpointRecord,
        current: bytes,
    ) -> str:
        restored = _read_json(
            self._paths.history_checkpoint_restore(record.checkpoint_id),
            "history checkpoint restore",
        )
        if restored is not None:
            restore_record = HistoryRestoreRecord.from_dict(restored)
            if restore_record.checkpoint_id != record.checkpoint_id:
                raise ValueError("History restore record checkpoint does not match")
            return "restored"
        lines = current.splitlines(keepends=True)
        after = b"".join(lines[:record.messages_after])
        if hashlib.sha256(after).hexdigest() == record.after_sha256:
            return "active"
        before = b"".join(lines[:record.messages_before])
        if hashlib.sha256(before).hexdigest() == record.before_sha256:
            return "prepared"
        return "superseded"

    @staticmethod
    def _serialize(messages: Sequence[Message]) -> str:
        return "".join(
            json.dumps(
                MessageRecord.from_message(message, index).to_dict(),
                ensure_ascii=False,
            ) + "\n"
            for index, message in enumerate(messages, start=1)
        )

    @staticmethod
    def _record_count(content: bytes) -> int:
        if content and not content.endswith(b"\n"):
            raise ValueError("messages.jsonl ends with an incomplete record")
        return len(content.splitlines())

    @staticmethod
    def _records_from_bytes(content: bytes, name: str) -> list[MessageRecord]:
        if content and not content.endswith(b"\n"):
            raise ValueError(f"{name} ends with an incomplete record")
        records = [
            MessageRecord.from_dict(_decode_json_line(line))
            for line in content.splitlines()
        ]
        positions = [record.position for record in records]
        if positions != list(range(1, len(records) + 1)):
            raise ValueError(f"{name} positions must be contiguous and start at 1")
        return records

    def _ensure_revision(self) -> str:
        if not self._revision_path.exists():
            write_text_atomic(self._revision_path, uuid4().hex + "\n")
        revision = self._revision_path.read_text(encoding="utf-8").strip()
        if len(revision) != 32 or any(
            value not in "0123456789abcdef" for value in revision
        ):
            raise ValueError("Invalid message history revision")
        return revision

    def _read_page_lines(self, end: int, limit: int) -> tuple[list[bytes], int]:
        with self._path.open("rb") as stream:
            stream.seek(end - 1)
            if stream.read(1) != b"\n":
                raise ValueError("History cursor does not end at a record boundary")
            position = end
            chunks: list[bytes] = []
            newline_count = 0
            while position and newline_count <= limit:
                chunk_size = min(65536, position)
                position -= chunk_size
                stream.seek(position)
                chunk = stream.read(chunk_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
        buffer = b"".join(reversed(chunks))
        lines = buffer.splitlines()
        selected = lines[-limit:]
        start = end - sum(len(line) + 1 for line in selected)
        return selected, start


def _decode_json_line(line: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid messages.jsonl record") from exc
    if not isinstance(value, Mapping):
        raise TypeError("messages.jsonl record must be an object")
    return value


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
