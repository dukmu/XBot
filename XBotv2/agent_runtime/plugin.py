"""Agent runtime component: provides ``ctx.agent_runtime`` (subagent factory)."""

from __future__ import annotations

from typing import Any


class AgentRuntimeComponent:
    """Create the EngineAgentRuntime and register it as a service.

    Disabled for subagent runtimes (the agents plugin is disabled there too).
    Configured by the plugin tree entry; requires the tools component
    (``ctx.agents``) and is mounted after it.
    """

    name = "xbot.agent_runtime"

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        if not config.get("enabled", False):
            return
        from core.agents import EngineAgentRuntime

        ctx.set(
            "agent_runtime",
            EngineAgentRuntime(
                registry=ctx.agents.registry,
                session_paths=config["session_paths"],
                parent_thread_id=config["parent_thread_id"],
                engine_factory=config["engine_factory"],
            ),
        )


plugin = AgentRuntimeComponent()
