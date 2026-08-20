"""Session-host routes: sessions, threads, messages, history, fork, events,
close, interrupt, and interactions.

This group owns the per-session application lifecycle and reaches the
session application through ``manager`` (the ``SessionManager`` host).
Session policy routes live in :mod:`XBotv2.http_transport.policy`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from XBotv2.protocol.http_util import (
    _SSE_RESPONSE,
    _error_payload,
    HttpServerError,
    _format_sse,
)
from XBotv2.protocol.models import (
    CloseResponse,
    ForkResponse,
    HelloResponse,
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
from XBotv2.session.http_util import (
    _open_session_response,
    _resolve_interaction,
)
from XBotv2.server.contracts import contribute_router
from XBotv2.server.contracts import ServerOptions
from XBotv2.server.events import ServerEvents
from XBotv2.server.http import ModelOverride
from XBotv2.session.contracts import PREPARE_FORK, PrepareFork
from XBotv2.session.session import fork_persisted_session
from XBotv2.session.runtime import SessionBusy, require_idle
from XBotv2.session.manager import (
    SessionExists,
    SessionManager,
    SessionNotFound,
    ThreadNotActive,
    persisted_thread_ids,
    session_summary,
    thread_summary,
)

logger = logging.getLogger("xbotv2.http_server")


def build_session_router(
    *,
    manager: SessionManager,
    options: ServerOptions,
    server_events: ServerEvents,
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
            and manager.paths.session(raw_session_id).root.exists()
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
            ctx = await manager.open_session(
                session_id=raw_session_id,
                thread_id=thread_id,
                provider_name=options.provider_name,
                workspace_root=workspace_root,
                mode=payload.mode,
                selected_agent=payload.agent,
                no_plugins=options.no_plugins,
                llm_override=llm_override,
            )
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
        return await _open_session_response(ctx)

    @router.get("/sessions", operation_id="list_sessions")
    async def list_sessions_endpoint() -> SessionListResponse:
        root = manager.paths.sessions_dir
        session_ids = sorted(
            path.name for path in root.iterdir() if path.is_dir()
        ) if root.is_dir() else []
        return SessionListResponse(sessions=[
            await session_summary(manager, session_id)
            for session_id in session_ids
        ])

    @router.get("/sessions/{session_id}", operation_id="get_session")
    async def get_session_endpoint(session_id: str) -> SessionSummary:
        return await session_summary(manager, session_id)

    @router.post(
        "/sessions/{session_id}/fork",
        operation_id="fork_session",
    )
    async def fork_session_endpoint(session_id: str) -> ForkResponse:
        await session_summary(manager, session_id)
        active = await manager.active_threads()
        session_contexts = [
            ctx
            for (active_session_id, _), ctx in active.items()
            if active_session_id == session_id
        ]
        for context in session_contexts:
            await context.services.emit(
                PREPARE_FORK,
                PrepareFork(session_id, context.thread_id),
            )
        forked_id = fork_persisted_session(manager.paths, session_id)
        return ForkResponse(
            session_id=forked_id,
            source_session_id=session_id,
        )

    @router.get(
        "/sessions/{session_id}/threads",
        operation_id="list_threads",
    )
    async def list_threads_endpoint(session_id: str) -> ThreadListResponse:
        await session_summary(manager, session_id)
        return ThreadListResponse(
            session_id=session_id,
            threads=[
                await thread_summary(manager, session_id, thread_id)
                for thread_id in persisted_thread_ids(manager.paths, session_id)
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
        await session_summary(manager, session_id)
        parent_thread_id = payload.parent_thread_id
        if payload.mode == "resume":
            session = manager.paths.session(session_id)
            if not session.has_thread(payload.thread_id):
                raise HttpServerError(
                    "session_not_found",
                    f"{session_id}/{payload.thread_id}",
                    status=404,
                )
            store = manager._state_store(
                session,
                thread_id=payload.thread_id,
            )
            parent_thread_id = str(
                store.read_thread_metadata().get("parent_thread_id") or ""
            )
        if not parent_thread_id or parent_thread_id == payload.thread_id:
            raise HttpServerError(
                "invalid_request",
                "A subagent thread requires a different parent_thread_id",
                status=400,
            )
        try:
            parent = await manager.get(session_id, parent_thread_id)
        except (SessionNotFound, ThreadNotActive) as exc:
            raise HttpServerError(
                "parent_thread_not_active",
                str(exc),
                status=409,
                retryable=True,
            ) from exc
        workspace_root = str(
            Path(payload.workspace_root or parent.workspace_root).resolve()
        )
        try:
            ctx = await manager.open_session(
                session_id=session_id,
                thread_id=payload.thread_id,
                provider_name=options.provider_name,
                workspace_root=workspace_root,
                mode=payload.mode,
                selected_agent=payload.agent,
                no_plugins=options.no_plugins,
                llm_override=llm_override,
                parent_thread_id=parent_thread_id,
                parent_permission_system=parent.services.permissions,
                is_subagent=True,
            )
        except SessionNotFound as exc:
            raise HttpServerError("session_not_found", str(exc), status=404) from exc
        except SessionExists as exc:
            raise HttpServerError("session_exists", str(exc), status=409) from exc
        return await _open_session_response(ctx)

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}",
        operation_id="get_thread",
    )
    async def get_thread_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ThreadSummary:
        return await thread_summary(manager, session_id, thread_id)

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/messages",
        operation_id="list_messages",
    )
    async def list_messages_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ThreadMessagesResponse:
        active = (await manager.active_threads()).get((session_id, thread_id))
        if active is not None:
            messages = active.engine.messages
        else:
            session = manager.paths.session(session_id)
            if not session.has_thread(thread_id):
                raise SessionNotFound(f"{session_id}/{thread_id}")
            store = manager._state_store(
                session,
                thread_id=thread_id,
            )
            messages = store.read_messages()
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
        ctx = await manager.get(session_id, thread_id)
        require_idle(ctx, "rewrite history")
        async with ctx.turn_lock:
            removed_turns = await ctx.services.session.clear_history()
        return HistoryMutationResponse(
            session_id=session_id,
            thread_id=thread_id,
            removed_turns=removed_turns,
            messages=[],
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
        ctx = await manager.get(session_id, thread_id)
        require_idle(ctx, "rewrite history")
        async with ctx.turn_lock:
            messages = await ctx.services.session.undo_history(payload.count)
        return HistoryMutationResponse(
            session_id=session_id,
            thread_id=thread_id,
            removed_turns=payload.count,
            messages=display_history(messages),
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
        ctx = await manager.get(session_id, thread_id)
        try:
            images = [
                ctx.services.storage.store_image(
                    image.data, image.media_type
                )
                for image in payload.images
            ]
            attachments = [
                ctx.services.storage.store_attachment(
                    attachment.data,
                    attachment.media_type,
                    attachment.name,
                )
                for attachment in payload.attachments
            ]
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
                    session_id=ctx.session_id,
                    thread_id=ctx.thread_id,
                    request_id=client_request_id,
                )

            try:
                try:
                    async for event in ctx.stream_message(
                        content,
                        client_request_id,
                        images=images,
                        artifacts=attachments,
                    ):
                        seq += 1
                        yield _format_sse(
                            event=event,
                            seq=seq,
                            session_id=ctx.session_id,
                            thread_id=ctx.thread_id,
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
                        session_id=ctx.session_id,
                        thread_id=ctx.thread_id,
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
        ctx = await manager.get(session_id, thread_id)
        try:
            events = ctx.attach_event_stream()
        except SessionBusy as exc:
            raise HttpServerError(
                "event_stream_connected", str(exc), status=409
            ) from exc

        async def sse_stream() -> AsyncIterator[bytes]:
            seq = 0
            disconnected = False
            request_id = f"events-{uuid.uuid4().hex}"
            try:
                while True:
                    event = await events.get()
                    if event is None:
                        return
                    seq += 1
                    event_type = str(event.get("type") or "")
                    if event_type:
                        event = {
                            **event,
                            "data": server_events.validate(
                                event_type, event.get("data")
                            ),
                        }
                    yield _format_sse(
                        event=event,
                        seq=seq,
                        session_id=ctx.session_id,
                        thread_id=ctx.thread_id,
                        request_id=request_id,
                    )
            except asyncio.CancelledError:
                disconnected = True
            finally:
                ctx.detach_event_stream(events)

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
        return await _resolve_interaction(
            manager=manager,
            session_id=session_id,
            thread_id=thread_id,
            payload=payload.model_dump(),
            kind="permission",
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/interactions/user-input",
        operation_id="respond_user_input",
    )
    async def post_user_input(
        session_id: str,
        thread_id: str,
        payload: UserInputResponseRequest,
    ) -> InteractionResponse:
        return await _resolve_interaction(
            manager=manager,
            session_id=session_id,
            thread_id=thread_id,
            payload=payload.model_dump(),
            kind="user_input",
        )

    @router.post(
        "/sessions/{session_id}/close",
        operation_id="close_session",
    )
    async def shutdown_session(session_id: str) -> CloseResponse:
        await manager.close_session(session_id)
        return CloseResponse(session_id=session_id)

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/close",
        operation_id="close_thread",
    )
    async def close_thread(session_id: str, thread_id: str) -> CloseResponse:
        await manager.close_thread(session_id, thread_id)
        return CloseResponse(session_id=session_id, thread_id=thread_id)

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/interrupt",
        operation_id="interrupt_thread",
    )
    async def interrupt_session(
        session_id: str,
        thread_id: str,
    ) -> InterruptResponse:
        ctx = await manager.get(session_id, thread_id)
        cancelled = ctx.request_interrupt()
        if not cancelled:
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


class SessionRouterPlugin:
    """Register the session HTTP surface into ``ctx.web_server``.

    The session capability owns its routes: when the server tree mounts this
    plugin, it registers the session router and its stream-event DTOs into the
    dumb ``ctx.web_server`` / ``ctx.server_events`` carriers.  Registration is
    a fiber effect, so it is undone when the plugin unloads.
    """

    inject = [
        'server',
        'session_host',
        'server_events',
        'server_options',
    ]
    name = "xbot.session.router"

    async def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.session.events import (
            AgentConfiguredData,
            ClientMessageData,
            HistoryUpdatedData,
        )

        for event_type, dto in (
            ("client_message", ClientMessageData),
            ("history_updated", HistoryUpdatedData),
            ("agent_configured", AgentConfiguredData),
        ):
            ctx.effect(lambda t=event_type, d=dto: ctx.server_events.register(t, d))

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
                manager=ctx.session_host,
                options=ctx.server_options,
                server_events=ctx.server_events,
            ),
            exception_handlers=(
                (SessionNotFound, _on_session_not_found),
                (ThreadNotActive, _on_thread_not_active),
            ),
        )


plugin = SessionRouterPlugin()
