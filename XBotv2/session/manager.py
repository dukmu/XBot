"""Live thread ownership and persisted session resource summaries."""

from __future__ import annotations

import asyncio
import base64
import binascii
import shutil
import time
from collections.abc import Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal, Protocol

from XBotv2.core.paths import RuntimePaths, SessionPaths
from XBotv2.core.runtime_logging import (
    DEFAULT_RUNTIME_LOG,
    RuntimeLog,
    push_log_context,
    reset_log_context,
)
from XBotv2.core.errors import OperationError
from XBotv2.core.filesystem.session_lock import acquire_session
from XBotv2.core.artifacts import ArtifactKind, ArtifactRef, ImageRef
from XBotv2.core.messages import ConversationMessage, HumanInputMessage, RuntimeNoticeMessage, ToolMessage
from XBotv2.core.parts import ImagePart
from XBotv2.core.tools import ToolFailed, ToolSucceeded
from XBotv2.agentloop import Events
from XBotv2.agentloop.events import StateChanged
from pydantic import JsonValue
from XBotv2.persistence import ThreadPersistenceFactory, ThreadPersistencePort
from XBotv2.usage import (
    USAGE_SNAPSHOT_KEY,
    USAGE_STATE_NAMESPACE,
)
from XBotv2.core.domain import Cursor, ReasoningGenerationMode, UsageSnapshot
from XBotv2.core.providers import BaseProvider
from XBotv2.permissions import Allowed, Denied, PermissionsPort
from XBotv2.interactions import (
    Answered,
    InteractionNotPending,
    InteractionResolution,
)
from XBotv2.core.timing import conversation_stats
from XBotv2.session.runtime import SessionRuntime, require_idle, start_regenerate_turn
from XBotv2.session.contracts import (
    AgentApplicationFactory,
    AgentApplicationOptions,
    PREPARE_FORK,
    PrepareFork,
    SESSION_RESOURCE_CHANGED,
    SESSION_RESOURCE_REMOVED,
    SessionResourceChanged,
    SessionResourceRemoved,
    SessionsPort,
    ArtifactPayload,
    HistoryMutation,
    InteractionReceipt,
    InterruptResult,
    OpenedThread,
    OpenSession,
    OpenThread,
    PendingInputData,
    PendingInputUpdate,
    RegenerateMessage,
    SendMessage,
    SessionExists,
    SessionEventSubscription,
    SessionNotFound,
    SessionSummary,
    ThreadNotActive,
    ThreadSummary,
    conversation_replay,
    new_session_id,
)
from XBotv2.session.session import delete_persisted_session, fork_persisted_session
from XBotv2.core.history import HistoryPage, HistoryCursorInvalid, TrajectoryRead
from XBotv2.core.operations import (
    Operation,
    RequestT,
    ResponseT,
    dispatch_operation,
)
from XBotv2.core.metadata import THREAD_METADATA_CHANGED, ThreadMetadataChanged


class ResourceEvents(Protocol):
    async def emit(self, event: str, *args: object) -> None: ...


class SessionManager(SessionsPort):
    """Own active thread runtimes grouped by persistent session id."""

    def __init__(
        self,
        paths: RuntimePaths,
        events: ResourceEvents,
        *,
        idle_timeout: float | None = 3600.0,
        reap_interval: float = 60.0,
        application_factory: AgentApplicationFactory,
        thread_persistence_factory: ThreadPersistenceFactory | None = None,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self.paths = paths
        self._events = events
        self._published_summaries: dict[str, SessionSummary] = {}
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
        self.empty_session_timeout: float | None = 24 * 60 * 60

    def _thread_persistence(
        self,
        session_paths: SessionPaths,
        *,
        thread_id: str,
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
        await self._gc_empty_sessions(now)
        async with self._lock:
            due = [
                ctx
                for ctx in self._sessions.values()
                if now - ctx.last_activity >= self.idle_timeout
                and not ctx.turn_lock.locked()
                and ctx.engine.pending_input_count == 0
                and ctx.event_stream.subscriber_count == 0
            ]
            for ctx in due:
                self._sessions.pop((ctx.session_id, ctx.thread_id), None)
        for ctx in due:
            await self._close_runtime(ctx, "idle_timeout")

    async def _gc_empty_sessions(self, now: float) -> None:
        """Remove session directories that never accumulated real state.

        A session that was opened but had no message, metadata, inbox, or
        plugin state before being abandoned is worthless on disk. Fresh
        directories are left alone for a grace period so a just-opened session
        that is about to be written to does not disappear mid-use; only
        abandoned (or legacy) empties are reclaimed.
        """
        if not self.empty_session_timeout:
            return
        root = self.paths.sessions_dir
        if not root.is_dir():
            return
        active_session_ids = {sid for sid, _ in await self.active_threads()}
        for candidate in root.iterdir():
            if not candidate.is_dir() or candidate.name in active_session_ids:
                continue
            try:
                # ``st_mtime`` is wall-clock time; compare it against
                # ``time.time()``, not the monotonic clock used for idling.
                age_seconds = time.time() - candidate.stat().st_mtime
            except OSError:
                continue
            if age_seconds < self.empty_session_timeout:
                continue
            if session_has_evidence(self.paths, candidate.name):
                continue
            try:
                shutil.rmtree(candidate)
                self._log.info(
                    "session.gc.removed_empty",
                    session_id=candidate.name,
                    age_seconds=round(age_seconds),
                )
            except OSError as exc:
                self._log.warning(
                    "session.gc.failed",
                    session_id=candidate.name,
                    error_type=type(exc).__name__,
                )

    def _subscribe_metadata_changes(self, runtime: SessionRuntime) -> None:
        """Translate a runtime's metadata changes into catalog changes.

        The runtime publishes the general fact on its own application bus;
        this process-level owner listens there and decides that a changed
        title is what clients need to see, so a caption (or any other writer)
        refreshes every open session list without knowing anything about
        events. The listener is registered on the application context and is
        released with its fiber when the runtime closes.
        """

        async def _on_changed(change: ThreadMetadataChanged) -> None:
            if change.previous.title == change.current.title:
                return
            await self._publish_session_change(runtime.session_id)

        runtime.application.events.on(THREAD_METADATA_CHANGED, _on_changed)

    def _subscribe_state_changes(self, runtime: SessionRuntime) -> None:
        """Refresh the session catalog when durable conversation state lands.

        A transport submission is acknowledged before the engine commits the
        message, so catalog changes must be published from the state-change
        boundary where the summary already reflects it.
        """

        async def _on_changed(_: StateChanged) -> None:
            await self._publish_session_change(runtime.session_id)

        runtime.application.events.on(Events.STATE_CHANGED, _on_changed)

    async def _publish_session_change(self, session_id: str) -> None:
        if not session_has_evidence(self.paths, session_id):
            return
        try:
            summary = await self.session_summary(session_id)
        except Exception:  # noqa: BLE001 — a vanished session needs no catalog event
            return
        previous = self._published_summaries.get(session_id)
        if previous == summary:
            return
        self._published_summaries[session_id] = summary
        await self._events.emit(
            SESSION_RESOURCE_CHANGED,
            SessionResourceChanged(summary, added=previous is None),
        )

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
        runtime = await self._runtime(session_id, thread_id)
        if runtime is None:
            if self.paths.session(session_id).has_thread(thread_id):
                raise ThreadNotActive(f"{session_id}/{thread_id}")
            raise SessionNotFound(f"{session_id}/{thread_id}")
        return runtime

    async def open_session(
        self,
        *,
        session_id: str | None,
        thread_id: str,
        provider_name: str | None,
        workspace_root: str,
        selected_agent: str | None = None,
        mode: str = "new",
        no_plugins: bool,
        plugin_configs: dict[str, dict[str, JsonValue]] | None = None,
        llm_override: BaseProvider | None = None,
        parent_thread_id: str = "",
        parent_permission_system: PermissionsPort | None = None,
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
        provider_name: str | None,
        workspace_root: str,
        selected_agent: str | None,
        mode: str,
        no_plugins: bool,
        plugin_configs: dict[str, dict[str, JsonValue]] | None,
        llm_override: BaseProvider | None,
        parent_thread_id: str,
        parent_permission_system: PermissionsPort | None,
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
                defer_persist=mode == "new",
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
                paths=self.paths,
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
            self._subscribe_metadata_changes(ctx)
            self._subscribe_state_changes(ctx)
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
            if mode == "resume" or session_has_evidence(self.paths, session_id):
                opened_summary = await self.session_summary(session_id)
                self._published_summaries[session_id] = opened_summary
                await self._events.emit(
                    SESSION_RESOURCE_CHANGED,
                    SessionResourceChanged(
                        opened_summary,
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
        close_errors: list[Exception] = []
        async with self._lock:
            opening = [
                task
                for (active_session_id, _), task in self._opening.items()
                if active_session_id == session_id
            ]
            contexts = {
                (ctx.session_id, ctx.thread_id): ctx
                for (active_session_id, _), ctx in self._sessions.items()
                if active_session_id == session_id
            }
            for ctx in contexts.values():
                self._sessions.pop((ctx.session_id, ctx.thread_id), None)
        for task in opening:
            try:
                runtime = await asyncio.shield(task)
            except Exception:
                continue
            async with self._lock:
                self._sessions.pop((runtime.session_id, runtime.thread_id), None)
            # A build registers the runtime immediately before its opening task
            # completes.  A close in that narrow window sees both references;
            # the thread identity makes it one lifetime, not two closes.
            contexts[(runtime.session_id, runtime.thread_id)] = runtime
        for ctx in contexts.values():
            try:
                await self._close_runtime(ctx, reason)
            except Exception as exc:
                close_errors.append(exc)
        self._log.info(
            "session.closed",
            session_id=session_id,
            threads=len(contexts),
            reason=reason,
        )
        if contexts:
            try:
                await self._emit_session_changed(session_id)
            except Exception as exc:
                close_errors.append(exc)
        if close_errors:
            raise ExceptionGroup(
                f"Failed to close session {session_id!r}", close_errors
            )

    async def _emit_session_changed(self, session_id: str) -> None:
        if not self.session_exists(session_id):
            return
        await self._publish_session_change(session_id)

    async def close_all(self) -> None:
        close_errors: list[Exception] = []
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
            try:
                await self._close_runtime(ctx, "session_closed")
            except Exception as exc:
                close_errors.append(exc)
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
            try:
                await self._discard_empty_session(session_id)
            except Exception as exc:
                close_errors.append(exc)
        reaper = self._reaper
        self._reaper = None
        if reaper is not None and not reaper.done():
            reaper.cancel()
            result = (await asyncio.gather(reaper, return_exceptions=True))[0]
            if isinstance(result, Exception):
                close_errors.append(result)
        if close_errors:
            raise ExceptionGroup("Failed to close all session runtimes", close_errors)

    async def active_threads(self) -> dict[tuple[str, str], SessionRuntime]:
        async with self._lock:
            return dict(self._sessions)

    async def _runtime(
        self,
        session_id: str,
        thread_id: str,
    ) -> SessionRuntime | None:
        """One live runtime, without copying the whole active catalog."""
        async with self._lock:
            return self._sessions.get((session_id, thread_id))

    async def _discard_empty_session(self, session_id: str) -> None:
        """Delete an unwritten session during owner shutdown.

        Reacquiring the stable cross-process lock serializes cleanup with a
        different server that may open this session as the current one exits.
        """
        if session_has_evidence(self.paths, session_id):
            return
        session_paths = self.paths.session(session_id)
        if not session_paths.root.exists():
            return
        try:
            ownership = acquire_session(
                session_paths.root,
                label=f"cleanup/{session_id}",
            )
        except OperationError as exc:
            if exc.code == "session_in_use":
                return
            raise
        removed = False
        try:
            if ownership.count == 1 and not session_has_evidence(
                self.paths, session_id
            ):
                shutil.rmtree(session_paths.root)
                removed = True
        finally:
            ownership.release()
        if not removed:
            return
        self._published_summaries.pop(session_id, None)
        await self._events.emit(
            SESSION_RESOURCE_REMOVED,
            SessionResourceRemoved(session_id),
        )

    async def _persisted_thread(
        self,
        session_id: str,
        thread_id: str,
    ) -> ThreadPersistencePort:
        """The on-disk reader for one thread, or a clear not-found error."""
        session = self.paths.session(session_id)
        if not session.has_thread(thread_id):
            raise SessionNotFound(f"{session_id}/{thread_id}")
        return self._thread_persistence(session, thread_id=thread_id)

    def session_exists(self, session_id: str) -> bool:
        return self.paths.session(session_id).root.is_dir() or any(
            active_session_id == session_id
            for active_session_id, _thread_id in self._sessions
        )

    def _require_session(self, session_id: str) -> None:
        """Validate a session id without reading any of its trajectories."""
        if not self.session_exists(session_id):
            raise SessionNotFound(session_id)

    async def open(self, request: OpenSession) -> OpenedThread:
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
        session_ids = [
            session_id
            for session_id in session_ids
            if session_has_evidence(self.paths, session_id)
        ]
        summaries = []
        for session_id in session_ids:
            try:
                summaries.append(
                    await _build_session_summary(self, session_id, active)
                )
            except Exception as exc:  # noqa: BLE001 - one session must not hide the catalog
                self._log.warning(
                    "session.catalog.unreadable",
                    session_id=session_id,
                    error_type=type(exc).__name__,
                    error=str(exc) or type(exc).__name__,
                )
                summaries.append(_unreadable_summary(self, session_id))
        return tuple(summaries)

    async def session_summary(self, session_id: str) -> SessionSummary:
        return await _build_session_summary(
            self,
            session_id,
            await self.active_threads(),
        )

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
                thread = await self.thread_summary(session_id, thread_id)
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
            await active.application.loop_state.metadata.replace_title(value)
        else:
            persistence = self._thread_persistence(
                self.paths.session(session_id),
                thread_id=main_id,
            )
            metadata = persistence.metadata.load()
            if metadata is None:
                raise OperationError(
                    "thread_metadata_missing",
                    f"Thread {session_id}/{main_id} has no runtime metadata",
                )
            persistence.metadata.save(metadata.model_copy(update={"title": value}))
        self._log.info("session.renamed", session_id=session_id)
        renamed = await self.session_summary(session_id)
        await self._events.emit(
            SESSION_RESOURCE_CHANGED,
            SessionResourceChanged(renamed),
        )
        return renamed

    async def fork_session(self, session_id: str) -> str:
        self._require_session(session_id)
        active = await self.active_threads()
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
        forked_summary = await self.session_summary(forked_id)
        self._published_summaries[forked_id] = forked_summary
        await self._events.emit(
            SESSION_RESOURCE_CHANGED,
            SessionResourceChanged(forked_summary, added=True),
        )
        return forked_id

    async def delete_session(self, session_id: str) -> None:
        self._require_session(session_id)
        active = await self.active_threads()
        runtimes = _session_runtimes(active, session_id)
        if any(runtime.turn_lock.locked() for runtime in runtimes):
            raise OperationError(
                "thread_busy",
                "Cannot delete while a session thread has an active turn.",
                retryable=True,
            )
        await self.close_session(session_id, reason="session_deleted")
        delete_persisted_session(self.paths, session_id)
        self._published_summaries.pop(session_id, None)
        self._log.info("session.deleted", session_id=session_id)
        await self._events.emit(
            SESSION_RESOURCE_REMOVED,
            SessionResourceRemoved(session_id),
        )

    async def list_threads(self, session_id: str) -> tuple[ThreadSummary, ...]:
        self._require_session(session_id)
        active = await self.active_threads()
        thread_ids = {
            *persisted_thread_ids(self.paths, session_id),
            *(
                thread_id
                for (active_session_id, thread_id), _runtime in active.items()
                if active_session_id == session_id
            ),
        }
        return tuple([
            await _thread_summary(
                self,
                session_id,
                thread_id,
                active.get((session_id, thread_id)),
            )
            for thread_id in sorted(thread_ids)
        ])

    async def open_thread(self, request: OpenThread) -> OpenedThread:
        await self.session_summary(request.session_id)
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
            metadata = persistence.metadata.load()
            if metadata is None:
                raise OperationError(
                    "thread_metadata_missing",
                    f"Thread {request.session_id}/{request.thread_id} has no runtime metadata",
                )
            parent_thread_id = metadata.parent_thread_id
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
        return await _thread_summary(
            self,
            session_id,
            thread_id,
            await self._runtime(session_id, thread_id),
        )

    async def messages(
        self,
        session_id: str,
        thread_id: str,
    ) -> tuple[ConversationMessage, ...]:
        """The visible conversation, as messages."""
        runtime = await self._runtime(session_id, thread_id)
        if runtime is not None:
            return runtime.application.loop_state.history.snapshot()
        persistence = await self._persisted_thread(session_id, thread_id)
        return tuple(persistence.history.load_transcript())

    async def message_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: Cursor | None,
        limit: int | None,
    ) -> HistoryPage[ConversationMessage]:
        if limit is None:
            if cursor is not None:
                raise OperationError(
                    "invalid_cursor", "A message cursor requires a page limit."
                )
            return HistoryPage(
                items=await self.messages(session_id, thread_id),
                older_cursor=None,
            )
        try:
            runtime = await self._runtime(session_id, thread_id)
            if runtime is not None:
                return runtime.application.history_pages.page(
                    limit=limit,
                    cursor=cursor,
                )
            persistence = await self._persisted_thread(session_id, thread_id)
            return persistence.history.page_transcript(
                limit=limit,
                cursor=cursor,
            )
        except HistoryCursorInvalid as exc:
            raise OperationError(
                "invalid_cursor", str(exc)
            ) from exc

    async def trajectory_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: Cursor | None,
        limit: int,
        before: int | None = None,
    ) -> TrajectoryRead:
        persistence = await self._persisted_thread(session_id, thread_id)
        try:
            page = persistence.history.page_trajectory(
                limit=limit, cursor=cursor, before=before
            )
        except HistoryCursorInvalid as exc:
            raise OperationError("invalid_cursor", str(exc)) from exc
        return page

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
        runtime = await self._runtime(session_id, thread_id)
        store = (
            runtime.application.artifacts
            if runtime is not None
            else (await self._persisted_thread(session_id, thread_id)).artifacts
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
        *,
        history_limit: int | None,
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
        messages = runtime.application.loop_state.history.snapshot()
        page = (
            HistoryPage(items=messages, older_cursor=None)
            if history_limit is None
            else runtime.application.loop_state.history.page(limit=history_limit)
        )
        return HistoryMutation(
            removed_turns=removed,
            history=HistoryPage(
                items=conversation_replay(page.items),
                older_cursor=page.older_cursor,
            ),
            stats=conversation_stats(messages),
        )

    async def undo_history(
        self,
        session_id: str,
        thread_id: str,
        count: int,
        *,
        history_limit: int | None,
    ) -> HistoryMutation:
        runtime = await self.get(session_id, thread_id)
        require_idle(runtime, "rewrite history")
        log_token = push_log_context(session_id=session_id, thread_id=thread_id)
        try:
            async with runtime.turn_lock:
                await runtime.application.history.undo_history(count)
        finally:
            reset_log_context(log_token)
        messages = runtime.application.loop_state.history.snapshot()
        self._log.info(
            "session.history.undone",
            session_id=session_id,
            thread_id=thread_id,
            removed_turns=count,
            remaining_messages=len(messages),
        )
        await self._emit_session_changed(session_id)
        page = (
            HistoryPage(items=messages, older_cursor=None)
            if history_limit is None
            else runtime.application.loop_state.history.page(limit=history_limit)
        )
        return HistoryMutation(
            removed_turns=count,
            history=HistoryPage(
                items=conversation_replay(page.items),
                older_cursor=page.older_cursor,
            ),
            stats=conversation_stats(messages),
        )

    async def send_message(self, request: SendMessage) -> None:
        runtime = await self.get(request.session_id, request.thread_id)
        images, attachments = self._store_message_inputs(runtime, request)
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
        await runtime.send_message(
            request.content,
            request.request_id,
            delivery=request.delivery,
            images=images,
            artifacts=attachments,
        )

    @staticmethod
    def _store_message_inputs(
        runtime: SessionRuntime,
        request: SendMessage,
    ) -> tuple[list[ImageRef], list[ArtifactRef]]:
        images = []
        for item in request.images:
            ref = runtime.application.artifacts.put(
                ArtifactKind.MEDIA,
                _upload_bytes(item.data),
                media_type=item.media_type,
            )
            images.append(
                ImageRef(artifact_id=ref.id, media_type=ref.media_type, size=ref.size)
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
        return images, attachments

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
    ) -> None:
        runtime = await self.get(request.session_id, request.thread_id)
        require_idle(runtime, "regenerate a response")
        self._log.info(
            "session.message.regenerated",
            session_id=request.session_id,
            thread_id=request.thread_id,
            request_id=request.request_id,
        )
        await start_regenerate_turn(runtime, request_id=request.request_id)

    async def stream_events(
        self,
        session_id: str,
        thread_id: str,
        *,
        after: int | None = None,
    ) -> SessionEventSubscription:
        runtime = await self.get(session_id, thread_id)
        return runtime.event_stream.subscribe(after)

    async def respond_permission(
        self,
        session_id: str,
        thread_id: str,
        request_id: str,
        decision: str,
        scope: str,
    ) -> InteractionReceipt:
        if decision == "allow":
            if scope not in {"once", "session"}:
                raise OperationError("invalid_permission_scope", scope)
            resolution: InteractionResolution = Allowed(scope=scope)
        elif decision == "deny":
            resolution = Denied(reason="permission denied")
        else:
            raise OperationError("invalid_permission_decision", decision)
        return await self._respond_interaction(
            session_id,
            thread_id,
            request_id,
            resolution,
        )

    async def respond_user_input(
        self,
        session_id: str,
        thread_id: str,
        request_id: str,
        answer: JsonValue,
    ) -> InteractionReceipt:
        return await self._respond_interaction(
            session_id,
            thread_id,
            request_id,
            Answered(answer=answer),
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
        try:
            return runtime.application.client_events.cancel(
                request_id,
                reason,
                expected_kind=event_type,
            )
        except (InteractionNotPending, TypeError) as exc:
            raise OperationError(
                "interaction_no_longer_pending",
                str(exc),
            ) from exc

    async def _respond_interaction(
        self,
        session_id: str,
        thread_id: str,
        request_id: str,
        resolution: InteractionResolution,
    ) -> InteractionReceipt:
        runtime = await self.get(session_id, thread_id)
        try:
            return runtime.application.client_events.resolve(request_id, resolution)
        except (InteractionNotPending, TypeError) as exc:
            raise OperationError(
                "interaction_no_longer_pending",
                str(exc),
            ) from exc

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
    session_paths: SessionPaths,
    thread_id: str,
) -> bool:
    """Whether a thread has committed real session evidence on disk."""
    return thread_has_evidence(session_paths, thread_id)


async def _opened_session(runtime: SessionRuntime) -> OpenedThread:
    event_cursor = runtime.event_stream.sequence
    snapshot = await runtime.application.snapshot()
    return OpenedThread(
        key=runtime.application.loop_state.session.key,
        metadata=snapshot.metadata,
        usage=snapshot.usage,
        history=HistoryPage(
            items=conversation_replay(snapshot.messages),
            older_cursor=None,
        ),
        status_slots=snapshot.status_slots,
        event_cursor=event_cursor,
        pending_inputs=runtime.pending_inputs(),
        pending_interactions=tuple(runtime.application.client_events.pending_interactions()),
    )


def thread_has_evidence(
    session_paths: "SessionPaths", thread_id: str
) -> bool:
    """Whether a thread committed real durable state.

    An opened-but-unused session holds only the ownership lock file; evidence
    is what makes it a session the user can actually resume: metadata, a
    message, a pending inbox, or plugin state.
    """
    thread = session_paths.thread(thread_id)
    return (
        thread.metadata_file.exists()
        or thread.messages_file.exists()
        or thread.inbox_file.exists()
        or thread.plugin_state_file.exists()
    )


def persisted_thread_ids(paths: RuntimePaths, session_id: str) -> list[str]:
    session = paths.session(session_id)
    thread_ids: set[str] = set()
    if session.threads_dir.is_dir():
        thread_ids.update(
            path.name
            for path in session.threads_dir.iterdir()
            if path.is_dir() and thread_has_evidence(session, path.name)
        )
    return sorted(thread_ids)


def session_has_evidence(paths: RuntimePaths, session_id: str) -> bool:
    """Whether a session contains user-meaningful durable state."""
    session = paths.session(session_id)
    return (
        session.config_file.is_file()
        or session.threads_log.is_file()
        or bool(persisted_thread_ids(paths, session_id))
    )


def _session_runtimes(
    active: Mapping[tuple[str, str], SessionRuntime],
    session_id: str,
) -> list[SessionRuntime]:
    return [
        runtime
        for (active_session_id, _), runtime in active.items()
        if active_session_id == session_id
    ]


async def _thread_summary(
    manager: SessionManager,
    session_id: str,
    thread_id: str,
    active: SessionRuntime | None,
) -> ThreadSummary:
    if active is not None:
        snapshot = await active.application.snapshot()
        metadata = snapshot.metadata
        parent_thread_id = metadata.parent_thread_id
        mode = metadata.runtime_selection.model.generation.mode
        stats = conversation_stats(snapshot.messages).model_copy(
            update={"turns": active.application.loop_state.turn_count}
        )
        return ThreadSummary(
            session_id=session_id,
            thread_id=thread_id,
            status="active",
            kind="subagent" if parent_thread_id else "main",
            turn_status="running" if active.turn_lock.locked() else "idle",
            parent_thread_id=parent_thread_id,
            agent=metadata.runtime_selection.agent_name,
            provider=active.provider_name,
            model=metadata.runtime_selection.model.route.model,
            model_mode=mode.effort if isinstance(mode, ReasoningGenerationMode) else "",
            context_window=metadata.runtime_selection.model.context_window,
            message_count=len(snapshot.messages),
            usage=snapshot.usage,
            session_stats=stats,
            pending_interactions=pending_interactions(active),
            status_slots=snapshot.status_slots,
            workspace_root=active.workspace_root,
            title=metadata.title,
        )

    persistence = await manager._persisted_thread(session_id, thread_id)
    metadata = persistence.metadata.load()
    if metadata is None:
        raise SessionNotFound(f"{session_id}/{thread_id}")
    parent_thread_id = metadata.parent_thread_id
    mode = metadata.runtime_selection.model.generation.mode
    messages = persistence.history.load_surface()
    stats = conversation_stats(messages).model_copy(
        update={"turns": persistence.history.count_turns()}
    )
    return ThreadSummary(
        session_id=session_id,
        thread_id=thread_id,
        status="inactive",
        kind="subagent" if parent_thread_id else "main",
        parent_thread_id=parent_thread_id,
        agent=metadata.runtime_selection.agent_name,
        provider=metadata.runtime_selection.model.route.provider,
        model=metadata.runtime_selection.model.route.model,
        model_mode=mode.effort if isinstance(mode, ReasoningGenerationMode) else "",
        context_window=metadata.runtime_selection.model.context_window,
        message_count=len(messages),
        usage=await _read_usage(persistence),
        session_stats=stats,
        workspace_root=metadata.workspace_root,
        title=metadata.title,
    )


async def _read_usage(
    persistence: ThreadPersistencePort,
) -> UsageSnapshot:
    stored = await persistence.state.namespace(USAGE_STATE_NAMESPACE).get(
        USAGE_SNAPSHOT_KEY
    )
    if stored is None:
        return UsageSnapshot()
    if not isinstance(stored, dict):
        raise TypeError("Persisted usage snapshot must be an object")
    return UsageSnapshot.model_validate(stored)


def _upload_bytes(data: str) -> bytes:
    try:
        payload = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("upload data must be valid base64") from exc
    if not payload:
        raise ValueError("upload data must not be empty")
    return payload


def _history_artifact(
    messages: tuple[ConversationMessage, ...],
    artifact_id: str,
) -> ArtifactRef | None:
    for message in messages:
        if isinstance(message, (HumanInputMessage, RuntimeNoticeMessage)):
            for artifact in message.artifacts:
                if artifact.id == artifact_id:
                    return artifact
            images = [part.image for part in message.parts if isinstance(part, ImagePart)]
            for image in images:
                if image.artifact_id == artifact_id:
                    return ArtifactRef(
                        id=image.artifact_id,
                        kind=ArtifactKind.MEDIA,
                        media_type=image.media_type,
                        size=image.size,
                    )
        if isinstance(message, ToolMessage):
            outcome = message.outcome
            if isinstance(outcome, (ToolSucceeded, ToolFailed)):
                for image in outcome.output.artifacts:
                    if image.id == artifact_id:
                        return image
    return None


async def _build_session_summary(
    manager: SessionManager,
    session_id: str,
    active: Mapping[tuple[str, str], SessionRuntime],
) -> SessionSummary:
    session = manager.paths.session(session_id)
    active_by_thread = {
        thread_id: runtime
        for (active_session_id, thread_id), runtime in active.items()
        if active_session_id == session_id
    }
    if not session.root.is_dir() and not active_by_thread:
        raise SessionNotFound(session_id)
    thread_ids = sorted(
        set(persisted_thread_ids(manager.paths, session_id)) | active_by_thread.keys()
    )
    main_id = "agent" if "agent" in thread_ids else None
    if main_id is None:
        for candidate_id in thread_ids:
            candidate = await _thread_summary(
                manager,
                session_id,
                candidate_id,
                active_by_thread.get(candidate_id),
            )
            if not candidate.parent_thread_id:
                main_id = candidate_id
                break
    main = (
        await _thread_summary(
            manager,
            session_id,
            main_id,
            active_by_thread.get(main_id),
        )
        if main_id
        else None
    )
    return SessionSummary(
        session_id=session_id,
        status="active" if active_by_thread else "inactive",
        active_threads=len(active_by_thread),
        thread_count=len(thread_ids),
        workspace_root=main.workspace_root if main is not None else "",
        title=main.title if main is not None else session_id,
        blank=main is None or main.message_count == 0,
    )


def _unreadable_summary(
    manager: SessionManager,
    session_id: str,
) -> SessionSummary:
    """Keep an unreadable session visible so it can still be deleted."""
    thread_ids = persisted_thread_ids(manager.paths, session_id)
    return SessionSummary(
        session_id=session_id,
        status="inactive",
        thread_count=len(thread_ids),
        title=session_id,
        blank=not thread_ids,
        unreadable=True,
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
]
