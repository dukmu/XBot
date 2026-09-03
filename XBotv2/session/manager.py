"""Live thread ownership and persisted session resource summaries."""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal, Protocol

from XBotv2.core.paths import RuntimePaths, SessionPaths
from XBotv2.core.runtime_logging import (
    DEFAULT_RUNTIME_LOG,
    RuntimeLog,
    push_log_context,
    reset_log_context,
)
from XBotv2.core.errors import OperationError
from XBotv2.core.artifacts import ArtifactKind, ArtifactRef
from XBotv2.core.messages import ImageContent, Message
from XBotv2.core.tools import ClientEvent
from pydantic import JsonValue
from XBotv2.persistence import ThreadPersistenceFactory, ThreadPersistencePort
from XBotv2.core.usage import UsageData
from XBotv2.core.timing import conversation_stats
from XBotv2.session.runtime import (
    SessionRuntime,
    regenerate_turn_stream,
    require_idle,
)
from XBotv2.session.contracts import (
    AgentApplicationFactory,
    AgentApplicationOptions,
    PREPARE_FORK,
    PrepareFork,
    SESSION_RESOURCE_CHANGED,
    SESSION_RESOURCE_REMOVED,
    SessionResourceChanged,
    SessionResourceRemoved,
)
from XBotv2.session.event_stream import (
    SessionEventFrame,
    SessionEventSubscription,
)
from XBotv2.session.session import delete_persisted_session, fork_persisted_session
from XBotv2.core.history import ConversationPage, HistoryCursorInvalid
from XBotv2.session.types import (
    ArtifactPayload,
    HistoryMutation,
    InteractionReceipt,
    InterruptResult,
    OpenedSession,
    OpenSession,
    OpenThread,
    PendingInputData,
    PendingInputUpdate,
    RegenerateMessage,
    SendMessage,
    SessionExists,
    SessionNotFound,
    SessionSummary,
    ThreadNotActive,
    ThreadSummary,
    new_session_id,
)
from XBotv2.server import QUERY_STATUS, ServerStatus
from XBotv2.core.operations import (
    Operation,
    RequestT,
    ResponseT,
    dispatch_operation,
)

async def _announce_runtime_events(
    manager: "SessionManager",
    runtime: SessionRuntime,
    events: AsyncIterator[ClientEvent],
) -> AsyncIterator[ClientEvent]:
    announced = False
    async for event in events:
        if not announced:
            announced = True
            await manager._emit_session_changed(runtime.session_id)
        yield event


async def _runtime_events(
    runtime: SessionRuntime,
    events: SessionEventSubscription,
) -> AsyncIterator[SessionEventFrame]:
    try:
        async for event in events:
            yield event
    finally:
        runtime.detach_event_stream(events)


class ResourceEvents(Protocol):
    async def emit(self, event: str, *args: object) -> None: ...


class SessionManager:
    """Own active thread runtimes grouped by persistent session id."""

    def __init__(
        self,
        paths: RuntimePaths,
        events: ResourceEvents,
        *,
        idle_timeout: float | None = 3600.0,
        reap_interval: float = 60.0,
        thread_persistence_factory: ThreadPersistenceFactory | None = None,
        application_factory: AgentApplicationFactory | None = None,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self.paths = paths
        self._events = events
        self.idle_timeout = idle_timeout
        self.reap_interval = reap_interval
        self.thread_persistence_factory = thread_persistence_factory
        self.application_factory = application_factory
        self._log = runtime_log.bind("session")
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
            except Exception as exc:
                self._log.exception(
                    "session.reaper.failed",
                    error_type=type(exc).__name__,
                )

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
                and ctx.event_stream.subscriber_count == 0
            ]
            for ctx in due:
                self._sessions.pop((ctx.session_id, ctx.thread_id), None)
        for ctx in due:
            await self._close_runtime(ctx, "idle_timeout")

    async def _close_runtime(
        self,
        runtime: SessionRuntime,
        reason: str,
    ) -> None:
        log_token = push_log_context(
            session_id=runtime.session_id,
            thread_id=runtime.thread_id,
        )
        try:
            await runtime.close(reason)
        finally:
            reset_log_context(log_token)

    @property
    def size(self) -> int:
        return len({session_id for session_id, _ in self._sessions})

    @property
    def thread_count(self) -> int:
        return len(self._sessions)

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
        plugin_configs: dict[str, dict[str, JsonValue]] | None = None,
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
            session_id = session_id or new_session_id()
        assert session_id is not None
        key = (session_id, thread_id)
        self._log.info(
            "session.open.request",
            session_id=session_id,
            thread_id=thread_id,
            mode=mode,
            provider=provider_name,
            workspace_root=workspace_root,
            no_plugins=no_plugins,
        )

        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                if mode == "resume":
                    existing.touch()
                    self._log.info(
                        "session.open.reused",
                        session_id=session_id,
                        thread_id=thread_id,
                    )
                    return existing
                else:
                    raise SessionExists(f"{session_id}/{thread_id}")
            opening = self._opening.get(key)
            if opening is not None:
                if mode == "new":
                    raise SessionExists(f"{session_id}/{thread_id}")
                task = opening
            else:
                workspace = Path(workspace_root).expanduser().resolve()
                if not workspace.is_dir():
                    raise OperationError(
                        "workspace_not_found",
                        f"Workspace path is not an existing directory: {workspace}",
                    )
                task = asyncio.create_task(
                    self._build_and_register(
                        key=key,
                        session_id=session_id,
                        thread_id=thread_id,
                        provider_name=provider_name,
                        workspace_root=str(workspace),
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
                self._opening[key] = task
        return await asyncio.shield(task)

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
        plugin_configs: dict[str, dict[str, JsonValue]] | None,
        llm_override: Any | None,
        parent_thread_id: str,
        parent_permission_system: Any | None,
        is_subagent: bool,
    ) -> SessionRuntime:
        started = time.perf_counter()
        log_token = push_log_context(
            session_id=session_id,
            thread_id=thread_id,
        )
        try:
            session_paths = self.paths.session(session_id)
            session_preexisting = session_paths.root.is_dir()
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
                runtime_log=self._log,
            )
            try:
                await engine.start_session()
            except BaseException:
                await ctx.close("session_start_failed")
                raise
            async with self._lock:
                self._sessions[key] = ctx
            pending_resumed = (
                ctx.resume_pending_inputs() if mode == "resume" else False
            )
            self._log.info(
                "session.opened",
                mode=mode,
                provider=ctx.provider_name,
                resumed=had_persisted_session,
                pending_resumed=pending_resumed,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            await self._events.emit(
                SESSION_RESOURCE_CHANGED,
                SessionResourceChanged(
                    await session_summary(self, session_id),
                    added=not session_preexisting,
                ),
            )
            return ctx
        except BaseException as exc:
            self._log.error(
                "session.open.failed",
                mode=mode,
                error_type=type(exc).__name__,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            raise
        finally:
            async with self._lock:
                current = asyncio.current_task()
                if self._opening.get(key) is current:
                    self._opening.pop(key, None)
            reset_log_context(log_token)

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
            await self._close_runtime(ctx, reason)
            self._log.info(
                "session.thread.closed",
                session_id=session_id,
                thread_id=thread_id,
                reason=reason,
            )
            await self._emit_session_changed(session_id)

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
            await self._close_runtime(ctx, reason)
        self._log.info(
            "session.closed",
            session_id=session_id,
            threads=len(contexts),
            reason=reason,
        )
        if contexts:
            await self._emit_session_changed(session_id)

    async def _emit_session_changed(self, session_id: str) -> None:
        if not self.session_exists(session_id):
            return
        await self._events.emit(
            SESSION_RESOURCE_CHANGED,
            SessionResourceChanged(await session_summary(self, session_id)),
        )

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
        closed_sessions: dict[str, int] = {}
        for ctx in contexts:
            await self._close_runtime(ctx, "session_closed")
            closed_sessions[ctx.session_id] = (
                closed_sessions.get(ctx.session_id, 0) + 1
            )
            self._log.info(
                "session.thread.closed",
                session_id=ctx.session_id,
                thread_id=ctx.thread_id,
                reason="session_closed",
            )
        for session_id, thread_count in closed_sessions.items():
            self._log.info(
                "session.closed",
                session_id=session_id,
                threads=thread_count,
                reason="session_closed",
            )
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

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        root = self.paths.sessions_dir
        session_ids = sorted(
            path.name for path in root.iterdir() if path.is_dir()
        ) if root.is_dir() else []
        active = await self.active_threads()
        return tuple([
            await _build_session_summary(self, session_id, active)
            for session_id in session_ids
        ])

    async def session_summary(self, session_id: str) -> SessionSummary:
        return await session_summary(self, session_id)

    async def rename_session(
        self,
        session_id: str,
        title: str,
    ) -> SessionSummary:
        value = title.strip()
        if not value:
            raise ValueError("Session title must be non-empty")
        if len(value) > 200:
            raise ValueError("Session title must not exceed 200 characters")
        active_threads = await self.active_threads()
        summary = await _build_session_summary(self, session_id, active_threads)
        thread_ids = persisted_thread_ids(self.paths, session_id)
        main_id = "agent" if "agent" in thread_ids else ""
        if not main_id:
            for thread_id in thread_ids:
                thread = await thread_summary(self, session_id, thread_id)
                if not thread.parent_thread_id:
                    main_id = thread_id
                    break
        if not main_id:
            raise OperationError(
                "main_thread_not_found",
                f"Session {session_id!r} has no main thread",
            )
        active = active_threads.get((session_id, main_id))
        if active is not None:
            active.application.loop_state.metadata.update(title=value)
        else:
            persistence = self._thread_persistence(
                self.paths.session(session_id),
                thread_id=main_id,
                workspace_root=summary.workspace_root,
            )
            metadata = persistence.metadata.load()
            persistence.metadata.save(metadata.model_copy(update={"title": value}))
        self._log.info("session.renamed", session_id=session_id)
        renamed = await session_summary(self, session_id)
        await self._events.emit(
            SESSION_RESOURCE_CHANGED,
            SessionResourceChanged(renamed),
        )
        return renamed

    async def fork_session(self, session_id: str) -> str:
        active = await self.active_threads()
        await _build_session_summary(self, session_id, active)
        runtimes = _session_runtimes(active, session_id)
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
            log_token = push_log_context(
                session_id=session_id,
                thread_id=runtime.thread_id,
            )
            try:
                await runtime.application.events.emit(
                    PREPARE_FORK,
                    PrepareFork(session_id, runtime.thread_id),
                )
            finally:
                reset_log_context(log_token)
        forked_id = fork_persisted_session(self.paths, session_id)
        self._log.info(
            "session.forked",
            session_id=session_id,
            forked_session_id=forked_id,
            threads=len(runtimes),
        )
        await self._events.emit(
            SESSION_RESOURCE_CHANGED,
            SessionResourceChanged(
                await session_summary(self, forked_id),
                added=True,
            ),
        )
        return forked_id

    async def delete_session(self, session_id: str) -> None:
        active = await self.active_threads()
        await _build_session_summary(self, session_id, active)
        runtimes = _session_runtimes(active, session_id)
        if any(runtime.turn_lock.locked() for runtime in runtimes):
            raise OperationError(
                "thread_busy",
                "Cannot delete while a session thread has an active turn.",
                retryable=True,
            )
        await self.close_session(session_id, reason="session_deleted")
        delete_persisted_session(self.paths, session_id)
        self._log.info("session.deleted", session_id=session_id)
        await self._events.emit(
            SESSION_RESOURCE_REMOVED,
            SessionResourceRemoved(session_id),
        )

    async def list_threads(self, session_id: str) -> tuple[ThreadSummary, ...]:
        active = await self.active_threads()
        await _build_session_summary(self, session_id, active)
        return tuple([
            await _thread_summary(self, session_id, thread_id, active)
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
    ) -> ThreadSummary:
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
        return tuple(persistence.history.load_transcript())

    async def message_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: str | None,
        limit: int | None,
    ) -> ConversationPage:
        if limit is None:
            if cursor is not None:
                raise OperationError(
                    "invalid_cursor", "A message cursor requires a page limit."
                )
            return ConversationPage(await self.messages(session_id, thread_id))
        session = self.paths.session(session_id)
        if not session.has_thread(thread_id):
            raise SessionNotFound(f"{session_id}/{thread_id}")
        persistence = self._thread_persistence(session, thread_id=thread_id)
        try:
            page = persistence.history.page_transcript(limit=limit, cursor=cursor)
        except HistoryCursorInvalid as exc:
            raise OperationError(
                "invalid_cursor", str(exc)
            ) from exc
        return ConversationPage(
            messages=page.messages,
            next_cursor=page.next_cursor,
        )

    async def artifact(
        self,
        session_id: str,
        thread_id: str,
        artifact_id: str,
    ) -> ArtifactPayload:
        messages = await self.messages(session_id, thread_id)
        ref = _history_artifact(messages, artifact_id)
        if ref is None:
            raise OperationError(
                "artifact_not_found",
                f"Artifact is not referenced by {session_id}/{thread_id}.",
            )
        active = (await self.active_threads()).get((session_id, thread_id))
        store = (
            active.application.artifacts
            if active is not None
            else self._thread_persistence(
                self.paths.session(session_id), thread_id=thread_id
            ).artifacts
        )
        try:
            content = store.read(ref)
        except (FileNotFoundError, ValueError) as exc:
            raise OperationError(
                "artifact_not_found", f"Artifact does not exist: {artifact_id}"
            ) from exc
        return ArtifactPayload(content, ref.media_type, ref.name)

    async def clear_history(
        self,
        session_id: str,
        thread_id: str,
    ) -> HistoryMutation:
        runtime = await self.get(session_id, thread_id)
        require_idle(runtime, "rewrite history")
        log_token = push_log_context(session_id=session_id, thread_id=thread_id)
        try:
            async with runtime.turn_lock:
                removed = await runtime.application.history.clear_history()
        finally:
            reset_log_context(log_token)
        self._log.info(
            "session.history.cleared",
            session_id=session_id,
            thread_id=thread_id,
            removed_turns=removed,
        )
        await self._emit_session_changed(session_id)
        return HistoryMutation(removed_turns=removed, messages=())

    async def undo_history(
        self,
        session_id: str,
        thread_id: str,
        count: int,
    ) -> HistoryMutation:
        runtime = await self.get(session_id, thread_id)
        require_idle(runtime, "rewrite history")
        log_token = push_log_context(session_id=session_id, thread_id=thread_id)
        try:
            async with runtime.turn_lock:
                messages = await runtime.application.history.undo_history(count)
        finally:
            reset_log_context(log_token)
        self._log.info(
            "session.history.undone",
            session_id=session_id,
            thread_id=thread_id,
            removed_turns=count,
            remaining_messages=len(messages),
        )
        await self._emit_session_changed(session_id)
        return HistoryMutation(removed_turns=count, messages=tuple(messages))

    async def stream_message(
        self,
        request: SendMessage,
    ) -> AsyncIterator[ClientEvent]:
        runtime = await self.get(request.session_id, request.thread_id)
        images = []
        for item in request.images:
            ref = runtime.application.artifacts.put(
                ArtifactKind.MEDIA,
                _upload_bytes(item.data),
                media_type=item.media_type,
            )
            images.append(
                ImageContent(path=ref.id, media_type=ref.media_type, size=ref.size)
            )
        attachments = [
            runtime.application.artifacts.put(
                ArtifactKind.ATTACHMENT,
                _upload_bytes(item.data),
                media_type=item.media_type,
                name=item.name,
            )
            for item in request.attachments
        ]
        self._log.info(
            "session.message.accepted",
            session_id=request.session_id,
            thread_id=request.thread_id,
            request_id=request.request_id,
            delivery=request.delivery,
            content_chars=len(request.content),
            images=len(images),
            attachments=len(attachments),
        )
        return _announce_runtime_events(
            self,
            runtime,
            runtime.stream_message(
                request.content,
                request.request_id,
                delivery=request.delivery,
                images=images,
                artifacts=attachments,
            ),
        )

    async def pending_inputs(
        self,
        session_id: str,
        thread_id: str,
    ) -> tuple[PendingInputData, ...]:
        runtime = await self.get(session_id, thread_id)
        return runtime.pending_inputs()

    async def update_pending_input(
        self,
        request: PendingInputUpdate,
    ) -> tuple[PendingInputData, ...]:
        runtime = await self.get(request.session_id, request.thread_id)
        items = await runtime.update_pending_input(
            request.message_id,
            request.action,
            request.content,
        )
        self._log.info(
            "session.queue.updated",
            session_id=request.session_id,
            thread_id=request.thread_id,
            request_id=request.message_id,
            action=request.action,
            pending_inputs=len(items),
        )
        return items

    async def regenerate_message(
        self,
        request: RegenerateMessage,
    ) -> AsyncIterator[ClientEvent]:
        runtime = await self.get(request.session_id, request.thread_id)
        require_idle(runtime, "regenerate a response")
        self._log.info(
            "session.message.regenerated",
            session_id=request.session_id,
            thread_id=request.thread_id,
            request_id=request.request_id,
        )
        return _announce_runtime_events(
            self,
            runtime,
            regenerate_turn_stream(runtime, request_id=request.request_id),
        )

    async def stream_events(
        self,
        session_id: str,
        thread_id: str,
        *,
        after: int | None = None,
    ) -> AsyncIterator[SessionEventFrame]:
        runtime = await self.get(session_id, thread_id)
        events = runtime.attach_event_stream(after)
        return _runtime_events(runtime, events)

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
        cancelled = runtime.request_interrupt()
        self._log.info(
            "session.interrupt",
            session_id=session_id,
            thread_id=thread_id,
            cancelled=cancelled,
        )
        return InterruptResult(cancelled=cancelled)

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
        self._log.debug(
            "session.operation",
            session_id=session_id,
            thread_id=thread_id,
            operation=operation.name,
            exclusive=operation.requires_exclusive(request),
        )
        log_token = push_log_context(session_id=session_id, thread_id=thread_id)
        try:
            if not operation.requires_exclusive(request):
                return await dispatch_operation(
                    runtime.application.events,
                    operation,
                    request,
                )
            require_idle(runtime, f"run {operation.name!r}")
            async with runtime.turn_lock:
                return await dispatch_operation(
                    runtime.application.events,
                    operation,
                    request,
                )
        finally:
            reset_log_context(log_token)

    async def dispatch_all(
        self,
        session_id: str,
        operation: Operation[RequestT, ResponseT],
        request: RequestT,
    ) -> tuple[ResponseT, ...]:
        active = await self.active_threads()
        runtimes = sorted(
            (
                runtime
                for (active_session_id, _), runtime in active.items()
                if active_session_id == session_id
            ),
            key=lambda runtime: runtime.thread_id,
        )
        if not runtimes:
            raise OperationError(
                "thread_not_active",
                "Session operations require at least one active thread.",
            )
        exclusive = operation.requires_exclusive(request)
        if exclusive and any(runtime.turn_lock.locked() for runtime in runtimes):
            raise OperationError(
                "thread_busy",
                f"Cannot run {operation.name!r} while a turn is active.",
                retryable=True,
            )
        async with AsyncExitStack() as stack:
            if exclusive:
                for runtime in runtimes:
                    await stack.enter_async_context(runtime.turn_lock)
            results = []
            for runtime in runtimes:
                runtime.touch()
                results.append(await dispatch_operation(
                    runtime.application.events, operation, request
                ))
        return tuple(results)

def _has_persisted_session(
    session_paths: Any,
    thread_id: str,
) -> bool:
    """Whether a thread has committed real session evidence on disk."""
    return session_paths.thread(thread_id).metadata_file.exists()


async def _opened_session(runtime: SessionRuntime) -> OpenedSession:
    event_cursor = runtime.event_stream.sequence
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
        session_stats=conversation_stats(snapshot.messages),
        history=snapshot.messages,
        status_slots=snapshot.status_slots,
        event_cursor=event_cursor,
        pending_inputs=runtime.pending_inputs(),
    )


def persisted_thread_ids(paths: RuntimePaths, session_id: str) -> list[str]:
    session = paths.session(session_id)
    thread_ids: set[str] = set()
    if session.threads_dir.is_dir():
        thread_ids.update(
            path.name for path in session.threads_dir.iterdir() if path.is_dir()
        )
    return sorted(thread_ids)


def _session_runtimes(
    active: Mapping[tuple[str, str], SessionRuntime],
    session_id: str,
) -> list[SessionRuntime]:
    return [
        runtime
        for (active_session_id, _), runtime in active.items()
        if active_session_id == session_id
    ]


async def thread_summary(
    manager: SessionManager,
    session_id: str,
    thread_id: str,
) -> ThreadSummary:
    return await _thread_summary(
        manager,
        session_id,
        thread_id,
        await manager.active_threads(),
    )


async def _thread_summary(
    manager: SessionManager,
    session_id: str,
    thread_id: str,
    active_threads: Mapping[tuple[str, str], SessionRuntime],
) -> ThreadSummary:
    active = active_threads.get((session_id, thread_id))
    if active is not None:
        snapshot = await active.application.snapshot()
        metadata = snapshot.metadata
        parent_thread_id = metadata.parent_thread_id
        return ThreadSummary(
            session_id=session_id,
            thread_id=thread_id,
            status="active",
            kind="subagent" if parent_thread_id else "main",
            turn_status="running" if active.turn_lock.locked() else "idle",
            parent_thread_id=parent_thread_id,
            agent=metadata.agent or snapshot.agent,
            provider=active.provider_name,
            model=snapshot.model,
            model_mode=snapshot.model_mode,
            context_window=snapshot.context_window,
            message_count=len(snapshot.messages),
            usage=snapshot.usage,
            session_stats=conversation_stats(snapshot.messages),
            pending_interactions=pending_interactions(active),
            status_slots=snapshot.status_slots,
            workspace_root=active.workspace_root,
            title=metadata.title or session_id,
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
    messages = persistence.history.load()
    return ThreadSummary(
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
        session_stats=conversation_stats(messages),
        workspace_root=metadata.workspace_root,
        title=metadata.title or session_id,
    )


async def _read_usage(
    persistence: ThreadPersistencePort,
) -> UsageData:
    stored = await persistence.state.namespace("usage").get("snapshot")
    if stored is None:
        return UsageData()
    if not isinstance(stored, dict):
        raise TypeError("Persisted usage snapshot must be an object")
    return UsageData.from_snapshot(stored)


def _upload_bytes(data: str) -> bytes:
    try:
        payload = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("upload data must be valid base64") from exc
    if not payload:
        raise ValueError("upload data must not be empty")
    return payload


def _history_artifact(
    messages: tuple[Message, ...],
    artifact_id: str,
) -> ArtifactRef | None:
    for message in messages:
        for image in message.images:
            if image.path == artifact_id:
                return ArtifactRef(
                    id=image.path,
                    kind=ArtifactKind.MEDIA,
                    media_type=image.media_type,
                    size=image.size,
                )
        for value in message.artifact or []:
            if isinstance(value, ArtifactRef) and value.id == artifact_id:
                return value
    return None


async def session_summary(
    manager: SessionManager,
    session_id: str,
) -> SessionSummary:
    return await _build_session_summary(manager, session_id, await manager.active_threads())


async def _build_session_summary(
    manager: SessionManager,
    session_id: str,
    active: Mapping[tuple[str, str], SessionRuntime],
) -> SessionSummary:
    session = manager.paths.session(session_id)
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
            candidate = await _thread_summary(
                manager, session_id, candidate_id, active
            )
            if not candidate.parent_thread_id:
                main_id = candidate_id
                break
    main = (
        await _thread_summary(manager, session_id, main_id, active)
        if main_id
        else None
    )
    return SessionSummary(
        session_id=session_id,
        status="active" if active_threads else "inactive",
        active_threads=active_threads,
        thread_count=len(thread_ids),
        workspace_root=main.workspace_root if main is not None else "",
        title=main.title if main is not None else session_id,
        blank=main is None or main.message_count == 0,
    )


def pending_interactions(ctx: SessionRuntime) -> list[str]:
    """List pending requests through the application client-event router."""
    return ctx.application.client_events.pending_request_ids()


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
        "runtime_log",
    ]

    def apply(self, ctx, config=None) -> None:
        manager = SessionManager(
            ctx.runtime_paths,
            ctx,
            thread_persistence_factory=ctx.thread_persistence_factory,
            application_factory=ctx.agent_application_factory,
            runtime_log=ctx.runtime_log,
        )
        ctx.set("sessions", manager)
        handlers = SessionManagerHandlers(manager, workspace_root=str(ctx.workspace_root))
        ctx.on(QUERY_STATUS, handlers.status)
        manager.start_reaper()
        ctx.dispose(manager.close_all)


class SessionManagerHandlers:
    def __init__(
        self,
        manager: SessionManager,
        *,
        workspace_root: str,
    ) -> None:
        self._manager = manager
        self._workspace_root = workspace_root

    def status(self) -> ServerStatus:
        return ServerStatus(
            sessions=self._manager.size,
            threads=self._manager.thread_count,
            workspace_root=self._workspace_root,
        )
