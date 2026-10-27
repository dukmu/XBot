"""Session component: the active session and session-level runtime services.

Provides the session identity, thread paths, artifacts, and
session-level runtime services. Agentloop owns working-state construction and
hydration; persistence supplies its durable stores.
"""

from __future__ import annotations

from XBotv2.workspaces.api.contracts import WORKSPACE_EVENTS
from XBotv2.commands.api.contracts import COMMANDS

from XBotv2.agentloop.api.contracts import INBOX
from XBotv2.agentloop.api.state import LOOP_STATE
from functools import partial
from xcore.api.context import Context
from XBotv2.core.api.variables import RuntimeVariables
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.session.impl.commands import build_session_commands
from XBotv2.session.api.contracts import (
    SessionNotFound,
    ThreadNotActive,
    THREAD_INFO,
    SESSIONS,
    THREAD_EVENTS,
)
from XBotv2.session.impl.event_stream import SessionEventStream
from XBotv2.loader.api.service import LOADER
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.session.impl.manager import SessionManager
from XBotv2.session.api.storage import THREAD_REPOSITORY, ThreadFacts
from XBotv2.core.api.metadata import (
    THREAD_METADATA_CHANGED,
    THREAD_METADATA_INITIALIZED,
)
from XBotv2.session.impl.config import SessionConfig
from XBotv2.session.impl.protocol import (
    _session_not_found,
    _thread_not_active,
    build_session_router,
)
from XBotv2.server.api.contracts import QUERY_STATUS, ServerStatus, contribute_router


def bind_thread_events(ctx: Context) -> None:
    ctx.provide(THREAD_EVENTS, SessionEventStream(ctx.require(LOOP_STATE).session))
    for command in build_session_commands(
        ctx.require(SESSIONS), ctx.require(THREAD_INFO)
    ):
        ctx.require(COMMANDS).register(command)


def mount_thread(ctx: Context, config: SessionConfig) -> None:
    info = ctx.require(THREAD_INFO)
    paths = ctx.runtime_paths
    thread_paths = paths.session(info.session_id).thread(info.thread_id)
    repository = ctx.require(SESSIONS)._repository.bind_thread(
        info.session_id, info.thread_id
    )
    ctx.provide(THREAD_REPOSITORY, repository)
    ctx.set("state", ctx.require(PERSISTENCE).state(info.session_id, info.thread_id))

    async def remember(change):
        metadata = change.current
        await repository.remember(
            ThreadFacts(
                agent_ref=metadata.runtime_selection.agent_name,
                provider_override=metadata.provider_override,
                model_override=metadata.model_override,
                effort_override=metadata.effort_override,
                parent_thread_id=metadata.parent_thread_id,
                workspace_root=metadata.workspace_root,
                title=metadata.title,
            )
        )

    ctx.on(THREAD_METADATA_CHANGED, remember)
    ctx.on(THREAD_METADATA_INITIALIZED, remember)
    ctx.set("workspace_root", info.workspace_root)
    ctx.set("data_root", paths.data_dir)
    ctx.set(
        "variables",
        RuntimeVariables.for_thread(paths, info.workspace_root, thread_paths),
    )
    ctx.set("thread_paths", thread_paths)
    ctx.inject([LOOP_STATE, INBOX, COMMANDS, SESSIONS, THREAD_INFO], bind_thread_events)


mount_thread.inject = (THREAD_INFO, "runtime_paths", PERSISTENCE, SESSIONS)
mount_thread.provide = (
    THREAD_REPOSITORY,
    "state",
    "workspace_root",
    "data_root",
    "variables",
    "thread_paths",
    THREAD_EVENTS,
)
mount_thread.Config = SessionConfig


def mount_process(ctx: Context, config: SessionConfig) -> None:
    manager = SessionManager(ctx.runtime_paths, ctx, runtime_log=ctx.runtime_log)
    ctx.provide(SESSIONS, manager)
    ctx.set("workspace_root", config.workspace_root)
    ctx.on(
        QUERY_STATUS, SessionManagerStatus(manager, str(config.workspace_root)).status
    )
    manager.start_reaper()
    ctx.pre_stop(manager.close_all)
    ctx.inject(
        ["server", SESSIONS, WORKSPACE_EVENTS, ARCHIVE],
        partial(mount_http, config=config),
    )


mount_process.inject = ("runtime_paths", "runtime_log", LOADER, PERSISTENCE)
mount_process.provide = (SESSIONS, "workspace_root")
mount_process.Config = SessionConfig


async def mount_http(ctx: Context, *, config: SessionConfig) -> None:
    await contribute_router(
        ctx,
        owner="xbot.session.http",
        router=build_session_router(
            sessions=ctx.sessions,
            options=config,
            workspace_events=ctx.require(WORKSPACE_EVENTS),
            archive=ctx.require(ARCHIVE),
        ),
        exception_handlers=(
            (SessionNotFound, _session_not_found),
            (ThreadNotActive, _thread_not_active),
        ),
    )


class SessionManagerStatus:
    def __init__(self, manager: SessionManager, workspace_root: str) -> None:
        self._manager = manager
        self._workspace_root = workspace_root

    def status(self, _request: EmptyRequest) -> ServerStatus:
        return ServerStatus(
            sessions=self._manager.size,
            threads=self._manager.thread_count,
            workspace_root=self._workspace_root,
        )


def mount_session(ctx, config):
    from XBotv2.session.api.contracts import SESSION_INFO

    info = ctx.require(SESSION_INFO)
    backend = ctx.require(PERSISTENCE)
    claim = backend.acquire(info.session_id)
    ctx.pre_stop(lambda: backend.drain(info.session_id))
    ctx.dispose(claim.release)


mount_session.inject = ("runtime_paths", PERSISTENCE, "session_info")
mount_session.provide = ()
