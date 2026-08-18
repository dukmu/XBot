"""Publish the mandatory Agent registry and creation seam."""

from __future__ import annotations

from typing import Any

from XBotv2.agents.service import AgentsService


class AgentsServiceComponent:
    name = "xbot.agents.service"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("agents", AgentsService(ctx))


plugin = AgentsServiceComponent()
