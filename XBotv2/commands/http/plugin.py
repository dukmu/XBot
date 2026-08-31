"""Register Command HTTP routes through XCore."""

from xcore import Context

from XBotv2.commands.protocol import build_commands_router
from XBotv2.server import contribute_router


class CommandsHttpPlugin:
    name = "xbot.http.commands"
    inject = ["server", "sessions"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_commands_router(sessions=ctx.sessions),
        )


plugin = CommandsHttpPlugin()

__all__ = ["CommandsHttpPlugin"]
