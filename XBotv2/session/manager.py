"""Live thread ownership and persisted session resource summaries."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from XBotv2.core.paths import RuntimePaths
from XBotv2.core.errors import OperationError
from XBotv2.session.runtime import SessionRuntime
from XBotv2.protocol.models import SessionSummary, ThreadSummary
from XBotv2.session.contracts import (
    AgentApplicationFactory,
    AgentApplicationOptions,
    DISPATCH_OPERATION,
    DISPATCH_SESSION_OPERATION,
    OPERATION_COMPLETED,
    SessionDispatch,
    SessionGroupDispatch,
    SessionOperationCompleted,
    SessionRef,
)
from XBotv2.server.contracts import QUERY_STATUS, ServerStatus
from XBotv2.core.operations import (
    Operation,
    RequestT,
    ResponseT,
    ScopeT,
    ScopedOperation,
    dispatch_operation,
    dispatch_scoped_operation,
)

logger = logging.getLogger("xbotv2.session_manager")


class SessionNotFound(KeyError):
    """The caller asked for a session or thread that does not exist."""


class SessionExists(RuntimeError):
    """A new session or thread conflicts with persisted state."""


class ThreadNotActive(RuntimeError):
    """The thread exists on disk but has no live runtime."""


class SessionManager:
    """Own active thread runtimes grouped by persistent session id."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        idle_timeout: float | None = 3600.0,
        reap_interval: float = 60.0,
        state_store_factory: Any | None = None,
        application_factory: AgentApplicationFactory | None = None,
    ) -> None:
        self.paths = paths
        self.idle_timeout = idle_timeout
        self.reap_interval = reap_interval
        self.state_store_factory = state_store_factory
        self.application_factory = application_factory
        self._sessions: dict[tuple[str, str], SessionRuntime] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    def _state_store(
        self,
        session_paths: Any,
        *,
        thread_id: str,
        workspace_root: str = "",
        provider: str = "",
    ) -> Any:
        """Construct a persisted-state reader through the persistence host."""
        if self.state_store_factory is None:
            raise OperationError(
                "persistence_unavailable",
                "no state_store_factory (persistence host not mounted)",
            )
        return self.state_store_factory(
            session_paths,
            thread_id=thread_id,
            workspace_root=workspace_root,
            provider=provider,
        )

    def start_reaper(self) -> None:
        """Start the idle-reaper loop; idempotent."""
        if self._reaper is not None and not self._reaper.done():
            return
        self._reaper = asyncio.create_task(
            self._reap_idle_loop(), name="xbotv2-session-reaper"
        )

    async def _reap_idle_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reap_interval)
            try:
                await self._reap_idle()
            except Exception:
                logger.exception("idle reaper pass failed")

    async def _reap_idle(self) -> None:
        if self.idle_timeout is None or self.idle_timeout <= 0:
            return
        now = time.monotonic()
        async with self._lock:
            due = [
                ctx
                for ctx in self._sessions.values()
                if now - ctx.last_activity >= self.idle_timeout
                and not ctx.turn_lock.locked()
                and not ctx.pending_responses
                and ctx.engine.pending_input_count == 0
                and ctx.session_events is None
            ]
            for ctx in due:
                self._sessions.pop((ctx.session_id, ctx.thread_id), None)
        for ctx in due:
            await ctx.close("idle_timeout")

    @property
    def size(self) -> int:
        return len({session_id for session_id, _ in self._sessions})

    @property
    def thread_count(self) -> int:
        return len(self._sessions)

    def touch(self, session_id: str, thread_id: str) -> None:
        """Mark a runtime active (e.g. on any API interaction)."""
        ctx = self._sessions.get((session_id, thread_id))
        if ctx is not None:
            ctx.touch()

    async def get(self, session_id: str, thread_id: str) -> SessionRuntime:
        async with self._lock:
            ctx = self._sessions.get((session_id, thread_id))
        if ctx is None:
            if self.paths.session(session_id).has_thread(thread_id):
                raise ThreadNotActive(f"{session_id}/{thread_id}")
            raise SessionNotFound(f"{session_id}/{thread_id}")
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
        plugin_configs: dict[str, dict[str, Any]] | None = None,
        llm_override: Any | None = None,
        parent_thread_id: str = "",
        parent_permission_system: Any | None = None,
        is_subagent: bool = False,
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
            key = (session_id, thread_id)
            existing = self._sessions.get(key)
            if existing is not None:
                if mode == "resume":
                    self._sessions.pop(key)
                    await existing.close()
                else:
                    raise SessionExists(f"{session_id}/{thread_id}")
            session_paths = self.paths.session(session_id)
            if mode == "resume" and not session_paths.has_thread(thread_id):
                raise SessionNotFound(f"{session_id}/{thread_id}")
            had_persisted_session = (
                mode == "resume"
                and _has_persisted_session(session_paths, thread_id)
            )
            if mode == "new" and session_paths.has_thread(thread_id):
                raise SessionExists(f"{session_id}/{thread_id}")
            if self.application_factory is None:
                raise OperationError(
                    "application_factory_unavailable",
                    "session host has no Agent application factory",
                )
            application = await self.application_factory(AgentApplicationOptions(
                paths=self.paths,
                provider_name=provider_name,
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=Path(workspace_root),
                no_plugins=no_plugins,
                plugin_configs=plugin_configs,
                model_override=llm_override,
                selected_agent=selected_agent,
                parent_thread_id=parent_thread_id,
                parent_permission_system=parent_permission_system,
                is_subagent=is_subagent,
            ))
            engine = application.driver
            if mode == "resume":
                if not application.persistence_available:
                    await application.close()
                    raise OperationError(
                        "persistence_unavailable",
                        f"Cannot resume {session_id}/{thread_id}: "
                        "message persistence is not mounted",
                    )
                if not had_persisted_session:
                    # A leftover thread directory (state/ exists but the
                    # session never committed thread metadata) must not
                    # silently reopen as an empty session on reconnect.
                    # Remove the fresh metadata the aborted start wrote so
                    # the leftover stays untouched.
                    for thread_paths in (
                        session_paths.thread(thread_id),
                        session_paths.thread(thread_id, legacy=True),
                    ):
                        thread_paths.metadata_file.unlink(missing_ok=True)
                    await application.close()
                    raise SessionNotFound(
                        f"{session_id}/{thread_id} has no persisted session"
                    )
            ctx = SessionRuntime(
                session_id=session_id,
                thread_id=thread_id,
                provider_name=engine.settings.provider,
                paths=self.paths,
                workspace_root=workspace_root,
                no_plugins=no_plugins,
                application=application,
                engine=engine,
            )
            try:
                await engine.start_session()
            except BaseException:
                await ctx.close("session_start_failed")
                raise
            self._sessions[key] = ctx
            return ctx

    async def close_thread(
        self,
        session_id: str,
        thread_id: str,
        *,
        expected: SessionRuntime | None = None,
        reason: str = "session_closed",
    ) -> None:
        async with self._lock:
            key = (session_id, thread_id)
            ctx = self._sessions.get(key)
            if expected is not None and ctx is not expected:
                return
            ctx = self._sessions.pop(key, None)
        if ctx is not None:
            await ctx.close(reason)

    async def close_session(
        self,
        session_id: str,
        *,
        reason: str = "session_closed",
    ) -> None:
        async with self._lock:
            contexts = [
                ctx
                for (active_session_id, _), ctx in self._sessions.items()
                if active_session_id == session_id
            ]
            for ctx in contexts:
                self._sessions.pop((ctx.session_id, ctx.thread_id), None)
        for ctx in contexts:
            await ctx.close(reason)

    async def close_all(self) -> None:
        async with self._lock:
            contexts = list(self._sessions.values())
            self._sessions.clear()
        for ctx in contexts:
            await ctx.close()
        reaper = self._reaper
        self._reaper = None
        if reaper is not None and not reaper.done():
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)

    async def active_threads(self) -> dict[tuple[str, str], SessionRuntime]:
        async with self._lock:
            return dict(self._sessions)

    async def dispatch(
        self,
        session_id: str,
        thread_id: str,
        operation: Operation[RequestT, ResponseT],
        request: RequestT,
    ) -> ResponseT:
        """Route one typed operation to the owning session application."""
        runtime = await self.get(session_id, thread_id)
        runtime.touch()
        return await dispatch_operation(runtime.application.events, operation, request)

    async def dispatch_scoped(
        self,
        session_id: str,
        thread_id: str,
        operation: ScopedOperation[ScopeT, RequestT, ResponseT],
        request: RequestT,
    ) -> ResponseT:
        """Route an operation with its host-owned typed session scope."""
        runtime = await self.get(session_id, thread_id)
        runtime.touch()
        return await dispatch_scoped_operation(
            runtime.application.events,
            operation,
            runtime,
            request,
        )


def _has_persisted_session(
    session_paths: Any,
    thread_id: str,
) -> bool:
    """Whether a thread has committed real session evidence on disk."""
    if session_paths.thread(thread_id).metadata_file.exists():
        return True
    legacy = session_paths.thread(thread_id, legacy=True)
    return legacy.metadata_file.exists()


def persisted_thread_ids(paths: RuntimePaths, session_id: str) -> list[str]:
    session = paths.session(session_id)
    thread_ids: set[str] = set()
    if session.threads_dir.is_dir():
        thread_ids.update(
            path.name for path in session.threads_dir.iterdir() if path.is_dir()
        )
    if (session.root / "state").is_dir():
        thread_ids.add("agent")
    return sorted(thread_ids)


async def thread_summary(
    manager: SessionManager,
    session_id: str,
    thread_id: str,
) -> ThreadSummary:
    active = (await manager.active_threads()).get((session_id, thread_id))
    if active is not None:
        snapshot = await active.application.snapshot()
        metadata = snapshot.metadata
        parent_thread_id = str(metadata.get("parent_thread_id") or "")
        return ThreadSummary(
            session_id=session_id,
            thread_id=thread_id,
            status="active",
            kind="subagent" if parent_thread_id else "main",
            turn_status="running" if active.turn_lock.locked() else "idle",
            parent_thread_id=parent_thread_id,
            agent=str(metadata.get("agent") or snapshot.agent),
            provider=active.provider_name,
            model=snapshot.model,
            model_mode=snapshot.model_mode,
            context_window=snapshot.context_window,
            message_count=len(snapshot.messages),
            usage=snapshot.usage,
            pending_interactions=pending_interactions(active),
            status_slots=snapshot.status_slots,
        )

    session = manager.paths.session(session_id)
    if not session.has_thread(thread_id):
        raise SessionNotFound(f"{session_id}/{thread_id}")
    store = manager._state_store(
        session,
        thread_id=thread_id,
    )
    metadata = store.read_thread_metadata()
    parent_thread_id = str(metadata.get("parent_thread_id") or "")
    return ThreadSummary(
        session_id=session_id,
        thread_id=thread_id,
        status="inactive",
        kind="subagent" if parent_thread_id else "main",
        parent_thread_id=parent_thread_id,
        agent=str(metadata.get("agent") or ""),
        provider=str(metadata.get("provider") or ""),
        model=str(metadata.get("model") or ""),
        model_mode=str(metadata.get("model_mode") or ""),
        context_window=int(metadata.get("context_window") or 0),
        message_count=store.message_count(),
        usage=_read_usage(store.paths.usage_file),
    )


def _read_usage(path: Any) -> dict[str, int]:
    if not path.exists():
        return _empty_usage()
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return _empty_usage()
    empty = _empty_usage()
    return {key: int(loaded.get(key) or 0) for key in empty}


async def session_summary(
    manager: SessionManager,
    session_id: str,
) -> SessionSummary:
    session = manager.paths.session(session_id)
    if not session.root.is_dir():
        raise SessionNotFound(session_id)
    thread_ids = persisted_thread_ids(manager.paths, session_id)
    active = await manager.active_threads()
    active_threads = sum(
        1 for active_session_id, _ in active if active_session_id == session_id
    )
    return SessionSummary(
        session_id=session_id,
        status="active" if active_threads else "inactive",
        active_threads=active_threads,
        thread_count=len(thread_ids),
    )


def pending_interactions(ctx: SessionRuntime) -> list[str]:
    """List pending requests through the application client-event router."""
    return ctx.application.client_events.pending_request_ids()


def _empty_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
        "context_tokens": 0,
    }


def _new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


__all__ = [
    "SessionExists",
    "SessionManager",
    "SessionNotFound",
    "ThreadNotActive",
    "pending_interactions",
    "persisted_thread_ids",
    "session_summary",
    "thread_summary",
]


class SessionHost:
    """Provides ``ctx.session_host`` in the server composition root.

    Constructed before the dumb web carrier so ``create_app`` can attach the
    host to ``app.state.manager``. Requires the persistence read service.
    """

    name = "xbot.session.host"
    inject = [
        "state_store_factory",
        "runtime_paths",
        "agent_application_factory",
        "server_options",
    ]

    def apply(self, ctx, config=None) -> None:
        manager = SessionManager(
            ctx.runtime_paths,
            state_store_factory=ctx.state_store_factory,
            application_factory=ctx.agent_application_factory,
        )
        ctx.set("session_host", manager)

        async def dispatch(envelope: SessionDispatch) -> object:
            if not isinstance(envelope, SessionDispatch):
                raise TypeError("session/dispatch requires SessionDispatch")
            runtime = await manager.get(
                envelope.target.session_id,
                envelope.target.thread_id,
            )
            runtime.touch()
            if not envelope.operation.exclusive:
                result = await dispatch_operation(
                    runtime.application.events,
                    envelope.operation,
                    envelope.request,
                )
            else:
                if runtime.turn_lock.locked():
                    raise OperationError(
                        "thread_busy",
                        f"Cannot run {envelope.operation.name!r} while a turn is active.",
                        retryable=True,
                    )
                async with runtime.turn_lock:
                    result = await dispatch_operation(
                        runtime.application.events,
                        envelope.operation,
                        envelope.request,
                    )
            await ctx.emit(
                OPERATION_COMPLETED,
                SessionOperationCompleted(
                    target=envelope.target,
                    operation_name=envelope.operation.name,
                    result=result,
                ),
            )
            return result

        ctx.on(DISPATCH_OPERATION, dispatch)

        async def dispatch_all(envelope: SessionGroupDispatch) -> object:
            if not isinstance(envelope, SessionGroupDispatch):
                raise TypeError(
                    "session/dispatch-all requires SessionGroupDispatch"
                )
            active = await manager.active_threads()
            runtimes = sorted(
                (
                    runtime
                    for (session_id, _thread_id), runtime in active.items()
                    if session_id == envelope.session_id
                ),
                key=lambda runtime: runtime.thread_id,
            )
            if not runtimes:
                raise OperationError(
                    "thread_not_active",
                    "Session operations require at least one active thread.",
                )
            if envelope.operation.exclusive and any(
                runtime.turn_lock.locked() for runtime in runtimes
            ):
                raise OperationError(
                    "thread_busy",
                    f"Cannot run {envelope.operation.name!r} while a turn is active.",
                    retryable=True,
                )
            async with AsyncExitStack() as stack:
                if envelope.operation.exclusive:
                    for runtime in runtimes:
                        await stack.enter_async_context(runtime.turn_lock)
                results = []
                for runtime in runtimes:
                    runtime.touch()
                    result = await dispatch_operation(
                        runtime.application.events,
                        envelope.operation,
                        envelope.request,
                    )
                    results.append(result)
                    await ctx.emit(
                        OPERATION_COMPLETED,
                        SessionOperationCompleted(
                            target=SessionRef(
                                envelope.session_id,
                                runtime.thread_id,
                            ),
                            operation_name=envelope.operation.name,
                            result=result,
                        ),
                    )
            return tuple(results)

        ctx.on(DISPATCH_SESSION_OPERATION, dispatch_all)
        ctx.on(
            QUERY_STATUS,
            lambda: ServerStatus(
                sessions=manager.size,
                threads=manager.thread_count,
                workspace_root=str(ctx.server_options.workspace_root),
            ),
        )
        manager.start_reaper()
        ctx.dispose(manager.close_all)


plugin = SessionHost()
