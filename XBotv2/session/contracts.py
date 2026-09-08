"""Public contracts for session management and Agent application creation."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.history import ConversationPage
from XBotv2.core.messages import Message
from XBotv2.core.operations import Operation
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.timing import SessionStats
from XBotv2.core.tools import ClientEvent
from XBotv2.core.usage import UsageData

if TYPE_CHECKING:
    from XBotv2.agents import AgentDefinition
    from XBotv2.application import AgentApplicationPort
    from XBotv2.core.providers import BaseProvider
    from XBotv2.permissions import PermissionsPort
    from XBotv2.session.event_stream import SessionEventFrame


SessionMode = Literal["new", "resume"]


def new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class SessionNotFound(KeyError):
    """The requested session or thread does not exist."""


class SessionExists(RuntimeError):
    """A new session or thread conflicts with persisted state."""


class ThreadNotActive(RuntimeError):
    """The thread exists on disk but has no live runtime."""


@dataclass
class SessionInfo:
    """Mutable identity and counters for one active Agent thread."""

    session_id: str
    thread_id: str
    workspace_root: str = ""
    provider: str = "default"
    turn_count: int = 0
    event_count: int = 0
    status: str = "active"


class ImageInput(BaseModel):
    data: str = Field(min_length=1)
    media_type: str = Field(pattern=r"^image/[A-Za-z0-9.+-]+$")
    model_config = ConfigDict(extra="forbid", frozen=True)


class AttachmentInput(BaseModel):
    data: str = Field(min_length=1)
    media_type: str = "application/octet-stream"
    name: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class OpenSession:
    session_id: str | None
    thread_id: str
    workspace_root: str
    provider_name: str
    mode: SessionMode
    no_plugins: bool
    selected_agent: str | None = None
    model_override: BaseProvider | None = None
    plugin_configs: dict[str, dict[str, JsonValue]] | None = None


@dataclass(frozen=True, slots=True)
class OpenThread:
    session_id: str
    thread_id: str
    parent_thread_id: str
    workspace_root: str | None
    provider_name: str
    mode: SessionMode
    no_plugins: bool
    selected_agent: str | None = None
    model_override: BaseProvider | None = None


class SessionDescriptor(BaseModel):
    session_id: str
    thread_id: str
    agent_name: str
    workspace_root: str
    provider: str
    model: str
    model_mode: str
    context_window: int
    usage: UsageData
    status_slots: dict[str, str]
    event_cursor: int
    session_stats: SessionStats = Field(default_factory=SessionStats)
    model_config = ConfigDict(extra="forbid", frozen=True)


class OpenedSession(SessionDescriptor):
    history: tuple[Message, ...]
    pending_inputs: tuple["PendingInputData", ...] = ()
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
    )


class SessionSummary(BaseModel):
    session_id: str
    status: Literal["active", "inactive"]
    active_threads: int = 0
    thread_count: int = 0
    workspace_root: str = ""
    title: str = ""
    blank: bool = True
    model_config = ConfigDict(extra="forbid", frozen=True)


class ThreadSummary(BaseModel):
    session_id: str
    thread_id: str
    status: Literal["active", "inactive"]
    kind: Literal["main", "subagent"] = "main"
    turn_status: Literal["idle", "running"] = "idle"
    parent_thread_id: str = ""
    agent: str = ""
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    context_window: int = 0
    message_count: int = 0
    usage: UsageData = Field(default_factory=UsageData)
    session_stats: SessionStats = Field(default_factory=SessionStats)
    pending_interactions: tuple[str, ...] = ()
    status_slots: dict[str, str] = Field(default_factory=dict)
    workspace_root: str = ""
    title: str = ""
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class HistoryMutation:
    removed_turns: int
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    content: bytes
    media_type: str
    name: str = ""


class PendingInputData(BaseModel):
    message_id: str
    content: str
    target: Literal["next-turn", "next-step"]
    source: str = "user"
    image_count: int = 0
    artifact_count: int = 0
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class PendingInputUpdate:
    session_id: str
    thread_id: str
    message_id: str
    action: Literal["edit", "remove", "steer"]
    content: str = ""


@dataclass(frozen=True, slots=True)
class SendMessage:
    session_id: str
    thread_id: str
    content: str
    request_id: str
    delivery: Literal["queue", "steer"] = "steer"
    images: tuple[ImageInput, ...] = ()
    attachments: tuple[AttachmentInput, ...] = ()


@dataclass(frozen=True, slots=True)
class RegenerateMessage:
    session_id: str
    thread_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class InteractionReceipt:
    request_id: str
    pending_interactions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InterruptResult:
    cancelled: bool

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")


PREPARE_FORK = "session/prepare-fork"
HISTORY_CHANGED = "session/history-changed"
SESSION_RESOURCE_CHANGED = "session/resource-changed"
SESSION_RESOURCE_REMOVED = "session/resource-removed"


@dataclass(frozen=True, slots=True)
class PrepareFork:
    session_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class HistoryChanged:
    messages: tuple[Message, ...]
    operation: str
    turns: int = 0


@dataclass(frozen=True, slots=True)
class SessionResourceChanged:
    session: SessionSummary
    added: bool = False


@dataclass(frozen=True, slots=True)
class SessionResourceRemoved:
    session_id: str


@dataclass(frozen=True, slots=True)
class SessionStatus:
    session_id: str
    thread_id: str
    workspace_root: str
    agent: str
    provider: str
    model: str
    model_mode: str
    context_window: int
    status: str
    resumed: bool
    turn_count: int
    message_count: int
    pending_inputs: int


@dataclass(frozen=True, slots=True)
class AgentApplicationOptions:
    """Launch facts for one session-owned Agent application."""

    paths: RuntimePaths
    provider_name: str
    session_id: str
    thread_id: str
    workspace_root: Path
    no_plugins: bool
    plugin_configs: dict[str, dict[str, JsonValue]] | None = None
    model_override: BaseProvider | None = None
    selected_agent: str | None = None
    agent_definition: AgentDefinition | None = None
    parent_thread_id: str = ""
    parent_permission_system: PermissionsPort | None = None
    is_subagent: bool = False
    interactive: bool = True


class AgentApplicationFactory(Protocol):
    """Composition-owned factory consumed by process session management."""

    async def __call__(self, options: AgentApplicationOptions) -> AgentApplicationPort: ...


class SessionPort(Protocol):
    session_id: str
    thread_id: str
    workspace_root: str

    @property
    def provider(self) -> str: ...
    def new_thread_id(self, owner: str) -> str: ...
    def status(self) -> SessionStatus: ...
    async def fork(self) -> str: ...
    async def clear_history(self) -> int: ...
    async def undo_history(self, count: int) -> list[Message]: ...
    async def regenerate_history(self) -> Message: ...


class SessionsPort(Protocol):
    """Transport-neutral process API for persistent sessions and threads."""

    def session_exists(self, session_id: str) -> bool: ...
    async def open(self, request: OpenSession) -> OpenedSession: ...
    async def list_sessions(self) -> tuple[SessionSummary, ...]: ...
    async def session_summary(self, session_id: str) -> SessionSummary: ...
    async def rename_session(self, session_id: str, title: str) -> SessionSummary: ...
    async def fork_session(self, session_id: str) -> str: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def list_threads(self, session_id: str) -> tuple[ThreadSummary, ...]: ...
    async def open_thread(self, request: OpenThread) -> OpenedSession: ...
    async def thread_summary(self, session_id: str, thread_id: str) -> ThreadSummary: ...
    async def messages(self, session_id: str, thread_id: str) -> tuple[Message, ...]: ...
    async def message_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: str | None,
        limit: int | None,
    ) -> ConversationPage: ...
    async def artifact(
        self,
        session_id: str,
        thread_id: str,
        artifact_id: str,
    ) -> ArtifactPayload: ...
    async def clear_history(self, session_id: str, thread_id: str) -> HistoryMutation: ...
    async def undo_history(
        self,
        session_id: str,
        thread_id: str,
        count: int,
    ) -> HistoryMutation: ...
    async def stream_message(self, request: SendMessage) -> AsyncIterator[ClientEvent]: ...
    async def pending_inputs(
        self,
        session_id: str,
        thread_id: str,
    ) -> tuple[PendingInputData, ...]: ...
    async def update_pending_input(
        self,
        request: PendingInputUpdate,
    ) -> tuple[PendingInputData, ...]: ...
    async def regenerate_message(
        self,
        request: RegenerateMessage,
    ) -> AsyncIterator[ClientEvent]: ...
    async def stream_events(
        self,
        session_id: str,
        thread_id: str,
        *,
        after: int | None = None,
    ) -> AsyncIterator[SessionEventFrame]: ...
    async def respond_permission(
        self,
        session_id: str,
        thread_id: str,
        request_id: str,
        decision: str,
        scope: str,
    ) -> InteractionReceipt: ...
    async def respond_user_input(
        self,
        session_id: str,
        thread_id: str,
        request_id: str,
        answer: JsonValue,
    ) -> InteractionReceipt: ...
    async def cancel_interaction(
        self,
        session_id: str,
        thread_id: str,
        event_type: Literal["permission_request", "user_input_required"],
        request_id: str,
        reason: str,
    ) -> InteractionReceipt: ...
    async def close_session(self, session_id: str) -> None: ...
    async def close_thread(self, session_id: str, thread_id: str) -> None: ...
    async def interrupt(self, session_id: str, thread_id: str) -> InterruptResult: ...
    async def dispatch(
        self,
        session_id: str,
        thread_id: str,
        operation: Operation[RequestT, ResponseT],
        request: RequestT,
    ) -> ResponseT: ...
    async def dispatch_all(
        self,
        session_id: str,
        operation: Operation[RequestT, ResponseT],
        request: RequestT,
    ) -> tuple[ResponseT, ...]: ...


__all__ = [
    "AgentApplicationFactory",
    "AgentApplicationOptions",
    "ArtifactPayload",
    "AttachmentInput",
    "HISTORY_CHANGED",
    "HistoryMutation",
    "HistoryChanged",
    "ImageInput",
    "InteractionReceipt",
    "InterruptResult",
    "OpenedSession",
    "OpenSession",
    "OpenThread",
    "PREPARE_FORK",
    "PendingInputData",
    "PendingInputUpdate",
    "PrepareFork",
    "RegenerateMessage",
    "SESSION_RESOURCE_CHANGED",
    "SESSION_RESOURCE_REMOVED",
    "SendMessage",
    "SessionDescriptor",
    "SessionExists",
    "SessionInfo",
    "SessionMode",
    "SessionNotFound",
    "SessionResourceChanged",
    "SessionResourceRemoved",
    "SessionStatus",
    "SessionPort",
    "SessionsPort",
    "SessionSummary",
    "ThreadNotActive",
    "ThreadSummary",
    "new_session_id",
]
