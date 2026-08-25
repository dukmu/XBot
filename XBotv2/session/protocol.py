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
from dataclasses import fields
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import Field, model_validator
from xcore import Context
from XBotv2.protocol.http_util import (
    _SSE_RESPONSE,
    _error_payload,
    HttpServerError,
    _format_sse,
)
from XBotv2.interactions import InteractionResponse, UserInputResponseRequest
from XBotv2.permission_request import PermissionResponseRequest
from XBotv2.protocol import ErrorEventData, WireModel
from XBotv2.usage import UsageData
from XBotv2.core.errors import OperationError
from XBotv2.session.history import display_history
from XBotv2.server import ModelOverride, ServerOptions, contribute_router
from XBotv2.session.services import SessionsPort
from XBotv2.session.types import (
    AttachmentUpload,
    HistoryMutation,
    ImageUpload,
    InteractionReceipt,
    OpenedSession,
    OpenSession,
    OpenThread,
    SendMessage,
    SessionExists,
    SessionNotFound,
    SessionSnapshot,
    ThreadNotActive,
    ThreadSnapshot,
)

logger = logging.getLogger("xbotv2.http_server")


def _empty_usage() -> UsageData:
    return UsageData(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        requests=0,
    )


class OpenSessionRequest(WireModel):
    session_id: str | None = None
    thread_id: str = "agent"
    workspace_root: str | None = None
    mode: Literal["new", "resume"] = "new"
    agent: str | None = None


class SessionHistoryItem(WireModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: str = ""
    status: str = ""
    data: Any = None
    error: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    runtime: dict[str, str] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ThreadSummary(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: Literal["active", "inactive"]
    kind: Literal["main", "subagent"] = "main"
    turn_status: Literal["idle", "running"] = "idle"
    parent_thread_id: str = ""
    agent: str = ""
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    context_window: int = Field(default=0, ge=0)
    message_count: int = Field(default=0, ge=0)
    usage: UsageData = Field(default_factory=_empty_usage)
    pending_interactions: list[str] = Field(default_factory=list)
    status_slots: dict[str, str] = Field(default_factory=dict)
    workspace_root: str = ""
    title: str = ""


class ThreadListResponse(WireModel):
    session_id: str = Field(min_length=1)
    threads: list[ThreadSummary] = Field(default_factory=list)


class SessionSummary(WireModel):
    session_id: str = Field(min_length=1)
    status: Literal["active", "inactive"]
    active_threads: int = Field(default=0, ge=0)
    thread_count: int = Field(default=0, ge=0)
    workspace_root: str = ""
    title: str = ""


class SessionListResponse(WireModel):
    sessions: list[SessionSummary] = Field(default_factory=list)


class OpenSessionResponse(WireModel):
    session_id: str
    thread_id: str
    status: Literal["ready"] = "ready"
    agent_name: str
    workspace_root: str
    provider: str
    model: str = ""
    model_mode: str = ""
    context_window: int = Field(default=0, ge=0)
    usage: UsageData = Field(default_factory=_empty_usage)
    history: list[SessionHistoryItem] = Field(default_factory=list)
    status_slots: dict[str, str] = Field(default_factory=dict)


class OpenThreadRequest(WireModel):
    thread_id: str = Field(min_length=1)
    parent_thread_id: str = Field(default="agent", min_length=1)
    workspace_root: str | None = None
    mode: Literal["new", "resume"] = "new"
    agent: str | None = None


class ThreadMessagesResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    messages: list[SessionHistoryItem] = Field(default_factory=list)


class UndoRequest(WireModel):
    count: int = Field(default=1, ge=1)


class HistoryMutationResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    removed_turns: int = Field(ge=0)
    messages: list[SessionHistoryItem] = Field(default_factory=list)


class ForkResponse(WireModel):
    session_id: str = Field(min_length=1)
    source_session_id: str = Field(min_length=1)
    status: Literal["forked"] = "forked"


class ImageInput(WireModel):
    data: str = Field(min_length=1)
    media_type: str = Field(pattern=r"^image/[A-Za-z0-9.+-]+$")


class AttachmentInput(WireModel):
    data: str = Field(min_length=1)
    media_type: str = "application/octet-stream"
    name: str = Field(min_length=1)


class MessageData(WireModel):
    id: str
    role: str
    content: str = ""


class HistoryUpdatedData(WireModel):
    history: list[SessionHistoryItem] = Field(default_factory=list)
    operation: str = Field(min_length=1)
    turns: int = Field(ge=0)


class AgentConfiguredData(WireModel):
    agent_name: str = ""
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    context_window: int = Field(default=0, ge=0)


SessionEventType = Literal[
    "agent_configured",
    "history_updated",
    "message",
]

_SESSION_EVENT_MODELS: dict[str, type[WireModel]] = {
    "agent_configured": AgentConfiguredData,
    "history_updated": HistoryUpdatedData,
    "message": MessageData,
}


def session_event(
    type: SessionEventType,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Validate one Session-owned event at its producer boundary."""
    payload = _SESSION_EVENT_MODELS[type].model_validate(data)
    return {"type": type, "data": payload.model_dump(exclude_unset=True)}


def session_error_event(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a Session-produced generic error event."""
    payload = ErrorEventData(
        code=code,
        message=message,
        details=details or {},
    )
    return {"type": "error", "data": payload.model_dump(exclude_unset=True)}


class MessageRequest(WireModel):
    content: str = ""
    request_id: str = ""
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


def _open_session_response(value: OpenedSession) -> OpenSessionResponse:
    return OpenSessionResponse.model_validate(
        {**_record_data(value), "history": display_history(value.history)}
    )


def _session_summary(value: SessionSnapshot) -> SessionSummary:
    return SessionSummary.model_validate(_record_data(value))


def _thread_summary(value: ThreadSnapshot) -> ThreadSummary:
    return ThreadSummary.model_validate(_record_data(value))


def _record_data(value: object) -> dict[str, Any]:
    return {field.name: getattr(value, field.name) for field in fields(value)}


def _history_response(
    session_id: str, thread_id: str, result: HistoryMutation
) -> HistoryMutationResponse:
    return HistoryMutationResponse(
        session_id=session_id,
        thread_id=thread_id,
        removed_turns=result.removed_turns,
        messages=display_history(result.messages),
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
    events: AsyncIterator,
    session_id: str,
    thread_id: str,
) -> AsyncIterator[bytes]:
    request_id = f"events-{uuid.uuid4().hex}"
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
        return _open_session_response(opened)

    @router.get("/sessions", operation_id="list_sessions")
    async def list_sessions_endpoint() -> SessionListResponse:
        return SessionListResponse(sessions=[
            _session_summary(value) for value in await sessions.list_sessions()
        ])

    @router.get("/sessions/{session_id}", operation_id="get_session")
    async def get_session_endpoint(session_id: str) -> SessionSummary:
        return _session_summary(await sessions.session_summary(session_id))

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

    @router.get(
        "/sessions/{session_id}/threads",
        operation_id="list_threads",
    )
    async def list_threads_endpoint(session_id: str) -> ThreadListResponse:
        return ThreadListResponse(
            session_id=session_id,
            threads=[
                _thread_summary(value)
                for value in await sessions.list_threads(session_id)
            ],
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
        return _open_session_response(opened)

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}",
        operation_id="get_thread",
    )
    async def get_thread_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ThreadSummary:
        return _thread_summary(
            await sessions.thread_summary(session_id, thread_id)
        )

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/messages",
        operation_id="list_messages",
    )
    async def list_messages_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ThreadMessagesResponse:
        messages = await sessions.messages(session_id, thread_id)
        return ThreadMessagesResponse(
            session_id=session_id,
            thread_id=thread_id,
            messages=display_history(messages),
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
        return _history_response(session_id, thread_id, result)

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
            images=tuple(
                ImageUpload(image.data, image.media_type)
                for image in payload.images
            ),
            attachments=tuple(
                AttachmentUpload(
                    attachment.data,
                    attachment.media_type,
                    attachment.name,
                )
                for attachment in payload.attachments
            ),
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
        "/sessions/{session_id}/threads/{thread_id}/events",
        operation_id="stream_events",
        response_class=StreamingResponse,
        responses=_SSE_RESPONSE,
    )
    async def session_events(session_id: str, thread_id: str) -> Response:
        events = await sessions.stream_events(session_id, thread_id)

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


class SessionProtocolPlugin:
    """Map the public Sessions API to HTTP and SSE."""

    inject = [
        'server',
        'sessions',
        'server_options',
    ]
    name = "xbot.protocol.session"

    async def apply(self, ctx: Context, config: object = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_session_router(
                sessions=ctx.sessions,
                options=ctx.server_options,
            ),
            exception_handlers=(
                (SessionNotFound, _session_not_found),
                (ThreadNotActive, _thread_not_active),
            ),
        )


plugin = SessionProtocolPlugin()


__all__ = [
    "AgentConfiguredData",
    "AttachmentInput",
    "CloseResponse",
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
