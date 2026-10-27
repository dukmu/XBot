"""Compose the Agent loop runtime, tool service, and HTTP integration."""

from __future__ import annotations

from XBotv2.agentloop.api.contracts import (
    AGENT_LOOP_FACTORY,
    INBOX,
    TOOLS,
    LIST_TOOLS,
    ToolCatalog,
    ToolDescription,
)
from pydantic import JsonValue
from xcore.api.context import Context
from XBotv2.agentloop.impl.factory import AgentLoopFactory
from XBotv2.agentloop.impl.state import LoopRuntimeState
from XBotv2.agentloop.impl.history import ConversationHistory
from XBotv2.agentloop.impl.inbox import AgentInbox, EphemeralInboxSink
from XBotv2.session.contracts import SessionKey
from XBotv2.agentloop.impl.protocol import build_tools_router
from XBotv2.agentloop.impl.tool_registry import ToolRegistry
from XBotv2.agentloop.impl.tool_service import ToolsService
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.server.api.contracts import contribute_router


async def mount_state(ctx: Context) -> None:
    """Create and hydrate the authoritative thread working set and inbox."""
    launch = ctx.session_launch
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
    ctx.provide(TOOLS, ToolsService(registry, events=ctx, runtime_log=ctx.runtime_log))
    ctx.on(LIST_TOOLS.name, ToolsCatalogHandler(registry).list_tools)


def mount_loop_factory(ctx: Context) -> None:
    ctx.provide(AGENT_LOOP_FACTORY, AgentLoopFactory(ctx.runtime_log))


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


class AgentLoopPlugin:
    """Activate Agent-loop integrations at their declared boundaries."""

    name = "xbot.agentloop"

    async def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        await ctx.inject({
            "required": ["session_launch", "variables"],
            "optional": ["thread_persistence"],
        }, mount_state)
        await ctx.inject(["runtime_log", "session_launch"], mount_tools)
        await ctx.inject(["runtime_log", "session_launch"], mount_loop_factory)
        await ctx.inject(["server", "sessions"], mount_http)


plugin = AgentLoopPlugin()
