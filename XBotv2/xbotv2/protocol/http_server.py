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
import shlex
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from xbotv2.protocol.models import (
    CommandRequest,
    CommandListResponse,
    CommandResponse,
    ErrorResponse,
    HelloRequest,
    HelloResponse,
    MessageRequest,
    OpenSessionRequest,
    OpenSessionResponse,
    PermissionResponseRequest,
    UserInputResponseRequest,
    server_event,
)
from xbotv2.protocol.sse import encode_server_event
from xbotv2.api.paths import RuntimePaths
from xbotv2.api.prompts import MESSAGE_FORMAT_KEY, tool_result_display_content
from xbotv2.core.session import SessionBusy, SessionRuntime, run_turn_stream
from xbotv2.core.bootstrap import bootstrap
from xbotv2.protocol.commands import execute_command, list_commands
from xbotv2.protocol.version import PROTOCOL_VERSION

logger = logging.getLogger("xbotv2.http_server")


class SessionNotFound(KeyError):
    """The caller asked for a session that has not been opened."""


class SessionExists(RuntimeError):
    """A new session was requested with an identifier already in use."""


class SessionManager:
    """Owns active HTTP sessions keyed by session id."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._sessions: dict[str, SessionRuntime] = {}
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._sessions)

    async def get(self, session_id: str) -> SessionRuntime:
        async with self._lock:
            ctx = self._sessions.get(session_id)
        if ctx is None:
            raise SessionNotFound(session_id)
        return ctx

    async def open_session(
        self,
        *,
        session_id: str | None,
        thread_id: str,
        provider_name: str,
        workspace_root: str,
        selected_agent: str | None = None,
        mode: str = "new",
        no_plugins: bool,
        llm_override: Any | None = None,
    ) -> SessionRuntime:
        async with self._lock:
            mode = (mode or "new").lower().strip()
            if mode not in {"new", "resume"}:
                raise ValueError("session mode must be new or resume")
            if mode == "resume" and not session_id:
                raise ValueError("resume mode requires session_id")
            if mode == "new":
                session_id = session_id or _new_session_id()
            assert session_id is not None
            existing = self._sessions.get(session_id)
            if existing is not None:
                if mode == "resume":
                    self._sessions.pop(session_id)
                    await existing.close()
                else:
                    raise SessionExists(session_id)
            session_paths = self.paths.session(session_id)
            if mode == "resume" and not session_paths.has_thread(thread_id):
                raise SessionNotFound(session_id)
            if mode == "new" and session_paths.root.exists():
                raise SessionExists(session_id)
            engine = await bootstrap(
                paths=self.paths,
                provider_name=provider_name,
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=workspace_root,
                plugin_dirs=[] if no_plugins else None,
                llm_override=llm_override,
                selected_agent=selected_agent,
            )
            await engine.start_session()
            ctx = SessionRuntime(
                session_id=session_id,
                thread_id=thread_id,
                provider_name=str(getattr(engine.config, "provider", provider_name)),
                paths=self.paths,
                workspace_root=workspace_root,
                no_plugins=no_plugins,
                engine=engine,
            )
            self._sessions[session_id] = ctx
            return ctx

    async def close_session(
        self,
        session_id: str,
        *,
        expected: SessionRuntime | None = None,
        reason: str = "session_closed",
    ) -> None:
        async with self._lock:
            ctx = self._sessions.get(session_id)
            if expected is not None and ctx is not expected:
                return
            ctx = self._sessions.pop(session_id, None)
        if ctx is not None:
            await ctx.close(reason)

    async def close_all(self) -> None:
        async with self._lock:
            contexts = list(self._sessions.values())
            self._sessions.clear()
        for ctx in contexts:
            await ctx.close()


class HttpServerError(Exception):
    """Domain error with an HTTP status hint."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        self.retryable = retryable


def create_app(
    *,
    paths: RuntimePaths,
    provider_name: str = "default",
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
    manager = SessionManager(paths)
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
    app.state.paths = paths
    app.state.workspace_root = str(Path(workspace_root or Path.cwd()).resolve())
    app.state.no_plugins = no_plugins
    app.state.started_at = started_at
    app.state.llm_override = _llm_override_ref

    _register_routes(app)
    return app


def set_llm_override(app: FastAPI, llm: Any | None) -> None:
    app.state.llm_override["value"] = llm


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
    async def hello(payload: HelloRequest) -> HelloResponse:
        if payload.protocol_version != PROTOCOL_VERSION:
            raise HttpServerError(
                "unsupported_protocol",
                f"Protocol {payload.protocol_version!r} is not supported; expected {PROTOCOL_VERSION!r}",
                status=426,
            )
        return HelloResponse(
            server_name=app.state.server_name,
            session_id=(payload.session_id or "").strip(),
            thread_id=payload.thread_id.strip() or "agent",
        )

    @app.post("/sessions")
    async def open_session(payload: OpenSessionRequest) -> OpenSessionResponse:
        raw_session_id = (payload.session_id or "").strip() or None
        thread_id = payload.thread_id.strip() or "agent"
        workspace_root = str(
            Path(payload.workspace_root or app.state.workspace_root).resolve()
        )
        try:
            ctx = await manager.open_session(
                session_id=raw_session_id,
                thread_id=thread_id,
                provider_name=app.state.provider_name,
                workspace_root=workspace_root,
                mode=payload.mode,
                selected_agent=payload.agent,
                no_plugins=app.state.no_plugins,
                llm_override=app.state.llm_override.get("value"),
            )
        except SessionNotFound as exc:
            raise HttpServerError("session_not_found", str(exc), status=404) from exc
        except SessionExists as exc:
            raise HttpServerError("session_exists", str(exc), status=409) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Session open failed for %s", raw_session_id or "<new>")
            raise HttpServerError(
                "session_open_failed", str(exc), status=500
            ) from exc
        return OpenSessionResponse(
            session_id=ctx.session_id,
            thread_id=ctx.thread_id,
            agent_name=getattr(ctx.engine.config, "agent_name", "XBotv2"),
            workspace_root=ctx.workspace_root,
            provider=ctx.provider_name,
            model=str(getattr(ctx.engine, "model", "")),
            context_window=int(getattr(ctx.engine, "context_window", 0)),
            usage=ctx.engine.session_usage,
            history=_display_history(ctx.engine.messages),
        )

    @app.get("/commands")
    async def commands() -> CommandListResponse:
        return CommandListResponse(commands=list_commands())

    @app.get("/sessions/{session_id}/commands")
    async def session_commands(session_id: str) -> CommandListResponse:
        ctx = await manager.get(session_id)
        loader = ctx.engine.plugin_loader
        return CommandListResponse(
            commands=list_commands(extra=loader.commands if loader is not None else ())
        )

    @app.post("/sessions/{session_id}/commands")
    async def run_command(session_id: str, payload: CommandRequest) -> CommandResponse:
        ctx = await manager.get(session_id)
        raw = payload.raw
        command = payload.command.strip().removeprefix("/")
        args = payload.args
        if args is None:
            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                raise HttpServerError(
                    "invalid_request",
                    f"Invalid command syntax: {exc}",
                    status=400,
                ) from exc
            if not command and parts:
                command = parts[0].removeprefix("/")
            args = parts[1:] if parts else []
        if not command:
            raise HttpServerError("invalid_request", "command must be non-empty", status=400)
        raw_args = raw.strip()
        if raw_args.startswith("/"):
            _, _, raw_args = raw_args.partition(" ")
        elif not raw_args:
            raw_args = " ".join(args)
        try:
            result = await execute_command(
                ctx,
                command,
                args,
                kind=payload.kind,
                raw_args=raw_args,
            )
        except SessionBusy as exc:
            raise HttpServerError(
                "session_busy",
                str(exc),
                status=409,
                retryable=True,
            ) from exc
        return CommandResponse.model_validate(result)

    @app.post("/sessions/{session_id}/messages")
    async def post_message(session_id: str, payload: MessageRequest) -> Response:
        content = payload.content
        client_request_id = payload.request_id.strip() or f"req-{uuid.uuid4().hex}"
        try:
            ctx = await manager.get(session_id)
        except SessionNotFound as exc:
            raise HttpServerError(
                "session_not_found", str(exc), status=404
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
                        content, client_request_id
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
                if disconnected:
                    if ctx.session_events is None:
                        await manager.close_session(
                            session_id,
                            expected=ctx,
                            reason="client_disconnected",
                        )
                else:
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

    @app.get("/sessions/{session_id}/events")
    async def session_events(session_id: str) -> Response:
        try:
            ctx = await manager.get(session_id)
            events = ctx.attach_event_stream()
        except SessionNotFound as exc:
            raise HttpServerError(
                "session_not_found", str(exc), status=404
            ) from exc
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
                if disconnected:
                    await manager.close_session(
                        session_id,
                        expected=ctx,
                        reason="client_disconnected",
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
        session_id: str, payload: PermissionResponseRequest, request: Request
    ) -> dict[str, Any]:
        return await _resolve_interaction(
            manager=request.app.state.manager,
            session_id=session_id,
            payload=payload.model_dump(),
            kind="permission",
        )

    @app.post("/sessions/{session_id}/interactions/user-input")
    async def post_user_input(
        session_id: str, payload: UserInputResponseRequest, request: Request
    ) -> dict[str, Any]:
        return await _resolve_interaction(
            manager=request.app.state.manager,
            session_id=session_id,
            payload=payload.model_dump(),
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
            content=_error_payload(
                exc.code,
                exc.message,
                details=exc.details,
                retryable=exc.retryable,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error_payload(
                "invalid_request",
                "Request does not match the protocol schema",
                details={"errors": exc.errors()},
            ),
        )

    @app.exception_handler(SessionNotFound)
    async def _on_session_not_found(_: Request, exc: SessionNotFound) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_error_payload("session_not_found", str(exc)),
        )


def _error_payload(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return ErrorResponse(
        code=code,
        message=message,
        details=details or {},
        retryable=retryable,
    ).model_dump()


def _new_session_id() -> str:
    from datetime import datetime

    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def _display_history(messages: list[Any]) -> list[dict[str, Any]]:
    history = []
    for message in messages:
        role = str(getattr(message, "role", ""))
        if role not in {"user", "assistant", "tool"}:
            continue
        additional = getattr(message, "additional_kwargs", {}) or {}
        artifacts = []
        for artifact in getattr(message, "artifact", None) or []:
            if hasattr(artifact, "to_dict"):
                artifacts.append(artifact.to_dict())
            elif isinstance(artifact, dict):
                artifacts.append(dict(artifact))
        item = {
            "role": role,
            "content": (
                tool_result_display_content(
                    str(getattr(message, "content", "") or "")
                )
                if role == "tool"
                and additional.get(MESSAGE_FORMAT_KEY) == "xml-v1"
                else str(getattr(message, "content", "") or "")
            ),
            "tool_calls": [
                call.to_dict() for call in (getattr(message, "tool_calls", None) or [])
            ],
            "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
            "status": str(getattr(message, "status", "") or ""),
        }
        if role == "tool":
            item.update({
                "data": additional.get("xbotv2_data"),
                "error": additional.get("xbotv2_error"),
                "artifacts": artifacts,
            })
        history.append(item)
    return history


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
        if scope not in {"once", "session"}:
            raise HttpServerError(
                "invalid_request",
                "permission.response payload.scope must be once or session",
                status=400,
            )
        try:
            ctx.engine.permission_waiter.answer(  # noqa: SLF001
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
        ctx.engine.user_input_waiter.answer(  # noqa: SLF001
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


def _pending_snapshot(ctx: SessionRuntime) -> list[str]:
    """Return the list of currently pending interaction request ids."""

    return list(
        ctx.engine.user_input_waiter.pending_request_ids()  # noqa: SLF001
    ) + list(
        ctx.engine.permission_waiter.pending_request_ids()  # noqa: SLF001
    )


def _format_sse(
    *,
    event: dict[str, Any],
    seq: int,
    session_id: str = "",
    thread_id: str = "agent",
    request_id: str = "",
) -> bytes:
    """Format a single SSE frame.

    Per §10.5.4: ``event: <type>`` and ``data: <json>`` on separate
    lines, with a single ``id: <seq>`` line. The ``type`` and the
    SSE ``event`` field share the same name so consumers can use
    either listener style.
    """

    payload_event = server_event(
        protocol_version=PROTOCOL_VERSION,
        session_id=session_id,
        thread_id=thread_id,
        request_id=request_id,
        sequence=seq,
        type=str(event.get("type", "message") or "message"),
        data=dict(event.get("data") or {}),
    )
    return encode_server_event(payload_event)
