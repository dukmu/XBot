"""Input control and execution snapshots shared with client adapters."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from XBotv2.core.api.domain import InboxTarget
from XBotv2.core.api.history import HistoryPage
from XBotv2.core.api.timing import SessionStats
from XBotv2.session.api.records import ConversationRecord

class AttachmentInput(BaseModel):
    data: str = Field(min_length=1)
    media_type: str = "application/octet-stream"
    name: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class InterruptResult:
    cancelled: bool


class PendingInputData(BaseModel):
    message_id: str
    content: str
    target: InboxTarget
    image_count: int = 0
    artifact_count: int = 0
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class RegenerateMessage:
    session_id: str
    thread_id: str
    request_id: str


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
class HistoryMutation:
    removed_turns: int
    history: HistoryPage[ConversationRecord]
    stats: SessionStats


class ImageInput(BaseModel):
    data: str = Field(min_length=1)
    media_type: str = Field(pattern=r"^image/[A-Za-z0-9.+-]+$")
    model_config = ConfigDict(extra="forbid", frozen=True)


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

