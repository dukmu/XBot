"""HTTP routes owned by interactions."""
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
from XBotv2.session.api.records import ConversationRecord
from XBotv2.session.api.snapshot import thread_snapshot
from XBotv2.session.api.protocol import (
    OpenSessionRequest, ThreadListResponse, SessionListResponse, WorkspaceEventCursor, SessionUpdateRequest, OpenThreadRequest, UndoRequest, RegenerateRequest, ForkResponse, DeleteSessionResponse, PendingInputListResponse, PendingInputUpdateRequest, MessageRequest, InterruptResponse, CloseResponse, OpenSessionResponse
)

async def _interaction_response(sessions, session_id, thread_id, request_id, resolution):
    scope = await sessions.locate(session_id, thread_id)
    try:
        value = scope.require(CLIENT_EVENTS).resolve(request_id, resolution)
    except (InteractionNotPending, TypeError) as exc:
        raise HttpServerError("interaction_no_longer_pending", str(exc), status=410) from exc
    return InteractionResponse(request_id=value.interaction_id, pending_interactions=list(value.pending_ids))

def build_router(*, sessions, persistence=None):
    router = APIRouter()

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/interactions/permission-response",
        operation_id="respond_permission",
    )
    async def post_permission_response(
        session_id: str,
        thread_id: str,
        payload: PermissionResponseRequest,
    ) -> InteractionResponse:
        resolution = Allowed(scope=payload.scope) if payload.decision == "allow" else Denied(reason="permission denied")
        result = await _interaction_response(sessions, session_id, thread_id, payload.request_id, resolution)
        return result
    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/interactions/user-input",
        operation_id="respond_user_input",
    )
    async def post_user_input(
        session_id: str,
        thread_id: str,
        payload: UserInputResponseRequest,
    ) -> InteractionResponse:
        result = await _interaction_response(sessions, session_id, thread_id, payload.request_id, Answered(answer=payload.answer))
        return result

    return router
