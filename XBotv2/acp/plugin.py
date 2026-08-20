"""XCore component for the ACP stdio carrier."""

from __future__ import annotations

from typing import Any

from XBotv2.acp.xbot_agent import XBotACPAgent


class ACPComponent:
    name = "xbot.acp"
    inject = ["sessions", "acp_launch"]

    def apply(self, ctx: Any, config: Any = None) -> None:
        launch = ctx.acp_launch
        agent = XBotACPAgent(
            sessions=ctx.sessions,
            provider_name=launch.provider_name,
            no_plugins=launch.no_plugins,
            selected_agent=launch.selected_agent,
            llm_override=launch.llm_override,
        )
        ctx.set("acp_agent", agent)
        ctx.dispose(agent.close)


plugin = ACPComponent()
