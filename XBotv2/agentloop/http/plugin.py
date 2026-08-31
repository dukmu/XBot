"""Register Tool catalog HTTP routes through XCore."""

from xcore import Context

from XBotv2.agentloop.protocol import build_tools_router
from XBotv2.server import contribute_router


class ToolsHttpPlugin:
    name = "xbot.http.tools"
    inject = ["server", "sessions"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_tools_router(sessions=ctx.sessions),
        )


plugin = ToolsHttpPlugin()

__all__ = ["ToolsHttpPlugin"]
