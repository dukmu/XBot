"""Workspace HTTP models."""

from typing import Literal
from pydantic import Field
from XBotv2.protocol.api.models import WireModel
from XBotv2.workspaces.api.contracts import WorkspaceView
from XBotv2.session.api.contracts import SessionSummary


class WorkspaceListResponse(WireModel):
    items: list[WorkspaceView] = Field(default_factory=list)
    previous_cursor: str | None = None
    next_cursor: str | None = None
    event_cursor: int = Field(default=0, ge=0)


class WorkspaceSessionPageResponse(WireModel):
    sessions: list[SessionSummary] = Field(default_factory=list)
    archived_session_ids: list[str] = Field(default_factory=list)
    previous_cursor: str | None = None
    next_cursor: str | None = None
    event_cursor: int = Field(default=0, ge=0)


class WorkspaceCreateRequest(WireModel):
    path: str = Field(min_length=1)


class WorkspaceCreateResponse(WireModel):
    workspace: WorkspaceView
    created: bool


class WorkspaceRenameRequest(WireModel):
    title: str = Field(min_length=1)


class WorkspaceResponse(WireModel):
    workspace: WorkspaceView


class WorkspaceOrderRequest(WireModel):
    before_workspace_id: str | None = None


class WorkspaceOrderResponse(WireModel):
    workspace_id: str
    before_workspace_id: str | None


class WorkspaceSessionOrderRequest(WireModel):
    before_session_id: str | None = None


class WorkspaceDeleteResponse(WireModel):
    workspace_id: str
    status: Literal["deleted"] = "deleted"


class ArchivedSessionsResponse(WireModel):
    session_id: str
    archived: bool
