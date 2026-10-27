"""Typed Workspace resource events emitted after durable commits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from xcore.api.services import ServiceKey
from xcore.api.events import EventKey
from XBotv2.session.api.contracts import SessionSummary

if TYPE_CHECKING:
    from XBotv2.workspaces.api.events import WorkspaceEventFrame


class WorkspaceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    session_count: int = Field(default=0, ge=0)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class WorkspaceListing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[WorkspaceView, ...] = ()
    previous_cursor: str | None = None
    next_cursor: str | None = None


class WorkspaceSessionPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sessions: tuple[SessionSummary, ...] = ()
    previous_cursor: str | None = None
    next_cursor: str | None = None
    archived_session_ids: tuple[str, ...] = ()


class DirectoryNotFound(FileNotFoundError):
    pass


class DirectoryNotReadable(PermissionError):
    pass


class DirectoryEntry(BaseModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    hidden: bool
    model_config = ConfigDict(extra="forbid", frozen=True)


class DirectoryListing(BaseModel):
    path: str = Field(min_length=1)
    parent: str | None
    home: str = Field(min_length=1)
    separator: Literal["/", "\\"]
    entries: tuple[DirectoryEntry, ...]
    truncated: bool
    model_config = ConfigDict(extra="forbid", frozen=True)


class DirectoriesPort(Protocol):
    def list(self, path: str | None = None) -> DirectoryListing: ...


class WorkspaceNotFound(LookupError):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"Workspace {workspace_id!r} was not found")
        self.workspace_id = workspace_id


class WorkspaceSessionNotFound(LookupError):
    pass


class WorkspaceSessionMoveInvalid(ValueError):
    pass


class WorkspacesPort(Protocol):
    async def list(self, *, limit: int = 100, cursor: str | None = None) -> WorkspaceListing: ...

    async def list_sessions(self, workspace_id: str, *, limit: int = 100, cursor: str | None = None) -> WorkspaceSessionPage: ...

    async def list_archived(self, *, limit: int = 100, cursor: str | None = None) -> WorkspaceSessionPage: ...

    async def create(self, path: str) -> tuple[WorkspaceView, bool]: ...

    async def rename(self, workspace_id: str, title: str) -> WorkspaceView: ...

    async def delete(self, workspace_id: str) -> bool: ...

    async def insert_before(
        self,
        workspace_id: str,
        before_workspace_id: str | None,
    ) -> None: ...

    async def insert_session_before(
        self,
        workspace_id: str,
        session_id: str,
        before_session_id: str | None,
    ) -> WorkspaceView: ...

    async def set_archived(
        self,
        session_id: str,
        archived: bool,
    ) -> None: ...


class WorkspaceEventSubscription(Protocol):
    def __aiter__(self) -> "WorkspaceEventSubscription": ...
    async def __anext__(self) -> "WorkspaceEventFrame": ...
    async def aclose(self) -> None: ...


class WorkspaceEventsPort(Protocol):
    @property
    def sequence(self) -> int: ...

    def subscribe(self, after: int) -> WorkspaceEventSubscription: ...


@dataclass(frozen=True, slots=True)
class WorkspaceResourceChanged:
    workspace: WorkspaceView


@dataclass(frozen=True, slots=True)
class WorkspaceResourceRemoved:
    workspace_id: str


@dataclass(frozen=True, slots=True)
class WorkspaceOrderChanged:
    workspace_id: str
    before_workspace_id: str | None


@dataclass(frozen=True, slots=True)
class ArchivedSessionsChanged:
    session_id: str
    archived: bool


@dataclass(frozen=True, slots=True)
class WorkspaceSessionOrderChanged:
    workspace_id: str
    session_id: str
    before_session_id: str | None


WORKSPACES = ServiceKey[WorkspacesPort]("workspaces")
WORKSPACE_EVENTS = ServiceKey[WorkspaceEventsPort]("workspace_events")
WORKSPACE_DIRECTORIES = ServiceKey[DirectoriesPort]("workspace_directories")
WORKSPACE_RESOURCE_CHANGED = EventKey[WorkspaceResourceChanged]("workspace/resource-changed")
WORKSPACE_RESOURCE_REMOVED = EventKey[WorkspaceResourceRemoved]("workspace/resource-removed")
WORKSPACE_SESSION_ORDER_CHANGED = EventKey[WorkspaceSessionOrderChanged]("workspace/session-order-changed")
WORKSPACE_ORDER_CHANGED = EventKey[WorkspaceOrderChanged]("workspace/order-changed")
ARCHIVED_SESSIONS_CHANGED = EventKey[ArchivedSessionsChanged]("workspace/archived-sessions-changed")

__all__ = [
    "WORKSPACES",
    "WORKSPACE_EVENTS",
    "WORKSPACE_DIRECTORIES",
    "ARCHIVED_SESSIONS_CHANGED",
    "ArchivedSessionsChanged",
    "DirectoriesPort",
    "DirectoryEntry",
    "DirectoryListing",
    "DirectoryNotFound",
    "DirectoryNotReadable",
    "WORKSPACE_ORDER_CHANGED",
    "WORKSPACE_RESOURCE_CHANGED",
    "WORKSPACE_RESOURCE_REMOVED",
    "WorkspaceOrderChanged",
    "WorkspaceEventsPort",
    "WorkspaceEventSubscription",
    "WorkspaceListing",
    "WorkspaceNotFound",
    "WorkspaceResourceChanged",
    "WorkspaceResourceRemoved",
    "WorkspaceSessionMoveInvalid",
    "WorkspaceSessionNotFound",
    "WorkspaceView",
    "WorkspacesPort",
]
