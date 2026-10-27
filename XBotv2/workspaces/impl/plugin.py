"""Provide the process Workspace registry through XCore dependencies."""

from __future__ import annotations

from pydantic import JsonValue
from xcore.api.context import Context
from XBotv2.session.api.contracts import SESSION_RESOURCE_CHANGED, SESSION_RESOURCE_REMOVED, SessionResourceChanged, SessionResourceRemoved
from XBotv2.workspaces.impl.service import WorkspaceRegistry
from XBotv2.workspaces.api.contracts import (
    WORKSPACES, WORKSPACE_EVENTS, WORKSPACE_DIRECTORIES,
    ARCHIVED_SESSIONS_CHANGED,
    WORKSPACE_ORDER_CHANGED,
    WORKSPACE_SESSION_ORDER_CHANGED,
    WORKSPACE_RESOURCE_CHANGED,
    WORKSPACE_RESOURCE_REMOVED,
)
from XBotv2.workspaces.api.events import WorkspaceCatalogChange
from XBotv2.workspaces.impl.events import WorkspaceEventStream
from XBotv2.workspaces.impl.directories import DirectoryBrowser
from XBotv2.workspaces.impl.protocol import build_router
from XBotv2.server.api.contracts import contribute_router


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.workspaces.http",
        router=build_router(
            workspaces=ctx.require(WORKSPACES),
            workspace_events=ctx.require(WORKSPACE_EVENTS),
            directories=ctx.require(WORKSPACE_DIRECTORIES),
        ),
    )


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

    async def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        stream = WorkspaceEventStream()
        registry = WorkspaceRegistry(
            ctx.state.namespace("workspaces"),
            ctx.sessions,
            ctx,
            ctx.runtime_log,
        )
        await registry.ensure(ctx.workspace_root)
        await registry.reconcile()
        handlers = WorkspaceSessionHandlers(registry)
        catalog = WorkspaceCatalogHandlers(stream)
        ctx.on(SESSION_RESOURCE_CHANGED, handlers.changed)
        ctx.on(SESSION_RESOURCE_REMOVED, handlers.removed)
        ctx.on(SESSION_RESOURCE_CHANGED, catalog.publish)
        ctx.on(SESSION_RESOURCE_REMOVED, catalog.publish)
        ctx.on(WORKSPACE_RESOURCE_CHANGED, catalog.publish)
        ctx.on(WORKSPACE_RESOURCE_REMOVED, catalog.publish)
        ctx.on(WORKSPACE_ORDER_CHANGED, catalog.publish)
        ctx.on(WORKSPACE_SESSION_ORDER_CHANGED, catalog.publish)
        ctx.on(ARCHIVED_SESSIONS_CHANGED, catalog.publish)
        ctx.set(WORKSPACES, registry)
        ctx.set(WORKSPACE_EVENTS, stream)
        ctx.set(WORKSPACE_DIRECTORIES, DirectoryBrowser(ctx.workspace_root))
        ctx.inject(
            ["server", WORKSPACES, WORKSPACE_EVENTS, WORKSPACE_DIRECTORIES],
            mount_http,
        )


async def mount_process(ctx, config):
    await WorkspacesPlugin().apply(ctx, config)


mount_process.inject = getattr(WorkspacesPlugin, "inject", ())
mount_process.provide = ('workspaces', 'workspace_events', 'workspace_directories')
mount_process.Config = getattr(WorkspacesPlugin, "Config", None)
