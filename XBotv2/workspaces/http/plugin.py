"""Register Workspace catalog HTTP routes through XCore."""

from __future__ import annotations

from xcore import Context

from XBotv2.server import contribute_router
from XBotv2.workspaces.protocol import build_router


class WorkspacesHttpPlugin:
    name = "xbot.http.workspaces"
    inject = ["server", "workspaces", "workspace_events"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_router(
                workspaces=ctx.workspaces,
                workspace_events=ctx.workspace_events,
            ),
        )


plugin = WorkspacesHttpPlugin()

__all__ = ["WorkspacesHttpPlugin"]
