"""Session routes: sessions, threads, messages, history, fork, events,
close, interrupt, and interactions.

Session lifecycle and archive remain behind the public SessionsPort;
this module owns only HTTP request/response and SSE mapping.
"""

from __future__ import annotations
from XBotv2.session.api.records import conversation_replay
from XBotv2.session.api.contracts import THREAD_EVENTS

import logging
from pathlib import Path
from traceback import format_exception_only
from typing import AsyncIterator
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, JsonValue
from XBotv2.protocol.api.http_util import (
    _SSE_RESPONSE,
    error_payload,
    _sse_response,
    HttpServerError,
)
from XBotv2.protocol.api.models import ServerEvent
from XBotv2.protocol.api.sse import encode_server_event
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.history import HistoryPage
from XBotv2.session.impl.config import SessionConfig
from XBotv2.session.api.events import MessagePublishedEvent
from XBotv2.session.api.contracts import (
    SessionsPort,
    OpenedThread,
    OpenSession,
    OpenThread,
    SessionExists,
    SessionEventSubscription,
    SessionEventFrame,
    SessionEventCursorExpired,
    SessionNotFound,
    SessionMode,
    SessionSummary,
    ThreadNotActive,
    ThreadSummary,
)
from XBotv2.session.api.records import ConversationRecord
from XBotv2.session.api.snapshot import thread_snapshot

logger = logging.getLogger("xbotv2.api")


from XBotv2.session.api.protocol import (
    OpenSessionRequest,
    ThreadListResponse,
    SessionListResponse,
    WorkspaceEventCursor,
    SessionUpdateRequest,
    OpenThreadRequest,
    UndoRequest,
    ForkResponse,
    DeleteSessionResponse,
    MessageRequest,
    InterruptResponse,
    CloseResponse,
    OpenSessionResponse,
)


def _open_session_response(
    value: OpenedThread,
    history: HistoryPage[ConversationRecord],
) -> OpenSessionResponse:
    value = value.model_copy(update={"history": history})
    return OpenSessionResponse(data=value)


async def _requested_history_page(
    archive,
    session_id: str,
    thread_id: str,
    limit: int | None,
) -> HistoryPage[ConversationRecord]:
    page = await archive.message_page(
        session_id,
        thread_id,
        cursor=None,
        limit=limit,
    )
    return HistoryPage(
        items=conversation_replay(page.items),
        older_cursor=page.older_cursor,
    )


def _session_event_payload(event: object) -> dict[str, JsonValue]:
    """Encode a typed internal event at the sole SSE boundary."""
    from XBotv2.agentloop.api.outputs import (
        AssistantCompleted,
        ToolCompleted,
        UsageObserved,
        is_loop_event,
    )
    from XBotv2.session.api.records import project_message

    if isinstance(event, MessagePublishedEvent):
        return event.record.root.model_dump(mode="json")
    if isinstance(event, AssistantCompleted):
        return project_message(event.message).model_dump(mode="json")
    if isinstance(event, ToolCompleted):
        return project_message(event.execution.message).model_dump(mode="json")
    if isinstance(event, UsageObserved):
        return event.usage.model_dump(mode="json")
    if is_loop_event(event):
        return event.model_dump(mode="json", exclude={"kind"})
    if isinstance(event, BaseModel):
        return event.model_dump(mode="json", exclude={"kind"})
    raise TypeError(f"Unsupported session event: {type(event).__name__}")


async def _session_sse(
    events: SessionEventSubscription,
    session_id: str,
    thread_id: str,
) -> AsyncIterator[bytes]:
    try:
        async for frame in events:
            yield _format_sse(frame, session_id=session_id, thread_id=thread_id)
    finally:
        await events.aclose()


def _format_sse(
    frame: SessionEventFrame,
    *,
    session_id: str,
    thread_id: str,
) -> bytes:
    """Encode a complete typed session frame at the HTTP boundary."""
    return encode_server_event(
        ServerEvent(
            session_id=session_id,
            thread_id=thread_id,
            sequence=frame.sequence,
            scope=frame.scope,
            kind=frame.event.kind,
            payload=_session_event_payload(frame.event),
        )
    )


async def _session_not_found(_: Request, exc: SessionNotFound) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content=error_payload("session_not_found", str(exc)),
    )


async def _thread_not_active(_: Request, exc: ThreadNotActive) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=error_payload("thread_not_active", str(exc), retryable=True),
    )


def build_session_router(
    *,
    sessions: SessionsPort,
    options: SessionConfig,
    workspace_events: WorkspaceEventCursor,
    archive,
) -> APIRouter:
    """Session, thread, message, history, fork, event, and policy routes."""

    router = APIRouter()

    @router.post("/sessions", operation_id="open_session")
    async def open_session(
        payload: OpenSessionRequest,
    ) -> OpenSessionResponse:
        raw_session_id = (payload.session_id or "").strip() or None
        thread_id = payload.thread_id.strip() or "main"
        if (
            payload.mode == "new"
            and raw_session_id is not None
            and sessions.session_exists(raw_session_id)
        ):
            raise HttpServerError(
                "session_exists",
                raw_session_id,
                status=409,
            )
        workspace_root = payload.workspace_root
        if workspace_root is None and payload.mode == "new":
            workspace_root = str(options.workspace_root)
        try:
            opened_scope = await sessions.open(
                OpenSession(
                    session_id=raw_session_id,
                    thread_id=thread_id,
                    provider_name=options.provider_name,
                    workspace_root=workspace_root,
                    mode=payload.mode,
                    selected_agent=payload.agent,
                    no_plugins=options.no_plugins,
                )
            )
            opened = await thread_snapshot(opened_scope)
        except SessionNotFound as exc:
            raise HttpServerError("session_not_found", str(exc), status=404) from exc
        except SessionExists as exc:
            raise HttpServerError("session_exists", str(exc), status=409) from exc
        except OperationError as exc:
            raise HttpServerError(
                exc.code,
                exc.message,
                status=409
                if exc.code in {"session_in_use", "session_closing"}
                else 404
                if exc.code.endswith("_not_found")
                else 400,
                retryable=exc.retryable,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Session open failed for %s", raw_session_id or "<new>")
            raise HttpServerError(
                "session_open_failed",
                "".join(format_exception_only(exc)).strip(),
                status=500,
            ) from exc
        page = await _requested_history_page(
            archive,
            opened.key.session_id,
            opened.key.thread_id,
            payload.history_limit,
        )
        return _open_session_response(opened, page)

    @router.get("/sessions", operation_id="list_sessions")
    async def list_sessions_endpoint() -> SessionListResponse:
        event_cursor = workspace_events.sequence
        return SessionListResponse(
            sessions=list(await sessions.list_sessions()),
            event_cursor=event_cursor,
        )

    @router.get("/sessions/{session_id}", operation_id="get_session")
    async def get_session_endpoint(session_id: str) -> SessionSummary:
        return await sessions.session_summary(session_id)

    @router.patch("/sessions/{session_id}", operation_id="rename_session")
    async def rename_session_endpoint(
        session_id: str,
        payload: SessionUpdateRequest,
    ) -> SessionSummary:
        try:
            return await sessions.rename_session(session_id, payload.title)
        except ValueError as exc:
            raise HttpServerError(
                "invalid_session_title", str(exc), status=400
            ) from exc

    @router.post(
        "/sessions/{session_id}/fork",
        operation_id="fork_session",
    )
    async def fork_session_endpoint(session_id: str) -> ForkResponse:
        forked_id = await sessions.fork_session(session_id)
        return ForkResponse(
            session_id=forked_id,
            source_session_id=session_id,
        )

    @router.delete(
        "/sessions/{session_id}",
        operation_id="delete_session",
    )
    async def delete_session_endpoint(session_id: str) -> DeleteSessionResponse:
        await sessions.delete_session(session_id)
        return DeleteSessionResponse(session_id=session_id)

    @router.get(
        "/sessions/{session_id}/threads",
        operation_id="list_threads",
    )
    async def list_threads_endpoint(session_id: str) -> ThreadListResponse:
        return ThreadListResponse(
            session_id=session_id,
            threads=list(await sessions.list_threads(session_id)),
        )

    @router.post(
        "/sessions/{session_id}/threads",
        operation_id="open_thread",
    )
    async def open_thread_endpoint(
        session_id: str,
        payload: OpenThreadRequest,
    ) -> OpenSessionResponse:
        try:
            opened_scope = await sessions.open_thread(
                OpenThread(
                    session_id=session_id,
                    thread_id=payload.thread_id,
                    parent_thread_id=payload.parent_thread_id,
                    provider_name=options.provider_name,
                    workspace_root=payload.workspace_root,
                    mode=payload.mode,
                    selected_agent=payload.agent,
                    no_plugins=options.no_plugins,
                )
            )
            opened = await thread_snapshot(opened_scope)
        except SessionNotFound as exc:
            raise HttpServerError("session_not_found", str(exc), status=404) from exc
        except SessionExists as exc:
            raise HttpServerError("session_exists", str(exc), status=409) from exc
        page = await _requested_history_page(
            archive,
            opened.key.session_id,
            opened.key.thread_id,
            payload.history_limit,
        )
        return _open_session_response(opened, page)

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}",
        operation_id="get_thread",
    )
    async def get_thread_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ThreadSummary:
        return await sessions.thread_summary(session_id, thread_id)

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/events",
        operation_id="stream_events",
        response_class=StreamingResponse,
        responses=_SSE_RESPONSE,
    )
    async def session_events(
        session_id: str,
        thread_id: str,
        after: int | None = Query(default=None, ge=0),
    ) -> Response:
        try:
            events = (
                (await sessions.locate(session_id, thread_id))
                .require(THREAD_EVENTS)
                .subscribe(after)
            )
        except SessionEventCursorExpired as exc:
            raise HttpServerError(
                "session_event_cursor_expired",
                str(exc),
                status=409,
                details={"oldest_sequence": exc.oldest},
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise HttpServerError(
                "invalid_session_event_cursor",
                str(exc),
                status=400,
            ) from exc

        return _sse_response(
            _session_sse(events, session_id, thread_id),
        )

    @router.post(
        "/sessions/{session_id}/close",
        operation_id="close_session",
    )
    async def shutdown_session(session_id: str) -> CloseResponse:
        await sessions.close_session(session_id)
        return CloseResponse(session_id=session_id)

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/close",
        operation_id="close_thread",
    )
    async def close_thread(session_id: str, thread_id: str) -> CloseResponse:
        await sessions.close_thread(session_id, thread_id)
        return CloseResponse(session_id=session_id, thread_id=thread_id)

    return router
