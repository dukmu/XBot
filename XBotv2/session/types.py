"""Public domain types for process-wide session management."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.messages import Message
from XBotv2.core.providers import BaseProvider
from XBotv2.core.timing import SessionStats
from XBotv2.core.usage import UsageData
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


__all__ = [
    "AttachmentInput",
    "ArtifactPayload",
    "HistoryMutation",
    "ImageInput",
    "InteractionReceipt",
    "InterruptResult",
    "OpenedSession",
    "OpenSession",
    "OpenThread",
    "PendingInputData",
    "PendingInputUpdate",
    "RegenerateMessage",
    "SendMessage",
    "SessionExists",
    "SessionInfo",
    "SessionMode",
    "new_session_id",
    "SessionNotFound",
    "SessionSummary",
    "ThreadNotActive",
    "ThreadSummary",
]
