"""Public domain types for process-wide session management."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Literal

from XBotv2.core.messages import Message
from XBotv2.core.history import ConversationPage
from XBotv2.core.providers import BaseProvider
from XBotv2.core.tools import JsonObject, json_object


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
    plugin_configs: dict[str, JsonObject] | None = None


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
    event_cursor: int
    pending_inputs: tuple["PendingInputSnapshot", ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "agent_name": self.agent_name,
            "workspace_root": self.workspace_root,
            "provider": self.provider,
            "model": self.model,
            "model_mode": self.model_mode,
            "context_window": self.context_window,
            "usage": self.usage,
            "history": self.history,
            "status_slots": self.status_slots,
            "event_cursor": self.event_cursor,
            "pending_inputs": [item.to_dict() for item in self.pending_inputs],
        }


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    status: Literal["active", "inactive"]
    active_threads: int = 0
    thread_count: int = 0
    workspace_root: str = ""
    title: str = ""
    blank: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "active_threads": self.active_threads,
            "thread_count": self.thread_count,
            "workspace_root": self.workspace_root,
            "title": self.title,
            "blank": self.blank,
        }


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
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
    workspace_root: str = ""
    title: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "kind": self.kind,
            "turn_status": self.turn_status,
            "parent_thread_id": self.parent_thread_id,
            "agent": self.agent,
            "provider": self.provider,
            "model": self.model,
            "model_mode": self.model_mode,
            "context_window": self.context_window,
            "message_count": self.message_count,
            "usage": self.usage,
            "pending_interactions": self.pending_interactions,
            "status_slots": self.status_slots,
            "workspace_root": self.workspace_root,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class HistoryMutation:
    removed_turns: int
    messages: tuple[Message, ...]


MessagePage = ConversationPage


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    content: bytes
    media_type: str
    name: str = ""


@dataclass(frozen=True, slots=True)
class PendingInputSnapshot:
    message_id: str
    content: str
    target: Literal["next-turn", "next-step"]
    source: str = "user"
    image_count: int = 0
    artifact_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "content": self.content,
            "target": self.target,
            "source": self.source,
            "image_count": self.image_count,
            "artifact_count": self.artifact_count,
        }


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
    images: tuple[ImageUpload, ...] = ()
    attachments: tuple[AttachmentUpload, ...] = ()


@dataclass(frozen=True, slots=True)
class RegenerateMessage:
    session_id: str
    thread_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class SessionStreamEvent:
    type: str
    data: JsonObject

    @classmethod
    def from_mapping(cls, event: Mapping[str, object]) -> "SessionStreamEvent":
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(event_type, str) or not event_type:
            raise TypeError("session stream event requires a non-empty string type")
        if not isinstance(data, dict):
            raise TypeError("session stream event data must be an object")
        return cls(type=event_type, data=json_object(data))

    def to_dict(self) -> JsonObject:
        return {"type": self.type, "data": json_object(self.data)}


@dataclass(frozen=True, slots=True)
class InteractionReceipt:
    request_id: str
    pending_interactions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InterruptResult:
    cancelled: bool


__all__ = [
    "AttachmentUpload",
    "ArtifactPayload",
    "HistoryMutation",
    "ImageUpload",
    "InteractionReceipt",
    "InterruptResult",
    "OpenedSession",
    "OpenSession",
    "OpenThread",
    "PendingInputSnapshot",
    "PendingInputUpdate",
    "MessagePage",
    "RegenerateMessage",
    "SendMessage",
    "SessionExists",
    "SessionInfo",
    "SessionNotFound",
    "SessionStreamEvent",
    "SessionSnapshot",
    "ThreadNotActive",
    "ThreadSnapshot",
]
