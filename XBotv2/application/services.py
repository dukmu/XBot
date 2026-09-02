"""Public composition services for launching child Agent applications."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from XBotv2.agentloop import AgentLoopDriverPort
from XBotv2.agents import AgentDefinition, AgentSession
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import Message
from XBotv2.core.usage import UsageData
from XBotv2.core.history import ConversationHistory, ConversationPageReader
from XBotv2.core.metadata import ThreadMetadata, ThreadMetadataState
from XBotv2.core.paths import SessionPaths
from XBotv2.core.operations import OperationContext
from pydantic import JsonValue

from XBotv2.core.tools import ClientEvent
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
class AgentApplicationSnapshot:
    agent: str
    provider: str
    model: str
    model_mode: str
    context_window: int
    messages: tuple[Message, ...]
    usage: UsageData
    metadata: ThreadMetadata
    status_slots: dict[str, str]


class ApplicationEventsPort(OperationContext, Protocol):
    def on(self, event: str, callback: Callable[..., object], **kwargs: Any) -> object: ...

    async def emit(self, event: str, *args: object) -> None: ...


class InteractionResultPort(Protocol):
    request_id: str
    status: str
    answer: object
    decision: str
    scope: str
    reason: str


class InteractionWaiterPort(Protocol):
    def register(self, request_id: str) -> object: ...

    async def wait_registered(
        self,
        request_id: str,
        pending: object,
        timeout_seconds: float | None,
    ) -> InteractionResultPort: ...

    def answer(self, request_id: str, **values: object) -> InteractionResultPort: ...

    def cancel(
        self,
        request_id: str,
        reason: str = "cancelled",
    ) -> InteractionResultPort: ...

    def pending_request_ids(self) -> list[str]: ...


class ClientEventSink(Protocol):
    async def __call__(
        self,
        event: ClientEvent,
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, JsonValue]: ...


class ClientEventsPort(Protocol):
    def set_sink(
        self,
        sink: ClientEventSink | None,
    ) -> ClientEventSink | None: ...

    async def request(
        self,
        event: ClientEvent,
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, JsonValue] | None: ...

    def register_waiter(
        self,
        event_type: str,
        waiter: InteractionWaiterPort,
    ) -> Callable[[], bool]: ...

    def waiter(self, event_type: str) -> InteractionWaiterPort | None: ...

    def pending_request_ids(self) -> list[str]: ...


class SessionHistoryPort(Protocol):
    async def clear_history(self) -> int: ...

    async def undo_history(self, count: int) -> list[Message]: ...

    async def regenerate_history(self) -> Message: ...


class UsageSnapshotPort(Protocol):
    def snapshot(self) -> UsageData: ...


class LoopStateView(Protocol):
    metadata: ThreadMetadataState
    history: ConversationHistory


class AgentApplicationPort(Protocol):
    events: ApplicationEventsPort
    driver: AgentLoopDriverPort
    artifacts: ArtifactStorePort
    client_events: ClientEventsPort
    history: SessionHistoryPort
    history_pages: ConversationPageReader
    usage: UsageSnapshotPort
    persistence_available: bool
    parent_permissions: PermissionsPort

    async def status_slots(self) -> dict[str, str]: ...

    async def snapshot(self) -> AgentApplicationSnapshot: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionLaunch:
    session_id: str
    thread_id: str
    workspace_root: Path
    provider_name: str
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


class ChildApplicationsPort(Protocol):
    async def spawn(
        self,
        request: ChildApplicationRequest,
        lifecycle: ThreadLifecycleWriterPort,
    ) -> AgentSession: ...


__all__ = [
    "AgentApplicationPort",
    "AgentApplicationSnapshot",
    "ApplicationEventsPort",
    "COLLECT_STATUS_SLOTS",
    "ChildApplicationRequest",
    "ChildApplicationsPort",
    "ClientEventSink",
    "ClientEventsPort",
    "InteractionResultPort",
    "InteractionWaiterPort",
    "LoopStateView",
    "ParentPermissions",
    "SessionLaunch",
    "SessionHistoryPort",
    "StatusSlots",
    "UsageSnapshotPort",
]
