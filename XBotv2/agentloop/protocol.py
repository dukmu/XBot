"""Tool catalog routes: the enabled tool registry for one thread."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import Field, JsonValue
from XBotv2.protocol import WireModel
from XBotv2.agentloop.contracts import LIST_TOOLS
from XBotv2.core.operations import EmptyRequest

if TYPE_CHECKING:
    from XBotv2.session.contracts import SessionsPort


class ToolInfo(WireModel):
    name: str = Field(min_length=1)
    registered_name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    description: str
    parameters: dict[str, JsonValue]
    timeout_seconds: float | None = Field(default=None, gt=0)


class ToolListResponse(WireModel):
    tools: list[ToolInfo] = Field(default_factory=list)


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
        catalog = await sessions.dispatch(
            session_id, thread_id, LIST_TOOLS, EmptyRequest()
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


__all__ = [
    "ToolInfo",
    "ToolListResponse",
    "build_tools_router",
]
