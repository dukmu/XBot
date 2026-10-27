"""Compose the Agent loop runtime, tool service, and HTTP integration."""

from __future__ import annotations
from XBotv2.agentloop.impl.client_http import build_router as build_client_router
from XBotv2.server.api.contracts import contribute_router
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.session.api.storage import THREAD_REPOSITORY
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.agentloop.impl.storage import ConversationStore
from XBotv2.agentloop.impl.archive import ArchiveService
from XBotv2.agentloop.impl.archive_http import build_router as archive_router

from XBotv2.agentloop.api.contracts import (
    AGENT_LOOP,
    INBOX,
    TOOLS,
    LIST_TOOLS,
    ToolCatalog,
    ToolDescription,
)
from pydantic import JsonValue
from xcore.api.context import Context
from XBotv2.agentloop.impl.engine import Engine
from XBotv2.agentloop.impl.execution import AgentLoop
from XBotv2.agentloop.impl.commands import build_loop_commands
from XBotv2.commands.api.contracts import COMMANDS
from XBotv2.agents.api.contracts import AGENT_RUNTIME
from XBotv2.config.api.contracts import SETTINGS
from XBotv2.llm.api.contracts import MODEL
from XBotv2.permissions.api.models import PERMISSIONS
from XBotv2.session.api.contracts import THREAD_EVENTS
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.agentloop.impl.state import LoopRuntimeState
from XBotv2.agentloop.impl.history import ConversationHistory
from XBotv2.agentloop.impl.inbox import AgentInbox
from XBotv2.session.api.contracts import SessionKey
from XBotv2.agentloop.impl.protocol import build_tools_router
from XBotv2.agentloop.impl.tool_registry import ToolRegistry
from XBotv2.agentloop.impl.tool_service import ToolsService
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.server.api.contracts import contribute_router


async def mount_state(ctx: Context) -> None:
    """Create and hydrate the authoritative thread working set and inbox."""
    launch = ctx.thread_info
    repository = ctx.require(THREAD_REPOSITORY)
    store = ConversationStore(
        ctx.require(PERSISTENCE), launch.session_id, launch.thread_id, repository
    )
    state = LoopRuntimeState(
        ctx,
        key=SessionKey(launch.session_id, launch.thread_id),
        variables=ctx.variables,
    )
    state.set_history(ConversationHistory(sink=store.history))
    await state.history.synchronize()
    state.restore_turn_count(await store.history.count_turns())
    state.restore_resumed(await repository.persisted())
    pending_inputs = await store.inbox.load()
    inbox_sink = store.inbox
    ctx.provide(
        INBOX,
        AgentInbox(
            events=ctx,
            sink=inbox_sink,
            items=pending_inputs,
            artifacts=ctx.artifacts,
        ),
    )


def mount_tools(ctx: Context) -> None:
    registry = ToolRegistry()
    tools = ToolsService(registry, events=ctx, runtime_log=ctx.runtime_log, owner=ctx)
    ctx.provide(TOOLS, tools)
    from XBotv2.agentloop.impl.archive_tools import ArchiveTools
    from XBotv2.core.api.tools import Tool
    from dataclasses import replace
    archive_tools = ArchiveTools(ctx.require(ARCHIVE), ctx.thread_info)
    tools.register(replace(Tool.from_function(archive_tools.archive_records), kind="read"))
    ctx.pre_stop(tools.close)
    ctx.on(LIST_TOOLS.name, ToolsCatalogHandler(registry).list_tools)


async def mount_engine(ctx):
    state = ctx.require(LOOP_STATE)
    settings = ctx.require(SETTINGS)
    selection = state.metadata.value.runtime_selection
    engine = Engine(
        model_client=ctx.require(MODEL),
        tools=ctx.require(TOOLS),
        events=ctx,
        state=state,
        user_identity=settings.user_context(),
        memory=settings.memory(),
        max_iterations=selection.limits.max_turns,
        runtime_log=ctx.runtime_log,
        inbox=ctx.require(INBOX),
        artifacts=ctx.artifacts,
    )
    runner = AgentLoop(_context=ctx, _engine=engine, interactive=ctx.interactive)
    ctx.provide(AGENT_LOOP, runner)
    ctx.pre_stop(runner.drain)
    for command in build_loop_commands(runner):
        ctx.require(COMMANDS).register(command)
    await engine.start_session()


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.agentloop.http",
        router=build_tools_router(sessions=ctx.sessions),
    )


class ToolsCatalogHandler:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def list_tools(self, _request: EmptyRequest) -> ToolCatalog:
        enabled = set(self._registry.names())
        return ToolCatalog(
            tools=tuple(
                ToolDescription(
                    name=entry.tool.name,
                    registered_name=entry.registered_name,
                    namespace=entry.namespace,
                    description=entry.tool.description,
                    parameters=dict(entry.tool.parameters),
                    timeout_seconds=entry.timeout_seconds,
                )
                for entry in self._registry.registered_entries()
                if entry.model_visible and entry.registered_name in enabled
            )
        )


def mount_thread(ctx, config):
    mount_tools(ctx)
    ctx.inject(
        ["thread_info", "variables", THREAD_REPOSITORY, PERSISTENCE, "artifacts"], mount_state
    )
    ctx.inject(
        [
            "artifacts",
            AGENT_RUNTIME,
            LOOP_STATE,
            INBOX,
            TOOLS,
            SETTINGS,
            MODEL,
            PERMISSIONS,
            THREAD_EVENTS,
            "interactive",
            COMMANDS,
        ],
        mount_engine,
    )


mount_thread.inject = ("runtime_log", "thread_info", ARCHIVE)
mount_thread.provide = (TOOLS, INBOX, LOOP_STATE, AGENT_LOOP)


def mount_process(ctx, config):
    archive = ArchiveService(ctx.require(PERSISTENCE), ctx.sessions)
    ctx.provide(ARCHIVE, archive)
    from XBotv2.session.api.storage import FORK_COPY_STATE, FORK_FINALIZE

    ctx.on(FORK_COPY_STATE, archive.copy_state)
    ctx.on(FORK_FINALIZE, archive.finalize_fork)
    ctx.inject(["server", ARCHIVE], mount_archive_http)
    ctx.inject(["server", "sessions", PERSISTENCE], mount_client_http)
    ctx.inject(["server", "sessions"], mount_http)


mount_process.inject = (PERSISTENCE, "sessions")
mount_process.provide = (ARCHIVE,)


async def mount_client_http(ctx):
    await contribute_router(
        ctx,
        owner="xbot.agentloop.client-http",
        router=build_client_router(sessions=ctx.sessions),
    )


async def mount_archive_http(ctx):
    await contribute_router(
        ctx,
        owner="xbot.agentloop.archive-http",
        router=archive_router(sessions=ctx.sessions, archive=ctx.require(ARCHIVE)),
    )
