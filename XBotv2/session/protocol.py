"""Session routes: sessions, threads, messages, history, fork, events,
close, interrupt, and interactions.

Session lifecycle and persistence remain behind the public SessionsPort;
this module owns only HTTP request/response and SSE mapping.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import Field, model_validator
from XBotv2.protocol.http_util import (
    _SSE_RESPONSE,
    _error_payload,
    HttpServerError,
    _format_sse,
)
from XBotv2.interactions import InteractionResponse, UserInputResponseRequest
from XBotv2.permission_request import PermissionResponseRequest
from XBotv2.protocol import ErrorEventData, WireModel
from XBotv2.core.errors import OperationError
from XBotv2.core.history import ConversationPage
from XBotv2.core.tools import ClientEvent
from XBotv2.session.event_stream import (
    SessionEventCursorExpired,
    SessionEventFrame,
)
from XBotv2.session.history import SessionHistoryItem, conversation_replay
from XBotv2.core.timing import SessionStats, conversation_stats
from XBotv2.server import ModelOverride, ServerOptions
from XBotv2.session.services import SessionsPort
from XBotv2.session.types import (
    AttachmentInput,
    HistoryMutation,
    ImageInput,
    InteractionReceipt,
    OpenedSession,
    OpenSession,
    OpenThread,
    PendingInputData,
    PendingInputUpdate,
    RegenerateMessage,
    SendMessage,
    SessionExists,
    SessionNotFound,
    SessionDescriptor,
    SessionSummary,
    ThreadNotActive,
    ThreadSummary,
)

logger = logging.getLogger("xbotv2.api")

class OpenSessionRequest(WireModel):
    session_id: str | None = None
    thread_id: str = "agent"
    workspace_root: str | None = None
    mode: Literal["new", "resume"] = "new"
    agent: str | None = None
    history_limit: int | None = Field(default=None, ge=1, le=500)


class ThreadListResponse(WireModel):
    session_id: str = Field(min_length=1)
    threads: list[ThreadSummary] = Field(default_factory=list)


class SessionListResponse(WireModel):
    sessions: list[SessionSummary] = Field(default_factory=list)
    event_cursor: int = Field(default=0, ge=0)


class WorkspaceEventCursor(Protocol):
    @property
    def sequence(self) -> int: ...


class SessionUpdateRequest(WireModel):
    title: str = Field(min_length=1, max_length=200)


class OpenSessionResponse(SessionDescriptor):
    status: Literal["ready"] = "ready"
    history: list[SessionHistoryItem] = Field(default_factory=list)
    history_cursor: str | None = None
    pending_inputs: list["PendingInputData"] = Field(default_factory=list)


class OpenThreadRequest(WireModel):
    thread_id: str = Field(min_length=1)
    parent_thread_id: str = Field(default="agent", min_length=1)
    workspace_root: str | None = None
    mode: Literal["new", "resume"] = "new"
    agent: str | None = None
    history_limit: int | None = Field(default=None, ge=1, le=500)


class ThreadMessagesResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    messages: list[SessionHistoryItem] = Field(default_factory=list)
    next_cursor: str | None = None


class UndoRequest(WireModel):
    count: int = Field(default=1, ge=1)
    history_limit: int | None = Field(default=None, ge=1, le=500)


class RegenerateRequest(WireModel):
    request_id: str = ""


class HistoryMutationResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    removed_turns: int = Field(ge=0)
    messages: list[SessionHistoryItem] = Field(default_factory=list)
    session_stats: SessionStats = Field(default_factory=SessionStats)
    history_cursor: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ForkResponse(WireModel):
    session_id: str = Field(min_length=1)
    source_session_id: str = Field(min_length=1)
    status: Literal["forked"] = "forked"


class DeleteSessionResponse(WireModel):
    session_id: str = Field(min_length=1)
    status: Literal["deleted"] = "deleted"


class MessageData(WireModel):
    id: str
    role: Literal["user"]
    content: str = ""
    images: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class HistoryUpdatedData(WireModel):
    history: list[SessionHistoryItem] = Field(default_factory=list)
    operation: str = Field(min_length=1)
    turns: int = Field(ge=0)
    history_cursor: str | None = None
    session_stats: SessionStats = Field(default_factory=SessionStats)


class AgentConfiguredData(WireModel):
    agent_name: str = ""
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    context_window: int = Field(default=0, ge=0)


class PendingInputListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    items: list[PendingInputData] = Field(default_factory=list)


class PendingInputUpdateRequest(WireModel):
    action: Literal["edit", "remove", "steer"]
    content: str = ""

    @model_validator(mode="after")
    def _validate_edit(self) -> "PendingInputUpdateRequest":
        if self.action == "edit" and not self.content.strip():
            raise ValueError("queue edit requires non-empty content")
        return self


class QueueUpdatedData(WireModel):
    items: list[PendingInputData] = Field(default_factory=list)


SessionEventType = Literal[
    "agent_configured",
    "history_updated",
    "message",
    "queue_updated",
]

_SESSION_EVENT_MODELS: dict[str, type[WireModel]] = {
    "agent_configured": AgentConfiguredData,
    "history_updated": HistoryUpdatedData,
    "message": MessageData,
    "queue_updated": QueueUpdatedData,
}


def session_event(
    type: SessionEventType,
    data: dict[str, Any],
) -> ClientEvent:
    """Validate one Session-owned event at its producer boundary."""
    payload = _SESSION_EVENT_MODELS[type].model_validate(data)
    return ClientEvent(
        type=type,
        data=payload.model_dump(mode="json", exclude_unset=True),
    )


def session_error_event(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> ClientEvent:
    """Validate a Session-produced generic error event."""
    payload = ErrorEventData(
        code=code,
        message=message,
        details=details or {},
    )
    return ClientEvent(
        type="error",
        data=payload.model_dump(mode="json", exclude_unset=True),
    )


class MessageRequest(WireModel):
    content: str = ""
    request_id: str = ""
    delivery: Literal["queue", "steer"] = "steer"
    images: list[ImageInput] = Field(default_factory=list)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_content(self) -> "MessageRequest":
        if not self.content.strip() and not self.images and not self.attachments:
            raise ValueError("message requires text, image, or attachment content")
        return self


class InterruptResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: Literal["idle", "interrupting"]
    cancelled: bool


class CloseResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = ""
    status: Literal["closed"] = "closed"


SessionMode = Literal["new", "resume"]


def _open_session_response(
    value: OpenedSession,
    page: ConversationPage | None = None,
) -> OpenSessionResponse:
    history = page.messages if page is not None else value.history
    return OpenSessionResponse.model_validate(
        {
            **value.model_dump(mode="json", exclude={"history"}),
            "history": conversation_replay(history),
            "history_cursor": page.next_cursor if page is not None else None,
        }
    )


def _history_response(
    session_id: str,
    thread_id: str,
    result: HistoryMutation,
    page: ConversationPage | None = None,
) -> HistoryMutationResponse:
    messages = page.messages if page is not None else result.messages
    return HistoryMutationResponse(
        session_id=session_id,
        thread_id=thread_id,
        removed_turns=result.removed_turns,
        messages=conversation_replay(messages),
        session_stats=conversation_stats(result.messages),
        history_cursor=page.next_cursor if page is not None else None,
    )


async def _interaction_response(
    pending: Awaitable[InteractionReceipt],
) -> InteractionResponse:
    try:
        value = await pending
    except OperationError as exc:
        if exc.code != "interaction_no_longer_pending":
            raise
        raise HttpServerError(
            exc.code,
            exc.message,
            status=410,
        ) from exc
    return InteractionResponse(
        request_id=value.request_id,
        pending_interactions=list(value.pending_interactions),
    )


async def _message_sse(
    events: AsyncIterator,
    session_id: str,
    thread_id: str,
    request_id: str,
) -> AsyncIterator[bytes]:
    sequence = 0
    try:
        async for event in events:
            sequence += 1
            yield _format_sse(
                event={"type": event.type, "data": event.data},
                seq=sequence,
                session_id=session_id,
                thread_id=thread_id,
                request_id=request_id,
            )
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for session %s", session_id)
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("SSE stream errored for %s", session_id)
        sequence += 1
        yield _format_sse(
            event=session_error_event(
                "stream_failed",
                str(exc),
                details={"exception_type": type(exc).__name__},
            ),
            seq=sequence,
            session_id=session_id,
            thread_id=thread_id,
            request_id=request_id,
        )
    yield _format_sse(
        event={"type": "end", "data": {"status": "ok"}},
        seq=sequence + 1,
        session_id=session_id,
        thread_id=thread_id,
        request_id=request_id,
    )


async def _session_sse(
    events: AsyncIterator[SessionEventFrame],
    session_id: str,
    thread_id: str,
) -> AsyncIterator[bytes]:
    try:
        async for frame in events:
            yield _format_sse(
                event={"type": frame.event.type, "data": frame.event.data},
                seq=frame.sequence,
                session_id=session_id,
                thread_id=thread_id,
                request_id=frame.request_id,
            )
    except asyncio.CancelledError:
        return


async def _session_not_found(
    _: Request, exc: SessionNotFound
) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content=_error_payload("session_not_found", str(exc)),
    )


async def _thread_not_active(
    _: Request, exc: ThreadNotActive
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=_error_payload("thread_not_active", str(exc), retryable=True),
    )


def build_session_router(
    *,
    sessions: SessionsPort,
    options: ServerOptions,
    workspace_events: WorkspaceEventCursor,
) -> APIRouter:
    """Session, thread, message, history, fork, event, and policy routes."""

    router = APIRouter()

    @router.post("/sessions", operation_id="open_session")
    async def open_session(
        payload: OpenSessionRequest,
        llm_override: ModelOverride,
    ) -> OpenSessionResponse:
        raw_session_id = (payload.session_id or "").strip() or None
        thread_id = payload.thread_id.strip() or "agent"
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
        workspace_root_value = payload.workspace_root
        if payload.mode == "resume" and raw_session_id and not workspace_root_value:
            try:
                workspace_root_value = (
                    await sessions.thread_summary(raw_session_id, thread_id)
                ).workspace_root or options.workspace_root
            except (SessionNotFound, OperationError):
                workspace_root_value = options.workspace_root
        workspace_root = str(Path(workspace_root_value or options.workspace_root).resolve())
        try:
            opened = await sessions.open(OpenSession(
                session_id=raw_session_id,
                thread_id=thread_id,
                provider_name=options.provider_name,
                workspace_root=workspace_root,
                mode=payload.mode,
                selected_agent=payload.agent,
                no_plugins=options.no_plugins,
                model_override=llm_override,
            ))
        except SessionNotFound as exc:
            raise HttpServerError("session_not_found", str(exc), status=404) from exc
        except SessionExists as exc:
            raise HttpServerError("session_exists", str(exc), status=409) from exc
        except OperationError as exc:
            raise HttpServerError(
                exc.code,
                exc.message,
                status=404 if exc.code.endswith("_not_found") else 400,
                retryable=exc.retryable,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Session open failed for %s", raw_session_id or "<new>")
            raise HttpServerError(
                "session_open_failed", str(exc), status=500
            ) from exc
        page = (
            await sessions.message_page(
                opened.session_id,
                opened.thread_id,
                cursor=None,
                limit=payload.history_limit,
            )
            if payload.history_limit is not None
            else None
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
            raise HttpServerError("invalid_session_title", str(exc), status=400) from exc

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
        llm_override: ModelOverride,
    ) -> OpenSessionResponse:
        try:
            opened = await sessions.open_thread(OpenThread(
                session_id=session_id,
                thread_id=payload.thread_id,
                parent_thread_id=payload.parent_thread_id,
                provider_name=options.provider_name,
                workspace_root=payload.workspace_root,
                mode=payload.mode,
                selected_agent=payload.agent,
                no_plugins=options.no_plugins,
                model_override=llm_override,
            ))
        except SessionNotFound as exc:
            raise HttpServerError("session_not_found", str(exc), status=404) from exc
        except SessionExists as exc:
            raise HttpServerError("session_exists", str(exc), status=409) from exc
        page = (
            await sessions.message_page(
                opened.session_id,
                opened.thread_id,
                cursor=None,
                limit=payload.history_limit,
            )
            if payload.history_limit is not None
            else None
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
        "/sessions/{session_id}/threads/{thread_id}/messages",
        operation_id="list_messages",
    )
    async def list_messages_endpoint(
        session_id: str,
        thread_id: str,
        cursor: str | None = None,
        limit: int | None = Query(default=None, ge=1, le=500),
    ) -> ThreadMessagesResponse:
        page = await sessions.message_page(
            session_id,
            thread_id,
            cursor=cursor,
            limit=limit,
        )
        return ThreadMessagesResponse(
            session_id=session_id,
            thread_id=thread_id,
            messages=conversation_replay(page.messages),
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/artifacts/{artifact_id:path}",
        operation_id="get_artifact",
    )
    async def get_artifact_endpoint(
        session_id: str,
        thread_id: str,
        artifact_id: str,
    ) -> Response:
        artifact = await sessions.artifact(session_id, thread_id, artifact_id)
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

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/history/clear",
        operation_id="clear_thread_history",
    )
    async def clear_thread_history(
        session_id: str,
        thread_id: str,
    ) -> HistoryMutationResponse:
        result = await sessions.clear_history(session_id, thread_id)
        return _history_response(session_id, thread_id, result)

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/history/undo",
        operation_id="undo_thread_history",
    )
    async def undo_thread_history(
        session_id: str,
        thread_id: str,
        payload: UndoRequest,
    ) -> HistoryMutationResponse:
        result = await sessions.undo_history(session_id, thread_id, payload.count)
        page = (
            await sessions.message_page(
                session_id,
                thread_id,
                cursor=None,
                limit=payload.history_limit,
            )
            if payload.history_limit is not None
            else None
        )
        return _history_response(
            session_id,
            thread_id,
            result,
            page,
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/history/regenerate",
        operation_id="regenerate_message",
        response_class=StreamingResponse,
        responses=_SSE_RESPONSE,
    )
    async def regenerate_message_endpoint(
        session_id: str,
        thread_id: str,
        payload: RegenerateRequest,
    ) -> Response:
        request_id = payload.request_id.strip() or f"req-{uuid.uuid4().hex}"
        events = await sessions.regenerate_message(RegenerateMessage(
            session_id=session_id,
            thread_id=thread_id,
            request_id=request_id,
        ))
        return StreamingResponse(
            _message_sse(events, session_id, thread_id, request_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/messages",
        operation_id="send_message",
        response_class=StreamingResponse,
        responses=_SSE_RESPONSE,
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
            events = await sessions.stream_message(message)
        except ValueError as exc:
            raise HttpServerError(
                "invalid_request",
                str(exc),
                status=400,
            ) from exc

        return StreamingResponse(
            _message_sse(
                events, session_id, thread_id, client_request_id
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/queue",
        operation_id="list_pending_inputs",
    )
    async def list_pending_inputs_endpoint(
        session_id: str,
        thread_id: str,
    ) -> PendingInputListResponse:
        items = await sessions.pending_inputs(session_id, thread_id)
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
            items = await sessions.update_pending_input(PendingInputUpdate(
                session_id=session_id,
                thread_id=thread_id,
                message_id=message_id,
                action=payload.action,
                content=payload.content,
            ))
        except OperationError as exc:
            raise HttpServerError(exc.code, exc.message, status=404) from exc
        return PendingInputListResponse(
            session_id=session_id,
            thread_id=thread_id,
            items=list(items),
        )

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
            events = await sessions.stream_events(
                session_id,
                thread_id,
                after=after,
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

        return StreamingResponse(
            _session_sse(events, session_id, thread_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/interactions/permission-response",
        operation_id="respond_permission",
    )
    async def post_permission_response(
        session_id: str,
        thread_id: str,
        payload: PermissionResponseRequest,
    ) -> InteractionResponse:
        result = await _interaction_response(sessions.respond_permission(
            session_id,
            thread_id,
            payload.request_id,
            payload.decision,
            payload.scope,
        ))
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
        result = await _interaction_response(sessions.respond_user_input(
            session_id,
            thread_id,
            payload.request_id,
            payload.answer,
        ))
        return result

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

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/interrupt",
        operation_id="interrupt_thread",
    )
    async def interrupt_session(
        session_id: str,
        thread_id: str,
    ) -> InterruptResponse:
        result = await sessions.interrupt(session_id, thread_id)
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


__all__ = [
    "AgentConfiguredData",
    "AttachmentInput",
    "CloseResponse",
    "DeleteSessionResponse",
    "ForkResponse",
    "HistoryMutationResponse",
    "HistoryUpdatedData",
    "ImageInput",
    "InterruptResponse",
    "MessageData",
    "MessageRequest",
    "OpenSessionRequest",
    "OpenSessionResponse",
    "OpenThreadRequest",
    "SessionHistoryItem",
    "SessionListResponse",
    "SessionMode",
    "SessionEventType",
    "SessionSummary",
    "ThreadListResponse",
    "ThreadMessagesResponse",
    "ThreadSummary",
    "UndoRequest",
    "build_session_router",
    "session_event",
    "session_error_event",
]
