"""Typed ports exposed by one thread persistence composition."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator

from XBotv2.agentloop.contracts import InboxInput
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.history import (
    ConversationPage,
    HistoryNode,
    TrajectoryPage,
    TrajectoryTransaction,
)
from XBotv2.core.messages import Message
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
    def load(self) -> list[Message]: ...

    def load_surface(self) -> tuple[HistoryNode, ...]: ...

    def load_transcript(self) -> list[Message]: ...

    def append(self, messages: Sequence[Message]) -> tuple[HistoryNode, ...]: ...

    def replace(self, messages: Sequence[Message]) -> None: ...

    def replace_surface(
        self,
        source_node_ids: Sequence[str],
        messages: Sequence[Message],
        *,
        operation: str,
        preserve_transcript: bool,
    ) -> tuple[HistoryNode, ...]: ...

    def record(
        self,
        event: str,
        data: dict[str, JsonValue],
        *,
        durable: bool = False,
    ) -> None: ...

    def open_transactions(
        self,
        transaction: TrajectoryTransaction,
    ) -> frozenset[str]: ...

    def count(self) -> int: ...

    def page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage: ...

    def page_transcript(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage: ...

    def page_trajectory(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> TrajectoryPage: ...


class MetadataPort(Protocol):
    def load(self) -> ThreadMetadata: ...

    def save(self, metadata: ThreadMetadata) -> None: ...





class InboxPersistencePort(Protocol):
    """Durable inbox operations owned by the persistence plugin."""

    def load(self) -> list[InboxInput]: ...

    def replace(self, items: Sequence[InboxInput]) -> None: ...

    def reconcile(self, committed_input_ids: set[str]) -> list[InboxInput]: ...


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
    workspace_root: str
    provider: str
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
        workspace_root: str = "",
        provider: str = "",
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
    "TrajectoryTransaction",
]
