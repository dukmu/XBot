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
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from xbotv2.protocol.models import (
    AgentInfo,
    AgentListResponse,
    AgentSelectionRequest,
    AgentSelectionResponse,
    CloseResponse,
    CommandRequest,
    CommandListResponse,
    CommandResponse,
    ErrorResponse,
    ForkResponse,
    HealthResponse,
    HistoryMutationResponse,
    HelloRequest,
    HelloResponse,
    InteractionResponse,
    InterruptResponse,
    MessageRequest,
    OpenSessionRequest,
    OpenSessionResponse,
    OpenThreadRequest,
    PermissionResponseRequest,
    ProviderInfo,
    ProviderListResponse,
    ProviderSelectionRequest,
    ProviderSelectionResponse,
    SessionListResponse,
    SessionPolicyPatch,
    SessionPolicyResponse,
    SessionSummary,
    TaskListResponse,
    TaskStopResponse,
    ThreadListResponse,
    ThreadMessagesResponse,
    ThreadSummary,
    ToolInfo,
    ToolListResponse,
    UndoRequest,
    UserInputResponseRequest,
    server_event,
)
from xbotv2.protocol.sse import encode_server_event
from xbotv2.api.paths import RuntimePaths
from xbotv2.config.loader import (
    load_provider_config,
    load_provider_names,
    load_system_config,
)
from xbotv2.core.operations import (
    OperationError,
    clear_history,
    fork_persisted_session,
    require_forkable,
    select_agent,
    select_provider,
    stop_all_tasks,
    stop_task,
    task_snapshots,
    undo_history,
    update_session_policy,
)
from xbotv2.config.policy import (
    load_session_policy,
    merge_sandbox_config,
)
from xbotv2.core.session import SessionBusy, SessionRuntime, run_turn_stream
from xbotv2.persistence.store import CoreStateStore
from xbotv2.protocol.commands import execute_command, list_commands
from xbotv2.protocol.history import display_history
from xbotv2.protocol.session_manager import (
    SessionExists,
    SessionManager,
    SessionNotFound,
    ThreadNotActive,
    close_disconnected_runtime,
    pending_interactions,
    persisted_thread_ids,
    session_summary,
    thread_summary,
)
from xbotv2.protocol.version import PROTOCOL_VERSION

logger = logging.getLogger("xbotv2.http_server")

_SSE_RESPONSE = {
    200: {
        "description": "Server-Sent Events stream",
        "content": {
            "text/event-stream": {
                "schema": {"type": "string"},
            },
        },
    },
}


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

    error_responses = {
        status: {
            "model": ErrorResponse,
            "description": description,
        }
        for status, description in {
            400: "Invalid request",
            404: "Resource not found",
            409: "Resource state conflict",
            410: "Interaction no longer pending",
            422: "Request schema validation failed",
            426: "Unsupported protocol version",
            500: "Server error",
        }.items()
    }
    app = FastAPI(
        title="XBot API",
        version=PROTOCOL_VERSION,
        lifespan=lifespan,
        responses=error_responses,
    )
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

    @app.get("/health", operation_id="health")
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            server_name=app.state.server_name,
            uptime_s=int(time.monotonic() - app.state.started_at),
            sessions=manager.size,
            threads=manager.thread_count,
            workspace_root=app.state.workspace_root,
        )

    @app.get("/providers", operation_id="list_providers")
    async def list_providers_endpoint() -> ProviderListResponse:
        default, names = load_provider_names(manager.paths)
        providers = []
        for name in names:
            config = load_provider_config(manager.paths, name)
            providers.append(ProviderInfo(
                name=name,
                provider=config.provider,
                model=config.model,
                max_tokens=config.max_tokens,
                reasoning_effort=config.reasoning_effort or "",
                thinking_enabled=config.thinking_enabled,
            ))
        return ProviderListResponse(default=default, providers=providers)

    @app.post("/hello", operation_id="hello")
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

    @app.post("/sessions", operation_id="open_session")
    async def open_session(payload: OpenSessionRequest) -> OpenSessionResponse:
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
        return await _open_session_response(ctx)

    @app.get("/sessions", operation_id="list_sessions")
    async def list_sessions_endpoint() -> SessionListResponse:
        root = manager.paths.sessions_dir
        session_ids = sorted(
            path.name for path in root.iterdir() if path.is_dir()
        ) if root.is_dir() else []
        return SessionListResponse(sessions=[
            await session_summary(manager, session_id)
            for session_id in session_ids
        ])

    @app.get("/sessions/{session_id}", operation_id="get_session")
    async def get_session_endpoint(session_id: str) -> SessionSummary:
        return await session_summary(manager, session_id)

    @app.get(
        "/sessions/{session_id}/policy",
        operation_id="get_session_policy",
    )
    async def get_session_policy_endpoint(
        session_id: str,
    ) -> SessionPolicyResponse:
        await session_summary(manager, session_id)
        policy = load_session_policy(manager.paths, session_id)
        return _session_policy_response(
            session_id,
            policy,
            await _effective_sandbox(manager, session_id, policy),
        )

    @app.patch(
        "/sessions/{session_id}/policy",
        operation_id="update_session_policy",
    )
    async def update_session_policy_endpoint(
        session_id: str,
        payload: SessionPolicyPatch,
    ) -> SessionPolicyResponse:
        await session_summary(manager, session_id)
        active = await manager.active_threads()
        contexts = [
            ctx
            for (active_session_id, _), ctx in active.items()
            if active_session_id == session_id
        ]
        policy = await update_session_policy(
            paths=manager.paths,
            session_id=session_id,
            contexts=contexts,
            permissions=payload.permissions,
            remove_permissions=payload.remove_permissions,
            sandbox=payload.sandbox,
            remove_sandbox=payload.remove_sandbox,
        )
        return _session_policy_response(
            session_id,
            policy,
            await _effective_sandbox(manager, session_id, policy),
        )

    @app.post(
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
        require_forkable(*session_contexts)
        async with AsyncExitStack() as stack:
            for ctx in sorted(session_contexts, key=lambda item: item.thread_id):
                await stack.enter_async_context(ctx.turn_lock)
            for ctx in session_contexts:
                await ctx.engine.save_messages()
            forked_id = fork_persisted_session(manager.paths, session_id)
        return ForkResponse(
            session_id=forked_id,
            source_session_id=session_id,
        )

    @app.get(
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

    @app.post(
        "/sessions/{session_id}/threads",
        operation_id="open_thread",
    )
    async def open_thread_endpoint(
        session_id: str,
        payload: OpenThreadRequest,
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
            store = CoreStateStore(
                session,
                thread_id=payload.thread_id,
                workspace_root="",
                provider="",
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
                provider_name=app.state.provider_name,
                workspace_root=workspace_root,
                mode=payload.mode,
                selected_agent=payload.agent,
                no_plugins=app.state.no_plugins,
                llm_override=app.state.llm_override.get("value"),
                parent_thread_id=parent_thread_id,
                parent_permission_system=parent.engine.permission_system,
                subagent_depth=1,
            )
        except SessionNotFound as exc:
            raise HttpServerError("session_not_found", str(exc), status=404) from exc
        except SessionExists as exc:
            raise HttpServerError("session_exists", str(exc), status=409) from exc
        return await _open_session_response(ctx)

    @app.get(
        "/sessions/{session_id}/threads/{thread_id}",
        operation_id="get_thread",
    )
    async def get_thread_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ThreadSummary:
        return await thread_summary(manager, session_id, thread_id)

    @app.get(
        "/sessions/{session_id}/threads/{thread_id}/agents",
        operation_id="list_agents",
    )
    async def list_agents_endpoint(
        session_id: str,
        thread_id: str,
    ) -> AgentListResponse:
        ctx = await manager.get(session_id, thread_id)
        registry = ctx.engine.agent_registry
        definitions = registry.definitions() if registry is not None else ()
        return AgentListResponse(
            active=str(getattr(ctx.engine.config, "agent_name", "")),
            agents=[
                AgentInfo(
                    name=definition.name,
                    description=definition.description,
                    mode=definition.mode,
                    provider=definition.provider or "",
                    model=definition.model or "",
                    context_window=definition.context_window or 0,
                )
                for definition in definitions
                if not definition.hidden
            ],
        )

    @app.put(
        "/sessions/{session_id}/threads/{thread_id}/agent",
        operation_id="select_agent",
    )
    async def select_agent_endpoint(
        session_id: str,
        thread_id: str,
        payload: AgentSelectionRequest,
    ) -> AgentSelectionResponse:
        ctx = await manager.get(session_id, thread_id)
        data = await select_agent(ctx, payload.name)
        return AgentSelectionResponse(
            session_id=session_id,
            thread_id=thread_id,
            agent=data["active"],
            provider=data["provider"],
            model=data["model"],
            model_mode=data["model_mode"],
            context_window=data["context_window"],
        )

    @app.put(
        "/sessions/{session_id}/threads/{thread_id}/provider",
        operation_id="select_provider",
    )
    async def select_provider_endpoint(
        session_id: str,
        thread_id: str,
        payload: ProviderSelectionRequest,
    ) -> ProviderSelectionResponse:
        ctx = await manager.get(session_id, thread_id)
        data = await select_provider(ctx, payload.name)
        return ProviderSelectionResponse(
            session_id=session_id,
            thread_id=thread_id,
            **data,
        )

    @app.get(
        "/sessions/{session_id}/threads/{thread_id}/tools",
        operation_id="list_tools",
    )
    async def list_tools_endpoint(
        session_id: str,
        thread_id: str,
    ) -> ToolListResponse:
        ctx = await manager.get(session_id, thread_id)
        registry = ctx.engine.tool_registry
        enabled = set(registry.names())
        return ToolListResponse(tools=[
            ToolInfo(
                name=str(getattr(entry.tool, "name", entry.registered_name)),
                registered_name=entry.registered_name,
                namespace=entry.namespace,
                description=str(getattr(entry.tool, "description", "") or ""),
                parameters=dict(getattr(entry.tool, "parameters", {}) or {}),
                sandbox_mode=entry.sandbox_mode,
                timeout_seconds=entry.timeout_seconds,
            )
            for entry in registry.registered_entries()
            if entry.model_visible and entry.registered_name in enabled
        ])

    @app.get(
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
            store = CoreStateStore(
                session,
                thread_id=thread_id,
                workspace_root="",
                provider="",
            )
            messages = store.read_messages()
        return ThreadMessagesResponse(
            session_id=session_id,
            thread_id=thread_id,
            messages=display_history(messages),
        )

    @app.post(
        "/sessions/{session_id}/threads/{thread_id}/history/clear",
        operation_id="clear_thread_history",
    )
    async def clear_thread_history(
        session_id: str,
        thread_id: str,
    ) -> HistoryMutationResponse:
        ctx = await manager.get(session_id, thread_id)
        removed_turns = await clear_history(ctx)
        return HistoryMutationResponse(
            session_id=session_id,
            thread_id=thread_id,
            removed_turns=removed_turns,
            messages=[],
        )

    @app.post(
        "/sessions/{session_id}/threads/{thread_id}/history/undo",
        operation_id="undo_thread_history",
    )
    async def undo_thread_history(
        session_id: str,
        thread_id: str,
        payload: UndoRequest,
    ) -> HistoryMutationResponse:
        ctx = await manager.get(session_id, thread_id)
        messages = await undo_history(ctx, payload.count)
        return HistoryMutationResponse(
            session_id=session_id,
            thread_id=thread_id,
            removed_turns=payload.count,
            messages=display_history(messages),
        )

    @app.get(
        "/sessions/{session_id}/threads/{thread_id}/tasks",
        operation_id="list_tasks",
    )
    async def list_tasks_endpoint(
        session_id: str,
        thread_id: str,
    ) -> TaskListResponse:
        ctx = await manager.get(session_id, thread_id)
        return TaskListResponse(
            session_id=session_id,
            thread_id=thread_id,
            tasks=task_snapshots(ctx),
        )

    @app.post(
        "/sessions/{session_id}/threads/{thread_id}/tasks/{task_id}/stop",
        operation_id="stop_task",
    )
    async def stop_task_endpoint(
        session_id: str,
        thread_id: str,
        task_id: str,
    ) -> TaskStopResponse:
        ctx = await manager.get(session_id, thread_id)
        task = await stop_task(ctx, task_id)
        return TaskStopResponse(
            session_id=session_id,
            thread_id=thread_id,
            matched_count=1,
            tasks=[task],
        )

    @app.post(
        "/sessions/{session_id}/threads/{thread_id}/tasks/stop",
        operation_id="stop_all_tasks",
    )
    async def stop_all_tasks_endpoint(
        session_id: str,
        thread_id: str,
    ) -> TaskStopResponse:
        ctx = await manager.get(session_id, thread_id)
        tasks = await stop_all_tasks(ctx)
        return TaskStopResponse(
            session_id=session_id,
            thread_id=thread_id,
            matched_count=len(tasks),
            tasks=tasks,
        )

    @app.get(
        "/sessions/{session_id}/threads/{thread_id}/commands",
        operation_id="list_commands",
        include_in_schema=False,
    )
    async def session_commands(
        session_id: str,
        thread_id: str,
    ) -> CommandListResponse:
        ctx = await manager.get(session_id, thread_id)
        loader = ctx.engine.plugin_loader
        return CommandListResponse(
            commands=list_commands(extra=loader.commands if loader is not None else ())
        )

    @app.post(
        "/sessions/{session_id}/threads/{thread_id}/commands",
        operation_id="run_command",
        include_in_schema=False,
    )
    async def run_command(
        session_id: str,
        thread_id: str,
        payload: CommandRequest,
    ) -> CommandResponse:
        ctx = await manager.get(session_id, thread_id)
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

    @app.post(
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
                        await close_disconnected_runtime(manager, ctx)
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

    @app.get(
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
                    await close_disconnected_runtime(manager, ctx)

        return StreamingResponse(
            sse_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/sessions/{session_id}/threads/{thread_id}/interactions/permission-response",
        operation_id="respond_permission",
    )
    async def post_permission_response(
        session_id: str,
        thread_id: str,
        payload: PermissionResponseRequest,
        request: Request,
    ) -> InteractionResponse:
        return await _resolve_interaction(
            manager=request.app.state.manager,
            session_id=session_id,
            thread_id=thread_id,
            payload=payload.model_dump(),
            kind="permission",
        )

    @app.post(
        "/sessions/{session_id}/threads/{thread_id}/interactions/user-input",
        operation_id="respond_user_input",
    )
    async def post_user_input(
        session_id: str,
        thread_id: str,
        payload: UserInputResponseRequest,
        request: Request,
    ) -> InteractionResponse:
        return await _resolve_interaction(
            manager=request.app.state.manager,
            session_id=session_id,
            thread_id=thread_id,
            payload=payload.model_dump(),
            kind="user_input",
        )

    @app.post(
        "/sessions/{session_id}/close",
        operation_id="close_session",
    )
    async def shutdown_session(session_id: str) -> CloseResponse:
        await manager.close_session(session_id)
        return CloseResponse(session_id=session_id)

    @app.post(
        "/sessions/{session_id}/threads/{thread_id}/close",
        operation_id="close_thread",
    )
    async def close_thread(session_id: str, thread_id: str) -> CloseResponse:
        await manager.close_thread(session_id, thread_id)
        return CloseResponse(session_id=session_id, thread_id=thread_id)

    @app.post(
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

    @app.exception_handler(OperationError)
    async def _on_operation_error(
        _: Request, exc: OperationError
    ) -> JSONResponse:
        if exc.code.endswith("_not_found"):
            status = 404
        elif exc.code in {"thread_busy", "task_not_background"}:
            status = 409
        else:
            status = 400
        return JSONResponse(
            status_code=status,
            content=_error_payload(
                exc.code,
                exc.message,
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

    @app.exception_handler(ThreadNotActive)
    async def _on_thread_not_active(
        _: Request,
        exc: ThreadNotActive,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_error_payload(
                "thread_not_active",
                str(exc),
                retryable=True,
            ),
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


async def _open_session_response(ctx: SessionRuntime) -> OpenSessionResponse:
    loader = getattr(ctx.engine, "plugin_loader", None)
    status_slots = await loader.status_slots() if loader is not None else {}
    return OpenSessionResponse(
        session_id=ctx.session_id,
        thread_id=ctx.thread_id,
        agent_name=getattr(ctx.engine.config, "agent_name", "XBotv2"),
        workspace_root=ctx.workspace_root,
        provider=ctx.provider_name,
        model=str(getattr(ctx.engine, "model", "")),
        model_mode=str(getattr(ctx.engine, "model_mode", "")),
        context_window=int(getattr(ctx.engine, "context_window", 0)),
        usage=ctx.engine.session_usage,
        history=display_history(ctx.engine.messages),
        status_slots=status_slots,
    )


def _session_policy_response(
    session_id: str,
    policy: dict[str, Any],
    effective_sandbox: dict[str, Any],
) -> SessionPolicyResponse:
    return SessionPolicyResponse(
        session_id=session_id,
        permissions=policy.get("permissions") or {},
        sandbox=policy.get("sandbox") or {},
        effective_sandbox=effective_sandbox,
    )


async def _effective_sandbox(
    manager: SessionManager,
    session_id: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    active = await manager.active_threads()
    contexts = [
        ctx
        for (active_session_id, _), ctx in active.items()
        if active_session_id == session_id
    ]
    if contexts:
        return contexts[0].engine.sandbox_policy.to_dict()

    thread_ids = persisted_thread_ids(manager.paths, session_id)
    workspace = Path.cwd()
    if thread_ids:
        thread_id = "agent" if "agent" in thread_ids else thread_ids[0]
        store = CoreStateStore(
            manager.paths.session(session_id),
            thread_id=thread_id,
            workspace_root="",
            provider="",
        )
        metadata = store.read_thread_metadata()
        workspace = Path(metadata.get("workspace_root") or workspace)
    config = load_system_config(manager.paths, workspace)
    return merge_sandbox_config(config.sandbox, policy.get("sandbox"))


async def _resolve_interaction(
    *,
    manager: SessionManager,
    session_id: str,
    thread_id: str,
    payload: dict[str, Any],
    kind: str,
) -> InteractionResponse:
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise HttpServerError(
            "invalid_request",
            f"{kind}.response payload.request_id must be non-empty",
            status=400,
        )
    ctx = await manager.get(session_id, thread_id)

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
        return InteractionResponse(
            request_id=request_id,
            pending_interactions=pending_interactions(ctx),
        )

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
    return InteractionResponse(
        request_id=request_id,
        pending_interactions=pending_interactions(ctx),
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
