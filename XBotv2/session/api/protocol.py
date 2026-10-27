"""Session HTTP request and response values."""
from __future__ import annotations
from typing import Literal, Protocol, TypeAlias
from pydantic import Field, model_validator
from XBotv2.protocol.api.models import ResourceResponse, WireModel
from XBotv2.session.api.contracts import SessionMode, OpenedThread, ThreadSummary, SessionSummary
from XBotv2.agentloop.api.models import PendingInputData, MediaInput, AttachmentInput

class OpenSessionRequest(WireModel):
    session_id: str | None = None
    thread_id: str = "main"
    workspace_root: str | None = None
    mode: SessionMode = "new"
    agent: str | None = None
    history_limit: int | None = Field(default=None, ge=1, le=500)


class ThreadListResponse(WireModel):
    session_id: str = Field(min_length=1)
    threads: list[ThreadSummary] = Field(default_factory=list)
    previous_cursor: str | None = None
    next_cursor: str | None = None


class SessionListResponse(WireModel):
    previous_cursor: str | None = None
    next_cursor: str | None = None
    sessions: list[SessionSummary] = Field(default_factory=list)
    event_cursor: int = Field(default=0, ge=0)


class WorkspaceEventCursor(Protocol):
    @property
    def sequence(self) -> int: ...


class SessionUpdateRequest(WireModel):
    title: str = Field(min_length=1, max_length=200)


OpenSessionResponse: TypeAlias = ResourceResponse[OpenedThread]


class OpenThreadRequest(WireModel):
    thread_id: str = Field(min_length=1)
    parent_thread_id: str = Field(default="main", min_length=1)
    workspace_root: str | None = None
    mode: SessionMode = "new"
    agent: str | None = None
    history_limit: int | None = Field(default=None, ge=1, le=500)


class UndoRequest(WireModel):
    count: int = Field(default=1, ge=1)
    history_limit: int | None = Field(default=None, ge=1, le=500)


class RegenerateRequest(WireModel):
    request_id: str = ""


class ForkResponse(WireModel):
    session_id: str = Field(min_length=1)
    source_session_id: str = Field(min_length=1)
    status: Literal["forked"] = "forked"


class DeleteSessionResponse(WireModel):
    session_id: str = Field(min_length=1)
    status: Literal["deleted"] = "deleted"


class PendingInputListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    items: list[PendingInputData] = Field(default_factory=list)


class PendingInputUpdateRequest(WireModel):
    action: Literal["edit", "remove", "steer"]
    content: str = ""

    @model_validator(mode="after")
    def _validate_edit(self) -> "PendingInputUpdateRequest":
        if self.action == "edit" and not self.content.strip():
            raise ValueError("queue edit requires non-empty content")
        return self


class MessageRequest(WireModel):
    content: str = ""
    request_id: str = ""
    delivery: Literal["queue", "steer"] = "steer"
    media: list[MediaInput] = Field(default_factory=list)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_content(self) -> "MessageRequest":
        if not self.content.strip() and not self.media and not self.attachments:
            raise ValueError("message requires text, media, or attachment content")
        return self


class InterruptResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: Literal["idle", "interrupting"]
    cancelled: bool


class CloseResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = ""
    status: Literal["closed"] = "closed"


