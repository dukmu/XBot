"""HTTP routes owned by agentloop."""
from __future__ import annotations
from XBotv2.interactions.api.contracts import CLIENT_EVENTS, InteractionNotPending
from XBotv2.interactions.api.models import Answered
from XBotv2.permissions.api.models import Allowed, Denied
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.session.api.contracts import THREAD_EVENTS
from XBotv2.agentloop.api.models import InterruptResult
import logging
import uuid
from collections.abc import Awaitable
from pathlib import Path
from traceback import format_exception_only
from typing import AsyncIterator, Literal, Protocol, TypeAlias
from urllib.parse import quote
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, JsonValue, model_validator
from XBotv2.protocol.api.http_util import _SSE_RESPONSE, error_payload, _sse_response, HttpServerError
from XBotv2.interactions.api.protocol import InteractionResponse, UserInputResponseRequest
from XBotv2.permissions.api.protocol import PermissionResponseRequest
from XBotv2.protocol.api.models import ResourceResponse, ServerEvent, WireModel
from XBotv2.protocol.api.sse import encode_server_event
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.domain import Cursor
from XBotv2.core.api.history import HistoryPage, TrajectoryRead
from XBotv2.session.api.events import MessagePublishedEvent
from XBotv2.session.api.contracts import SessionsPort, InteractionReceipt, OpenedThread, OpenSession, OpenThread, SessionExists, SessionEventSubscription, SessionEventFrame, SessionEventCursorExpired, SessionNotFound, SessionMode, SessionSummary, ThreadNotActive, ThreadSummary
from XBotv2.agentloop.api.models import AttachmentInput, HistoryMutation, ImageInput, PendingInputData, PendingInputUpdate, RegenerateMessage, SendMessage
from XBotv2.session.api.records import ConversationRecord
from XBotv2.session.api.snapshot import thread_snapshot
from XBotv2.session.api.protocol import (
    OpenSessionRequest, ThreadListResponse, SessionListResponse, WorkspaceEventCursor, SessionUpdateRequest, OpenThreadRequest, UndoRequest, RegenerateRequest, ForkResponse, DeleteSessionResponse, PendingInputListResponse, PendingInputUpdateRequest, MessageRequest, InterruptResponse, CloseResponse, OpenSessionResponse
)

def build_router(*, sessions, persistence=None):
    router = APIRouter()

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/history/clear",
        operation_id="clear_thread_history",
    )
    async def clear_thread_history(
        session_id: str,
        thread_id: str,
        history_limit: int | None = Query(default=None, ge=1, le=500),
    ) -> HistoryMutation:
        scope = await sessions.locate(session_id, thread_id)
        return await scope.require(AGENT_LOOP).clear_history(history_limit=history_limit)
    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/history/undo",
        operation_id="undo_thread_history",
    )
    async def undo_thread_history(
        session_id: str,
        thread_id: str,
        payload: UndoRequest,
    ) -> HistoryMutation:
        scope = await sessions.locate(session_id, thread_id)
        return await scope.require(AGENT_LOOP).undo_history(payload.count, history_limit=payload.history_limit)
    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/history/regenerate",
        operation_id="regenerate_message",
        status_code=202,
        responses={202: {"description": "Command accepted; events are delivered on the session stream."}},
    )
    async def regenerate_message_endpoint(
        session_id: str,
        thread_id: str,
        payload: RegenerateRequest,
    ) -> Response:
        request_id = payload.request_id.strip() or f"req-{uuid.uuid4().hex}"
        await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).regenerate(request_id)
        return Response(status_code=202)
    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/messages",
        operation_id="send_message",
        status_code=202,
        responses={202: {"description": "Command accepted; events are delivered on the session stream."}},
    )
    async def post_message(
        session_id: str,
        thread_id: str,
        payload: MessageRequest,
    ) -> Response:
        content = payload.content
        client_request_id = payload.request_id.strip() or f"req-{uuid.uuid4().hex}"
        message = SendMessage(
            session_id=session_id,
            thread_id=thread_id,
            content=content,
            request_id=client_request_id,
            delivery=payload.delivery,
            images=tuple(payload.images),
            attachments=tuple(payload.attachments),
        )
        try:
            await (await sessions.locate(message.session_id, message.thread_id)).require(AGENT_LOOP).submit_message(message)
        except ValueError as exc:
            raise HttpServerError("invalid_request", str(exc), status=400) from exc
        return Response(status_code=202)
    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/queue",
        operation_id="list_pending_inputs",
    )
    async def list_pending_inputs_endpoint(
        session_id: str,
        thread_id: str,
    ) -> PendingInputListResponse:
        items = (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).pending_inputs()
        return PendingInputListResponse(
            session_id=session_id,
            thread_id=thread_id,
            items=list(items),
        )
    @router.patch(
        "/sessions/{session_id}/threads/{thread_id}/queue/{message_id}",
        operation_id="update_pending_input",
    )
    async def update_pending_input_endpoint(
        session_id: str,
        thread_id: str,
        message_id: str,
        payload: PendingInputUpdateRequest,
    ) -> PendingInputListResponse:
        try:
            items = await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).update_pending_input(message_id, payload.action, payload.content)
        except OperationError as exc:
            raise HttpServerError(exc.code, exc.message, status=404) from exc
        return PendingInputListResponse(
            session_id=session_id,
            thread_id=thread_id,
            items=list(items),
        )
    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/interrupt",
        operation_id="interrupt_thread",
    )
    async def interrupt_session(
        session_id: str,
        thread_id: str,
    ) -> InterruptResponse:
        result = InterruptResult(cancelled=await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).interrupt())
        if not result.cancelled:
            # No running turn to cancel — treat as no-op success so
            # the TUI can press ESC any time without a 4xx.
            return InterruptResponse(
                session_id=session_id,
                thread_id=thread_id,
                status="idle",
                cancelled=False,
            )
        return InterruptResponse(
            session_id=session_id,
            thread_id=thread_id,
            status="interrupting",
            cancelled=True,
        )

    return router
