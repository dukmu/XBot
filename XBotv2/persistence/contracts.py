"""Typed ports exposed by one thread persistence composition."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Annotated, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from XBotv2.agentloop.contracts import InboxItem, InboxSink
from XBotv2.core.history import (
    HistoryPage,
    HistorySink,
    TrajectoryRead,
)
from XBotv2.core.domain import Cursor
from XBotv2.core.messages import ConversationMessage
from XBotv2.core.paths import SessionPaths
from XBotv2.core.metadata import ThreadMetadata


class _ThreadLifecycleRecord(BaseModel):
    """Fields shared by every version-2 child lifecycle fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    thread_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("timestamp")
    @classmethod
    def _timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value


class ThreadStarted(_ThreadLifecycleRecord):
    event: Literal["started"] = "started"
    parent_thread_id: str
    agent: str


class ThreadCompleted(_ThreadLifecycleRecord):
    event: Literal["completed"] = "completed"


class ThreadFailed(_ThreadLifecycleRecord):
    event: Literal["failed"] = "failed"
    error: str = Field(min_length=1)


class ThreadCancelled(_ThreadLifecycleRecord):
    event: Literal["cancelled"] = "cancelled"
    reason: str = Field(min_length=1)


ThreadLifecycleRecord: TypeAlias = Annotated[
    ThreadStarted | ThreadCompleted | ThreadFailed | ThreadCancelled,
    Field(discriminator="event"),
]


class HistoryPort(HistorySink, Protocol):
    def load(self) -> list[ConversationMessage]: ...

    def load_surface(self) -> tuple[ConversationMessage, ...]: ...

    def load_transcript(self) -> list[ConversationMessage]: ...

    def replace(self, messages: Sequence[ConversationMessage]) -> None: ...

    def count(self) -> int: ...

    def count_turns(self) -> int: ...

    def committed_input_ids(self) -> set[str]: ...

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





class InboxPersistencePort(InboxSink, Protocol):
    """Durable inbox operations owned by the persistence plugin."""

    def load(self) -> list[InboxItem]: ...

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
    "ThreadCancelled",
    "ThreadCompleted",
    "ThreadFailed",
    "ThreadStarted",
    "ThreadLifecycleWriterPort",
    "ThreadPersistenceFactory",
    "ThreadPersistencePort",
]
