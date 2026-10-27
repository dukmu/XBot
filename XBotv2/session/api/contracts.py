"""Public identity, ownership, opening and summary contracts for sessions and threads."""

from __future__ import annotations
from XBotv2.agentloop.api.models import PendingInputData

import re
import secrets
from dataclasses import dataclass
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from XBotv2.core.api.history import HistoryPage
from XBotv2.core.api.messages import CompactionSummaryMessage, ConversationMessage
from XBotv2.core.api.metadata import ThreadMetadata
from XBotv2.core.api.timing import SessionStats
from XBotv2.core.api.domain import EventScope, InboxTarget, UsageSnapshot
from XBotv2.session.api.records import ConversationRecord
from XBotv2.interactions.api.contracts import InteractionReceipt
from XBotv2.interactions.api.models import UserInputRequest
from XBotv2.permissions.api.models import PermissionRequest

if TYPE_CHECKING:
    from XBotv2.core.api.providers import Provider


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


@dataclass(frozen=True, slots=True)
class SessionEventFrame:
    sequence: int
    scope: EventScope
    event: SessionEvent


class SessionEventSubscription(AsyncIterator[SessionEventFrame], Protocol):
    """A live cursor over one session's event stream."""

    async def aclose(self) -> None: ...


def compaction_summary_text(messages: Iterable[ConversationMessage]) -> str:
    """Read the summary text out of a compaction replacement.

    The replacement message carries the summary inside a
    ``conversation_summary`` prompt container; clients render that text as
    the expandable compaction entry.
    """
    for message in messages:
        content = (
            message.summary if isinstance(message, CompactionSummaryMessage) else ""
        )
        match = re.search(
            r"<conversation_summary[^>]*>([\s\S]*?)</conversation_summary>",
            content,
        )
        if match:
            return match.group(1).strip()
    return ""


@dataclass(frozen=True, slots=True)
class OpenSession:
    session_id: str | None
    thread_id: str
    workspace_root: str | None
    provider_name: str | None
    mode: SessionMode
    no_plugins: bool
    selected_agent: str | None = None
    model_override: Provider | None = None
    plugin_configs: dict[str, dict[str, JsonValue]] | None = None
    interactive: bool = True


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
    model_override: Provider | None = None


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
class SessionInfo:
    session_id: str
    workspace_root: Path


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    session_id: str
    thread_id: str
    workspace_root: Path
    parent_thread_id: str = ""

    @property
    def is_subagent(self) -> bool:
        return bool(self.parent_thread_id)


class SessionsPort(Protocol):
    def thread_repository(self, session_id: str, thread_id: str): ...

    """Transport-neutral process API for persistent sessions and threads."""

    async def new_thread_id(self, session_id: str, agent_ref: str) -> str: ...
    async def locate(self, session_id: str, thread_id: str) -> Context: ...
    async def scopes(self, session_id: str) -> tuple[Context, ...]: ...
    def session_exists(self, session_id: str) -> bool: ...
    async def open(self, request: OpenSession) -> Context: ...
    async def list_sessions(self) -> tuple[SessionSummary, ...]: ...
    async def session_summary(self, session_id: str) -> SessionSummary: ...
    async def rename_session(self, session_id: str, title: str) -> SessionSummary: ...
    async def fork_session(self, session_id: str) -> str: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def list_threads(self, session_id: str) -> tuple[ThreadSummary, ...]: ...
    def set_child_limit(self, session_id: str, limit: int) -> None:
        """Limit live child threads for the lifetime of this session owner."""
        ...
    async def open_thread(self, request: OpenThread) -> Context: ...
    async def thread_summary(
        self, session_id: str, thread_id: str
    ) -> ThreadSummary: ...
    async def close_session(self, session_id: str) -> None: ...
    async def close_thread(self, session_id: str, thread_id: str) -> None: ...


from xcore.api.context import Context
from xcore.api.services import ServiceKey

SESSION_INFO: ServiceKey[SessionInfo] = ServiceKey("session_info")
THREAD_INFO: ServiceKey[ThreadInfo] = ServiceKey("thread_info")
SESSIONS: ServiceKey[SessionsPort] = ServiceKey("sessions")


class ThreadEvents(Protocol):
    sequence: int
    subscriber_count: int

    def publish(self, event: SessionEvent, *, scope: EventScope = ...) -> None: ...
    def subscribe(self, *, after: int | None = None) -> SessionEventSubscription: ...
    def close(self) -> None: ...


THREAD_EVENTS: ServiceKey[ThreadEvents] = ServiceKey("thread_events")
