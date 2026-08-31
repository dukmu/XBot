"""Register configuration HTTP routes through XCore."""

from xcore import Context

from XBotv2.config.protocol import build_router
from XBotv2.server import contribute_router


class ConfigHttpPlugin:
    name = "xbot.http.config"
    inject = ["server", "sessions"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_router(sessions=ctx.sessions),
        )


plugin = ConfigHttpPlugin()

__all__ = ["ConfigHttpPlugin"]
