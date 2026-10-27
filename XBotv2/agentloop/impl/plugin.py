"""Compose the Agent loop runtime, tool service, and HTTP integration."""

from __future__ import annotations
from XBotv2.agentloop.impl.client_http import build_router as build_client_router
from XBotv2.server.api.contracts import contribute_router
from XBotv2.persistence.api.contracts import PERSISTENCE

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
from XBotv2.agentloop.impl.inbox import AgentInbox, EphemeralInboxSink
from XBotv2.session.api.contracts import SessionKey
from XBotv2.agentloop.impl.protocol import build_tools_router
from XBotv2.agentloop.impl.tool_registry import ToolRegistry
from XBotv2.agentloop.impl.tool_service import ToolsService
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.server.api.contracts import contribute_router


async def mount_state(ctx: Context) -> None:
    """Create and hydrate the authoritative thread working set and inbox."""
    launch = ctx.thread_info
    persistence = ctx.get("thread_persistence", strict=False)
    state = LoopRuntimeState(
        ctx, key=SessionKey(launch.session_id, launch.thread_id),
        variables=ctx.variables,
    )
    if persistence is None:
        pending_inputs = ()
        inbox_sink = EphemeralInboxSink()
    else:
        state.set_history(ConversationHistory(
            persistence.history.load_surface(), sink=persistence.history,
        ))
        state.restore_turn_count(persistence.history.count_turns())
        state.restore_resumed(persistence.has_persisted_state())
        stored_metadata = persistence.metadata.load()
        if stored_metadata is not None:
            await state.metadata.initialize(stored_metadata)
        pending_inputs = persistence.inbox.reconcile(
            persistence.history.committed_input_ids(),
        )
        inbox_sink = persistence.inbox
    ctx.provide(INBOX, AgentInbox(
        events=ctx, sink=inbox_sink, items=pending_inputs,
    ))


def mount_tools(ctx: Context) -> None:
    registry = ToolRegistry()
    tools = ToolsService(registry, events=ctx, runtime_log=ctx.runtime_log, owner=ctx)
    ctx.provide(TOOLS, tools)
    ctx.pre_stop(tools.close)
    ctx.on(LIST_TOOLS.name, ToolsCatalogHandler(registry).list_tools)


async def mount_engine(ctx):
    state = ctx.require(LOOP_STATE)
    settings = ctx.require(SETTINGS)
    selection = state.metadata.value.runtime_selection
    engine = Engine(
        model_client=ctx.require(MODEL), tools=ctx.require(TOOLS), events=ctx,
        state=state, user_identity=settings.user_context(), memory=settings.memory(),
        max_iterations=selection.limits.max_turns,
        runtime_log=ctx.runtime_log, inbox=ctx.require(INBOX),
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
        return ToolCatalog(tools=tuple(
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
        ))


def mount_thread(ctx, config):
    mount_tools(ctx)
    ctx.inject(["thread_info", "variables", "thread_persistence"], mount_state)
    ctx.inject([AGENT_RUNTIME, LOOP_STATE, INBOX, TOOLS, SETTINGS, MODEL, PERMISSIONS, THREAD_EVENTS, "interactive", COMMANDS], mount_engine)

mount_thread.inject = ("runtime_log", "thread_info")
mount_thread.provide = (TOOLS, INBOX, LOOP_STATE, AGENT_LOOP)


def mount_process(ctx, config):
    ctx.inject(["server", "sessions", PERSISTENCE], mount_client_http)
    ctx.inject(["server", "sessions"], mount_http)

mount_process.inject = ()
mount_process.provide = ()


async def mount_client_http(ctx):
    await contribute_router(ctx, owner="xbot.agentloop.client-http", router=build_client_router(sessions=ctx.sessions, persistence=ctx.require(PERSISTENCE)))
