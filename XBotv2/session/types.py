"""Public domain types for the process-wide Session host."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from XBotv2.core.messages import Message
from XBotv2.core.providers import BaseProvider


class SessionNotFound(KeyError):
    """The requested session or thread does not exist."""


class SessionExists(RuntimeError):
    """A new session or thread conflicts with persisted state."""


class ThreadNotActive(RuntimeError):
    """The thread exists on disk but has no live runtime."""


@dataclass(frozen=True, slots=True)
class ImageUpload:
    data: str
    media_type: str


@dataclass(frozen=True, slots=True)
class AttachmentUpload:
    data: str
    media_type: str
    name: str


@dataclass(frozen=True, slots=True)
class OpenSession:
    session_id: str | None
    thread_id: str
    workspace_root: str
    provider_name: str
    mode: Literal["new", "resume"]
    no_plugins: bool
    selected_agent: str | None = None
    model_override: BaseProvider | None = None


@dataclass(frozen=True, slots=True)
class OpenThread:
    session_id: str
    thread_id: str
    parent_thread_id: str
    workspace_root: str | None
    provider_name: str
    mode: Literal["new", "resume"]
    no_plugins: bool
    selected_agent: str | None = None
    model_override: BaseProvider | None = None


@dataclass(frozen=True, slots=True)
class OpenedSession:
    session_id: str
    thread_id: str
    agent_name: str
    workspace_root: str
    provider: str
    model: str
    model_mode: str
    context_window: int
    usage: dict[str, int]
    history: tuple[Message, ...]
    status_slots: dict[str, str]


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    status: Literal["active", "inactive"]
    active_threads: int = 0
    thread_count: int = 0


@dataclass(frozen=True, slots=True)
class ThreadSummary:
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
    usage: dict[str, int] = field(default_factory=dict)
    pending_interactions: tuple[str, ...] = ()
    status_slots: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HistoryMutation:
    removed_turns: int
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class SendMessage:
    session_id: str
    thread_id: str
    content: str
    request_id: str
    images: tuple[ImageUpload, ...] = ()
    attachments: tuple[AttachmentUpload, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionStreamEvent:
    type: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InteractionReceipt:
    request_id: str
    pending_interactions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InterruptResult:
    cancelled: bool


__all__ = [
    "AttachmentUpload",
    "HistoryMutation",
    "ImageUpload",
    "InteractionReceipt",
    "InterruptResult",
    "OpenedSession",
    "OpenSession",
    "OpenThread",
    "SendMessage",
    "SessionExists",
    "SessionNotFound",
    "SessionStreamEvent",
    "SessionSummary",
    "ThreadNotActive",
    "ThreadSummary",
]
