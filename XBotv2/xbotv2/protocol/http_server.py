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
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from xbotv2.protocol.commands import execute_command, list_commands

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
    provider_name: str = "default",
    data_dir: str = "data",
    workspace_root: str | None = None,
    no_plugins: bool = False,
    server_name: str = "xbotv2",
    llm_override: Any | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    A single ``SessionManager`` instance is shared across the process.
    Tests can call this function with custom parameters.
    """

    started_at = time.monotonic()
    manager = SessionManager()
    # Stash the LLM override on app.state so the open_session route can use it.
    # This is a test seam: production passes llm_override=None and the server
    # loads the configured provider. Tests pass a MockLLM to skip network.
    _llm_override_ref: dict[str, Any] = {"value": llm_override}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await manager.close_all()

    app = FastAPI(title="XBotv2 TUI HTTP Server", lifespan=lifespan)
    app.state.manager = manager
    app.state.server_name = server_name
    app.state.provider_name = provider_name
    app.state.data_dir = data_dir
    app.state.workspace_root = str(Path(workspace_root or Path.cwd()).resolve())
    app.state.no_plugins = no_plugins
    app.state.started_at = started_at
    app.state.llm_override = _llm_override_ref

    _register_routes(app)
    return app


def set_llm_override(app: FastAPI, llm: Any | None) -> None:
    """Inject a test LLM at runtime. No-op in production."""

    app.state.llm_override["value"] = llm

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
            "workspace_root": app.state.workspace_root,
        }

    @app.post("/hello")
    async def hello(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip()
        thread_id = str(payload.get("thread_id") or "agent").strip() or "agent"
        return {
            "server_name": app.state.server_name,
            "protocol_version": PROTOCOL_VERSION,
            "session_id": session_id,
            "thread_id": thread_id,
        }

    @app.post("/sessions")
    async def open_session(payload: dict[str, Any]) -> dict[str, Any]:
        raw_session_id = str(payload.get("session_id") or "").strip() or None
        thread_id = str(payload.get("thread_id") or "agent").strip() or "agent"
        workspace_root = str(
            Path(payload.get("workspace_root") or app.state.workspace_root).resolve()
        )
        mode = str(payload.get("mode") or "new")
        try:
            ctx = await manager.open_session(
                session_id=raw_session_id,
                thread_id=thread_id,
                provider_name=app.state.provider_name,
                data_dir=app.state.data_dir,
                workspace_root=workspace_root,
                mode=mode,
                no_plugins=app.state.no_plugins,
                llm_override=app.state.llm_override.get("value"),
            )
        except SessionNotFound as exc:
            raise HttpServerError("session_not_found", str(exc), status=404) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Session open failed for %s", raw_session_id or "<new>")
            raise HttpServerError(
                "session_open_failed", str(exc), status=500
            ) from exc
        return {
            "session_id": ctx.session_id,
            "thread_id": ctx.thread_id,
            "status": "ready",
            "agent_name": getattr(ctx.engine.config, "agent_name", "XBotv2"),
            "workspace_root": ctx.workspace_root,
            "provider": ctx.provider_name,
        }

    @app.get("/commands")
    async def commands() -> dict[str, Any]:
        return {"commands": list_commands()}

    @app.get("/sessions/{session_id}/commands")
    async def session_commands(session_id: str) -> dict[str, Any]:
        await manager.get(session_id)
        return {"commands": list_commands()}

    @app.post("/sessions/{session_id}/commands")
    async def run_command(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            ctx = await manager.get(session_id)
        except SessionNotFound as exc:
            raise HttpServerError("session_not_found", str(exc), status=404) from exc
        raw = str(payload.get("raw") or "")
        command = str(payload.get("command") or "").strip().removeprefix("/")
        args = payload.get("args")
        if not isinstance(args, list):
            parts = raw.split()
            if not command and parts:
                command = parts[0].removeprefix("/")
            args = parts[1:] if parts else []
        if not command:
            raise HttpServerError("invalid_request", "command must be non-empty", status=400)
        result = execute_command(ctx, command, [str(arg) for arg in args])
        ctx.engine.state_store.append_event(
            "server_command_result",
            {
                "command": command,
                "args": [str(arg) for arg in args],
                "result": result.get("data", {}),
            },
        )
        ctx.engine.state_store.materialize()
        return result

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
            end_emitted = False

            def emit_end() -> bytes:
                nonlocal end_emitted
                if end_emitted:
                    return b""
                end_emitted = True
                return _format_sse(
                    event={"type": "end", "data": {"status": "ok"}},
                    seq=seq + 1,
                )

            try:
                try:
                    async for event in run_turn_stream(ctx, content=content):
                        seq += 1
                        yield _format_sse(event=event, seq=seq)
                except SessionBusy as exc:
                    seq += 1
                    yield _format_sse(
                        event={
                            "type": "error",
                            "data": {
                                "code": "engine_busy",
                                "message": str(exc),
                            },
                        },
                        seq=seq,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("SSE stream errored for %s", session_id)
                    seq += 1
                    yield _format_sse(
                        event={
                            "type": "error",
                            "data": {"code": "stream_failed", "message": str(exc)},
                        },
                        seq=seq,
                    )
            except asyncio.CancelledError:
                # TUI pressed ESC, or the HTTP client disconnected
                # mid-turn. The dispatcher has already pushed the
                # ``turn_cancelled`` event to the bus (or the pump's
                # except branch will), so the TUI has enough
                # information. Let the end marker flush so the
                # client can close cleanly.
                logger.info("SSE stream cancelled for session %s", session_id)
            finally:
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

    @app.post("/sessions/{session_id}/interrupt")
    async def interrupt_session(session_id: str) -> dict[str, Any]:
        try:
            ctx = await manager.get(session_id)
        except SessionNotFound as exc:
            raise HttpServerError(
                "session_not_found", str(exc), status=404
            ) from exc
        cancelled = ctx.request_interrupt()
        if not cancelled:
            # No running turn to cancel — treat as no-op success so
            # the TUI can press ESC any time without a 4xx.
            return {
                "session_id": session_id,
                "status": "idle",
                "cancelled": False,
            }
        return {
            "session_id": session_id,
            "status": "interrupting",
            "cancelled": True,
        }

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
        scope = str(payload.get("scope") or "once").strip().lower()
        if scope not in {"once", "session", "always"}:
            raise HttpServerError(
                "invalid_request",
                "permission.response payload.scope must be once, session, or always",
                status=400,
            )
        try:
            ctx.engine._permission_waiter.answer(  # noqa: SLF001
                request_id, decision=decision, scope=scope
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
