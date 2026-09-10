"""First-class process Workspace resources."""

from XBotv2.workspaces.contracts import (
    ARCHIVED_SESSIONS_CHANGED,
    ArchivedSessionsChanged,
    WORKSPACE_ORDER_CHANGED,
    WORKSPACE_RESOURCE_CHANGED,
    WORKSPACE_RESOURCE_REMOVED,
    WorkspaceOrderChanged,
    WorkspaceListing,
    WorkspaceNotFound,
    WorkspaceRecord,
    WorkspaceResourceChanged,
    WorkspaceResourceRemoved,
    WorkspaceSessionMoveInvalid,
    WorkspaceSessionNotFound,
    WorkspaceSnapshot,
    WorkspaceView,
    WorkspacesPort,
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
    "WorkspaceSnapshot",
    "WorkspaceSessionMoveInvalid",
    "WorkspaceSessionNotFound",
    "WorkspaceOrderChanged",
    "WorkspaceResourceChanged",
    "WorkspaceResourceRemoved",
    "WorkspaceView",
    "WorkspacesPort",
]
