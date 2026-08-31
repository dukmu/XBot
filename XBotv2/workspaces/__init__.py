"""First-class process Workspace resources."""

from XBotv2.workspaces.contracts import (
    ARCHIVED_SESSIONS_CHANGED,
    ArchivedSessionsChanged,
    WORKSPACE_ORDER_CHANGED,
    WORKSPACE_RESOURCE_CHANGED,
    WORKSPACE_RESOURCE_REMOVED,
    WorkspaceOrderChanged,
    WorkspaceResourceChanged,
    WorkspaceResourceRemoved,
)
from XBotv2.workspaces.models import WorkspaceListing, WorkspaceRecord, WorkspaceSnapshot, WorkspaceView
from XBotv2.workspaces.service import (
    WorkspaceNotFound,
    WorkspaceRegistry,
    WorkspaceSessionMoveInvalid,
    WorkspaceSessionNotFound,
)

__all__ = [
    "ARCHIVED_SESSIONS_CHANGED",
    "ArchivedSessionsChanged",
    "WORKSPACE_ORDER_CHANGED",
    "WORKSPACE_RESOURCE_CHANGED",
    "WORKSPACE_RESOURCE_REMOVED",
    "WorkspaceNotFound",
    "WorkspaceListing",
    "WorkspaceRecord",
    "WorkspaceRegistry",
    "WorkspaceSnapshot",
    "WorkspaceSessionMoveInvalid",
    "WorkspaceSessionNotFound",
    "WorkspaceOrderChanged",
    "WorkspaceResourceChanged",
    "WorkspaceResourceRemoved",
    "WorkspaceView",
]
