"""Provide the Agent definition catalog independently of Session runtime."""

from __future__ import annotations

from typing import Any

from XBotv2.agents.catalog import AgentCatalog


class AgentCatalogComponent:
    name = "xbot.agents.catalog"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("agent_catalog", AgentCatalog())


plugin = AgentCatalogComponent()
