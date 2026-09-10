"""Register subagent tools and prompt contributions."""

from __future__ import annotations

from xcore import Context

from XBotv2.subagents.service import (
    SubagentCatalogPrompt,
    SubagentLauncher,
    SubagentTools,
)
from XBotv2.application import APPLICATION_INITIALIZED
from XBotv2.core import Tool
from XBotv2.subagents.contracts import SubagentsConfig


class SubagentsRuntimeComponent:
    inject = [
        "session",
        "agent_catalog",
        "child_applications",
        "permissions",
        "client_events",
        "jobs",
        "tools",
        "prompts",
        "thread_persistence",
    ]
    name = "xbot.subagents"

    def apply(
        self,
        ctx: Context,
        config: SubagentsConfig,
    ) -> None:
        timeout_seconds = config.timeout_seconds
        ctx.on(
            APPLICATION_INITIALIZED,
            SubagentCatalogPrompt(ctx.agent_catalog, ctx.prompts).publish,
        )
        handlers = SubagentTools(
            registry=ctx.jobs,
            catalog=ctx.agent_catalog,
            launcher=SubagentLauncher(
                catalog=ctx.agent_catalog,
                session=ctx.session,
                children=ctx.child_applications,
                lifecycle=ctx.thread_persistence.lifecycle,
                parent_permissions=ctx.permissions,
                client_events=ctx.client_events,
            ),
        )
        ctx.tools.register(
            Tool.from_function(handlers.spawn_subagent),
            timeout_seconds=timeout_seconds,
        )
        for handler in (
            handlers.list_subagents,
            handlers.wait_subagent,
            handlers.read_subagent,
            handlers.cancel_subagent,
        ):
            ctx.tools.register(Tool.from_function(handler))


class SubagentsPlugin:
    """Mount subagent support only when thread persistence is available."""

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
