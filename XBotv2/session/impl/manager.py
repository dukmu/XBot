"""Live thread ownership and persisted session resource summaries."""

from __future__ import annotations
import asyncio
import time
import heapq
import os
import base64
import json
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from XBotv2.core.api.paths import RuntimePaths, SessionPaths
from XBotv2.core.api.runtime_logging import (
    DEFAULT_RUNTIME_LOG,
    RuntimeLog,
    push_log_context,
    reset_log_context,
)
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.messages import AssistantMessage
from XBotv2.agentloop.api.events import Events, StateChanged
from pydantic import JsonValue
from XBotv2.session.impl.storage import SessionRepository
from XBotv2.session.api.storage import ForkState, FORK_COPY_STATE, FORK_FINALIZE
from XBotv2.usage.api.service import USAGE_COUNTERS_KEY, USAGE_STATE_NAMESPACE
from XBotv2.core.api.domain import ReasoningGenerationMode, TokenCounters, UsageSnapshot
from XBotv2.core.api.providers import Provider
from XBotv2.core.api.timing import conversation_stats
from xcore.api.context import Context
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.usage.api.service import USAGE_ARCHIVE
from XBotv2.agents.api.contracts import AgentCreateOptions
from XBotv2.interactions.api.contracts import CLIENT_EVENTS
from XBotv2.interactions.api.status import status_slots
from XBotv2.usage.api.service import USAGE
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.loader.api.service import LOADER
from XBotv2.loader.api.contracts import LoadError
from XBotv2.config.api.tree import load_plugin_tree
from XBotv2.session.api.contracts import (
    SESSION_INFO,
    THREAD_INFO,
    HISTORY_CHANGED,
    SessionInfo,
    ThreadInfo,
    THREAD_EVENTS,
)


def require_idle(runtime, action):
    if runtime.require(AGENT_LOOP).busy:
        raise OperationError(
            "thread_busy", f"Cannot {action} while a turn is active.", retryable=True
        )


from XBotv2.session.api.contracts import (
    PREPARE_FORK,
    PrepareFork,
    SESSION_RESOURCE_CHANGED,
    SESSION_RESOURCE_REMOVED,
    SessionResourceChanged,
    SessionResourceRemoved,
    SessionsPort,
    OpenSession,
    OpenThread,
    SessionExists,
    SessionNotFound,
    SessionSummary,
    SessionPage,
    ThreadPage,
    ThreadNotActive,
    ThreadSummary,
    new_session_id,
)
from XBotv2.core.api.metadata import THREAD_METADATA_CHANGED, ThreadMetadataChanged


class SessionManager(SessionsPort):
    """Own active thread runtimes grouped by persistent session id."""

    def __init__(
        self,
        paths: RuntimePaths,
        events: Context,
        *,
        idle_timeout: float | None = 3600.0,
        reap_interval: float = 60.0,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self.paths = paths
        self._repository = SessionRepository(events.require(PERSISTENCE))
        self._events = events
        self._published_summaries: OrderedDict[str, SessionSummary] = OrderedDict()
        self.idle_timeout = idle_timeout
        self.reap_interval = reap_interval
        self._root = events.root
        self._owners: dict[str, Context] = {}
        self._session_opening: dict[str, asyncio.Task[Context]] = {}
        self._closing = False
        self._thread_numbers: dict[str, int] = {}
        self._closing_threads: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._closing_sessions: set[str] = set()
        self._session_operations: dict[str, asyncio.Task] = {}
        self._log = runtime_log.bind("session")
        self._sessions: dict[tuple[str, str], Context] = {}
        self._opening: dict[tuple[str, str], asyncio.Task[Context]] = {}
        self._opening_children: dict[tuple[str, str], str] = {}
        self._child_limits: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    def _thread_repository(self, session_paths, *, thread_id):
        return self._repository.thread(session_paths.session_id, thread_id)

    def thread_repository(self, session_id, thread_id):
        return self._repository.thread(session_id, thread_id)

    async def _session_owner(self, session_id, workspace, tree):
        row = await self._repository.row(session_id)
        if row is not None and Path(row["workspace"]) != workspace:
            raise OperationError(
                "workspace_conflict",
                f"Session {session_id} belongs to another workspace",
            )
        owner = self._owners.get(session_id)
        if owner is not None:
            if owner.require(SESSION_INFO).workspace_root != workspace:
                raise OperationError(
                    "workspace_conflict",
                    f"Session {session_id} belongs to another workspace",
                )
            return owner
        task = self._session_opening.get(session_id)
        if task is None:

            async def build():
                loader = self._root.require(LOADER)
                ctx = loader.scope(
                    self._root,
                    f"session:{session_id}",
                    kind="session",
                    tree=tree,
                    provides=(SESSION_INFO,),
                )
                ctx.provide(SESSION_INFO, SessionInfo(session_id, workspace))
                try:
                    try:
                        await loader.mount(ctx, tree, kind="session")
                    except LoadError as error:
                        if isinstance(error.__cause__, OperationError):
                            raise error.__cause__
                        raise
                    self._owners[session_id] = ctx
                    return ctx
                except BaseException:
                    try:
                        await ctx.close()
                    except BaseException:
                        self._owners[session_id] = ctx
                        self._closing_sessions.add(session_id)
                        raise
                    self._child_limits.pop(session_id, None)
                    raise
                finally:
                    self._session_opening.pop(session_id, None)

            task = asyncio.create_task(build(), name=f"open-session:{session_id}")
            self._session_opening[session_id] = task
        owner = await asyncio.shield(task)
        if owner.require(SESSION_INFO).workspace_root != workspace:
            raise OperationError(
                "workspace_conflict",
                f"Session {session_id} belongs to another workspace",
            )
        return owner

    async def new_thread_id(self, session_id: str, agent_ref: str) -> str:
        self._require_session(session_id)
        while True:
            number = self._thread_numbers.get(session_id, 0) + 1
            self._thread_numbers[session_id] = number
            thread_id = f"{agent_ref}-{number}"
            self.paths.session(session_id).thread(thread_id)
            if (
                (session_id, thread_id) not in self._sessions
                and (session_id, thread_id) not in self._opening
                and not await self._repository.has_thread(session_id, thread_id)
            ):
                return thread_id

    def set_child_limit(self, session_id: str, limit: int | None) -> None:
        if limit is not None and limit < 1:
            raise ValueError("child thread limit must be positive")
        if session_id not in self._owners and session_id not in self._session_opening:
            raise SessionNotFound(session_id)
        if limit is None:
            self._child_limits.pop(session_id, None)
        else:
            self._child_limits[session_id] = limit

    def _admit_child(self, key: tuple[str, str]) -> None:
        session_id, _ = key
        limit = self._child_limits.get(session_id)
        if limit is None:
            return
        live_children = {
            child_key for child_key, scope in self._sessions.items()
            if child_key[0] == session_id and scope.require(THREAD_INFO).parent_thread_id
        }
        for child_key in self._opening_children:
            if child_key == key:
                break
            if child_key[0] == session_id:
                live_children.add(child_key)
        if len(live_children) >= limit:
            raise OperationError(
                "child_limit_reached", "Session live child thread limit reached"
            )

    async def locate(self, session_id: str, thread_id: str) -> Context:
        scope = self._sessions.get((session_id, thread_id))
        if scope is None:
            if await self._repository.has_thread(session_id, thread_id):
                raise ThreadNotActive(f"{session_id}/{thread_id}")
            raise SessionNotFound(f"{session_id}/{thread_id}")
        return scope

    async def scopes(self, session_id: str) -> tuple[Context, ...]:
        return tuple(
            scope for (sid, _), scope in self._sessions.items() if sid == session_id
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
        for key, scope in list(self._sessions.items()):
            loop = scope.require(AGENT_LOOP)
            if (
                scope.is_active
                and now - loop.last_activity >= self.idle_timeout
                and not loop.busy
                and loop.pending_input_count == 0
                and scope.require(THREAD_EVENTS).subscriber_count == 0
            ):
                await self.close_thread(*key, expected=scope, reason="idle_timeout")

    def _subscribe_metadata_changes(self, runtime: Context) -> None:
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
            await self._publish_session_change(runtime.require(THREAD_INFO).session_id)

        runtime.on(THREAD_METADATA_CHANGED, _on_changed)

    def _subscribe_state_changes(self, runtime: Context) -> None:
        """Refresh the session catalog when durable conversation state lands.

        A transport submission is acknowledged before the engine commits the
        message, so catalog changes must be published from the state-change
        boundary where the summary already reflects it.
        """

        async def _on_changed(_: StateChanged) -> None:
            await self._publish_session_change(runtime.require(THREAD_INFO).session_id)

        runtime.on(Events.STATE_CHANGED, _on_changed)
        runtime.on(HISTORY_CHANGED, _on_changed)

    async def _publish_session_change(self, session_id: str) -> None:
        if self._closing or not self._root.is_active:
            return
        if not session_has_evidence(self.paths, session_id):
            return
        try:
            summary = await self.session_summary(session_id)
        except Exception:  # noqa: BLE001 — a vanished session needs no catalog event
            return
        previous = self._published_summaries.get(session_id)
        if previous == summary:
            self._published_summaries.move_to_end(session_id)
            return
        self._remember_summary(session_id, summary)
        await self._events.emit(
            SESSION_RESOURCE_CHANGED,
            SessionResourceChanged(summary, added=False),
        )

    async def _close_runtime(self, scope: Context, reason: str) -> None:
        info = scope.require(THREAD_INFO)
        await scope.close()
        key = (info.session_id, info.thread_id)
        if self._sessions.get(key) is scope:
            self._sessions.pop(key)
        await self._close_unused_owner(info.session_id)

    async def _cancel_session_opening(self, session_id):
        task = self._session_opening.get(session_id)
        if task is not None:
            task.cancel()
            (result,) = await asyncio.gather(task, return_exceptions=True)
            if isinstance(result, Exception):
                raise result

    async def _close_unused_owner(self, session_id):
        if any(sid == session_id for sid, _ in self._sessions) or any(
            sid == session_id for sid, _ in self._opening
        ):
            return
        already_closing = session_id in self._closing_sessions
        self._closing_sessions.add(session_id)
        await self._cancel_session_opening(session_id)
        owner = self._owners.get(session_id)
        if owner is not None:
            await owner.close()
            self._owners.pop(session_id, None)
            self._child_limits.pop(session_id, None)
        if not already_closing:
            self._closing_sessions.discard(session_id)

    @property
    def size(self) -> int:
        return len({session_id for session_id, _ in self._sessions})

    @property
    def thread_count(self) -> int:
        return len(self._sessions)

    async def open_session(
        self,
        *,
        session_id: str | None,
        thread_id: str,
        provider_name: str | None,
        workspace_root: str | None,
        selected_agent: str | None = None,
        mode: str = "new",
        no_plugins: bool,
        plugin_configs: dict[str, dict[str, JsonValue]] | None = None,
        llm_override: Provider | None = None,
        parent_thread_id: str = "",
        interactive: bool = True,
    ) -> Context:
        if thread_id.startswith("__"):
            raise OperationError("invalid_request", "Thread IDs beginning with '__' are reserved")
        if self._closing:
            raise OperationError("session_closing", "Runtime is closing")
        mode = (mode or "new").lower().strip()
        if mode not in {"new", "resume"}:
            raise ValueError("session mode must be new or resume")
        if mode == "resume" and not session_id:
            raise ValueError("resume mode requires session_id")
        if mode == "new":
            session_id = session_id or new_session_id()
        assert session_id is not None
        key = (session_id, thread_id)
        requested_workspace = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else None
        )
        if session_id in self._session_operations:
            raise OperationError(
                "session_busy",
                "A session snapshot or deletion is in progress",
                retryable=True,
            )
        if session_id in self._closing_sessions or key in self._closing_threads:
            raise OperationError("session_closing", f"Session {session_id} is closing")
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
            if session_id in self._session_operations:
                raise OperationError(
                    "session_busy", "A session operation is in progress", retryable=True
                )
            existing = self._sessions.get(key)
            if existing is not None:
                if (
                    requested_workspace is not None
                    and requested_workspace
                    != existing.require(THREAD_INFO).workspace_root
                ):
                    raise OperationError(
                        "workspace_conflict",
                        f"Thread {session_id}/{thread_id} belongs to another workspace",
                    )
                if not existing.is_active:
                    raise OperationError(
                        "session_closing", f"Thread {session_id}/{thread_id} is closing"
                    )
                if mode == "resume":
                    existing.require(AGENT_LOOP).touch()
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
                task = opening
            else:
                workspace = requested_workspace or self._root.require("workspace_root")
                if mode == "resume":
                    session_paths = self.paths.session(session_id)
                    if not await self._repository.has_thread(session_id, thread_id):
                        raise SessionNotFound(f"{session_id}/{thread_id}")
                    persistence = self._thread_repository(
                        session_paths,
                        thread_id=thread_id,
                    )
                    metadata = await persistence.load()
                    if metadata is None:
                        raise OperationError(
                            "thread_metadata_missing",
                            f"Thread {session_id}/{thread_id} has no runtime metadata",
                        )
                    if (
                        parent_thread_id
                        and parent_thread_id != metadata.parent_thread_id
                    ):
                        raise OperationError(
                            "parent_thread_conflict",
                            f"Thread {session_id}/{thread_id} belongs to another parent",
                        )
                    parent_thread_id = metadata.parent_thread_id
                    workspace = Path(metadata.workspace_root).expanduser().resolve()
                    if (
                        requested_workspace is not None
                        and requested_workspace != workspace
                    ):
                        raise OperationError(
                            "workspace_conflict",
                            f"Thread {session_id}/{thread_id} belongs to another workspace",
                        )
                if not workspace.is_dir():
                    raise OperationError(
                        "workspace_not_found",
                        f"Workspace path is not an existing directory: {workspace}",
                    )
                if parent_thread_id and (session_id, parent_thread_id) in self._closing_threads:
                    raise OperationError("session_closing", f"Parent thread {session_id}/{parent_thread_id} is closing")
                if parent_thread_id:
                    self._admit_child(key)
                    self._opening_children[key] = parent_thread_id
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
                        interactive=interactive,
                    ),
                    name=f"xbotv2-open-{session_id}-{thread_id}",
                )
                self._opening[key] = task
        scope = await asyncio.shield(task)
        if (
            requested_workspace is not None
            and requested_workspace != scope.require(THREAD_INFO).workspace_root
        ):
            raise OperationError(
                "workspace_conflict",
                f"Thread {session_id}/{thread_id} belongs to another workspace",
            )
        return scope

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
        llm_override: Provider | None,
        parent_thread_id: str,
        interactive: bool,
    ) -> Context:
        started = time.perf_counter()
        log_token = push_log_context(
            session_id=session_id,
            thread_id=thread_id,
        )
        try:
            session_paths = self.paths.session(session_id)
            session_preexisting = session_paths.root.is_dir()
            persisted_thread = await self._repository.has_thread(session_id, thread_id)
            if mode == "resume" and not persisted_thread:
                raise SessionNotFound(f"{session_id}/{thread_id}")
            had_persisted_session = mode == "resume" and persisted_thread
            if mode == "new" and persisted_thread:
                raise SessionExists(f"{session_id}/{thread_id}")
            extra_plugins = [
                {"id": name, "config": config}
                for name, config in (plugin_configs or {}).items()
            ]
            tree = load_plugin_tree(
                self.paths,
                workspace_root=workspace_root,
                thread_id=thread_id,
                no_plugins=no_plugins,
                plugin_dirs=self._root.get("plugin_dirs", strict=False) or (),
                extra_plugins=extra_plugins,
                session_id=session_id,
            )
            owner = await self._session_owner(session_id, Path(workspace_root), tree)
            if parent_thread_id:
                async with self._lock:
                    self._admit_child(key)
            loader = self._root.require(LOADER)
            scope = loader.scope(
                owner,
                f"thread:{thread_id}",
                kind="thread",
                tree=tree,
                provides=(
                    THREAD_INFO,
                    "agent_options",
                    "plugin_overrides",
                    "plugin_dirs",
                    "no_plugins",
                    "interactive",
                ),
            )
            scope.provide(
                THREAD_INFO,
                ThreadInfo(
                    session_id, thread_id, Path(workspace_root), parent_thread_id
                ),
            )
            scope.set(
                "agent_options",
                AgentCreateOptions(
                    provider_name=provider_name,
                    model_override=llm_override,
                    selected_agent=selected_agent,
                ),
            )
            scope.set("plugin_overrides", extra_plugins)
            scope.set("plugin_dirs", self._root.get("plugin_dirs", strict=False) or ())
            scope.set("no_plugins", no_plugins)
            scope.set("interactive", interactive)
            try:
                await loader.mount(scope, tree, kind="thread", required=(AGENT_LOOP,))
                ctx = scope
            except BaseException:
                try:
                    await scope.close()
                except BaseException:
                    self._sessions[key] = scope
                    self._closing_sessions.add(session_id)
                    raise
                siblings = any(sid == session_id for sid, _ in self._sessions)
                opening_siblings = any(
                    other[0] == session_id and other != key for other in self._opening
                )
                if not siblings and not opening_siblings:
                    try:
                        await owner.close()
                    except BaseException:
                        self._closing_sessions.add(session_id)
                        raise
                    self._owners.pop(session_id, None)
                    self._child_limits.pop(session_id, None)
                raise
            async with self._lock:
                self._sessions[key] = scope
            self._subscribe_metadata_changes(ctx)
            self._subscribe_state_changes(ctx)
            pending_resumed = (
                ctx.require(AGENT_LOOP).resume_pending_inputs()
                if mode == "resume"
                else False
            )
            self._log.info(
                "session.opened",
                mode=mode,
                provider=ctx.require(
                    LOOP_STATE
                ).metadata.value.runtime_selection.model.route.provider,
                resumed=had_persisted_session,
                pending_resumed=pending_resumed,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            if mode == "resume" or session_has_evidence(self.paths, session_id):
                opened_summary = await self.session_summary(session_id)
                self._remember_summary(session_id, opened_summary)
                await self._events.emit(
                    SESSION_RESOURCE_CHANGED,
                    SessionResourceChanged(
                        opened_summary,
                        added=not session_preexisting,
                    ),
                )
            return scope
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
                    self._opening_children.pop(key, None)
            reset_log_context(log_token)

    async def close_thread(
        self,
        session_id: str,
        thread_id: str,
        *,
        expected: Context | None = None,
        reason: str = "session_closed",
    ) -> None:
        operation = self._session_operations.get(session_id)
        if operation is not None and operation is not asyncio.current_task():
            raise OperationError(
                "session_busy", "A session operation is in progress", retryable=True
            )
        key = (session_id, thread_id)
        task = self._closing_threads.get(key)
        if task is None:

            async def close():
                try:
                    opening = self._opening.get(key)
                    if opening is not None:
                        opening.cancel()
                        await asyncio.gather(opening, return_exceptions=True)
                        if self._opening.get(key) is opening:
                            self._opening.pop(key, None)
                            self._opening_children.pop(key, None)
                    scope = self._sessions.get(key)
                    if scope is None:
                        await self._close_unused_owner(session_id)
                        return
                    if expected is not None and scope is not expected:
                        return
                    children = {
                        tid for (sid, tid), child in self._sessions.items()
                        if sid == session_id
                        and child.require(THREAD_INFO).parent_thread_id == thread_id
                    } | {
                        tid for (sid, tid), parent in self._opening_children.items()
                        if sid == session_id and parent == thread_id
                    }
                    for child_id in children:
                        await self.close_thread(session_id, child_id, reason=reason)
                    await self._close_runtime(scope, reason)
                    self._log.info(
                        "session.thread.closed",
                        session_id=session_id,
                        thread_id=thread_id,
                        reason=reason,
                    )
                    await self._emit_session_changed(session_id)
                finally:
                    if self._closing_threads.get(key) is asyncio.current_task():
                        self._closing_threads.pop(key, None)

            task = asyncio.create_task(
                close(), name=f"close-thread:{session_id}/{thread_id}"
            )
            self._closing_threads[key] = task
        await asyncio.shield(task)

    async def close_session(
        self, session_id: str, *, reason: str = "session_closed"
    ) -> None:
        operation = self._session_operations.get(session_id)
        if operation is not None and operation is not asyncio.current_task():
            raise OperationError(
                "session_busy", "A session operation is in progress", retryable=True
            )
        self._closing_sessions.add(session_id)
        opening = {
            key: task for key, task in self._opening.items() if key[0] == session_id
        }
        for task in opening.values():
            task.cancel()
        if opening:
            await asyncio.gather(*opening.values(), return_exceptions=True)
            for key, task in opening.items():
                if self._opening.get(key) is task:
                    self._opening.pop(key, None)
                    self._opening_children.pop(key, None)
        await self._cancel_session_opening(session_id)
        errors = []
        keys = [key for key in self._sessions if key[0] == session_id]
        parents = {
            tid: self._sessions[(sid, tid)].require(THREAD_INFO).parent_thread_id
            for sid, tid in keys
        }

        def depth(key):
            parent = parents[key[1]]
            seen = {key[1]}
            while parent in parents and parent not in seen:
                seen.add(parent)
                parent = parents[parent]
            return len(seen)

        keys.sort(key=depth, reverse=True)
        for sid, tid in keys:
            if any(
                parent == tid and (sid, child) in self._sessions
                for child, parent in parents.items()
            ):
                continue
            try:
                await self.close_thread(sid, tid, reason=reason)
            except Exception as error:
                errors.append(error)
        if not errors:
            owner = self._owners.get(session_id)
            if owner is not None:
                try:
                    await owner.close()
                except Exception as error:
                    errors.append(error)
                else:
                    self._owners.pop(session_id, None)
                    self._child_limits.pop(session_id, None)
        if errors:
            raise ExceptionGroup(
                f"Failed to close session {session_id!r}; resources remain owned",
                errors,
            )
        self._closing_sessions.discard(session_id)
        await self._emit_session_changed(session_id)
        self._log.info(
            "session.closed", session_id=session_id, threads=len(keys), reason=reason
        )

    async def _emit_session_changed(self, session_id: str) -> None:
        if not self.session_exists(session_id):
            return
        await self._publish_session_change(session_id)

    async def close_all(self) -> None:
        self._closing = True
        operations = {
            task
            for task in self._session_operations.values()
            if task is not asyncio.current_task()
        }
        for task in operations:
            task.cancel()
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        reaper, self._reaper = self._reaper, None
        if reaper is not None:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        session_ids = (
            set(self._owners)
            | set(self._session_opening)
            | {sid for sid, _ in self._opening}
            | {sid for sid, _ in self._sessions}
        )
        errors = []
        for session_id in session_ids:
            try:
                await self.close_session(session_id)
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup(
                "Failed to close runtime scopes; resources remain owned", errors
            )

    async def active_threads(self) -> dict[tuple[str, str], Context]:
        async with self._lock:
            return dict(self._sessions)

    async def _runtime(
        self,
        session_id: str,
        thread_id: str,
    ) -> Context | None:
        """One live runtime, without copying the whole active catalog."""
        async with self._lock:
            scope = self._sessions.get((session_id, thread_id))
            return scope

    async def _persisted_thread(
        self,
        session_id: str,
        thread_id: str,
    ) -> object:
        """The on-disk reader for one thread, or a clear not-found error."""
        session = self.paths.session(session_id)
        if not await self._repository.has_thread(session_id, thread_id):
            raise SessionNotFound(f"{session_id}/{thread_id}")
        return self._thread_repository(session, thread_id=thread_id)

    def session_exists(self, session_id: str) -> bool:
        return self.paths.session(session_id).root.is_dir() or any(
            active_session_id == session_id
            for active_session_id, _thread_id in self._sessions
        )

    def _require_session(self, session_id: str) -> None:
        """Validate a session id without reading any of its trajectories."""
        if not self.session_exists(session_id):
            raise SessionNotFound(session_id)

    async def open(self, request: OpenSession) -> Context:
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
            interactive=request.interactive,
        )
        return runtime

    def _remember_summary(self, session_id: str, summary: SessionSummary) -> None:
        self._published_summaries[session_id] = summary
        self._published_summaries.move_to_end(session_id)
        if len(self._published_summaries) > 256:
            self._published_summaries.popitem(last=False)

    async def list_sessions(
        self, *, limit: int = 100, cursor: str | None = None,
        workspace_root: str | None = None,
    ) -> SessionPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        root = self.paths.sessions_dir
        active = await self.active_threads()
        try:
            boundary, direction = (
                json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if cursor else ("", "next")
            )
            if not isinstance(boundary, str) or direction not in (
                "next", "previous", "next_inclusive", "previous_inclusive",
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid page cursor") from exc
        backwards = direction.startswith("previous")
        inclusive = direction.endswith("_inclusive")
        summaries = []
        after = boundary
        while root.is_dir():
            def eligible(entry):
                if entry.name.startswith(".") or not entry.is_dir():
                    return False
                if entry.name == after:
                    return inclusive
                return entry.name < after if backwards else entry.name > after

            with os.scandir(root) as entries:
                names = (entry.name for entry in entries if eligible(entry))
                candidates = (
                    heapq.nlargest(100, names) if backwards
                    else heapq.nsmallest(100, names)
                )
            if not candidates:
                break
            inclusive = False
            for session_id in candidates:
                after = session_id
                if not session_has_evidence(self.paths, session_id):
                    continue
                if workspace_root is not None:
                    try:
                        row = await self._repository.row(session_id)
                        if row is not None and row["workspace"] != workspace_root:
                            continue
                    except Exception as exc:
                        self._log.warning(
                            "session.catalog.unreadable", session_id=session_id,
                            error_type=type(exc).__name__, error=str(exc) or type(exc).__name__,
                        )
                        continue
                try:
                    summary = await _build_session_summary(self, session_id, active)
                except Exception as exc:
                    self._log.warning(
                        "session.catalog.unreadable", session_id=session_id,
                        error_type=type(exc).__name__, error=str(exc) or type(exc).__name__,
                    )
                    summary = await _unreadable_summary(self, session_id)
                if workspace_root is not None and summary.workspace_root != workspace_root:
                    continue
                summaries.append(summary)
                if len(summaries) > limit:
                    break
            if len(summaries) > limit or len(candidates) < 100:
                break
        more = len(summaries) > limit
        summaries = summaries[:limit]
        if backwards:
            summaries.reverse()

        def encode(identity, direction):
            return base64.urlsafe_b64encode(
                json.dumps([identity, direction]).encode()
            ).decode()

        previous = None
        next_cursor = None
        if summaries:
            if more if backwards else cursor is not None:
                previous = encode(summaries[0].session_id, "previous")
            if cursor is not None if backwards else more:
                next_cursor = encode(summaries[-1].session_id, "next")
        elif cursor:
            if backwards:
                next_cursor = encode(boundary, "next_inclusive")
            else:
                previous = encode(boundary, "previous_inclusive")
        return SessionPage(
            items=tuple(summaries), next_cursor=next_cursor, previous_cursor=previous,
        )

    async def catalog_summary(self, session_id: str) -> SessionSummary:
        try:
            return await self.session_summary(session_id)
        except SessionNotFound:
            raise
        except Exception as exc:
            self._log.warning(
                "session.catalog.unreadable", session_id=session_id,
                error_type=type(exc).__name__, error=str(exc) or type(exc).__name__,
            )
            return await _unreadable_summary(self, session_id)

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
        await self._repository.rename(session_id, value)
        for (sid, _), scope in active_threads.items():
            if sid == session_id:
                await scope.require(LOOP_STATE).metadata.replace(
                    scope.require(LOOP_STATE).metadata.value.model_copy(
                        update={"title": value}
                    )
                )
        self._log.info("session.renamed", session_id=session_id)
        renamed = await self.session_summary(session_id)
        await self._events.emit(
            SESSION_RESOURCE_CHANGED,
            SessionResourceChanged(renamed),
        )
        return renamed

    async def fork_session(self, session_id: str) -> str:
        task = asyncio.current_task()
        async with self._lock:
            if (
                session_id in self._session_operations
                or session_id in self._closing_sessions
                or session_id in self._session_opening
                or any(sid == session_id for sid, _ in self._opening)
            ):
                raise OperationError(
                    "session_busy", "Session ownership is changing", retryable=True
                )
            self._session_operations[session_id] = task
        try:
            return await self._fork_session(session_id)
        finally:
            self._session_operations.pop(session_id, None)

    async def _fork_session(self, session_id: str) -> str:
        self._require_session(session_id)
        active = await self.active_threads()
        runtimes = _session_runtimes(active, session_id)
        if any(runtime.require(AGENT_LOOP).busy for runtime in runtimes):
            raise OperationError(
                "thread_busy",
                "Cannot fork while a session thread has an active turn.",
                retryable=True,
            )
        async with AsyncExitStack() as idle:
            for scope in runtimes:
                await idle.enter_async_context(
                    scope.require(AGENT_LOOP).idle("fork session")
                )
            for runtime in runtimes:
                log_token = push_log_context(
                    session_id=session_id,
                    thread_id=runtime.require(THREAD_INFO).thread_id,
                )
                try:
                    await runtime.emit(
                        PREPARE_FORK,
                        PrepareFork(session_id, runtime.require(THREAD_INFO).thread_id),
                    )
                finally:
                    reset_log_context(log_token)
            backend = self._root.require(PERSISTENCE)
            thread_ids = []
            after = ""
            while True:
                page = await self._repository.thread_page(session_id, after)
                if not page:
                    break
                for (thread_id,) in page:
                    await self._root.require(ARCHIVE).flush(session_id, thread_id)
                    thread_ids.append(thread_id)
                after = page[-1][0]
            required = await backend.owners(session_id)
            forked_id = new_session_id()
            async with backend.copy_session(session_id, forked_id) as prepared:
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc).isoformat()
                await self._repository.domain(forked_id).transaction(
                    lambda sql: sql.execute(
                        "UPDATE session SET id=?,created_at=?,updated_at=?",
                        (forked_id, now, now),
                    )
                )
                event = ForkState(
                    session_id,
                    forked_id,
                    tuple(thread_ids),
                    prepared.cuts,
                    handled={"session"},
                )
                await self._root.emit(FORK_COPY_STATE, event)
                missing = required - event.handled
                if missing:
                    raise OperationError(
                        "fork_owner_unavailable",
                        f"Cannot safely copy unhandled storage owners: {sorted(missing)}",
                    )
                await self._root.emit(FORK_FINALIZE, event)
                await prepared.publish()
        self._log.info(
            "session.forked",
            session_id=session_id,
            forked_session_id=forked_id,
            threads=len(runtimes),
        )
        forked_summary = await self.session_summary(forked_id)
        self._remember_summary(forked_id, forked_summary)
        await self._events.emit(
            SESSION_RESOURCE_CHANGED,
            SessionResourceChanged(forked_summary, added=True),
        )
        return forked_id

    async def delete_session(self, session_id: str) -> None:
        task = asyncio.current_task()
        async with self._lock:
            if (
                session_id in self._session_operations
                or session_id in self._closing_sessions
                or session_id in self._session_opening
                or any(sid == session_id for sid, _ in self._opening)
            ):
                raise OperationError(
                    "session_busy", "Session ownership is changing", retryable=True
                )
            self._session_operations[session_id] = task
        try:
            return await self._delete_session(session_id)
        finally:
            self._session_operations.pop(session_id, None)

    async def _delete_session(self, session_id: str) -> None:
        self._require_session(session_id)
        active = await self.active_threads()
        runtimes = _session_runtimes(active, session_id)
        if any(runtime.require(AGENT_LOOP).busy for runtime in runtimes):
            raise OperationError(
                "thread_busy",
                "Cannot delete while a session thread has an active turn.",
                retryable=True,
            )
        await self.close_session(session_id, reason="session_deleted")
        ownership = self._root.require(PERSISTENCE).acquire(session_id)
        try:
            root = self.paths.session(session_id).root
            if root.exists() or root.is_symlink():
                await self._root.require(PERSISTENCE).delete_session(session_id)
        finally:
            ownership.release()
        self._published_summaries.pop(session_id, None)
        self._log.info("session.deleted", session_id=session_id)
        await self._events.emit(
            SESSION_RESOURCE_REMOVED,
            SessionResourceRemoved(session_id),
        )

    async def list_threads(
        self, session_id: str, *, limit: int = 100, cursor: str | None = None,
        parent_thread_id: str | None = None, kind: str | None = None,
    ) -> ThreadPage:
        self._require_session(session_id)
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if kind not in (None, "main", "subagent"):
            raise ValueError("Invalid thread kind")
        try:
            boundary, direction = (
                json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if cursor else ("", "next")
            )
            if not isinstance(boundary, str) or direction not in (
                "next", "previous", "next_inclusive", "previous_inclusive",
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid page cursor") from exc
        backwards = direction.startswith("previous")
        inclusive = direction.endswith("_inclusive")
        persisted = await self._repository.thread_page(
            session_id, boundary, limit=limit + 1, backwards=backwards,
            inclusive=inclusive, parent_thread_id=parent_thread_id, kind=kind,
        )
        active = await self.active_threads()
        live = []
        for (sid, identity), scope in active.items():
            if sid != session_id:
                continue
            parent = scope.require(THREAD_INFO).parent_thread_id
            if parent_thread_id is not None and parent != parent_thread_id:
                continue
            if (kind == "main" and parent) or (kind == "subagent" and not parent):
                continue
            if identity == boundary:
                if not inclusive:
                    continue
            elif backwards and identity > boundary:
                continue
            elif not backwards and identity < boundary:
                continue
            live.append(identity)
        live.sort(reverse=backwards)
        ids = []
        for identity in heapq.merge(
            (row[0] for row in persisted), live, reverse=backwards,
        ):
            if ids and identity == ids[-1]:
                continue
            ids.append(identity)
            if len(ids) > limit:
                break
        more = len(ids) > limit
        ids = ids[:limit]
        if backwards:
            ids.reverse()

        def encode(identity, direction):
            return base64.urlsafe_b64encode(
                json.dumps([identity, direction]).encode()
            ).decode()

        previous = None
        next_cursor = None
        if ids:
            if more if backwards else cursor is not None:
                previous = encode(ids[0], "previous")
            if cursor is not None if backwards else more:
                next_cursor = encode(ids[-1], "next")
        elif cursor:
            if backwards:
                next_cursor = encode(boundary, "next_inclusive")
            else:
                previous = encode(boundary, "previous_inclusive")
        summaries = tuple([
            await _thread_summary(
                self, session_id, identity, active.get((session_id, identity)),
            )
            for identity in ids
        ])
        return ThreadPage(
            items=summaries, previous_cursor=previous, next_cursor=next_cursor,
        )

    async def open_thread(self, request: OpenThread) -> Context:
        self._require_session(request.session_id)
        parent_thread_id = request.parent_thread_id
        if request.mode == "resume":
            session = self.paths.session(request.session_id)
            if not await self._repository.has_thread(request.session_id, request.thread_id):
                raise SessionNotFound(f"{request.session_id}/{request.thread_id}")
            persistence = self._thread_repository(
                session,
                thread_id=request.thread_id,
            )
            metadata = await persistence.load()
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
            parent = await self.locate(request.session_id, parent_thread_id)
        except (SessionNotFound, ThreadNotActive) as exc:
            raise OperationError(
                "parent_thread_not_active",
                str(exc),
                retryable=True,
            ) from exc
        workspace_root = str(
            Path(
                request.workspace_root
                or str(parent.require(THREAD_INFO).workspace_root)
            ).resolve()
        )
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
        )
        return runtime

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


def session_has_evidence(paths: RuntimePaths, session_id: str) -> bool:
    session = paths.session(session_id)
    return session.database_file.is_file() or session.config_file.is_file()


def _session_runtimes(
    active: Mapping[tuple[str, str], Context],
    session_id: str,
) -> list[Context]:
    return [
        runtime
        for (active_session_id, _), runtime in active.items()
        if active_session_id == session_id
    ]


async def _thread_summary(
    manager: SessionManager,
    session_id: str,
    thread_id: str,
    active: Context | None,
) -> ThreadSummary:
    if active is not None:
        snapshot = await _snapshot(active)
        metadata = snapshot.metadata
        parent_thread_id = metadata.parent_thread_id
        mode = metadata.runtime_selection.model.generation.mode
        stats = conversation_stats(snapshot.messages).model_copy(
            update={"turns": active.require(LOOP_STATE).turn_count}
        )
        return ThreadSummary(
            session_id=session_id,
            thread_id=thread_id,
            status="active",
            kind="subagent" if parent_thread_id else "main",
            turn_status="running" if active.require(AGENT_LOOP).busy else "idle",
            parent_thread_id=parent_thread_id,
            agent=metadata.runtime_selection.agent_name,
            provider=active.require(
                LOOP_STATE
            ).metadata.value.runtime_selection.model.route.provider,
            model=metadata.runtime_selection.model.route.model,
            model_mode=mode.effort if isinstance(mode, ReasoningGenerationMode) else "",
            context_window=metadata.runtime_selection.model.context_window,
            message_count=len(snapshot.messages),
            usage=snapshot.usage,
            session_stats=stats,
            pending_interactions=pending_interactions(active),
            status_slots=snapshot.status_slots,
            workspace_root=str(active.require(THREAD_INFO).workspace_root),
            title=metadata.title,
        )

    persistence = await manager._persisted_thread(session_id, thread_id)
    metadata = await persistence.load()
    if metadata is None:
        raise SessionNotFound(f"{session_id}/{thread_id}")
    parent_thread_id = metadata.parent_thread_id
    messages = await manager._root.require(ARCHIVE).surface(session_id, thread_id)
    stats = conversation_stats(messages).model_copy(
        update={
            "turns": await manager._root.require(ARCHIVE).turn_count(
                session_id, thread_id
            )
        }
    )
    return ThreadSummary(
        session_id=session_id,
        thread_id=thread_id,
        status="inactive",
        kind="subagent" if parent_thread_id else "main",
        parent_thread_id=parent_thread_id,
        agent=metadata.agent_ref,
        provider=metadata.provider_override or "",
        model=metadata.model_override or "",
        model_mode=metadata.effort_override or "",
        message_count=len(messages),
        usage=await manager._root.require(USAGE_ARCHIVE).read(session_id, thread_id),
        session_stats=stats,
        workspace_root=metadata.workspace_root,
        title=metadata.title,
    )


async def _build_session_summary(
    manager: SessionManager,
    session_id: str,
    active: Mapping[tuple[str, str], Context],
) -> SessionSummary:
    session = manager.paths.session(session_id)
    active_by_thread = {
        thread_id: runtime
        for (active_session_id, thread_id), runtime in active.items()
        if active_session_id == session_id
    }
    if not session.root.is_dir() and not active_by_thread:
        raise SessionNotFound(session_id)
    thread_count = await manager._repository.thread_count(session_id)
    for tid in active_by_thread:
        if not await manager._repository.has_thread(session_id, tid):
            thread_count += 1
    blank = True
    after = ""
    archive = manager._root.require(ARCHIVE)
    while blank:
        page = await manager._repository.thread_page(session_id, after)
        if not page:
            break
        for (tid,) in page:
            if await archive.message_count(session_id, tid):
                blank = False
                break
        after = page[-1][0]
    if blank:
        for tid in active_by_thread:
            if await archive.message_count(session_id, tid):
                blank = False
                break
    row = await manager._repository.row(session_id)
    main = active_by_thread.get("main")
    metadata = main.require(LOOP_STATE).metadata.value if main is not None else None
    return SessionSummary(
        session_id=session_id,
        status="active" if active_by_thread else "inactive",
        active_threads=len(active_by_thread),
        thread_count=thread_count,
        workspace_root=row["workspace"]
        if row is not None
        else metadata.workspace_root
        if metadata is not None
        else "",
        title=row["title"]
        if row is not None
        else metadata.title
        if metadata is not None
        else session_id,
        blank=blank,
    )


async def _unreadable_summary(
    manager: SessionManager,
    session_id: str,
) -> SessionSummary:
    """Keep an unreadable session visible so it can still be deleted."""
    return SessionSummary(
        session_id=session_id,
        status="inactive",
        title=session_id,
        blank=False,
        unreadable=True,
    )


def pending_interactions(ctx: Context) -> list[str]:
    """List pending requests through the application client-event router."""
    return ctx.require(CLIENT_EVENTS).pending_request_ids()


from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _Snapshot:
    metadata: object
    messages: tuple
    usage: UsageSnapshot
    status_slots: dict[str, str]


async def _snapshot(scope):
    state = scope.require(LOOP_STATE)
    return _Snapshot(
        state.metadata.value,
        state.history.snapshot(),
        scope.require(USAGE).snapshot(),
        await status_slots(scope),
    )
