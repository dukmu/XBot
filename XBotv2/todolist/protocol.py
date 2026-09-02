"""Todo projection route backed by the plugin-owned typed operation."""

from __future__ import annotations

from fastapi import APIRouter

from XBotv2.core.operations import EmptyRequest
from XBotv2.session.services import SessionsPort
from XBotv2.todolist.contracts import GET_TODOS
from XBotv2.todolist.models import TodoSnapshot


def build_router(*, sessions: SessionsPort) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/todos",
        operation_id="get_todos",
    )
    async def get_todos(session_id: str, thread_id: str) -> TodoSnapshot:
        return await sessions.dispatch(
            session_id, thread_id, GET_TODOS, EmptyRequest()
        )

    return router


__all__ = ["build_router"]
