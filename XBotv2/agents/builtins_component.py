"""Register bundled and data-root Agent definitions into the catalog."""

from __future__ import annotations

from typing import Any

from XBotv2.agents.builtins import BUILTIN_AGENT_DEFINITIONS
from XBotv2.agents.loader import load_definitions


class BuiltinAgentsComponent:
    name = "xbot.agents.builtins"
    inject = ["agent_catalog", "data_root", "variables"]

    def apply(self, ctx: Any, config: Any = None) -> None:
        definitions = {
            definition.name: definition for definition in BUILTIN_AGENT_DEFINITIONS
        }
        definitions.update({
            definition.name: definition
            for definition in load_definitions(
                ctx.data_root / ".agents",
                ctx.variables,
            )
        })
        for definition in definitions.values():
            ctx.agent_catalog.register(definition)


plugin = BuiltinAgentsComponent()
