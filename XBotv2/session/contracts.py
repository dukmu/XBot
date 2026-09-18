"""Public contracts for session management and Agent application creation."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import datetime
from operator import not_
from pathlib import Path
from typing import Annotated, TYPE_CHECKING, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.artifacts import ArtifactRef, ImageContent
from XBotv2.core.history import (
    ConversationPage,
    TrajectoryEvent,
    TrajectoryMessage,
    TrajectoryPage,
    TrajectorySurfaceReplace,
)
from XBotv2.core.messages import RUNTIME_INPUT_KEY, Message
from XBotv2.core.operations import Operation
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.prompts import MESSAGE_FORMAT_KEY, tool_result_display_content
from XBotv2.core.timing import SessionStats, TIMING_METADATA_KEY
from XBotv2.core.tools import ClientEvent, ToolCall
from XBotv2.core.usage import UsageData

if TYPE_CHECKING:
    from XBotv2.agents import AgentDefinition
    from XBotv2.application import AgentApplicationPort
    from XBotv2.core.providers import BaseProvider
    from XBotv2.permissions import PermissionsPort


SessionMode = Literal["new", "resume"]


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


@dataclass(frozen=True, slots=True)
class SessionEventFrame:
    sequence: int
    request_id: str
    event: ClientEvent


class SessionEventSubscription(AsyncIterator[SessionEventFrame], Protocol):
    """A live cursor over one session's event stream."""

    async def aclose(self) -> None: ...


class SessionHistoryItem(BaseModel):
    """Transport-neutral projection of one visible conversation record."""

    role: Literal["user", "assistant", "tool"]
    content: str = ""
    reasoning: str = Field(default="", exclude_if=not_)
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""
    input_id: str = Field(default="", exclude=True)
    status: str = ""
    data: JsonValue = None
    images: tuple[ImageContent, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    error: dict[str, JsonValue] | None = None
    runtime: dict[str, str] | None = Field(default=None, exclude_if=lambda value: value is None)
    timing: dict[str, JsonValue] | None = Field(default=None, exclude_if=lambda value: value is None)
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionTrajectoryMessage(BaseModel):
    position: int = Field(ge=1)
    kind: Literal["message"] = "message"
    message_id: str = ""
    message: SessionHistoryItem
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionTrajectorySurfaceReplace(BaseModel):
    position: int = Field(ge=1)
    kind: Literal["surface_replace"] = "surface_replace"
    operation: str
    transcript: Literal["preserve", "replace"]
    source_node_ids: tuple[str, ...]
    messages: tuple[SessionHistoryItem, ...]
    #: The durable summary text of a compaction replacement. The replacement
    #: message is a system prompt container that the human transcript replay
    #: must not surface verbatim; this derived field lets clients render the
    #: expandable compaction entry after a reload without storing the summary
    #: a second time (the durable record keeps exactly one copy).
    summary: str = ""
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionTrajectoryEvent(BaseModel):
    position: int = Field(ge=1)
    kind: Literal["event"] = "event"
    event: str
    data: dict[str, JsonValue]
    timestamp: str
    model_config = ConfigDict(extra="forbid", frozen=True)


SessionTrajectoryItem = Annotated[
    SessionTrajectoryMessage | SessionTrajectorySurfaceReplace | SessionTrajectoryEvent,
    Field(discriminator="kind"),
]


class SessionTrajectoryPage(BaseModel):
    items: tuple[SessionTrajectoryItem, ...]
    next_cursor: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


def conversation_replay(messages: Iterable[Message]) -> tuple[SessionHistoryItem, ...]:
    replay: list[SessionHistoryItem] = []
    for message in messages:
        if message.role not in {"user", "assistant", "tool"}:
            continue
        additional = message.additional_kwargs or {}
        content = str(message.content or "")
        if message.role == "tool" and additional.get(MESSAGE_FORMAT_KEY):
            content = tool_result_display_content(content)
        runtime_value = additional.get(RUNTIME_INPUT_KEY)
        timing = message.response_metadata.get(TIMING_METADATA_KEY)
        replay.append(SessionHistoryItem(
            role=message.role,
            content=content,
            reasoning=message.reasoning if message.role == "assistant" else "",
            tool_calls=tuple(message.tool_calls or ()),
            tool_call_id=message.tool_call_id or "",
            input_id=message.input_id or "",
            status=message.status or "",
            data=message.data,
            images=tuple(message.images),
            artifacts=_artifacts(message),
            error=message.error if message.role == "tool" else None,
            runtime=(
                {str(key): str(value) for key, value in runtime_value.items()}
                if isinstance(runtime_value, dict)
                else None
            ),
            timing=dict(timing) if isinstance(timing, Mapping) else None,
        ))
    return tuple(replay)


def trajectory_replay(page: TrajectoryPage) -> SessionTrajectoryPage:
    items: list[SessionTrajectoryItem] = []
    for item in page.items:
        if isinstance(item, TrajectoryMessage):
            replay = conversation_replay((item.message,))
            if replay:
                items.append(SessionTrajectoryMessage(
                    position=item.position,
                    message_id=(
                        item.message.input_id
                        or str(item.message.additional_kwargs.get("xbot_message_id") or "")
                        or item.message.tool_call_id
                    ),
                    message=replay[0],
                ))
        elif isinstance(item, TrajectorySurfaceReplace):
            items.append(SessionTrajectorySurfaceReplace(
                position=item.position,
                operation=item.operation,
                transcript=item.transcript,
                source_node_ids=item.source_node_ids,
                messages=conversation_replay(item.messages),
                summary=compaction_summary_text(item.messages),
            ))
        elif isinstance(item, TrajectoryEvent):
            items.append(SessionTrajectoryEvent(
                position=item.position,
                event=item.event,
                data=item.data,
                timestamp=item.timestamp,
            ))
    return SessionTrajectoryPage(items=tuple(items), next_cursor=page.next_cursor)


def compaction_summary_text(messages: Iterable[Message]) -> str:
    """Read the summary text out of a compaction replacement.

    The replacement message carries the summary inside a
    ``conversation_summary`` prompt container; clients render that text as
    the expandable compaction entry.
    """
    for message in messages:
        content = str(message.content or "")
        match = re.search(
            r"<conversation_summary[^>]*>([\s\S]*?)</conversation_summary>",
            content,
        )
        if match:
            return match.group(1).strip()
    return ""


def _artifacts(message: Message) -> tuple[ArtifactRef, ...]:
    values = tuple(message.artifact or ())
    if not all(isinstance(value, ArtifactRef) for value in values):
        raise TypeError("Session history artifacts must be ArtifactRef values")
    return values


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
    title: str = ""
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
    pending_interactions: tuple["PendingInteractionData", ...] = ()
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


class PendingInteractionData(BaseModel):
    """One unanswered client interaction, replayable from an open response.

    A live approval or question only exists in the event stream, so a client
    that reloads or reconnects while it is pending must be able to rebuild the
    dialog from the session snapshot instead of losing it.
    """

    type: str
    data: dict[str, JsonValue] = Field(default_factory=dict)
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

    @property
    def provider(self) -> str: ...
    def new_thread_id(self, owner: str) -> str: ...
    def status(self, *, pending_input_count: int) -> SessionStatus: ...
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
    async def trajectory_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> SessionTrajectoryPage: ...
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
    "OpenedSession",
    "OpenSession",
    "OpenThread",
    "PREPARE_FORK",
    "PendingInputData",
    "PendingInteractionData",
    "PendingInputUpdate",
    "PrepareFork",
    "RegenerateMessage",
    "SESSION_RESOURCE_CHANGED",
    "SESSION_RESOURCE_REMOVED",
    "SendMessage",
    "SessionDescriptor",
    "SessionEventFrame",
    "SessionEventSubscription",
    "SessionEventCursorExpired",
    "SessionExists",
    "SessionInfo",
    "SessionHistoryItem",
    "SessionTrajectoryEvent",
    "SessionTrajectoryItem",
    "SessionTrajectoryMessage",
    "SessionTrajectoryPage",
    "SessionTrajectorySurfaceReplace",
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
    "trajectory_replay",
    "new_session_id",
]
