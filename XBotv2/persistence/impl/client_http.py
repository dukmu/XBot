"""HTTP routes owned by persistence."""
from __future__ import annotations
from XBotv2.session.api.records import conversation_replay
from urllib.parse import quote
from fastapi import APIRouter, Query
from fastapi.responses import Response
from XBotv2.core.api.domain import Cursor
from XBotv2.core.api.history import HistoryPage, TrajectoryRead
from XBotv2.session.api.records import ConversationRecord

def build_router(*, sessions, persistence):
    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/messages",
        operation_id="list_messages",
    )
    async def list_messages_endpoint(
        session_id: str,
        thread_id: str,
        cursor: str | None = None,
        limit: int | None = Query(default=None, ge=1, le=500),
    ) -> HistoryPage[ConversationRecord]:
        await sessions.thread_summary(session_id, thread_id)
        page = persistence.message_page(
            session_id,
            thread_id,
            cursor=Cursor(cursor) if cursor is not None else None,
            limit=limit,
        )
        return HistoryPage(
            items=conversation_replay(page.items),
            older_cursor=page.older_cursor,
        )
    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/trajectory",
        operation_id="list_trajectory",
    )
    async def list_trajectory_endpoint(
        session_id: str,
        thread_id: str,
        cursor: str | None = None,
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=160, ge=1, le=500),
    ) -> TrajectoryRead:
        await sessions.thread_summary(session_id, thread_id)
        page = persistence.trajectory_page(
            session_id,
            thread_id,
            cursor=Cursor(cursor) if cursor is not None else None,
            limit=limit,
            before=before,
        )
        return page
    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/artifacts/{artifact_id:path}",
        operation_id="get_artifact",
    )
    async def get_artifact_endpoint(
        session_id: str,
        thread_id: str,
        artifact_id: str,
    ) -> Response:
        await sessions.thread_summary(session_id, thread_id)
        artifact = persistence.artifact(session_id, thread_id, artifact_id)
        headers = (
            {
                "Content-Disposition": (
                    "inline; filename*=UTF-8''" + quote(artifact.name)
                )
            }
            if artifact.name
            else None
        )
        return Response(
            content=artifact.content,
            media_type=artifact.media_type,
            headers=headers,
        )

    return router
