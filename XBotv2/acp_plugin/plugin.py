"""XCore component for the ACP stdio carrier."""

from __future__ import annotations

from pydantic import JsonValue
from xcore import Context

from XBotv2.acp_plugin.xbot_agent import XBotACPAgent


class ACPComponent:
    name = "xbot.acp"
    inject = ["sessions", "acp_launch", "runtime_log"]

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        launch = ctx.acp_launch
        agent = XBotACPAgent(
            sessions=ctx.sessions,
            provider_name=launch.provider_name,
            no_plugins=launch.no_plugins,
            selected_agent=launch.selected_agent,
            llm_override=launch.llm_override,
            runtime_log=ctx.runtime_log,
        )
        ctx.set("acp_agent", agent)
        ctx.dispose(agent.close)


plugin = ACPComponent()
