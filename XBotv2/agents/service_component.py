"""Publish the session-scoped active Agent runtime."""

from __future__ import annotations

from xcore import Context

from XBotv2.agents.commands import build_agent_commands
from XBotv2.agents.contracts import (
    AgentCatalog,
    AgentCreateOptions,
    AgentInitialized,
    AgentSelection,
    INITIALIZE_AGENT,
    LIST_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
from XBotv2.agents.service import AgentsService
from XBotv2.agents.services import AgentCatalogPort
from XBotv2.core.operations import EmptyRequest
from XBotv2.llm import (
    EffortSelection,
    ProviderSelection,
    SELECT_EFFORT,
    SELECT_PROVIDER,
    SelectEffort,
    SelectProvider,
)


class AgentRuntimeOperations:
    """Named operation handlers for one Agent runtime."""

    def __init__(
        self,
        service: AgentsService,
        catalog: AgentCatalogPort,
    ) -> None:
        self._service = service
        self._catalog = catalog

    async def initialize(self, request: AgentCreateOptions) -> AgentInitialized:
        engine = await self._service.create(request)
        return AgentInitialized(
            active=engine.settings.agent_name,
            provider=engine.settings.provider,
            model=engine.settings.model,
            model_mode=engine.settings.model_mode,
            context_window=engine.context_window,
        )

    def list_agents(self, _request: EmptyRequest) -> AgentCatalog:
        return AgentCatalog(
            active=self._service.current_selection().active,
            agents=tuple(
                definition
                for definition in self._catalog.definitions()
                if not definition.hidden
            ),
        )

    async def select_agent(self, request: SelectAgent) -> AgentSelection:
        data = await self._service.select(request.name)
        return AgentSelection(
            active=data["active"],
            provider=data["provider"],
            model=data["model"],
            model_mode=data["model_mode"],
            context_window=data["context_window"],
        )

    async def select_provider(self, request: SelectProvider) -> ProviderSelection:
        data = await self._service.select_provider(
            request.name,
            model=request.model,
        )
        return ProviderSelection(
            provider=data["provider"],
            model=data["model"],
            model_mode=data["model_mode"],
        )

    async def select_effort(self, request: SelectEffort) -> EffortSelection:
        data = await self._service.select_effort(request.effort)
        return EffortSelection(
            provider=data["provider"],
            model=data["model"],
            reasoning_effort=data["reasoning_effort"],
            model_mode=data["model_mode"],
            available=tuple(data["available"]),
        )

    def register(self, ctx: Context) -> None:
        for command in build_agent_commands(self._service, self._catalog):
            ctx.commands.register(command)
        ctx.on(INITIALIZE_AGENT.name, self.initialize)
        ctx.on(LIST_AGENTS.name, self.list_agents)
        ctx.on(SELECT_AGENT.name, self.select_agent)
        ctx.on(SELECT_PROVIDER.name, self.select_provider)
        ctx.on(SELECT_EFFORT.name, self.select_effort)


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
        "commands",
    ]

    def apply(self, ctx: Context, config: object | None = None) -> None:
        service = AgentsService(
            ctx,
            catalog=ctx.agent_catalog,
            factory=ctx.agent_loop_factory,
        )
        ctx.set("agent_runtime", service)
        AgentRuntimeOperations(service, ctx.agent_catalog).register(ctx)


plugin = AgentRuntimeComponent()
