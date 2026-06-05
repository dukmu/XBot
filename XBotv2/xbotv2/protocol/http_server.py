"""FastAPI HTTP server for the XBotv2 TUI protocol.

Implements the endpoints in ``docsv2/tui_opencode_requirements.md``
§10.5.3, with SSE event streams per §10.5.4.

v1 only binds to loopback (127.0.0.1); ``--bind 0.0.0.0`` is rejected
because authentication is not implemented in v1. See §10.5.7.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from xbotv2.protocol.dispatcher import (
    SessionBusy,
    SessionContext,
    SessionManager,
    SessionNotFound,
    run_turn_stream,
)
from xbotv2.protocol.frames import PROTOCOL_VERSION, ProtocolFrame

logger = logging.getLogger("xbotv2.http_server")


class HttpServerError(Exception):
    """Domain error with an HTTP status hint."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def create_app(
    *,
    personality_id: str = "default",
    provider_name: str = "default",
    data_dir: str = "data",
    no_plugins: bool = False,
    server_name: str = "xbotv2",
) -> FastAPI:
    """Build the FastAPI app.

    A single ``SessionManager`` instance is shared across the process.
    Tests can call this function with custom parameters.
    """

    started_at = time.monotonic()
    manager = SessionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await manager.close_all()

    app = FastAPI(title="XBotv2 TUI HTTP Server", lifespan=lifespan)
    app.state.manager = manager
    app.state.server_name = server_name
    app.state.personality_id = personality_id
    app.state.provider_name = provider_name
    app.state.data_dir = data_dir
    app.state.no_plugins = no_plugins
    app.state.started_at = started_at

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    manager: SessionManager = app.state.manager

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "server_name": app.state.server_name,
            "protocol_version": PROTOCOL_VERSION,
            "uptime_s": int(time.monotonic() - app.state.started_at),
            "sessions": manager.size,
        }

    @app.post("/hello")
    async def hello(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip() or "default"
        thread_id = str(payload.get("thread_id") or "agent").strip() or "agent"
        return {
            "server_name": app.state.server_name,
            "protocol_version": PROTOCOL_VERSION,
            "session_id": session_id,
            "thread_id": thread_id,
        }

    @app.post("/sessions")
    async def open_session(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "default").strip() or "default"
        thread_id = str(payload.get("thread_id") or "agent").strip() or "agent"
        try:
            ctx = await manager.open_session(
                session_id=session_id,
                thread_id=thread_id,
                personality_id=app.state.personality_id,
                provider_name=app.state.provider_name,
                data_dir=app.state.data_dir,
                no_plugins=app.state.no_plugins,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Session open failed for %s", session_id)
            raise HttpServerError(
                "session_open_failed", str(exc), status=500
            ) from exc
        return {
            "session_id": ctx.session_id,
            "thread_id": ctx.thread_id,
            "status": "ready",
            "agent_name": getattr(ctx.engine.config, "agent_name", "XBotv2"),
        }

    @app.post("/sessions/{session_id}/messages")
    async def post_message(session_id: str, request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HttpServerError(
                "invalid_request", f"Body must be JSON: {exc}", status=400
            ) from exc
        content = str(payload.get("content") or "")
        if not content.strip():
            raise HttpServerError(
                "invalid_request",
                "payload.content must be non-empty",
                status=400,
            )
        client_request_id = str(payload.get("request_id") or "")
        try:
            ctx = await manager.get(session_id)
        except SessionNotFound as exc:
            raise HttpServerError(
                "session_not_found", str(exc), status=404
            ) from exc

        async def sse_stream() -> AsyncIterator[bytes]:
            seq = 0
            try:
                async for event in run_turn_stream(ctx, content=content):
                    seq += 1
                    yield _format_sse(event=event, seq=seq)
            except SessionBusy as exc:
                yield _format_sse(
                    event={
                        "type": "error",
                        "data": {
                            "code": "engine_busy",
                            "message": str(exc),
                        },
                    },
                    seq=seq + 1,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("SSE stream errored for %s", session_id)
                yield _format_sse(
                    event={
                        "type": "error",
                        "data": {"code": "stream_failed", "message": str(exc)},
                    },
                    seq=seq + 1,
                )
            finally:
                # Always emit an explicit end marker so clients can close cleanly.
                yield _format_sse(
                    event={"type": "end", "data": {"status": "ok"}},
                    seq=seq + 1,
                )

        return StreamingResponse(
            sse_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/sessions/{session_id}/interactions/permission-response")
    async def post_permission_response(
        session_id: str, payload: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        return await _resolve_interaction(
            manager=request.app.state.manager,
            session_id=session_id,
            payload=payload,
            kind="permission",
        )

    @app.post("/sessions/{session_id}/interactions/user-input")
    async def post_user_input(
        session_id: str, payload: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        return await _resolve_interaction(
            manager=request.app.state.manager,
            session_id=session_id,
            payload=payload,
            kind="user_input",
        )

    @app.post("/sessions/{session_id}/shutdown")
    async def shutdown_session(session_id: str) -> dict[str, Any]:
        await manager.close_session(session_id)
        return {"status": "closed", "session_id": session_id}

    @app.exception_handler(HttpServerError)
    async def _on_http_error(_: Request, exc: HttpServerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"code": exc.code, "message": exc.message},
        )


async def _resolve_interaction(
    *,
    manager: SessionManager,
    session_id: str,
    payload: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise HttpServerError(
            "invalid_request",
            f"{kind}.response payload.request_id must be non-empty",
            status=400,
        )
    try:
        ctx = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HttpServerError(
            "session_not_found", str(exc), status=404
        ) from exc

    if kind == "permission":
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"allow", "deny"}:
            raise HttpServerError(
                "invalid_request",
                "permission.response payload.decision must be allow or deny",
                status=400,
            )
        try:
            ctx.engine._permission_waiter.answer(  # noqa: SLF001
                request_id, decision=decision
            )
        except Exception as exc:  # noqa: BLE001
            raise HttpServerError(
                "interaction_no_longer_pending",
                str(exc),
                status=410,
            ) from exc
        return {
            "request_id": request_id,
            "recorded": True,
            "pending_interactions": _pending_snapshot(ctx),
        }

    answer = payload.get("answer", "")
    try:
        ctx.engine._user_input_waiter.answer(  # noqa: SLF001
            request_id, answer=answer
        )
    except Exception as exc:  # noqa: BLE001
        raise HttpServerError(
            "interaction_no_longer_pending",
            str(exc),
            status=410,
        ) from exc
    return {
        "request_id": request_id,
        "recorded": True,
        "pending_interactions": _pending_snapshot(ctx),
    }


def _pending_snapshot(ctx: SessionContext) -> list[str]:
    """Return the list of currently pending interaction request ids."""

    return list(
        ctx.engine._user_input_waiter.pending_request_ids()  # noqa: SLF001
    ) + list(
        ctx.engine._permission_waiter.pending_request_ids()  # noqa: SLF001
    )


def _format_sse(*, event: dict[str, Any], seq: int) -> bytes:
    """Format a single SSE frame.

    Per §10.5.4: ``event: <type>`` and ``data: <json>`` on separate
    lines, with a single ``id: <seq>`` line. The ``type`` and the
    SSE ``event`` field share the same name so consumers can use
    either listener style.
    """

    payload = json.dumps(event, ensure_ascii=False, default=str)
    type_name = event.get("type", "message") or "message"
    return (
        f"event: {type_name}\n"
        f"id: {seq}\n"
        f"data: {payload}\n\n"
    ).encode("utf-8")