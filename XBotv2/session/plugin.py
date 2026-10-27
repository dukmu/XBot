"""Session component: the active session and session-level runtime services.

Provides the session identity, thread paths, artifacts, and
session-level runtime services. Agentloop owns working-state construction and
hydration; persistence supplies its durable stores.
"""

from __future__ import annotations
from XBotv2.commands.api.contracts import COMMANDS

from XBotv2.agentloop.api.contracts import INBOX
from XBotv2.agentloop.api.state import LOOP_STATE
from functools import partial
from pathlib import Path
from pydantic import JsonValue
from xcore.api.context import Context
from xcore import StateService
from XBotv2.core.api.variables import RuntimeVariables
from XBotv2.core.api.errors import OperationError
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.session.session import Session
from XBotv2.session.commands import build_session_commands
from XBotv2.session.contracts import SessionNotFound, ThreadNotActive
from XBotv2.session.manager import SessionManager
from XBotv2.session.config import SessionConfig
from XBotv2.session.protocol import _session_not_found, _thread_not_active, build_session_router
from XBotv2.server import QUERY_STATUS, ServerStatus, contribute_router


_MANAGER_DEPENDENCIES = {
    "required": [
        "runtime_paths",
        "agent_application_factory",
        "runtime_log",
    ],
    "optional": ["thread_persistence_factory"],
}


class SessionRuntimeComponent:
    inject = {
        "required": ["runtime_paths", "session_launch", COMMANDS, "runtime_log"],
        "optional": ["thread_persistence"],
    }
    """Register the session entity and session-level runtime services."""

    name = "xbot.session"

    async def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        launch = ctx.session_launch
        paths = ctx.runtime_paths
        session_id = launch.session_id
        thread_id = launch.thread_id
        workspace_root = launch.workspace_root
        session_paths = launch.session_paths
        persistence = ctx.get("thread_persistence", strict=False)
        stored_metadata = persistence.metadata.load() if persistence is not None else None
        if stored_metadata is not None:
            launch_workspace = Path(workspace_root).expanduser().resolve()
            persisted_workspace = Path(
                stored_metadata.workspace_root
            ).expanduser().resolve()
            if launch_workspace != persisted_workspace:
                raise OperationError(
                    "workspace_conflict",
                    f"Thread {session_id}/{thread_id} belongs to workspace "
                    f"{persisted_workspace}, not {launch_workspace}",
                )

        thread_paths = session_paths.thread(thread_id)
        ctx.set("artifacts", ArtifactStore(thread_paths, ctx.runtime_log))
        data_root = paths.data_dir
        variables = RuntimeVariables.for_thread(
            paths, workspace_root, thread_paths
        )
        ctx.set("state", persistence.state if persistence is not None else StateService.memory())
        ctx.set("paths", paths)
        ctx.set("workspace_root", workspace_root)
        ctx.set("data_root", data_root)
        ctx.set("variables", variables)
        ctx.set("thread_paths", thread_paths)
        ctx.inject([LOOP_STATE, INBOX], mount_session)


def mount_session(ctx: Context) -> None:
    launch = ctx.session_launch
    session = Session(
        events=ctx,
        paths=ctx.paths,
        variables=ctx.variables,
        state=ctx.require(LOOP_STATE),
        session_paths=launch.session_paths,
    )
    ctx.set("session", session)
    for command in build_session_commands(
        session,
        pending_input_count=lambda: ctx.engine.pending_input_count,
    ):
        ctx.require(COMMANDS).register(command)


async def mount_runtime(ctx: Context) -> None:
    await SessionRuntimeComponent().apply(ctx)


def mount_manager(ctx: Context, *, config: SessionConfig) -> None:
    manager = SessionManager(
        ctx.runtime_paths,
        ctx,
        thread_persistence_factory=ctx.get("thread_persistence_factory"),
        application_factory=ctx.agent_application_factory,
        runtime_log=ctx.runtime_log,
    )
    ctx.set("sessions", manager)
    ctx.set("workspace_root", config.workspace_root)
    ctx.on(
        QUERY_STATUS,
        SessionManagerStatus(manager, str(config.workspace_root)).status,
    )
    manager.start_reaper()
    ctx.dispose(manager.close_all)


async def mount_http(ctx: Context, *, config: SessionConfig) -> None:
    await contribute_router(
        ctx,
        owner="xbot.session.http",
        router=build_session_router(
            sessions=ctx.sessions,
            options=config,
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
    Config = SessionConfig

    def apply(
        self, ctx: Context, config: SessionConfig
    ) -> None:
        ctx.inject(SessionRuntimeComponent.inject, mount_runtime)
        ctx.inject(_MANAGER_DEPENDENCIES, partial(mount_manager, config=config))
        ctx.inject(
            ["server", "sessions", "workspace_events"],
            partial(mount_http, config=config),
        )


plugin = SessionPlugin()

__all__ = ["SessionPlugin"]
