"""Register core HTTP routes through XCore."""

from xcore import Context

from XBotv2.server import contribute_router
from XBotv2.server.protocol import build_core_router


class CoreHttpPlugin:
    name = "xbot.http.core"
    inject = ["server", "server_info"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_core_router(events=ctx, info=ctx.server_info),
        )


plugin = CoreHttpPlugin()

__all__ = ["CoreHttpPlugin"]
