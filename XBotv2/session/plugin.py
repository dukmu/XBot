"""Session component: the active session and session-level runtime services.

Provides the session identity, thread paths, Agentloop ``LoopState``, and
session-level runtime services. Persistence may hydrate and observe that state,
but it does not create the state consumed by the loop.
"""

from __future__ import annotations

from xcore import Context
from XBotv2.agentloop import LoopState
from XBotv2.core.variables import RuntimeVariables
from XBotv2.session.session import Session
from XBotv2.session.commands import build_session_commands
from XBotv2.session.contracts import SessionInfo, SessionNotFound, ThreadNotActive
from XBotv2.session.manager import SessionManager
from XBotv2.session.protocol import (
    _session_not_found,
    _thread_not_active,
    build_session_router,
)
from XBotv2.server import QUERY_STATUS, ServerStatus, contribute_router


_MANAGER_DEPENDENCIES = {
    "required": [
        "runtime_paths",
        "agent_application_factory",
        "workspace_root",
        "runtime_log",
    ],
    "optional": ["thread_persistence_factory"],
}


class SessionRuntimeComponent:
    inject = ["runtime_paths", "session_launch", "commands", "artifacts"]
    """Register the session entity and session-level runtime services."""

    name = "xbot.session"

    def apply(self, ctx: Context, config: object = None) -> None:
        launch = ctx.session_launch
        paths = ctx.runtime_paths
        session_id = launch.session_id
        thread_id = launch.thread_id
        workspace_root = launch.workspace_root
        session_paths = launch.session_paths

        thread_paths = session_paths.thread(thread_id)
        data_root = paths.data_dir
        variables = RuntimeVariables.for_thread(
            paths, workspace_root, thread_paths
        )
        info = SessionInfo(
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=str(workspace_root),
            provider="default",
        )
        state = LoopState(session=info, variables=variables)
        artifacts = ctx.artifacts
        session = Session(
            events=ctx,
            info=info,
            paths=paths,
            variables=variables,
            state=state,
            session_paths=session_paths,
        )

        ctx.set("session", session)
        ctx.set("paths", paths)
        ctx.set("workspace_root", workspace_root)
        ctx.set("data_root", data_root)
        ctx.set("variables", variables)
        ctx.set("thread_paths", thread_paths)
        ctx.set("loop_state", state)
        for command in build_session_commands(session):
            ctx.commands.register(command)



def mount_runtime(ctx: Context) -> None:
    SessionRuntimeComponent().apply(ctx)


def mount_manager(ctx: Context) -> None:
    manager = SessionManager(
        ctx.runtime_paths,
        ctx,
        thread_persistence_factory=ctx.get("thread_persistence_factory"),
        application_factory=ctx.agent_application_factory,
        runtime_log=ctx.runtime_log,
    )
    ctx.set("sessions", manager)
    ctx.on(
        QUERY_STATUS,
        SessionManagerStatus(manager, str(ctx.workspace_root)).status,
    )
    manager.start_reaper()
    ctx.dispose(manager.close_all)


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.session.http",
        router=build_session_router(
            sessions=ctx.sessions,
            options=ctx.server_options,
            workspace_events=ctx.workspace_events,
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

    def status(self) -> ServerStatus:
        return ServerStatus(
            sessions=self._manager.size,
            threads=self._manager.thread_count,
            workspace_root=self._workspace_root,
        )


class SessionPlugin:
    """Compose thread-local, process-level, and HTTP session behavior."""

    name = "xbot.session"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        ctx.inject(SessionRuntimeComponent.inject, mount_runtime)
        ctx.inject(_MANAGER_DEPENDENCIES, mount_manager)
        ctx.inject(
            ["server", "sessions", "server_options", "workspace_events"],
            mount_http,
        )


plugin = SessionPlugin()

__all__ = ["SessionPlugin"]
