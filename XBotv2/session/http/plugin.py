"""Register Session HTTP routes through XCore."""

from __future__ import annotations

from xcore import Context

from XBotv2.server import contribute_router
from XBotv2.session.protocol import (
    _session_not_found,
    _thread_not_active,
    build_session_router,
)
from XBotv2.session.types import SessionNotFound, ThreadNotActive


class SessionHttpPlugin:
    name = "xbot.http.session"
    inject = ["server", "sessions", "server_options", "workspace_events"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_session_router(
                sessions=ctx.sessions,
                options=ctx.server_options,
                workspace_events=ctx.workspace_events,
            ),
            exception_handlers=(
                (SessionNotFound, _session_not_found),
                (ThreadNotActive, _thread_not_active),
            ),
        )


plugin = SessionHttpPlugin()

__all__ = ["SessionHttpPlugin"]
