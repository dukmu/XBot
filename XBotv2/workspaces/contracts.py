"""Typed Workspace resource events emitted after durable commits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from XBotv2.workspaces.events import WorkspaceEventFrame


class WorkspaceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    path: str
    title: str
    session_ids: tuple[str, ...] = ()
    created_at: str
    updated_at: str


class WorkspaceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    items: tuple[WorkspaceRecord, ...] = ()
    archived_session_ids: tuple[str, ...] = ()


class WorkspaceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    session_ids: tuple[str, ...] = ()
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class WorkspaceListing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[WorkspaceView, ...] = ()
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
    async def list(self) -> WorkspaceListing: ...

    async def create(self, path: str) -> tuple[WorkspaceView, bool]: ...

    async def rename(self, workspace_id: str, title: str) -> WorkspaceView: ...

    async def delete(self, workspace_id: str) -> bool: ...

    async def insert_before(
        self,
        workspace_id: str,
        before_workspace_id: str | None,
    ) -> tuple[str, ...]: ...


class WorkspaceEventSubscription(Protocol):
    def __aiter__(self) -> "WorkspaceEventSubscription": ...
    async def __anext__(self) -> "WorkspaceEventFrame": ...
    async def aclose(self) -> None: ...


class WorkspaceEventsPort(Protocol):
    @property
    def sequence(self) -> int: ...

    def subscribe(self, after: int) -> WorkspaceEventSubscription: ...

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
    ) -> tuple[str, ...]: ...


WORKSPACE_RESOURCE_CHANGED = "workspace/resource-changed"
WORKSPACE_RESOURCE_REMOVED = "workspace/resource-removed"
WORKSPACE_ORDER_CHANGED = "workspace/order-changed"
ARCHIVED_SESSIONS_CHANGED = "workspace/archived-sessions-changed"


@dataclass(frozen=True, slots=True)
class WorkspaceResourceChanged:
    workspace: WorkspaceView


@dataclass(frozen=True, slots=True)
class WorkspaceResourceRemoved:
    workspace_id: str


@dataclass(frozen=True, slots=True)
class WorkspaceOrderChanged:
    workspace_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchivedSessionsChanged:
    session_ids: tuple[str, ...]


__all__ = [
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
    "WorkspaceRecord",
    "WorkspaceResourceChanged",
    "WorkspaceResourceRemoved",
    "WorkspaceSessionMoveInvalid",
    "WorkspaceSessionNotFound",
    "WorkspaceSnapshot",
    "WorkspaceView",
    "WorkspacesPort",
]
