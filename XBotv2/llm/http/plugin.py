"""Register LLM HTTP routes through XCore."""

from xcore import Context

from XBotv2.llm.protocol import build_router
from XBotv2.server import contribute_router


class LlmHttpPlugin:
    name = "xbot.http.llm"
    inject = ["server", "llm", "sessions"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_router(events=ctx, sessions=ctx.sessions),
        )


plugin = LlmHttpPlugin()

__all__ = ["LlmHttpPlugin"]
