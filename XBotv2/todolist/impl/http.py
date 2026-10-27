"""Todo projection route backed by the plugin-owned typed operation."""

from __future__ import annotations
from XBotv2.agentloop.api.contracts import AGENT_LOOP

from fastapi import APIRouter, Query
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.session.api.contracts import SessionsPort
from XBotv2.todolist.api.service import (
    GET_TODOS,
    GET_COMPLETED_TODOS,
    CompletedTasksRequest,
    CompletedTasksPage,
)
from XBotv2.todolist.api.models import TaskList


def build_router(*, sessions: SessionsPort) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/todos",
        operation_id="get_todos",
    )
    async def get_todos(session_id: str, thread_id: str) -> TaskList:
        return (
            await (await sessions.locate(session_id, thread_id))
            .require(AGENT_LOOP)
            .invoke(GET_TODOS, EmptyRequest())
        )

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/todos/completed",
        operation_id="get_completed_todos",
    )
    async def get_completed_todos(
        session_id: str,
        thread_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=400),
    ) -> CompletedTasksPage:
        request = CompletedTasksRequest(after=after, limit=limit)
        return (
            await (await sessions.locate(session_id, thread_id))
            .require(AGENT_LOOP)
            .invoke(GET_COMPLETED_TODOS, request)
        )

    return router
