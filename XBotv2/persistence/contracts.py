"""Typed ports exposed by one thread persistence composition."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import JsonValue

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
    "ThreadPersistenceFactory",
    "ThreadPersistencePort",
]
