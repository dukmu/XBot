"""Typed ports exposed by one thread persistence composition."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator

from XBotv2.agentloop.contracts import InboxItem
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.history import (
    DurableEvent,
    HistoryPage,
    TrajectoryRead,
)
from XBotv2.core.domain import Cursor
from XBotv2.core.messages import ConversationMessage
from XBotv2.core.paths import SessionPaths
from XBotv2.core.metadata import ThreadMetadata


class ThreadLifecycleRecord(BaseModel):
    """Durable lifecycle entry shared with child-application orchestration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event: Literal["started", "completed", "failed", "cancelled"]
    thread_id: str
    parent_thread_id: str
    agent: str
    timestamp: str
    error: str = ""

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("timestamp must be a string")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("timestamp must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value

    @classmethod
    def create(
        cls,
        event: Literal["started", "completed", "failed", "cancelled"],
        *,
        thread_id: str,
        parent_thread_id: str,
        agent: str,
        error: str = "",
    ) -> "ThreadLifecycleRecord":
        return cls(
            event=event,
            thread_id=thread_id,
            parent_thread_id=parent_thread_id,
            agent=agent,
            timestamp=datetime.now(timezone.utc).isoformat(),
            error=error,
        )


class HistoryPort(Protocol):
    def load(self) -> list[ConversationMessage]: ...

    def load_surface(self) -> tuple[ConversationMessage, ...]: ...

    def load_transcript(self) -> list[ConversationMessage]: ...

    def append(self, messages: Sequence[ConversationMessage]) -> tuple[ConversationMessage, ...]: ...

    def replace(self, messages: Sequence[ConversationMessage]) -> None: ...

    def replace_surface(
        self,
        source_ids: Sequence[str],
        messages: Sequence[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool,
    ) -> tuple[ConversationMessage, ...]: ...

    def record(self, event: DurableEvent, *, durable: bool = False) -> None: ...

    def open_transactions(
        self,
        transaction_kind: str,
    ) -> frozenset[str]: ...

    def count(self) -> int: ...

    def page(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
    ) -> HistoryPage[ConversationMessage]: ...

    def page_transcript(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
    ) -> HistoryPage[ConversationMessage]: ...

    def page_trajectory(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
        before: int | None = None,
    ) -> TrajectoryRead: ...


class MetadataPort(Protocol):
    def load(self) -> ThreadMetadata | None: ...

    def save(self, metadata: ThreadMetadata) -> None: ...





class InboxPersistencePort(Protocol):
    """Durable inbox operations owned by the persistence plugin."""

    def load(self) -> list[InboxItem]: ...

    def replace(self, items: Sequence[InboxItem]) -> None: ...

    def reconcile(self, committed_input_ids: set[str]) -> list[InboxItem]: ...


class ThreadLifecyclePort(Protocol):
    def append(self, record: ThreadLifecycleRecord) -> None: ...

    def load(self) -> list[ThreadLifecycleRecord]: ...


class ThreadLifecycleWriterPort(Protocol):
    def append(self, record: ThreadLifecycleRecord) -> None: ...


class StatePort(Protocol):
    async def get(
        self,
        key: str,
        default: JsonValue = None,
    ) -> JsonValue: ...

    async def set(self, key: str, value: JsonValue) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def clear(self) -> None: ...

    def namespace(self, prefix: str) -> "StatePort": ...


class ThreadPersistencePort(Protocol):
    session_id: str
    thread_id: str
    history: HistoryPort
    state: StatePort
    artifacts: ArtifactStorePort
    metadata: MetadataPort
    inbox: InboxPersistencePort
    lifecycle: ThreadLifecyclePort

    def has_persisted_state(self) -> bool: ...


class ThreadPersistenceFactory(Protocol):
    def __call__(
        self,
        session_paths: SessionPaths,
        *,
        thread_id: str,
    ) -> ThreadPersistencePort: ...


__all__ = [
    "HistoryPort",
    "InboxPersistencePort",
    "MetadataPort",
    "StatePort",
    "ThreadLifecyclePort",
    "ThreadLifecycleRecord",
    "ThreadLifecycleWriterPort",
    "ThreadPersistenceFactory",
    "ThreadPersistencePort",
]
