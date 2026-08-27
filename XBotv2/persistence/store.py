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
from XBotv2.core.messages import Message
from XBotv2.core.paths import SessionPaths, ThreadPaths
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
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

    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._path = paths.messages_file
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
        self._log.info(
            "persistence.history.replaced",
            messages=len(records),
            bytes=len(content.encode("utf-8")),
        )

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
        return [
            MessageRecord.from_dict(raw)
            for raw in _read_jsonl(self._path, "messages.jsonl")
        ]


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
            artifacts if artifacts is not None else ArtifactStore(paths)
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
