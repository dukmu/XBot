"""Tool catalog routes: the enabled tool registry for one thread."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from XBotv2.protocol.models import (
    ToolInfo,
    ToolListResponse,
)
from XBotv2.server.contracts import contribute_router
from XBotv2.agentloop.contracts import LIST_TOOLS
from XBotv2.core.operations import EmptyRequest
from XBotv2.session.contracts import SessionRef, dispatch_session_operation


def build_tools_router(*, events: Any) -> APIRouter:
    """Read-only tool catalog for the active thread."""

    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/tools",
        operation_id="list_tools",
    )
    async def list_tools_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ToolListResponse:
        catalog = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            LIST_TOOLS,
            EmptyRequest(),
        )
        return ToolListResponse(tools=[
            ToolInfo(
                name=tool.name,
                registered_name=tool.registered_name,
                namespace=tool.namespace,
                description=tool.description,
                parameters=tool.parameters,
                timeout_seconds=tool.timeout_seconds,
            )
            for tool in catalog.tools
        ])

    return router


class ToolsHttpAdapter:
    """Register the tools HTTP surface into ``ctx.web_server``.

    The tools capability owns its routes: when the server tree mounts this
    plugin, it registers ``build_tools_router`` into the dumb
    ``ctx.web_server`` carrier.  Registration is a fiber effect, undone on
    unload.
    """

    inject = ["server"]
    name = "xbot.http.tools"

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_tools_router(events=ctx),
        )


plugin = ToolsHttpAdapter()
