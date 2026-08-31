"""Register Jobs HTTP routes through XCore."""

from xcore import Context

from XBotv2.jobs.protocol import build_tasks_router
from XBotv2.server import contribute_router


class JobsHttpPlugin:
    name = "xbot.http.jobs"
    inject = ["server", "sessions"]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_tasks_router(sessions=ctx.sessions),
        )


plugin = JobsHttpPlugin()

__all__ = ["JobsHttpPlugin"]
