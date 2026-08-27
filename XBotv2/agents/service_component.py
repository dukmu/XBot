"""Publish the session-scoped active Agent runtime."""

from __future__ import annotations

from xcore import Context

from XBotv2.agents.commands import build_agent_commands
from XBotv2.agents.contracts import (
    AgentCatalog,
    AgentSelection,
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
        "artifacts",
        "loop_state",
        "commands",
        "agent_options",
        "thread_metadata",
        "runtime_log",
    ]

    async def apply(self, ctx: Context, config: object | None = None) -> None:
        service = AgentsService(
            catalog=ctx.agent_catalog,
            factory=ctx.agent_loop_factory,
            events=ctx,
            state=ctx.loop_state,
            settings=ctx.settings,
            providers=ctx.llm,
            model=ctx.model,
            tools=ctx.tools,
            artifacts=ctx.artifacts,
            metadata=ctx.thread_metadata,
            runtime_log=ctx.runtime_log,
        )
        ctx.set("agent_runtime", service)
        engine = await service.create(ctx.agent_options)
        ctx.set("engine", engine)
        AgentRuntimeOperations(service, ctx.agent_catalog).register(ctx)


plugin = AgentRuntimeComponent()
