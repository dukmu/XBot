"""Concrete factory for the core ReAct loop driver."""

from __future__ import annotations

from XBotv2.agentloop.engine import Engine
from XBotv2.agentloop.contracts import AgentLoopFactoryPort, LoopFactoryOptions
from XBotv2.core.runtime_logging import RuntimeLog


class AgentLoopFactory(AgentLoopFactoryPort):
    """Construct Engine only from already-resolved core ports."""

    def __init__(self, runtime_log: RuntimeLog) -> None:
        self._runtime_log = runtime_log

    def create(self, options: LoopFactoryOptions) -> Engine:
        engine = Engine(
            model_client=options.model_client,
            tools=options.tools,
            events=options.events,
            state=options.state,
            user_identity=options.user_identity,
            memory=options.memory,
            max_iterations=options.max_iterations,
            runtime_log=self._runtime_log,
            inbox=options.inbox,
        )
        return engine
