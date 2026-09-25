"""Public contracts for Agent application composition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from XBotv2.agentloop import AgentLoopDriverPort
from XBotv2.agents import AgentDefinition
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import ConversationMessage, HumanInputMessage
from XBotv2.core.domain import UsageSnapshot
from XBotv2.interactions.contracts import (
    InteractionRegistration,
    InteractionRequest,
    InteractionReceipt,
    InteractionResolution,
    InteractionWaiterPort,
)
from XBotv2.core.history import ConversationHistory, HistoryReader
from XBotv2.core.metadata import ThreadMetadata, ThreadMetadataState
from XBotv2.core.paths import SessionPaths
from XBotv2.core.operations import OperationContext
from pydantic import BaseModel, JsonValue

from XBotv2.permissions import PermissionsPort
from XBotv2.persistence import ThreadLifecycleWriterPort


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

class ApplicationEventsPort(OperationContext, Protocol):
    def on(self, event: str, callback: Callable[..., object], **kwargs: Any) -> object: ...

    async def emit(self, event: str, *args: object) -> None: ...


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


class LoopStateView(Protocol):
    metadata: ThreadMetadataState
    history: ConversationHistory


class AgentApplicationPort(Protocol):
    events: ApplicationEventsPort
    driver: AgentLoopDriverPort
    artifacts: ArtifactStorePort
    client_events: ClientEventsPort
    history: SessionHistoryPort
    history_pages: HistoryReader
    usage: UsageSnapshotPort
    loop_state: LoopStateView
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


@dataclass(frozen=True, slots=True)
class ParentPermissions:
    value: PermissionsPort | None


@dataclass(frozen=True, slots=True)
class ChildApplicationRequest:
    definition: AgentDefinition
    thread_id: str
    prompt: str
    parent_permissions: PermissionsPort
    client_events: ClientEventsPort | None


@dataclass(frozen=True, slots=True)
class ChildApplicationResult:
    final_response: str
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)


class ChildApplication(Protocol):
    async def wait(self) -> ChildApplicationResult: ...

    async def cancel(self) -> None: ...


class ChildApplicationError(RuntimeError):
    code = "child_application_failed"


class ChildApplicationsPort(Protocol):
    async def spawn(
        self,
        request: ChildApplicationRequest,
        lifecycle: ThreadLifecycleWriterPort,
    ) -> ChildApplication: ...


__all__ = [
    "AgentApplicationPort",
    "ApplicationSnapshot",
    "ApplicationEventsPort",
    "COLLECT_STATUS_SLOTS",
    "ChildApplicationRequest",
    "ChildApplication",
    "ChildApplicationError",
    "ChildApplicationResult",
    "ChildApplicationsPort",
    "ClientEventSink",
    "ClientEventsPort",
    "InteractionWaiterPort",
    "LoopStateView",
    "ParentPermissions",
    "SessionLaunch",
    "SessionHistoryPort",
    "StatusSlots",
    "UsageSnapshotPort",
]
