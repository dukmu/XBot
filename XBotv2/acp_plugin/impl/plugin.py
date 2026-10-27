"""XCore component for the ACP stdio carrier."""

from __future__ import annotations

from xcore.api.context import Context
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.acp_plugin.api.contracts import ACPConfig, ACP_SERVER
from XBotv2.acp_plugin.impl.xbot_agent import XBotACPAgent


class ACPComponent:
    name = "xbot.acp"
    inject = ["sessions", "runtime_log", ARCHIVE]
    Config = ACPConfig

    def apply(self, ctx: Context, config: ACPConfig) -> None:
        agent = XBotACPAgent(
            sessions=ctx.sessions,
            archive=ctx.require(ARCHIVE),
            provider_name=config.provider_name,
            no_plugins=config.no_plugins,
            selected_agent=config.selected_agent,
            runtime_log=ctx.runtime_log,
        )
        ctx.provide(ACP_SERVER, agent)
        ctx.pre_stop(agent.close)


def mount_process(ctx, config):
    ACPComponent().apply(ctx, config)


mount_process.inject = getattr(ACPComponent, "inject", ())
mount_process.provide = (ACP_SERVER,)
mount_process.Config = getattr(ACPComponent, "Config", None)
