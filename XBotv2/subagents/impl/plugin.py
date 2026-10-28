"""Register subagent tools and prompt contributions."""

from __future__ import annotations
from XBotv2.agents.api.contracts import AGENT_CATALOG, AGENT_RUNTIME

from XBotv2.agentloop.api.contracts import TOOLS, INBOX
from XBotv2.agentloop.api.storage import ARCHIVE
from xcore.api.context import Context
from XBotv2.subagents.impl.service import SubagentCatalogPrompt, SubagentLauncher, SubagentTools
from XBotv2.context_builder.api.events import CONTEXT_COMPONENTS_BUILT
from XBotv2.core.api.tools import Tool
from XBotv2.subagents.api.contracts import SubagentsConfig, SUBAGENTS
from XBotv2.subagents.impl.service import SubagentCoordinator
from XBotv2.session.api.contracts import SESSION_INFO, SESSIONS


class SubagentsRuntimeComponent:
    inject = [
        SUBAGENTS,
        "thread_info",
        "sessions",
        AGENT_CATALOG, AGENT_RUNTIME,
        "thread_info",
        "no_plugins",
        ARCHIVE, INBOX,
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
            session=ctx.thread_info,
            sessions=ctx.sessions,
            coordinator=ctx.require(SUBAGENTS),
            provider_name=ctx.require(AGENT_RUNTIME).current_selection().provider,
            no_plugins=ctx.no_plugins,
            archive=ctx.require(ARCHIVE),
            inbox=ctx.require(INBOX),
        )
        handlers = SubagentTools(
            launcher=launcher,
        )
        ctx.pre_stop(launcher.close)
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
            handlers.close_subagent,
        ):
            ctx.require(TOOLS).register(Tool.from_function(handler))


async def mount_thread(ctx, config):
    await ctx.plugin(SubagentsRuntimeComponent(), config)

mount_thread.inject = ("thread_info",)
mount_thread.provide = ()
mount_thread.Config = SubagentsConfig


def mount_session(ctx, config):
    session = ctx.require(SESSION_INFO)
    sessions = ctx.require(SESSIONS)
    sessions.set_child_limit(session.session_id, config.max_live_threads)
    ctx.provide(SUBAGENTS, SubagentCoordinator(session, sessions))

mount_session.inject = (SESSION_INFO, SESSIONS)
mount_session.provide = (SUBAGENTS,)
mount_session.Config = SubagentsConfig
