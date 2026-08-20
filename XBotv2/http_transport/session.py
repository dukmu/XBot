"""Session-host routes: sessions, threads, messages, history, fork, events,
close, interrupt, and interactions.

Session lifecycle and persistence remain behind the public SessionHostPort;
this module owns only HTTP request/response and SSE mapping.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from xcore import Context
from XBotv2.protocol.http_util import (
    _SSE_RESPONSE,
    _error_payload,
    HttpServerError,
    _format_sse,
)
from XBotv2.protocol.models import (
    CloseResponse,
    ForkResponse,
    HistoryMutationResponse,
    InteractionResponse,
    InterruptResponse,
    MessageRequest,
    OpenSessionRequest,
    OpenSessionResponse,
    OpenThreadRequest,
    PermissionResponseRequest,
    SessionListResponse,
    SessionSummary,
    ThreadListResponse,
    ThreadMessagesResponse,
    ThreadSummary,
    UndoRequest,
    UserInputResponseRequest,
)
from XBotv2.core.errors import OperationError
from XBotv2.core.history import display_history
from XBotv2.server import ModelOverride, ServerOptions, contribute_router
from XBotv2.session import (
    AttachmentUpload,
    ImageUpload,
    InteractionReceipt,
    OpenedSession,
    OpenSession,
    OpenThread,
    SendMessage,
    SessionExists,
    SessionHostPort,
    SessionNotFound,
    SessionSummary as SessionSummaryData,
    ThreadNotActive,
    ThreadSummary as ThreadSummaryData,
)

logger = logging.getLogger("xbotv2.http_server")


def _open_session_response(value: OpenedSession) -> OpenSessionResponse:
    return OpenSessionResponse(
        session_id=value.session_id,
        thread_id=value.thread_id,
        agent_name=value.agent_name,
        workspace_root=value.workspace_root,
        provider=value.provider,
        model=value.model,
        model_mode=value.model_mode,
        context_window=value.context_window,
        usage=value.usage,
        history=display_history(value.history),
        status_slots=value.status_slots,
    )


def _session_summary(value: SessionSummaryData) -> SessionSummary:
    return SessionSummary(
        session_id=value.session_id,
        status=value.status,
        active_threads=value.active_threads,
        thread_count=value.thread_count,
    )


def _thread_summary(value: ThreadSummaryData) -> ThreadSummary:
    return ThreadSummary(
        session_id=value.session_id,
        thread_id=value.thread_id,
        status=value.status,
        kind=value.kind,
        turn_status=value.turn_status,
        parent_thread_id=value.parent_thread_id,
        agent=value.agent,
        provider=value.provider,
        model=value.model,
        model_mode=value.model_mode,
        context_window=value.context_window,
        message_count=value.message_count,
        usage=value.usage,
        pending_interactions=list(value.pending_interactions),
        status_slots=value.status_slots,
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


def build_session_router(
    *,
    host: SessionHostPort,
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
            and host.session_exists(raw_session_id)
        ):
            raise HttpServerError(
                "session_exists",
                raw_session_id,
                status=409,
            )
        workspace_root = str(
            Path(payload.workspace_root or options.workspace_root).resolve()
        )
        try:
            opened = await host.open(OpenSession(
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
            _session_summary(value) for value in await host.list_sessions()
        ])

    @router.get("/sessions/{session_id}", operation_id="get_session")
    async def get_session_endpoint(session_id: str) -> SessionSummary:
        return _session_summary(await host.session_summary(session_id))

    @router.post(
        "/sessions/{session_id}/fork",
        operation_id="fork_session",
    )
    async def fork_session_endpoint(session_id: str) -> ForkResponse:
        forked_id = await host.fork_session(session_id)
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
                for value in await host.list_threads(session_id)
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
            opened = await host.open_thread(OpenThread(
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
        return _thread_summary(await host.thread_summary(session_id, thread_id))

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/messages",
        operation_id="list_messages",
    )
    async def list_messages_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ThreadMessagesResponse:
        messages = await host.messages(session_id, thread_id)
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
        result = await host.clear_history(session_id, thread_id)
        return HistoryMutationResponse(
            session_id=session_id,
            thread_id=thread_id,
            removed_turns=result.removed_turns,
            messages=display_history(result.messages),
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/history/undo",
        operation_id="undo_thread_history",
    )
    async def undo_thread_history(
        session_id: str,
        thread_id: str,
        payload: UndoRequest,
    ) -> HistoryMutationResponse:
        result = await host.undo_history(session_id, thread_id, payload.count)
        return HistoryMutationResponse(
            session_id=session_id,
            thread_id=thread_id,
            removed_turns=result.removed_turns,
            messages=display_history(result.messages),
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
            events = await host.stream_message(message)
        except ValueError as exc:
            raise HttpServerError(
                "invalid_request",
                str(exc),
                status=400,
            ) from exc

        async def sse_stream() -> AsyncIterator[bytes]:
            seq = 0
            end_emitted = False
            disconnected = False

            def emit_end() -> bytes:
                nonlocal end_emitted
                if end_emitted:
                    return b""
                end_emitted = True
                return _format_sse(
                    event={"type": "end", "data": {"status": "ok"}},
                    seq=seq + 1,
                    session_id=session_id,
                    thread_id=thread_id,
                    request_id=client_request_id,
                )

            try:
                try:
                    async for event in events:
                        seq += 1
                        yield _format_sse(
                            event={"type": event.type, "data": event.data},
                            seq=seq,
                            session_id=session_id,
                            thread_id=thread_id,
                            request_id=client_request_id,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("SSE stream errored for %s", session_id)
                    seq += 1
                    yield _format_sse(
                        event={
                            "type": "error",
                            "data": {
                                "code": "stream_failed",
                                "message": str(exc),
                                "details": {
                                    "exception_type": type(exc).__name__,
                                },
                            },
                        },
                        seq=seq,
                        session_id=session_id,
                        thread_id=thread_id,
                        request_id=client_request_id,
                    )
            except asyncio.CancelledError:
                disconnected = True
                logger.info("SSE stream cancelled for session %s", session_id)
            finally:
                if not disconnected:
                    final = emit_end()
                    if final:
                        yield final

        return StreamingResponse(
            sse_stream(),
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
        events = await host.stream_events(session_id, thread_id)

        async def sse_stream() -> AsyncIterator[bytes]:
            seq = 0
            request_id = f"events-{uuid.uuid4().hex}"
            try:
                async for event in events:
                    seq += 1
                    yield _format_sse(
                        event={"type": event.type, "data": event.data},
                        seq=seq,
                        session_id=session_id,
                        thread_id=thread_id,
                        request_id=request_id,
                    )
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            sse_stream(),
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
        result = await _interaction_response(host.respond_permission(
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
        result = await _interaction_response(host.respond_user_input(
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
        await host.close_session(session_id)
        return CloseResponse(session_id=session_id)

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/close",
        operation_id="close_thread",
    )
    async def close_thread(session_id: str, thread_id: str) -> CloseResponse:
        await host.close_thread(session_id, thread_id)
        return CloseResponse(session_id=session_id, thread_id=thread_id)

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/interrupt",
        operation_id="interrupt_thread",
    )
    async def interrupt_session(
        session_id: str,
        thread_id: str,
    ) -> InterruptResponse:
        result = await host.interrupt(session_id, thread_id)
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


class SessionHttpAdapter:
    """Map the public Session host API to HTTP and SSE."""

    inject = [
        'server',
        'session_host',
        'server_options',
    ]
    name = "xbot.http.session"

    async def apply(self, ctx: Context, config: object = None) -> None:
        async def _on_session_not_found(
            _: Request, exc: SessionNotFound
        ) -> JSONResponse:
            return JSONResponse(
                status_code=404,
                content=_error_payload("session_not_found", str(exc)),
            )

        async def _on_thread_not_active(
            _: Request, exc: ThreadNotActive
        ) -> JSONResponse:
            return JSONResponse(
                status_code=409,
                content=_error_payload("thread_not_active", str(exc), retryable=True),
            )

        await contribute_router(
            ctx,
            owner=self.name,
            router=build_session_router(
                host=ctx.session_host,
                options=ctx.server_options,
            ),
            exception_handlers=(
                (SessionNotFound, _on_session_not_found),
                (ThreadNotActive, _on_thread_not_active),
            ),
        )


plugin = SessionHttpAdapter()
