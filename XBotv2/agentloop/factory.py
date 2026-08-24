"""Concrete factory for the core ReAct loop driver."""

from __future__ import annotations

from xcore import Context

from XBotv2.agentloop.engine import Engine
from XBotv2.agentloop.services import LoopFactoryOptions


class AgentLoopFactory:
    """Construct Engine only from already-resolved core ports."""

    def create(self, options: LoopFactoryOptions) -> Engine:
        engine = Engine(
            model_client=options.model_client,
            tools=options.tools,
            events=options.events,
            state=options.state,
            settings=options.settings,
            max_iterations=options.max_iterations,
        )
        return engine


class AgentLoopFactoryComponent:
    name = "xbot.agentloop.factory"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        ctx.set("agent_loop_factory", AgentLoopFactory())


plugin = AgentLoopFactoryComponent()
