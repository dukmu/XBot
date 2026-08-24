"""Filesystem adapters composed as one thread persistence service."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.filesystem.atomic import write_text_atomic
from XBotv2.core.messages import Message
from XBotv2.core.paths import SessionPaths, ThreadPaths
from XBotv2.agentloop.inbox import InboxInput
from XBotv2.persistence.models import (
    InboxSnapshot,
    MessageRecord,
    ThreadLifecycleRecord,
    ThreadMetadata,
)
from xcore.state import StateService


class MessageHistoryStore:
    """Durable current-history store implementing the HistorySink contract."""

    def __init__(self, paths: ThreadPaths) -> None:
        self._path = paths.messages_file
        self._next_position = 1

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[Message]:
        records = self._records()
        positions = [record.position for record in records]
        if positions != list(range(1, len(positions) + 1)):
            raise ValueError("MessageRecord positions must be contiguous and start at 1")
        self._next_position = len(records) + 1
        messages = [record.to_message() for record in records]
        for message in messages:
            message.seal()
        return messages

    def append(self, messages: Sequence[Message]) -> None:
        if not messages:
            return
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

    def replace(self, messages: Sequence[Message]) -> None:
        records = [
            MessageRecord.from_message(message, index)
            for index, message in enumerate(messages, start=1)
        ]
        content = "".join(
            json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
            for record in records
        )
        write_text_atomic(self._path, content)
        self._next_position = len(records) + 1

    def count(self) -> int:
        return len(self._records())

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
        if not self._path.exists():
            return []
        records: list[MessageRecord] = []
        with self._path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    raise ValueError(
                        f"messages.jsonl contains a blank record at line {line_number}"
                    )
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid messages.jsonl record at line {line_number}"
                    ) from exc
                if not isinstance(raw, Mapping):
                    raise TypeError(
                        f"messages.jsonl line {line_number} must be an object"
                    )
                records.append(MessageRecord.from_dict(raw))
        return records


class ThreadMetadataStore:
    def __init__(self, paths: ThreadPaths) -> None:
        self._path = paths.metadata_file

    def load(self) -> ThreadMetadata:
        if not self._path.exists():
            return ThreadMetadata()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid thread metadata JSON") from exc
        if not isinstance(raw, Mapping):
            raise TypeError("Thread metadata must be an object")
        return ThreadMetadata.from_dict(raw)

    def save(self, metadata: ThreadMetadata) -> None:
        write_text_atomic(
            self._path,
            json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )


class InboxStore:
    """Atomic projection of inputs not yet committed to conversation history."""

    def __init__(self, paths: ThreadPaths) -> None:
        self._path = paths.inbox_file

    def load(self) -> list[InboxInput]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid inbox snapshot JSON") from exc
        if not isinstance(raw, Mapping):
            raise TypeError("Inbox snapshot must be an object")
        return InboxSnapshot.from_dict(raw).to_inputs()

    def replace(self, items: Sequence[InboxInput]) -> None:
        snapshot = InboxSnapshot.from_inputs(items)
        write_text_atomic(
            self._path,
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )

    def reconcile(self, committed_input_ids: set[str]) -> list[InboxInput]:
        stored = self.load()
        pending = [
            item for item in stored if item.message_id not in committed_input_ids
        ]
        if len(pending) != len(stored):
            self.replace(pending)
        return pending


class ThreadLifecycleStore:
    def __init__(self, paths: ThreadPaths) -> None:
        self._path = paths.session.threads_log

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

    def load(self) -> list[ThreadLifecycleRecord]:
        if not self._path.exists():
            return []
        records: list[ThreadLifecycleRecord] = []
        with self._path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid thread lifecycle record at line {line_number}"
                    ) from exc
                if not isinstance(raw, Mapping):
                    raise TypeError(
                        f"Thread lifecycle line {line_number} must be an object"
                    )
                records.append(ThreadLifecycleRecord.from_dict(raw))
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
        self.history = MessageHistoryStore(paths)
        self.artifacts: ArtifactStorePort = (
            artifacts if artifacts is not None else ArtifactStore(paths)
        )
        self.metadata = ThreadMetadataStore(paths)
        self.inbox = InboxStore(paths)
        self.lifecycle = ThreadLifecycleStore(paths)
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
