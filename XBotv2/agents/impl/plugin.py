"""Compose Agent catalog, runtime, and HTTP integration."""

from __future__ import annotations
from XBotv2.commands.api.contracts import COMMANDS
from XBotv2.session.api.storage import THREAD_REPOSITORY
from XBotv2.permissions.api.models import PERMISSIONS

from XBotv2.agentloop.api.contracts import INBOX, TOOLS
from XBotv2.agentloop.api.state import LOOP_STATE
from pathlib import Path
from pydantic import JsonValue
from xcore.api.context import Context
from XBotv2.agents.impl.builtins import BUILTIN_AGENT_DEFINITIONS
from XBotv2.agents.impl.catalog import AgentCatalog
from XBotv2.agents.impl.commands import build_agent_commands
from XBotv2.agents.api.contracts import (
    AGENT_CATALOG,
    AGENT_RUNTIME,
    AgentCatalog as AgentCatalogData,
    AgentCatalogPort,
    AgentSelection,
    LIST_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
from XBotv2.agents.impl.loader import load_definitions
from XBotv2.agents.impl.router import build_router
from XBotv2.agents.impl.service import AgentsService
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.config.api.contracts import SETTINGS
from XBotv2.llm.api.contracts import LLM, MODEL
from XBotv2.llm.api.contracts import (
    EffortSelection,
    ProviderSelection,
    SELECT_EFFORT,
    SELECT_PROVIDER,
    SelectEffort,
    SelectProvider,
)
from XBotv2.server.api.contracts import contribute_router


_CATALOG_DEPENDENCIES = [
    "data_root",
    "variables",
    "workspace_root",
]
_RUNTIME_DEPENDENCIES = [
    AGENT_CATALOG,
    SETTINGS,
    # A conversational runtime must not become usable before its tool policy
    # guard is installed; a failed policy plugin is not an allow-all mode.
    PERMISSIONS,
    LLM,
    MODEL,
    TOOLS,
    "artifacts",
    LOOP_STATE,
    # The inbox is composed by agentloop after state restoration; requiring it
    # here builds the engine only after restored input is on hand, with no
    # reliance on plugin-tree order.
    INBOX,
    THREAD_REPOSITORY,
    COMMANDS,
    "agent_options",
    "thread_info",
    "runtime_log",
]


def mount_catalog(ctx: Context) -> None:
    catalog = AgentCatalog()
    definitions = {
        definition.name: definition for definition in BUILTIN_AGENT_DEFINITIONS
    }
    definitions.update(
        {
            definition.name: definition
            for definition in load_definitions(
                Path(ctx.data_root) / ".agents",
                ctx.variables,
            )
        }
    )
    for definition in definitions.values():
        catalog.register(definition)
    catalog.register_markdown(
        Path(ctx.workspace_root) / ".agents",
        variables=ctx.variables,
        overlay=True,
    )
    ctx.set(AGENT_CATALOG, catalog)


async def mount_runtime(ctx: Context) -> None:
    repository = ctx.require(THREAD_REPOSITORY)
    service = AgentsService(
        catalog=ctx.require(AGENT_CATALOG),
        events=ctx,
        state=ctx.require(LOOP_STATE),
        settings=ctx.require(SETTINGS),
        providers=ctx.require(LLM),
        model=ctx.require(MODEL),
        tools=ctx.require(TOOLS),
        runtime_log=ctx.runtime_log,
        stored_facts=await repository.load(),
        session_title=await repository.session_title(),
    )
    ctx.set(AGENT_RUNTIME, service)
    await service.bind(ctx.agent_options, ctx.thread_info)
    AgentRuntimeOperations(service, ctx.require(AGENT_CATALOG)).register(ctx)
    ctx.on("ready", service.announce_initialized)


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
            ctx.require(COMMANDS).register(command)
        ctx.on(LIST_AGENTS.name, self.list_agents)
        ctx.on(SELECT_AGENT.name, self.select_agent)
        ctx.on(SELECT_PROVIDER.name, self.select_provider)
        ctx.on(SELECT_EFFORT.name, self.select_effort)


def mount_thread(ctx, config):
    mount_catalog(ctx)
    ctx.inject(_RUNTIME_DEPENDENCIES, mount_runtime)


mount_thread.inject = tuple(_CATALOG_DEPENDENCIES)
mount_thread.provide = (AGENT_CATALOG, AGENT_RUNTIME)


def mount_process(ctx, config):
    ctx.inject(["server", "sessions"], mount_http)


mount_process.inject = ()
mount_process.provide = ()
