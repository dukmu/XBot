"""Typed ports exposed by one thread persistence composition."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from pydantic import JsonValue
from XBotv2.agentloop.api.contracts import InboxItem, InboxSink
from XBotv2.core.api.history import HistoryPage, HistorySink, TrajectoryRead
from XBotv2.core.api.domain import Cursor
from XBotv2.core.api.messages import ConversationMessage
from XBotv2.core.api.paths import SessionPaths, ThreadPaths
from XBotv2.core.api.artifacts import ArtifactStorePort
from xcore.api.services import ServiceKey
from XBotv2.core.api.metadata import ThreadMetadata


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


class SessionOwnership(Protocol):
    def release(self) -> None: ...

class Persistence(Protocol):
    def fork_session(self, session_id: str) -> str: ...
    def delete_session(self, session_id: str) -> None: ...
    def messages(self, session_id: str, thread_id: str) -> tuple[ConversationMessage, ...]: ...
    def message_page(self, session_id: str, thread_id: str, *, cursor=None, limit=None): ...
    def trajectory_page(self, session_id: str, thread_id: str, *, cursor=None, limit=100, before=None): ...
    def artifact(self, session_id: str, thread_id: str, artifact_id: str): ...
    def thread(self, session_paths: SessionPaths, *, thread_id: str) -> ThreadPersistencePort: ...
    def artifacts(self, thread_paths: ThreadPaths) -> ArtifactStorePort: ...
    def acquire(self, session_paths: SessionPaths) -> SessionOwnership: ...

PERSISTENCE: ServiceKey[Persistence] = ServiceKey("persistence")
THREAD_PERSISTENCE: ServiceKey[ThreadPersistencePort] = ServiceKey("thread_persistence")
