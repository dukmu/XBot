"""Compose the Agent loop runtime, tool service, and HTTP integration."""

from __future__ import annotations

from pydantic import JsonValue
from xcore import Context

from XBotv2.agentloop.contracts import LIST_TOOLS, ToolCatalog, ToolDescription
from XBotv2.agentloop.factory import AgentLoopFactory
from XBotv2.agentloop.protocol import build_tools_router
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.tool_service import ToolsService
from XBotv2.core.operations import EmptyRequest
from XBotv2.server import contribute_router


def mount_tools(ctx: Context) -> None:
    registry = ToolRegistry()
    ctx.set("tools", ToolsService(registry, events=ctx, runtime_log=ctx.runtime_log))
    ctx.on(LIST_TOOLS.name, ToolsCatalogHandler(registry).list_tools)


def mount_loop_factory(ctx: Context) -> None:
    ctx.set("agent_loop_factory", AgentLoopFactory(ctx.runtime_log))


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
        await ctx.inject(["runtime_log", "session_launch"], mount_tools)
        await ctx.inject(["runtime_log", "session_launch"], mount_loop_factory)
        await ctx.inject(["server", "sessions"], mount_http)


plugin = AgentLoopPlugin()

__all__ = ["AgentLoopPlugin"]
