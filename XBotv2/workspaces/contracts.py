"""Typed Workspace resource events emitted after durable commits."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.workspaces.models import WorkspaceView


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
    "WORKSPACE_ORDER_CHANGED",
    "WORKSPACE_RESOURCE_CHANGED",
    "WORKSPACE_RESOURCE_REMOVED",
    "WorkspaceOrderChanged",
    "WorkspaceResourceChanged",
    "WorkspaceResourceRemoved",
]
