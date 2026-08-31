"""Register Agent HTTP routes through XCore."""

from xcore import Context

from XBotv2.agents.protocol import build_router
from XBotv2.server import contribute_router


class AgentsHttpPlugin:
    name = "xbot.http.agents"
    inject = ["server", "sessions"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_router(sessions=ctx.sessions),
        )


plugin = AgentsHttpPlugin()

__all__ = ["AgentsHttpPlugin"]
