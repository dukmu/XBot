"""Todo projection route backed by the plugin-owned typed operation."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import Field

from XBotv2.core.operations import EmptyRequest
from XBotv2.protocol import WireModel
from XBotv2.server import contribute_router
from XBotv2.session import SessionsPort
from XBotv2.todolist.contracts import GET_TODOS


class TodoItemData(WireModel):
    content: str = Field(min_length=1)
    status: Literal["pending", "in_progress", "completed"]


class TodoResponse(WireModel):
    schema_version: Literal[1] = 1
    items: list[TodoItemData] = Field(default_factory=list)


def build_router(*, sessions: SessionsPort) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/todos",
        operation_id="get_todos",
    )
    async def get_todos(session_id: str, thread_id: str) -> TodoResponse:
        snapshot = await sessions.dispatch(
            session_id, thread_id, GET_TODOS, EmptyRequest()
        )
        return TodoResponse.model_validate(snapshot.to_dict())

    return router


class TodolistProtocolPlugin:
    inject = ["server", "sessions"]
    name = "xbot.protocol.todolist"

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_router(sessions=ctx.sessions),
        )


plugin = TodolistProtocolPlugin()


__all__ = ["TodoItemData", "TodoResponse", "build_router"]
