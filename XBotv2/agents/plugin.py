"""Compose Agent catalog, runtime, and HTTP integration."""

from __future__ import annotations

from pathlib import Path
from pydantic import JsonValue

from xcore import Context

from XBotv2.agents.builtins import BUILTIN_AGENT_DEFINITIONS
from XBotv2.agents.catalog import AgentCatalog
from XBotv2.agents.commands import build_agent_commands
from XBotv2.agents.contracts import (
    AgentCatalog as AgentCatalogData,
    AgentCatalogPort,
    AgentSelection,
    LIST_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
from XBotv2.agents.loader import load_definitions
from XBotv2.agents.protocol import build_router
from XBotv2.agents.service import AgentsService
from XBotv2.core.operations import EmptyRequest
from XBotv2.llm import (
    EffortSelection,
    ProviderSelection,
    SELECT_EFFORT,
    SELECT_PROVIDER,
    SelectEffort,
    SelectProvider,
)
from XBotv2.server import contribute_router


_CATALOG_DEPENDENCIES = [
    "data_root",
    "variables",
    "workspace_root",
    "session_launch",
]
_RUNTIME_DEPENDENCIES = [
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


def mount_catalog(ctx: Context) -> None:
    catalog = AgentCatalog()
    definitions = {
        definition.name: definition for definition in BUILTIN_AGENT_DEFINITIONS
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


async def mount_runtime(ctx: Context) -> None:
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
    ctx.set("engine", await service.create(ctx.agent_options))
    AgentRuntimeOperations(service, ctx.agent_catalog).register(ctx)


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.agents.http",
        router=build_router(sessions=ctx.sessions),
    )


class AgentRuntimeOperations:
    """Bind typed operations and commands to one active Agent runtime."""

    def __init__(self, service: AgentsService, catalog: AgentCatalogPort) -> None:
        self._service = service
        self._catalog = catalog

    def list_agents(self, _request: EmptyRequest) -> AgentCatalogData:
        return AgentCatalogData(
            active=self._service.current_selection().active,
            agents=tuple(
                definition
                for definition in self._catalog.definitions()
                if not definition.hidden
            ),
        )

    async def select_agent(self, request: SelectAgent) -> AgentSelection:
        return await self._service.select(request.name)

    async def select_provider(self, request: SelectProvider) -> ProviderSelection:
        return await self._service.select_provider(request.name, model=request.model)

    async def select_effort(self, request: SelectEffort) -> EffortSelection:
        return await self._service.select_effort(request.effort)

    def register(self, ctx: Context) -> None:
        for command in build_agent_commands(self._service, self._catalog):
            ctx.commands.register(command)
        ctx.on(LIST_AGENTS.name, self.list_agents)
        ctx.on(SELECT_AGENT.name, self.select_agent)
        ctx.on(SELECT_PROVIDER.name, self.select_provider)
        ctx.on(SELECT_EFFORT.name, self.select_effort)


class AgentsPlugin:
    """Activate each Agent integration when its services are available."""

    name = "xbot.agents"

    async def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        await ctx.inject(_CATALOG_DEPENDENCIES, mount_catalog)
        await ctx.inject(_RUNTIME_DEPENDENCIES, mount_runtime)
        await ctx.inject(["server", "sessions"], mount_http)


plugin = AgentsPlugin()

__all__ = ["AgentsPlugin"]
