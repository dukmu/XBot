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
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from xbotv2.core.bootstrap import bootstrap
from xbotv2.protocol.commands import execute_command, list_commands
from xbotv2.protocol.version import PROTOCOL_VERSION

logger = logging.getLogger("xbotv2.http_server")


class SessionNotFound(KeyError):
    """The caller asked for a session that has not been opened."""


class SessionBusy(RuntimeError):
    """The session is already processing a turn; the new request is rejected."""


class SessionExists(RuntimeError):
    """A new session was requested with an identifier already in use."""


@dataclass
class SessionContext:
    """One active HTTP session runtime."""

    session_id: str
    thread_id: str
    provider_name: str
    paths: RuntimePaths
    workspace_root: str
    no_plugins: bool
    engine: Any
    permission_overrides: dict[str, str] = field(default_factory=dict)
    sandbox_overrides: dict[str, str] = field(default_factory=dict)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_task: asyncio.Task | None = None

    def request_interrupt(self) -> bool:
        task = self.turn_task
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def close(self) -> None:
        task = self.turn_task
        if (
            task is not None
            and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.turn_task = None
        try:
            await self.engine.close_session()
        except Exception:
            logger.exception("Engine close_session failed for %s", self.session_id)


class SessionManager:
    """Owns active HTTP sessions keyed by session id."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._sessions: dict[str, SessionContext] = {}
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._sessions)

    async def get(self, session_id: str) -> SessionContext:
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
        mode: str = "new",
        no_plugins: bool,
        llm_override: Any | None = None,
    ) -> SessionContext:
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
            if mode == "resume" and not session_paths.state_dir.exists():
                raise SessionNotFound(session_id)
            if mode == "new" and session_paths.state_dir.exists():
                raise SessionExists(session_id)
            engine = await bootstrap(
                paths=self.paths,
                provider_name=provider_name,
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=workspace_root,
                plugin_dirs=[] if no_plugins else None,
                llm_override=llm_override,
            )
            await engine.start_session()
            ctx = SessionContext(
                session_id=session_id,
                thread_id=thread_id,
                provider_name=provider_name,
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
        expected: SessionContext | None = None,
    ) -> None:
        async with self._lock:
            ctx = self._sessions.get(session_id)
            if expected is not None and ctx is not expected:
                return
            ctx = self._sessions.pop(session_id, None)
        if ctx is not None:
            await ctx.close()

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
            history=_display_history(ctx.engine.messages),
        )

    @app.get("/commands")
    async def commands() -> CommandListResponse:
        return CommandListResponse(commands=list_commands())

    @app.get("/sessions/{session_id}/commands")
    async def session_commands(session_id: str) -> CommandListResponse:
        ctx = await manager.get(session_id)
        return CommandListResponse(
            commands=list_commands(extra=_tool_commands(ctx.engine.tool_registry))
        )

    @app.post("/sessions/{session_id}/commands")
    async def run_command(session_id: str, payload: CommandRequest) -> CommandResponse:
        ctx = await manager.get(session_id)
        raw = payload.raw
        command = payload.command.strip().removeprefix("/")
        args = payload.args
        if args is None:
            parts = raw.split()
            if not command and parts:
                command = parts[0].removeprefix("/")
            args = parts[1:] if parts else []
        if not command:
            raise HttpServerError("invalid_request", "command must be non-empty", status=400)
        return CommandResponse.model_validate(
            execute_command(ctx, command, args, kind=payload.kind)
        )

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
                    async for event in run_turn_stream(
                        ctx,
                        content=content,
                        request_id=client_request_id,
                    ):
                        seq += 1
                        yield _format_sse(
                            event=event,
                            seq=seq,
                            session_id=ctx.session_id,
                            thread_id=ctx.thread_id,
                            request_id=client_request_id,
                        )
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
                    await manager.close_session(session_id, expected=ctx)
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


def _event_to_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {"type": event.get("type", ""), "data": event.get("data", {})}


def _display_history(messages: list[Any]) -> list[dict[str, Any]]:
    history = []
    for message in messages:
        role = str(getattr(message, "role", ""))
        if role not in {"user", "assistant", "tool"}:
            continue
        history.append({
            "role": role,
            "content": str(getattr(message, "content", "") or ""),
            "tool_calls": [
                call.to_dict() for call in (getattr(message, "tool_calls", None) or [])
            ],
            "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
            "status": str(getattr(message, "status", "") or ""),
        })
    return history

def _tool_commands(reg: Any) -> list[dict[str, Any]]:
    result = []
    for entry in reg.registered_entries():
        ns = entry.namespace
        kind = _ns_kind(ns)
        display = entry.tool.name
        desc = getattr(entry.tool, "description", "") or display
        result.append(
            {
                "name": display,
                "slash": f"/{display}",
                "kind": kind,
                "description": desc,
                "registered_name": entry.registered_name,
                "namespace": ns,
            }
        )
    return result


def _ns_kind(ns: str) -> str:
    if ns.startswith("skills:"):
        return "skill"
    if ns.startswith("mcp:"):
        return "mcp"
    return "tool"


async def _live_sink(
    client_event: dict[str, Any],
    *,
    engine: Any,
    events: asyncio.Queue[dict[str, Any] | None],
    disconnect_task: asyncio.Task[Any],
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    event_type = str(client_event.get("type") or "")
    event_data = client_event.get("data") or {}
    req_id = str(event_data.get("request_id") or "")
    waiter = engine.permission_waiter if event_type == "permission_request" else engine.user_input_waiter
    pending = waiter.register(req_id)
    wait_task = asyncio.create_task(
        waiter.wait_registered(req_id, pending, timeout_seconds)
    )
    try:
        await events.put(_event_to_payload(client_event))
        done, _ = await asyncio.wait({wait_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        wait_task.cancel()
        try:
            await wait_task
        except (asyncio.CancelledError, Exception):
            pass
        raise
    if wait_task not in done:
        wait_task.cancel()
        try:
            await wait_task
        except (asyncio.CancelledError, Exception):
            pass
        return {"request_id": req_id, "status": "disconnected", "reason": "client_disconnected"}
    try:
        result = wait_task.result()
    except Exception as exc:
        return {"request_id": req_id, "status": "error", "reason": str(exc)}
    await events.put({
        "type": "permission_response_recorded" if event_type == "permission_request" else "user_input_recorded",
        "data": {"request_id": req_id, "status": result.status, "decision": result.decision, "scope": result.scope, "answer": result.answer, "pending_interactions": []},
    })
    return result.__dict__


@asynccontextmanager
async def _live_interaction_sink(
    ctx: SessionContext,
    events: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
) -> AsyncIterator[None]:
    disconnect_task = asyncio.create_task(disconnected.wait())

    async def sink(client_event, *, timeout_seconds=None, tool_call_id=""):
        return await _live_sink(client_event, engine=ctx.engine, events=events, disconnect_task=disconnect_task, timeout_seconds=timeout_seconds)

    previous = ctx.engine.set_client_event_sink(sink)
    try:
        yield
    finally:
        ctx.engine.set_client_event_sink(previous)
        if not disconnect_task.done():
            disconnect_task.cancel()
            try:
                await disconnect_task
            except (asyncio.CancelledError, Exception):
                pass


async def _pump_turn(
    ctx: SessionContext,
    events: asyncio.Queue[dict[str, Any] | None],
    content: str,
    request_id: str,
) -> None:
    turn_stream = ctx.engine.run_turn(content, request_id=request_id)
    try:
        async for event in turn_stream:
            await events.put(_event_to_payload(event))
    except asyncio.CancelledError:
        logger.info("Turn cancelled for session %s", ctx.session_id)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Engine run_turn failed")
        await events.put({
            "type": "error",
            "data": {
                "code": "turn_failed",
                "message": str(exc),
                "details": {"exception_type": type(exc).__name__},
            },
        })
    finally:
        try:
            close = getattr(turn_stream, "aclose", None)
            if close is not None:
                await close()
        finally:
            await events.put(None)


async def run_turn_stream(
    ctx: SessionContext,
    *,
    content: str,
    request_id: str = "",
) -> AsyncIterator[dict[str, Any]]:
    if ctx.turn_lock.locked():
        raise SessionBusy(ctx.session_id)

    async with ctx.turn_lock:
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        disconnected = asyncio.Event()
        stream_completed = False
        pump_task = asyncio.create_task(
            _pump_turn(ctx, events, content, request_id)
        )
        ctx.turn_task = pump_task
        try:
            async with _live_interaction_sink(ctx, events, disconnected):
                while True:
                    event = await events.get()
                    if event is None:
                        stream_completed = True
                        break
                    yield event
        finally:
            disconnected.set()
            if not stream_completed and not pump_task.done():
                pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("pump_task ended with error for session %s", ctx.session_id)
            finally:
                ctx.turn_task = None


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


def _pending_snapshot(ctx: SessionContext) -> list[str]:
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
