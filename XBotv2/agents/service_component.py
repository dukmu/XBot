"""Publish the session-scoped active Agent runtime."""

from __future__ import annotations

from typing import Any

from XBotv2.agents.service import AgentsService
from XBotv2.agents.commands import build_agent_commands
from XBotv2.agents.contracts import (
    AgentCatalog,
    AgentInitialized,
    AgentSelection,
    INITIALIZE_AGENT,
    LIST_AGENTS,
    RELOAD_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
from XBotv2.agents.contracts import AgentCreateOptions
from XBotv2.agentloop import Events
from XBotv2.core.operations import EmptyRequest
from XBotv2.llm import (
    EffortSelection,
    ProviderSelection,
    SELECT_EFFORT,
    SELECT_PROVIDER,
    SelectEffort,
    SelectProvider,
)


class AgentRuntimeComponent:
    name = "xbot.agents.runtime"
    inject = [
        "agent_catalog",
        "agent_loop_factory",
        "settings",
        "llm",
        "model",
        "tools",
        "loop_state",
        "loader",
        "commands",
    ]

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = AgentsService(
            ctx,
            catalog=ctx.agent_catalog,
            factory=ctx.agent_loop_factory,
        )
        ctx.set("agent_runtime", service)
        for command in build_agent_commands(service, ctx.agent_catalog):
            ctx.commands.register(command)
        ctx.on(Events.SOFT_RELOAD, service.rebind_on_soft_reload)
        engine: Any = None

        async def initialize(request: AgentCreateOptions) -> AgentInitialized:
            nonlocal engine
            engine = await service.create(request)
            return AgentInitialized(
                active=engine.settings.agent_name,
                provider=engine.settings.provider,
                model=engine.settings.model,
                model_mode=engine.settings.model_mode,
                context_window=engine.context_window,
            )

        def catalog() -> AgentCatalog:
            if engine is None:
                raise RuntimeError("Agent runtime is not initialized")
            return AgentCatalog(
                active=engine.settings.agent_name,
                agents=tuple(
                    definition
                    for definition in ctx.agent_catalog.definitions()
                    if not definition.hidden
                ),
            )

        def list_agents(
            _request: EmptyRequest,
        ) -> AgentCatalog:
            return catalog()

        async def select_agent(
            request: SelectAgent,
        ) -> AgentSelection:
            data = await service.select(request.name)
            return AgentSelection(
                active=data["active"],
                provider=data["provider"],
                model=data["model"],
                model_mode=data["model_mode"],
                context_window=data["context_window"],
            )

        async def reload_agents(
            _request: EmptyRequest,
        ) -> AgentCatalog:
            await service.reload_active()
            return catalog()

        async def select_provider(request: SelectProvider) -> ProviderSelection:
            data = await service.select_provider(request.name, model=request.model)
            return ProviderSelection(
                provider=data["provider"],
                model=data["model"],
                model_mode=data["model_mode"],
            )

        async def select_effort(request: SelectEffort) -> EffortSelection:
            data = await service.select_effort(request.effort)
            return EffortSelection(
                provider=data["provider"],
                model=data["model"],
                reasoning_effort=data["reasoning_effort"],
                model_mode=data["model_mode"],
                available=tuple(data["available"]),
            )

        ctx.on(INITIALIZE_AGENT.name, initialize)
        ctx.on(LIST_AGENTS.name, list_agents)
        ctx.on(SELECT_AGENT.name, select_agent)
        ctx.on(RELOAD_AGENTS.name, reload_agents)
        ctx.on(SELECT_PROVIDER.name, select_provider)
        ctx.on(SELECT_EFFORT.name, select_effort)


plugin = AgentRuntimeComponent()
