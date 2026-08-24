"""Provide the Agent definition catalog independently of Session runtime."""

from __future__ import annotations

from xcore import Context

from XBotv2.agents.catalog import AgentCatalog


class AgentCatalogComponent:
    name = "xbot.agents.catalog"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        ctx.set("agent_catalog", AgentCatalog())


plugin = AgentCatalogComponent()
