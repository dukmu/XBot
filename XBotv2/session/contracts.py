"""Public contracts for session management and Agent application creation."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.artifacts import ArtifactRef, ImageRef
from XBotv2.core.history import (
    HistoryPage,
    TrajectoryRead,
)
from XBotv2.core.messages import (
    AssistantMessage,
    CompactionSummaryMessage,
    ConversationMessage,
    HumanInputMessage,
    RuntimeNoticeMessage,
    ToolMessage,
)
from XBotv2.core.metadata import ThreadMetadata, ThreadMetadataState
from XBotv2.core.parts import ReasoningPart, TextPart
from XBotv2.core.operations import Operation
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.timing import SessionStats
from XBotv2.core.tools import ToolCall
from XBotv2.core.domain import Cursor, EventScope, UsageSnapshot
from XBotv2.session.records import ConversationRecord, project_message
from XBotv2.interactions.contracts import InteractionReceipt
from XBotv2.interactions.protocol import UserInputRequest
from XBotv2.permissions.contracts import PermissionRequest

if TYPE_CHECKING:
    from XBotv2.agents import AgentDefinition
    from XBotv2.application import AgentApplicationPort
    from XBotv2.core.providers import BaseProvider
    from XBotv2.permissions import PermissionsPort


SessionMode = Literal["new", "resume"]
PendingInteraction = UserInputRequest | PermissionRequest


def new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class SessionNotFound(KeyError):
    """The requested session or thread does not exist."""


class SessionExists(RuntimeError):
    """A new session or thread conflicts with persisted state."""


class ThreadNotActive(RuntimeError):
    """The thread exists on disk but has no live runtime."""


class SessionEventCursorExpired(LookupError):
    def __init__(self, cursor: int, oldest: int) -> None:
        super().__init__(
            f"Session event cursor {cursor} expired; "
            f"oldest available sequence is {oldest}"
        )
        self.cursor = cursor
        self.oldest = oldest


class SessionEvent(Protocol):
    kind: str


@dataclass(frozen=True, slots=True)
class SessionKey:
    session_id: str
    thread_id: str


@dataclass(slots=True)
class SessionRuntimeState:
    """Mutable runtime state for one Agent thread.

    Identity is carried by ``key``. Workspace and effective runtime selection
    are always read through the single thread-metadata owner.
    """

    key: SessionKey
    metadata: ThreadMetadataState
    status: str = "active"
    turn_count: int = 0
    event_cursor: int = 0

    @property
    def session_id(self) -> str:
        return self.key.session_id

    @property
    def thread_id(self) -> str:
        return self.key.thread_id

    @property
    def workspace_root(self) -> str:
        return self.metadata.value.workspace_root


@dataclass(frozen=True, slots=True)
class SessionEventFrame:
    sequence: int
    scope: EventScope
    event: SessionEvent


class SessionEventSubscription(AsyncIterator[SessionEventFrame], Protocol):
    """A live cursor over one session's event stream."""

    async def aclose(self) -> None: ...


def conversation_replay(
    messages: Iterable[ConversationMessage],
) -> tuple[ConversationRecord, ...]:
    """Project visible conversation messages for clients."""
    return tuple(project_message(message) for message in messages)


def compaction_summary_text(messages: Iterable[ConversationMessage]) -> str:
    """Read the summary text out of a compaction replacement.

    The replacement message carries the summary inside a
    ``conversation_summary`` prompt container; clients render that text as
    the expandable compaction entry.
    """
    for message in messages:
        content = message.summary if isinstance(message, CompactionSummaryMessage) else ""
        match = re.search(
            r"<conversation_summary[^>]*>([\s\S]*?)</conversation_summary>",
            content,
        )
        if match:
            return match.group(1).strip()
    return ""


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
    provider_name: str | None
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
    provider_name: str | None
    mode: SessionMode
    no_plugins: bool
    selected_agent: str | None = None
    model_override: BaseProvider | None = None


class OpenedThread(BaseModel):
    key: SessionKey
    metadata: ThreadMetadata
    usage: UsageSnapshot
    status_slots: dict[str, str]
    event_cursor: int
    history: HistoryPage[ConversationRecord]
    pending_inputs: tuple[PendingInputData, ...]
    pending_interactions: tuple[PendingInteraction, ...]
    model_config = ConfigDict(
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
    # A session whose trajectory cannot be read stays listed so it can be
    # deleted; clients should show it as damaged instead of opening it.
    unreadable: bool = False
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
    usage: UsageSnapshot = Field(default_factory=UsageSnapshot)
    session_stats: SessionStats = Field(default_factory=SessionStats)
    pending_interactions: tuple[str, ...] = ()
    status_slots: dict[str, str] = Field(default_factory=dict)
    workspace_root: str = ""
    title: str = ""
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class HistoryMutation:
    removed_turns: int
    history: HistoryPage[ConversationRecord]
    stats: SessionStats


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    content: bytes
    media_type: str
    name: str = ""


class PendingInputData(BaseModel):
    message_id: str
    content: str
    target: Literal["next-turn", "next-step"]
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
    messages: tuple[ConversationMessage, ...]
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
    title: str = ""


@dataclass(frozen=True, slots=True)
class AgentApplicationOptions:
    """Launch facts for one session-owned Agent application."""

    paths: RuntimePaths
    provider_name: str | None
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
    # A brand-new session does not materialize on disk until its first record;
    # resume sessions persist metadata immediately.
    defer_persist: bool = False


class AgentApplicationFactory(Protocol):
    """Composition-owned factory consumed by process session management."""

    async def __call__(self, options: AgentApplicationOptions) -> AgentApplicationPort: ...


class SessionPort(Protocol):
    session_id: str
    thread_id: str
    workspace_root: str

    def new_thread_id(self, owner: str) -> str: ...
    def status(self, *, pending_input_count: int) -> SessionStatus: ...
    async def fork(self) -> str: ...
    async def clear_history(self) -> int: ...
    async def undo_history(self, count: int) -> list[ConversationMessage]: ...
    async def regenerate_history(self) -> HumanInputMessage: ...


class SessionsPort(Protocol):
    """Transport-neutral process API for persistent sessions and threads."""

    def session_exists(self, session_id: str) -> bool: ...
    async def open(self, request: OpenSession) -> OpenedThread: ...
    async def list_sessions(self) -> tuple[SessionSummary, ...]: ...
    async def session_summary(self, session_id: str) -> SessionSummary: ...
    async def rename_session(self, session_id: str, title: str) -> SessionSummary: ...
    async def fork_session(self, session_id: str) -> str: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def list_threads(self, session_id: str) -> tuple[ThreadSummary, ...]: ...
    async def open_thread(self, request: OpenThread) -> OpenedThread: ...
    async def thread_summary(self, session_id: str, thread_id: str) -> ThreadSummary: ...
    async def messages(self, session_id: str, thread_id: str) -> tuple[ConversationMessage, ...]: ...
    async def message_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: Cursor | None,
        limit: int | None,
    ) -> HistoryPage[ConversationMessage]: ...
    async def trajectory_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: Cursor | None,
        limit: int,
        before: int | None = None,
    ) -> TrajectoryRead: ...
    async def artifact(
        self,
        session_id: str,
        thread_id: str,
        artifact_id: str,
    ) -> ArtifactPayload: ...
    async def clear_history(
        self,
        session_id: str,
        thread_id: str,
        *,
        history_limit: int | None,
    ) -> HistoryMutation: ...
    async def undo_history(
        self,
        session_id: str,
        thread_id: str,
        count: int,
        *,
        history_limit: int | None,
    ) -> HistoryMutation: ...
    async def send_message(self, request: SendMessage) -> None: ...
    async def pending_inputs(
        self,
        session_id: str,
        thread_id: str,
    ) -> tuple[PendingInputData, ...]: ...
    async def update_pending_input(
        self,
        request: PendingInputUpdate,
    ) -> tuple[PendingInputData, ...]: ...
    async def regenerate_message(self, request: RegenerateMessage) -> None: ...
    async def stream_events(
        self,
        session_id: str,
        thread_id: str,
        *,
        after: int | None = None,
    ) -> SessionEventSubscription: ...
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
    "OpenedThread",
    "OpenSession",
    "OpenThread",
    "PREPARE_FORK",
    "PendingInputData",
    "PendingInteraction",
    "PendingInputUpdate",
    "PrepareFork",
    "RegenerateMessage",
    "SESSION_RESOURCE_CHANGED",
    "SESSION_RESOURCE_REMOVED",
    "SendMessage",
    "SessionEventFrame",
    "SessionEventSubscription",
    "SessionEventCursorExpired",
    "SessionExists",
    "SessionKey",
    "SessionRuntimeState",
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
    "conversation_replay",
    "new_session_id",
]
