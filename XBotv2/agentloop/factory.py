"""Concrete factory for the core ReAct loop driver."""

from __future__ import annotations

from xcore import Context

from XBotv2.agentloop.engine import Engine
from XBotv2.agentloop.services import LoopFactoryOptions
from XBotv2.core.runtime_logging import RuntimeLog


class AgentLoopFactory:
    """Construct Engine only from already-resolved core ports."""

    def __init__(self, runtime_log: RuntimeLog) -> None:
        self._runtime_log = runtime_log

    def create(self, options: LoopFactoryOptions) -> Engine:
        engine = Engine(
            model_client=options.model_client,
            tools=options.tools,
            events=options.events,
            state=options.state,
            settings=options.settings,
            max_iterations=options.max_iterations,
            runtime_log=self._runtime_log,
        )
        return engine


class AgentLoopFactoryComponent:
    name = "xbot.agentloop.factory"
    inject = ["runtime_log"]

    def apply(self, ctx: Context, config: object | None = None) -> None:
        ctx.set("agent_loop_factory", AgentLoopFactory(ctx.runtime_log))
