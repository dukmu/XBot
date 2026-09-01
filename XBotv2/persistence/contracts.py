"""Typed ports exposed by one thread persistence composition."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from XBotv2.agentloop.inbox import InboxInput
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.history import ConversationPage, HistoryCheckpoint
from XBotv2.core.messages import Message
from XBotv2.core.paths import SessionPaths
from XBotv2.persistence.models import ThreadLifecycleRecord, ThreadMetadata


class HistoryPort(Protocol):
    def load(self) -> list[Message]: ...

    def append(self, messages: Sequence[Message]) -> None: ...

    def replace(self, messages: Sequence[Message]) -> None: ...

    def replace_recoverable(
        self,
        messages: Sequence[Message],
        *,
        operation: str,
        reason: str,
    ) -> HistoryCheckpoint: ...

    def checkpoints(self, *, operation: str) -> tuple[HistoryCheckpoint, ...]: ...

    def restore(
        self,
        checkpoint_id: str,
        *,
        operation: str,
    ) -> tuple[list[Message], HistoryCheckpoint]: ...

    def count(self) -> int: ...

    def page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage: ...


class MetadataPort(Protocol):
    def load(self) -> ThreadMetadata: ...

    def save(self, metadata: ThreadMetadata) -> None: ...


class InboxPort(Protocol):
    def load(self) -> list[InboxInput]: ...

    def replace(self, items: Sequence[InboxInput]) -> None: ...

    def reconcile(self, committed_input_ids: set[str]) -> list[InboxInput]: ...


class ThreadLifecyclePort(Protocol):
    def append(self, record: ThreadLifecycleRecord) -> None: ...

    def load(self) -> list[ThreadLifecycleRecord]: ...


class ThreadLifecycleWriterPort(Protocol):
    def append(self, record: ThreadLifecycleRecord) -> None: ...


class StatePort(Protocol):
    async def get(self, key: str, default: object | None = None) -> object: ...

    async def set(self, key: str, value: object) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def clear(self) -> None: ...

    def namespace(self, prefix: str) -> "StatePort": ...


class ThreadPersistencePort(Protocol):
    session_id: str
    thread_id: str
    workspace_root: str
    provider: str
    history: HistoryPort
    state: StatePort
    artifacts: ArtifactStorePort
    metadata: MetadataPort
    inbox: InboxPort
    lifecycle: ThreadLifecyclePort

    def has_persisted_state(self) -> bool: ...


class ThreadPersistenceFactory(Protocol):
    def __call__(
        self,
        session_paths: SessionPaths,
        *,
        thread_id: str,
        workspace_root: str = "",
        provider: str = "",
    ) -> ThreadPersistencePort: ...


__all__ = [
    "HistoryPort",
    "InboxPort",
    "MetadataPort",
    "StatePort",
    "ThreadLifecyclePort",
    "ThreadLifecycleWriterPort",
    "ThreadPersistenceFactory",
    "ThreadPersistencePort",
]
