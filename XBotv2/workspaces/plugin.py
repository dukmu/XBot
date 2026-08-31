"""Provide the process Workspace registry through XCore dependencies."""

from __future__ import annotations

from xcore import Context

from XBotv2.session.contracts import (
    SESSION_RESOURCE_CHANGED,
    SESSION_RESOURCE_REMOVED,
    SessionResourceChanged,
    SessionResourceRemoved,
)
from XBotv2.workspaces.service import WorkspaceRegistry
from XBotv2.workspaces.contracts import (
    ARCHIVED_SESSIONS_CHANGED,
    WORKSPACE_ORDER_CHANGED,
    WORKSPACE_RESOURCE_CHANGED,
    WORKSPACE_RESOURCE_REMOVED,
)
from XBotv2.workspaces.events import WorkspaceCatalogChange, WorkspaceEventStream


class WorkspaceSessionHandlers:
    def __init__(self, registry: WorkspaceRegistry) -> None:
        self._registry = registry

    async def changed(self, event: SessionResourceChanged) -> None:
        await self._registry.attach_session(
            event.session.session_id,
            event.session.workspace_root,
        )

    async def removed(self, event: SessionResourceRemoved) -> None:
        await self._registry.detach_session(event.session_id)


class WorkspaceCatalogHandlers:
    def __init__(self, stream: WorkspaceEventStream) -> None:
        self._stream = stream

    def publish(self, event: WorkspaceCatalogChange) -> None:
        self._stream.publish(event)


class WorkspacesPlugin:
    name = "xbot.workspaces"
    inject = ["runtime_log", "sessions", "state", "workspace_root"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        stream = WorkspaceEventStream()
        registry = WorkspaceRegistry(
            ctx.state.namespace("workspaces"),
            ctx.sessions,
            ctx,
            ctx.runtime_log,
        )
        await registry.ensure(ctx.workspace_root)
        handlers = WorkspaceSessionHandlers(registry)
        catalog = WorkspaceCatalogHandlers(stream)
        ctx.on(SESSION_RESOURCE_CHANGED, handlers.changed)
        ctx.on(SESSION_RESOURCE_REMOVED, handlers.removed)
        ctx.on(SESSION_RESOURCE_CHANGED, catalog.publish)
        ctx.on(SESSION_RESOURCE_REMOVED, catalog.publish)
        ctx.on(WORKSPACE_RESOURCE_CHANGED, catalog.publish)
        ctx.on(WORKSPACE_RESOURCE_REMOVED, catalog.publish)
        ctx.on(WORKSPACE_ORDER_CHANGED, catalog.publish)
        ctx.on(ARCHIVED_SESSIONS_CHANGED, catalog.publish)
        ctx.set("workspaces", registry)
        ctx.set("workspace_events", stream)


plugin = WorkspacesPlugin()

__all__ = [
    "WorkspaceCatalogHandlers",
    "WorkspaceSessionHandlers",
    "WorkspacesPlugin",
]
