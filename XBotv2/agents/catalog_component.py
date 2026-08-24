"""Load and provide the complete Agent definition catalog."""

from __future__ import annotations

from pathlib import Path

from xcore import Context

from XBotv2.agents.builtins import BUILTIN_AGENT_DEFINITIONS
from XBotv2.agents.catalog import AgentCatalog
from XBotv2.agents.loader import load_definitions


class AgentCatalogComponent:
    name = "xbot.agents.catalog"
    inject = ["data_root", "variables", "workspace_root"]

    def apply(self, ctx: Context, config: object | None = None) -> None:
        catalog = AgentCatalog()
        definitions = {
            definition.name: definition
            for definition in BUILTIN_AGENT_DEFINITIONS
        }
        definitions.update({
            definition.name: definition
            for definition in load_definitions(
                Path(ctx.data_root) / ".agents",
                ctx.variables,
            )
        })
        for definition in definitions.values():
            catalog.register(definition)
        catalog.register_markdown(
            Path(ctx.workspace_root) / ".agents",
            variables=ctx.variables,
            overlay=True,
        )
        ctx.set("agent_catalog", catalog)


plugin = AgentCatalogComponent()
