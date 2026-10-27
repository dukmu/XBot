"""Public contracts for Agent application composition."""

from __future__ import annotations
from xcore.api.context import Context
from XBotv2.agentloop.api.state import LoopState
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from XBotv2.agentloop.api.contracts import AgentLoopDriverPort
from XBotv2.core.api.artifacts import ArtifactStorePort
from XBotv2.core.api.messages import ConversationMessage, HumanInputMessage
from XBotv2.core.api.domain import UsageSnapshot
from XBotv2.interactions.contracts import (
    InteractionRegistration,
    InteractionRequest,
    InteractionReceipt,
    InteractionResolution,
    InteractionWaiterPort,
)
from XBotv2.core.api.history import History, HistoryReader
from XBotv2.core.api.metadata import ThreadMetadata, Metadata
from XBotv2.core.api.paths import SessionPaths
from xcore.api.events import EventQuery
from pydantic import BaseModel, JsonValue
from XBotv2.permissions import PermissionsPort


COLLECT_STATUS_SLOTS = "application/status-slots/collect"


@dataclass(slots=True)
class StatusSlots:
    values: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, value: str) -> None:
        name = str(name).strip()
        value = str(value).strip()
        if name and value:
            self.values[name] = value


@dataclass(frozen=True, slots=True)
class ApplicationSnapshot:
    metadata: ThreadMetadata
    messages: tuple[ConversationMessage, ...]
    usage: UsageSnapshot
    status_slots: dict[str, str]


class ClientEventSink(Protocol):
    async def __call__(
        self,
        event: InteractionRequest,
    ) -> InteractionResolution: ...


class ClientEventsPort(Protocol):
    def install(self, sink: ClientEventSink | None) -> Callable[[], None]: ...

    async def request(
        self,
        event: InteractionRequest,
    ) -> InteractionResolution | None: ...

    def register_interaction(
        self,
        registration: InteractionRegistration,
    ) -> Callable[[], bool]: ...

    def waiter_for(self, request: InteractionRequest) -> InteractionWaiterPort: ...

    def timeout_for(self, request: InteractionRequest) -> float | None: ...

    def resolve(
        self,
        interaction_id: str,
        resolution: InteractionResolution,
    ) -> InteractionReceipt: ...

    def cancel(
        self,
        interaction_id: str,
        reason: str,
        *,
        expected_kind: str | None = None,
    ) -> InteractionReceipt: ...

    def recorded_event(
        self,
        request: InteractionRequest,
        resolution: InteractionResolution,
    ) -> BaseModel: ...

    def pending_request_ids(self) -> list[str]: ...

    def pending_interactions(self) -> list[InteractionRequest]: ...


class SessionHistoryPort(Protocol):
    async def clear_history(self) -> int: ...

    async def undo_history(self, count: int) -> list[ConversationMessage]: ...

    async def regenerate_history(self) -> HumanInputMessage: ...


class UsageSnapshotPort(Protocol):
    def snapshot(self) -> UsageSnapshot: ...


class AgentApplicationPort(Protocol):
    events: Context
    driver: AgentLoopDriverPort
    artifacts: ArtifactStorePort
    client_events: ClientEventsPort
    history: SessionHistoryPort
    history_pages: HistoryReader
    usage: UsageSnapshotPort
    loop_state: LoopState
    persistence_available: bool
    parent_permissions: PermissionsPort

    async def status_slots(self) -> dict[str, str]: ...

    async def snapshot(self) -> ApplicationSnapshot: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionLaunch:
    session_id: str
    thread_id: str
    workspace_root: Path
    provider_name: str | None
    session_paths: SessionPaths
    interactive: bool
    is_subagent: bool
    parent_thread_id: str = ""
    parent_client_events: ClientEventsPort | None = None
    defer_persist: bool = False


@dataclass(frozen=True, slots=True)
class ParentPermissions:
    value: PermissionsPort | None


__all__ = [
    "AgentApplicationPort",
    "ApplicationSnapshot",
    "COLLECT_STATUS_SLOTS",
    "ClientEventSink",
    "ClientEventsPort",
    "InteractionWaiterPort",
    "ParentPermissions",
    "SessionLaunch",
    "SessionHistoryPort",
    "StatusSlots",
    "UsageSnapshotPort",
]
