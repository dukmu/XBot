"""Live thread ownership and persisted session resource summaries."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from xbotv2.api.paths import RuntimePaths
from xbotv2.core.bootstrap import bootstrap
from xbotv2.core.session import SessionRuntime
from xbotv2.persistence.store import CoreStateStore
from xbotv2.protocol.models import SessionSummary, ThreadSummary


class SessionNotFound(KeyError):
    """The caller asked for a session or thread that does not exist."""


class SessionExists(RuntimeError):
    """A new session or thread conflicts with persisted state."""


class ThreadNotActive(RuntimeError):
    """The thread exists on disk but has no live runtime."""


class SessionManager:
    """Own active thread runtimes grouped by persistent session id."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._sessions: dict[tuple[str, str], SessionRuntime] = {}
        self._lock = asyncio.Lock()

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
        llm_override: Any | None = None,
        parent_thread_id: str = "",
        parent_permission_system: Any | None = None,
        subagent_depth: int = 0,
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
            if mode == "new" and session_paths.has_thread(thread_id):
                raise SessionExists(f"{session_id}/{thread_id}")
            engine = await bootstrap(
                paths=self.paths,
                provider_name=provider_name,
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=workspace_root,
                plugin_dirs=[] if no_plugins else None,
                llm_override=llm_override,
                selected_agent=selected_agent,
                parent_thread_id=parent_thread_id,
                parent_permission_system=parent_permission_system,
                subagent_depth=subagent_depth,
            )
            await engine.start_session()
            ctx = SessionRuntime(
                session_id=session_id,
                thread_id=thread_id,
                provider_name=str(
                    getattr(engine.config, "provider", provider_name)
                ),
                paths=self.paths,
                workspace_root=workspace_root,
                no_plugins=no_plugins,
                engine=engine,
            )
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

    async def active_threads(self) -> dict[tuple[str, str], SessionRuntime]:
        async with self._lock:
            return dict(self._sessions)


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
        engine = active.engine
        loader = getattr(engine, "plugin_loader", None)
        status_slots = await loader.status_slots() if loader is not None else {}
        metadata = engine.state_store.read_thread_metadata()
        parent_thread_id = str(metadata.get("parent_thread_id") or "")
        return ThreadSummary(
            session_id=session_id,
            thread_id=thread_id,
            status="active",
            kind="subagent" if parent_thread_id else "main",
            turn_status="running" if active.turn_lock.locked() else "idle",
            parent_thread_id=parent_thread_id,
            agent=str(
                metadata.get("agent")
                or getattr(engine.config, "agent_name", "")
            ),
            provider=active.provider_name,
            model=str(getattr(engine, "model", "")),
            model_mode=str(getattr(engine, "model_mode", "")),
            context_window=int(getattr(engine, "context_window", 0)),
            message_count=len(engine.messages),
            usage=engine.session_usage,
            pending_interactions=pending_interactions(active),
            status_slots=status_slots,
        )

    session = manager.paths.session(session_id)
    if not session.has_thread(thread_id):
        raise SessionNotFound(f"{session_id}/{thread_id}")
    store = CoreStateStore(
        session,
        thread_id=thread_id,
        workspace_root="",
        provider="",
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
        usage=store.read_usage() or _empty_usage(),
    )


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


async def close_disconnected_runtime(
    manager: SessionManager,
    ctx: SessionRuntime,
) -> None:
    metadata = ctx.engine.state_store.read_thread_metadata()
    if metadata.get("parent_thread_id"):
        await manager.close_thread(
            ctx.session_id,
            ctx.thread_id,
            expected=ctx,
            reason="client_disconnected",
        )
    else:
        await manager.close_session(
            ctx.session_id,
            reason="client_disconnected",
        )


def pending_interactions(ctx: SessionRuntime) -> list[str]:
    return list(ctx.engine.user_input_waiter.pending_request_ids()) + list(
        ctx.engine.permission_waiter.pending_request_ids()
    )


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
    "close_disconnected_runtime",
    "pending_interactions",
    "persisted_thread_ids",
    "session_summary",
    "thread_summary",
]
