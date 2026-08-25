"""Live thread ownership and persisted session resource summaries."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from XBotv2.core.paths import RuntimePaths, SessionPaths
from XBotv2.core.errors import OperationError
from XBotv2.core.artifacts import ArtifactKind
from XBotv2.core.messages import ImageContent, Message
from XBotv2.core.tools import JsonObject
from XBotv2.persistence import ThreadPersistenceFactory, ThreadPersistencePort
from XBotv2.usage.models import UsageSnapshot
from XBotv2.session.runtime import SessionRuntime, require_idle
from XBotv2.session.contracts import (
    AgentApplicationFactory,
    AgentApplicationOptions,
    DISPATCH_OPERATION,
    DISPATCH_SESSION_OPERATION,
    OPERATION_COMPLETED,
    PREPARE_FORK,
    PrepareFork,
    SessionDispatch,
    SessionGroupDispatch,
    SessionOperationCompleted,
    SessionRef,
)
from XBotv2.session.session import fork_persisted_session
from XBotv2.session.types import (
    HistoryMutation,
    InteractionReceipt,
    InterruptResult,
    OpenedSession,
    OpenSession,
    OpenThread,
    SendMessage,
    SessionExists,
    SessionNotFound,
    SessionStreamEvent,
    SessionSnapshot,
    ThreadNotActive,
    ThreadSnapshot,
)
from XBotv2.server import QUERY_STATUS, ServerStatus
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


class SessionManager:
    """Own active thread runtimes grouped by persistent session id."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        idle_timeout: float | None = 3600.0,
        reap_interval: float = 60.0,
        thread_persistence_factory: ThreadPersistenceFactory | None = None,
        application_factory: AgentApplicationFactory | None = None,
    ) -> None:
        self.paths = paths
        self.idle_timeout = idle_timeout
        self.reap_interval = reap_interval
        self.thread_persistence_factory = thread_persistence_factory
        self.application_factory = application_factory
        self._sessions: dict[tuple[str, str], SessionRuntime] = {}
        self._opening: dict[
            tuple[str, str], asyncio.Task[SessionRuntime]
        ] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    def _thread_persistence(
        self,
        session_paths: SessionPaths,
        *,
        thread_id: str,
        workspace_root: str = "",
        provider: str = "",
    ) -> ThreadPersistencePort:
        """Construct a persisted-state reader through the persistence host."""
        if self.thread_persistence_factory is None:
            raise OperationError(
                "persistence_unavailable",
                "no thread_persistence_factory (persistence host not mounted)",
            )
        return self.thread_persistence_factory(
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
                and not ctx.event_streams
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
        plugin_configs: dict[str, JsonObject] | None = None,
        llm_override: Any | None = None,
        parent_thread_id: str = "",
        parent_permission_system: Any | None = None,
        is_subagent: bool = False,
    ) -> SessionRuntime:
        mode = (mode or "new").lower().strip()
        if mode not in {"new", "resume"}:
            raise ValueError("session mode must be new or resume")
        if mode == "resume" and not session_id:
            raise ValueError("resume mode requires session_id")
        if mode == "new":
            session_id = session_id or _new_session_id()
        assert session_id is not None
        key = (session_id, thread_id)

        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                if mode == "resume":
                    existing.touch()
                    return existing
                else:
                    raise SessionExists(f"{session_id}/{thread_id}")
            opening = self._opening.get(key)
            if opening is not None:
                if mode == "new":
                    raise SessionExists(f"{session_id}/{thread_id}")
                task = opening
            else:
                task = self._prepare_open_task(
                    key=key,
                    session_id=session_id,
                    thread_id=thread_id,
                    provider_name=provider_name,
                    workspace_root=workspace_root,
                    selected_agent=selected_agent,
                    mode=mode,
                    no_plugins=no_plugins,
                    plugin_configs=plugin_configs,
                    llm_override=llm_override,
                    parent_thread_id=parent_thread_id,
                    parent_permission_system=parent_permission_system,
                    is_subagent=is_subagent,
                )
                self._opening[key] = task
        return await asyncio.shield(task)

    def _prepare_open_task(
        self,
        *,
        key: tuple[str, str],
        session_id: str,
        thread_id: str,
        provider_name: str,
        workspace_root: str,
        selected_agent: str | None,
        mode: str,
        no_plugins: bool,
        plugin_configs: dict[str, JsonObject] | None,
        llm_override: Any | None,
        parent_thread_id: str,
        parent_permission_system: Any | None,
        is_subagent: bool,
    ) -> asyncio.Task[SessionRuntime]:
        return asyncio.create_task(
            self._build_and_register(
                key=key,
                session_id=session_id,
                thread_id=thread_id,
                provider_name=provider_name,
                workspace_root=workspace_root,
                selected_agent=selected_agent,
                mode=mode,
                no_plugins=no_plugins,
                plugin_configs=plugin_configs,
                llm_override=llm_override,
                parent_thread_id=parent_thread_id,
                parent_permission_system=parent_permission_system,
                is_subagent=is_subagent,
            ),
            name=f"xbotv2-open-{session_id}-{thread_id}",
        )

    async def _build_and_register(
        self,
        *,
        key: tuple[str, str],
        session_id: str,
        thread_id: str,
        provider_name: str,
        workspace_root: str,
        selected_agent: str | None,
        mode: str,
        no_plugins: bool,
        plugin_configs: dict[str, JsonObject] | None,
        llm_override: Any | None,
        parent_thread_id: str,
        parent_permission_system: Any | None,
        is_subagent: bool,
    ) -> SessionRuntime:
        try:
            session_paths = self.paths.session(session_id)
            if mode == "resume" and not session_paths.has_thread(thread_id):
                raise SessionNotFound(f"{session_id}/{thread_id}")
            had_persisted_session = (
                mode == "resume"
                and _has_persisted_session(session_paths, thread_id)
            )
            if mode == "resume" and not had_persisted_session:
                raise SessionNotFound(
                    f"{session_id}/{thread_id} has no persisted session"
                )
            if mode == "new" and session_paths.has_thread(thread_id):
                raise SessionExists(f"{session_id}/{thread_id}")
            if self.application_factory is None:
                raise OperationError(
                    "application_factory_unavailable",
                    "session management has no Agent application factory",
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
            async with self._lock:
                self._sessions[key] = ctx
            return ctx
        finally:
            async with self._lock:
                current = asyncio.current_task()
                if self._opening.get(key) is current:
                    self._opening.pop(key, None)

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
            opening = self._opening.get(key)
            ctx = self._sessions.get(key)
            if expected is not None and ctx is not expected:
                return
            ctx = self._sessions.pop(key, None)
        if opening is not None:
            try:
                ctx = await asyncio.shield(opening)
            except Exception:
                ctx = None
            else:
                async with self._lock:
                    self._sessions.pop(key, None)
        if ctx is not None:
            await ctx.close(reason)

    async def close_session(
        self,
        session_id: str,
        *,
        reason: str = "session_closed",
    ) -> None:
        async with self._lock:
            opening = [
                task
                for (active_session_id, _), task in self._opening.items()
                if active_session_id == session_id
            ]
            contexts = [
                ctx
                for (active_session_id, _), ctx in self._sessions.items()
                if active_session_id == session_id
            ]
            for ctx in contexts:
                self._sessions.pop((ctx.session_id, ctx.thread_id), None)
        for task in opening:
            try:
                runtime = await asyncio.shield(task)
            except Exception:
                continue
            async with self._lock:
                self._sessions.pop((runtime.session_id, runtime.thread_id), None)
            contexts.append(runtime)
        for ctx in contexts:
            await ctx.close(reason)

    async def close_all(self) -> None:
        async with self._lock:
            opening = list(self._opening.values())
        if opening:
            await asyncio.gather(
                *(asyncio.shield(task) for task in opening),
                return_exceptions=True,
            )
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

    def session_exists(self, session_id: str) -> bool:
        return self.paths.session(session_id).root.is_dir()

    async def open(self, request: OpenSession) -> OpenedSession:
        runtime = await self.open_session(
            session_id=request.session_id,
            thread_id=request.thread_id,
            provider_name=request.provider_name,
            workspace_root=request.workspace_root,
            selected_agent=request.selected_agent,
            mode=request.mode,
            no_plugins=request.no_plugins,
            llm_override=request.model_override,
            plugin_configs=request.plugin_configs,
        )
        return await _opened_session(runtime)

    async def list_sessions(self) -> tuple[SessionSnapshot, ...]:
        root = self.paths.sessions_dir
        session_ids = sorted(
            path.name for path in root.iterdir() if path.is_dir()
        ) if root.is_dir() else []
        return tuple([
            await session_summary(self, session_id)
            for session_id in session_ids
        ])

    async def session_summary(self, session_id: str) -> SessionSnapshot:
        return await session_summary(self, session_id)

    async def fork_session(self, session_id: str) -> str:
        await session_summary(self, session_id)
        active = await self.active_threads()
        runtimes = [
            runtime
            for (active_session_id, _), runtime in active.items()
            if active_session_id == session_id
        ]
        if any(runtime.turn_lock.locked() for runtime in runtimes):
            raise OperationError(
                "thread_busy",
                "Cannot fork while a session thread has an active turn.",
                retryable=True,
            )
        if any(not runtime.application.persistence_available for runtime in runtimes):
            raise OperationError(
                "persistence_unavailable",
                f"Cannot fork {session_id}: message persistence is not mounted",
            )
        for runtime in runtimes:
            await runtime.application.events.emit(
                PREPARE_FORK,
                PrepareFork(session_id, runtime.thread_id),
            )
        return fork_persisted_session(self.paths, session_id)

    async def list_threads(self, session_id: str) -> tuple[ThreadSnapshot, ...]:
        await session_summary(self, session_id)
        return tuple([
            await thread_summary(self, session_id, thread_id)
            for thread_id in persisted_thread_ids(self.paths, session_id)
        ])

    async def open_thread(self, request: OpenThread) -> OpenedSession:
        await session_summary(self, request.session_id)
        parent_thread_id = request.parent_thread_id
        if request.mode == "resume":
            session = self.paths.session(request.session_id)
            if not session.has_thread(request.thread_id):
                raise SessionNotFound(
                    f"{request.session_id}/{request.thread_id}"
                )
            persistence = self._thread_persistence(
                session,
                thread_id=request.thread_id,
            )
            parent_thread_id = str(
                persistence.metadata.load().parent_thread_id
            )
        if not parent_thread_id or parent_thread_id == request.thread_id:
            raise OperationError(
                "invalid_request",
                "A subagent thread requires a different parent_thread_id",
            )
        try:
            parent = await self.get(request.session_id, parent_thread_id)
        except (SessionNotFound, ThreadNotActive) as exc:
            raise OperationError(
                "parent_thread_not_active",
                str(exc),
                retryable=True,
            ) from exc
        workspace_root = str(Path(
            request.workspace_root or parent.workspace_root
        ).resolve())
        runtime = await self.open_session(
            session_id=request.session_id,
            thread_id=request.thread_id,
            provider_name=request.provider_name,
            workspace_root=workspace_root,
            mode=request.mode,
            selected_agent=request.selected_agent,
            no_plugins=request.no_plugins,
            llm_override=request.model_override,
            parent_thread_id=parent_thread_id,
            parent_permission_system=parent.application.parent_permissions,
            is_subagent=True,
        )
        return await _opened_session(runtime)

    async def thread_summary(
        self,
        session_id: str,
        thread_id: str,
    ) -> ThreadSnapshot:
        return await thread_summary(self, session_id, thread_id)

    async def messages(
        self,
        session_id: str,
        thread_id: str,
    ) -> tuple[Message, ...]:
        active = (await self.active_threads()).get((session_id, thread_id))
        if active is not None:
            return tuple(active.engine.messages)
        session = self.paths.session(session_id)
        if not session.has_thread(thread_id):
            raise SessionNotFound(f"{session_id}/{thread_id}")
        persistence = self._thread_persistence(session, thread_id=thread_id)
        return tuple(persistence.history.load())

    async def clear_history(
        self,
        session_id: str,
        thread_id: str,
    ) -> HistoryMutation:
        runtime = await self.get(session_id, thread_id)
        require_idle(runtime, "rewrite history")
        async with runtime.turn_lock:
            removed = await runtime.application.history.clear_history()
        return HistoryMutation(removed_turns=removed, messages=())

    async def undo_history(
        self,
        session_id: str,
        thread_id: str,
        count: int,
    ) -> HistoryMutation:
        runtime = await self.get(session_id, thread_id)
        require_idle(runtime, "rewrite history")
        async with runtime.turn_lock:
            messages = await runtime.application.history.undo_history(count)
        return HistoryMutation(removed_turns=count, messages=tuple(messages))

    async def stream_message(
        self,
        request: SendMessage,
    ) -> AsyncIterator[SessionStreamEvent]:
        runtime = await self.get(request.session_id, request.thread_id)
        images = []
        for item in request.images:
            ref = runtime.application.artifacts.put(
                ArtifactKind.MEDIA,
                _upload_bytes(item.data),
                media_type=item.media_type,
            )
            images.append(ImageContent(ref.id, ref.media_type, ref.size))
        attachments = [
            runtime.application.artifacts.put(
                ArtifactKind.ATTACHMENT,
                _upload_bytes(item.data),
                media_type=item.media_type,
                name=item.name,
            )
            for item in request.attachments
        ]
        async def stream():
            async for event in runtime.stream_message(
                request.content,
                request.request_id,
                images=images,
                artifacts=attachments,
            ):
                yield SessionStreamEvent.from_mapping(event)

        return stream()

    async def stream_events(
        self,
        session_id: str,
        thread_id: str,
    ) -> AsyncIterator[SessionStreamEvent]:
        runtime = await self.get(session_id, thread_id)
        events = runtime.attach_event_stream()
        async def stream():
            try:
                while True:
                    event = await events.get()
                    if event is None:
                        return
                    yield SessionStreamEvent.from_mapping(event)
            finally:
                runtime.detach_event_stream(events)

        return stream()

    async def respond_permission(
        self,
        session_id: str,
        thread_id: str,
        request_id: str,
        decision: str,
        scope: str,
    ) -> InteractionReceipt:
        return await self._respond_interaction(
            session_id,
            thread_id,
            "permission_request",
            request_id,
            decision=decision,
            scope=scope,
        )

    async def respond_user_input(
        self,
        session_id: str,
        thread_id: str,
        request_id: str,
        answer: Any,
    ) -> InteractionReceipt:
        return await self._respond_interaction(
            session_id,
            thread_id,
            "user_input_required",
            request_id,
            answer=answer,
        )

    async def cancel_interaction(
        self,
        session_id: str,
        thread_id: str,
        event_type: Literal["permission_request", "user_input_required"],
        request_id: str,
        reason: str,
    ) -> InteractionReceipt:
        runtime = await self.get(session_id, thread_id)
        waiter = runtime.application.client_events.waiter(event_type)
        if waiter is None:
            raise OperationError(
                "capability_unavailable",
                f"No waiter is registered for {event_type!r}",
            )
        try:
            waiter.cancel(request_id, reason)
        except Exception as exc:
            raise OperationError(
                "interaction_no_longer_pending",
                str(exc),
            ) from exc
        return InteractionReceipt(
            request_id=request_id,
            pending_interactions=tuple(pending_interactions(runtime)),
        )

    async def _respond_interaction(
        self,
        session_id: str,
        thread_id: str,
        event_type: str,
        request_id: str,
        **values: object,
    ) -> InteractionReceipt:
        runtime = await self.get(session_id, thread_id)
        waiter = runtime.application.client_events.waiter(event_type)
        if waiter is None:
            raise OperationError(
                "capability_unavailable",
                f"No waiter is registered for {event_type!r}",
            )
        try:
            waiter.answer(request_id, **values)
        except Exception as exc:
            raise OperationError(
                "interaction_no_longer_pending",
                str(exc),
            ) from exc
        return InteractionReceipt(
            request_id=request_id,
            pending_interactions=tuple(pending_interactions(runtime)),
        )

    async def interrupt(
        self,
        session_id: str,
        thread_id: str,
    ) -> InterruptResult:
        runtime = await self.get(session_id, thread_id)
        return InterruptResult(cancelled=runtime.request_interrupt())

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
        """Route an operation with its manager-owned typed session scope."""
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
    return session_paths.thread(thread_id).metadata_file.exists()


async def _opened_session(runtime: SessionRuntime) -> OpenedSession:
    snapshot = await runtime.application.snapshot()
    return OpenedSession(
        session_id=runtime.session_id,
        thread_id=runtime.thread_id,
        agent_name=snapshot.agent,
        workspace_root=runtime.workspace_root,
        provider=runtime.provider_name,
        model=snapshot.model,
        model_mode=snapshot.model_mode,
        context_window=snapshot.context_window,
        usage=snapshot.usage,
        history=snapshot.messages,
        status_slots=snapshot.status_slots,
    )


def persisted_thread_ids(paths: RuntimePaths, session_id: str) -> list[str]:
    session = paths.session(session_id)
    thread_ids: set[str] = set()
    if session.threads_dir.is_dir():
        thread_ids.update(
            path.name for path in session.threads_dir.iterdir() if path.is_dir()
        )
    return sorted(thread_ids)


async def thread_summary(
    manager: SessionManager,
    session_id: str,
    thread_id: str,
) -> ThreadSnapshot:
    active = (await manager.active_threads()).get((session_id, thread_id))
    if active is not None:
        snapshot = await active.application.snapshot()
        metadata = snapshot.metadata
        parent_thread_id = str(metadata.get("parent_thread_id") or "")
        return ThreadSnapshot(
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
            workspace_root=active.workspace_root,
            title=str(metadata.get("title") or session_id),
        )

    session = manager.paths.session(session_id)
    if not session.has_thread(thread_id):
        raise SessionNotFound(f"{session_id}/{thread_id}")
    persistence = manager._thread_persistence(
        session,
        thread_id=thread_id,
    )
    metadata = persistence.metadata.load()
    parent_thread_id = metadata.parent_thread_id
    return ThreadSnapshot(
        session_id=session_id,
        thread_id=thread_id,
        status="inactive",
        kind="subagent" if parent_thread_id else "main",
        parent_thread_id=parent_thread_id,
        agent=metadata.agent,
        provider=metadata.provider,
        model=metadata.model,
        model_mode=metadata.model_mode,
        context_window=metadata.context_window,
        message_count=persistence.history.count(),
        usage=await _read_usage(persistence),
        workspace_root=metadata.workspace_root,
        title=metadata.title or session_id,
    )


async def _read_usage(
    persistence: ThreadPersistencePort,
) -> dict[str, int]:
    stored = await persistence.state.namespace("usage").get("snapshot")
    if stored is None:
        return _empty_usage()
    if not isinstance(stored, dict):
        raise TypeError("Persisted usage snapshot must be an object")
    return UsageSnapshot.from_dict(stored).totals()


def _upload_bytes(data: str) -> bytes:
    try:
        payload = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("upload data must be valid base64") from exc
    if not payload:
        raise ValueError("upload data must not be empty")
    return payload


async def session_summary(
    manager: SessionManager,
    session_id: str,
) -> SessionSnapshot:
    session = manager.paths.session(session_id)
    active = await manager.active_threads()
    active_ids = {
        thread_id
        for (active_session_id, thread_id) in active
        if active_session_id == session_id
    }
    if not session.root.is_dir() and not active_ids:
        raise SessionNotFound(session_id)
    thread_ids = sorted(
        set(persisted_thread_ids(manager.paths, session_id)) | active_ids
    )
    active_threads = len(active_ids)
    main_id = "agent" if "agent" in thread_ids else None
    if main_id is None:
        for candidate_id in thread_ids:
            candidate = await thread_summary(manager, session_id, candidate_id)
            if not candidate.parent_thread_id:
                main_id = candidate_id
                break
    main = await thread_summary(manager, session_id, main_id) if main_id else None
    return SessionSnapshot(
        session_id=session_id,
        status="active" if active_threads else "inactive",
        active_threads=active_threads,
        thread_count=len(thread_ids),
        workspace_root=main.workspace_root if main is not None else "",
        title=main.title if main is not None else session_id,
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


class SessionManagerComponent:
    """Provide process-level Session management to carrier profiles.

    Requires the persistence read service and exposes lifecycle operations
    only through the public SessionsPort and typed dispatch events.
    """

    name = "xbot.session.manager"
    inject = [
        "thread_persistence_factory",
        "runtime_paths",
        "agent_application_factory",
        "workspace_root",
    ]

    def apply(self, ctx, config=None) -> None:
        manager = SessionManager(
            ctx.runtime_paths,
            thread_persistence_factory=ctx.thread_persistence_factory,
            application_factory=ctx.agent_application_factory,
        )
        ctx.set("sessions", manager)
        handlers = SessionManagerHandlers(
            manager,
            ctx,
            workspace_root=str(ctx.workspace_root),
        )
        ctx.on(DISPATCH_OPERATION, handlers.dispatch)
        ctx.on(DISPATCH_SESSION_OPERATION, handlers.dispatch_all)
        ctx.on(QUERY_STATUS, handlers.status)
        manager.start_reaper()
        ctx.dispose(manager.close_all)


class SessionManagerHandlers:
    def __init__(
        self,
        manager: SessionManager,
        events: Any,
        *,
        workspace_root: str,
    ) -> None:
        self._manager = manager
        self._events = events
        self._workspace_root = workspace_root

    async def dispatch(self, envelope: SessionDispatch) -> object:
        if not isinstance(envelope, SessionDispatch):
            raise TypeError("session/dispatch requires SessionDispatch")
        runtime = await self._manager.get(
            envelope.target.session_id,
            envelope.target.thread_id,
        )
        runtime.touch()
        if not envelope.operation.requires_exclusive(envelope.request):
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
        await self._completed(envelope.target, envelope.operation.name, result)
        return result

    async def dispatch_all(self, envelope: SessionGroupDispatch) -> object:
        if not isinstance(envelope, SessionGroupDispatch):
            raise TypeError("session/dispatch-all requires SessionGroupDispatch")
        active = await self._manager.active_threads()
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
        exclusive = envelope.operation.requires_exclusive(envelope.request)
        if exclusive and any(runtime.turn_lock.locked() for runtime in runtimes):
            raise OperationError(
                "thread_busy",
                f"Cannot run {envelope.operation.name!r} while a turn is active.",
                retryable=True,
            )
        async with AsyncExitStack() as stack:
            if exclusive:
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
                await self._completed(
                    SessionRef(envelope.session_id, runtime.thread_id),
                    envelope.operation.name,
                    result,
                )
        return tuple(results)

    async def _completed(self, target: SessionRef, name: str, result: object) -> None:
        await self._events.emit(
            OPERATION_COMPLETED,
            SessionOperationCompleted(
                target=target,
                operation_name=name,
                result=result,
            ),
        )

    def status(self) -> ServerStatus:
        return ServerStatus(
            sessions=self._manager.size,
            threads=self._manager.thread_count,
            workspace_root=self._workspace_root,
        )


plugin = SessionManagerComponent()
