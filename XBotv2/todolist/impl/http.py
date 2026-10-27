"""Todo projection route backed by the plugin-owned typed operation."""

from __future__ import annotations
from XBotv2.agentloop.api.contracts import AGENT_LOOP

from fastapi import APIRouter
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.session.api.contracts import SessionsPort
from XBotv2.todolist.api.service import GET_TODOS
from XBotv2.todolist.api.models import TaskList


def build_router(*, sessions: SessionsPort) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/todos",
        operation_id="get_todos",
    )
    async def get_todos(session_id: str, thread_id: str) -> TaskList:
        return await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).invoke(GET_TODOS, EmptyRequest())

    return router
