"""Tool catalog routes: the enabled tool registry for one thread."""

from __future__ import annotations
from XBotv2.agentloop.api.contracts import AGENT_LOOP

from typing import TYPE_CHECKING
from fastapi import APIRouter
from XBotv2.agentloop.api.protocol import ToolInfo, ToolListResponse
from XBotv2.agentloop.api.contracts import LIST_TOOLS
from XBotv2.core.api.operations import EmptyRequest

if TYPE_CHECKING:
    from XBotv2.session.api.contracts import SessionsPort


def build_tools_router(*, sessions: "SessionsPort") -> APIRouter:
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
        catalog = await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).invoke(LIST_TOOLS, EmptyRequest())
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
