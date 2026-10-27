"""Register subagent tools and prompt contributions."""

from __future__ import annotations
from XBotv2.agents.api.contracts import AGENT_CATALOG

from XBotv2.agentloop.api.contracts import TOOLS
from XBotv2.jobs.api.contracts import JOBS
from xcore.api.context import Context
from XBotv2.subagents.impl.service import SubagentCatalogPrompt, SubagentLauncher, SubagentTools
from XBotv2.context_builder.api.events import CONTEXT_COMPONENTS_BUILT
from XBotv2.core.api.tools import Tool
from XBotv2.subagents.api.contracts import SubagentsConfig


class SubagentsRuntimeComponent:
    inject = [
        "session",
        "sessions",
        AGENT_CATALOG,
        "session_launch",
        "no_plugins",
        JOBS,
        TOOLS,
    ]
    name = "xbot.subagents"

    def apply(
        self,
        ctx: Context,
        config: SubagentsConfig,
    ) -> None:
        timeout_seconds = config.timeout_seconds
        # The catalog is contributed per build (dynamic content), not as a
        # static prompt component registered from an out-of-apply listener.
        ctx.on(
            CONTEXT_COMPONENTS_BUILT,
            SubagentCatalogPrompt(ctx.require(AGENT_CATALOG)).contribute,
        )
        launcher = SubagentLauncher(
            catalog=ctx.require(AGENT_CATALOG),
            session=ctx.session,
            sessions=ctx.sessions,
            provider_name=ctx.session_launch.provider_name,
            no_plugins=ctx.no_plugins,
        )
        handlers = SubagentTools(
            registry=ctx.require(JOBS),
            launcher=launcher,
        )
        ctx.dispose(launcher.close)
        ctx.require(TOOLS).register(
            Tool.from_function(handlers.spawn_subagent),
            timeout_seconds=timeout_seconds,
        )
        for handler in (
            handlers.send_message,
            handlers.followup_task,
            handlers.list_subagents,
            handlers.wait_subagent,
            handlers.read_subagent,
            handlers.cancel_subagent,
        ):
            ctx.require(TOOLS).register(Tool.from_function(handler))


class SubagentsPlugin:
    """Mount collaboration tools on a session-owned Agent runtime."""

    name = "xbot.subagents"
    Config = SubagentsConfig

    async def apply(
        self,
        ctx: Context,
        config: SubagentsConfig,
    ) -> None:
        await ctx.plugin(SubagentsRuntimeComponent(), config)


plugin = SubagentsPlugin()

__all__ = ["SubagentsPlugin", "SubagentsConfig"]
