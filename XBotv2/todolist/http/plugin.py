"""Register Todo HTTP routes through XCore."""

from xcore import Context

from XBotv2.server import contribute_router
from XBotv2.todolist.protocol import build_router


class TodolistHttpPlugin:
    name = "xbot.http.todolist"
    inject = ["server", "sessions"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_router(sessions=ctx.sessions),
        )


plugin = TodolistHttpPlugin()

__all__ = ["TodolistHttpPlugin"]
