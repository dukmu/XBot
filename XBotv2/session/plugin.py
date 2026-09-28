"""Session component: the active session and session-level runtime services.

Provides the session identity, thread paths, Agentloop ``LoopState``, and
session-level runtime services. Persistence may hydrate and observe that state,
but it does not create the state consumed by the loop.
"""

from __future__ import annotations

from functools import partial
from pydantic import JsonValue
from xcore import Context, StateService
from XBotv2.agentloop import AgentInbox, EphemeralInboxSink, LoopState
from XBotv2.core.variables import RuntimeVariables
from XBotv2.core.history import ConversationHistory
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.session.session import Session
from XBotv2.session.commands import build_session_commands
from XBotv2.session.contracts import SessionKey, SessionNotFound, ThreadNotActive
from XBotv2.session.manager import SessionManager
from XBotv2.session.config import SessionConfig
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
        "runtime_log",
    ],
    "optional": ["thread_persistence_factory"],
}


class SessionRuntimeComponent:
    inject = {
        "required": ["runtime_paths", "session_launch", "commands", "runtime_log"],
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

        thread_paths = session_paths.thread(thread_id)
        ctx.set("artifacts", ArtifactStore(thread_paths, ctx.runtime_log))
        data_root = paths.data_dir
        variables = RuntimeVariables.for_thread(
            paths, workspace_root, thread_paths
        )
        key = SessionKey(
            session_id=session_id,
            thread_id=thread_id,
        )
        persistence = ctx.get("thread_persistence", strict=False)
        ctx.set("state", persistence.state if persistence is not None else StateService.memory())
        # The loop state and its metadata register themselves on the context
        # at construction; the inbox service (durable or transient) is composed
        # separately and the loop driver requires it as a constructor argument,
        # so availability, not plugin-tree order, decides when the engine can
        # be built.
        state = LoopState(
            ctx,
            key=key,
            variables=variables,
        )
        if persistence is None:
            pending_inputs = ()
            inbox_sink = EphemeralInboxSink()
        else:
            state.set_history(ConversationHistory(
                persistence.history.load_surface(), sink=persistence.history,
            ))
            state.restore_turn_count(persistence.history.count_turns())
            state.restore_resumed(persistence.has_persisted_state())
            stored_metadata = persistence.metadata.load()
            if stored_metadata is not None:
                await state.metadata.initialize(stored_metadata)
            pending_inputs = persistence.inbox.reconcile(
                persistence.history.committed_input_ids(),
            )
            inbox_sink = persistence.inbox
        ctx.set("agent_inbox", AgentInbox(
            events=ctx, sink=inbox_sink, items=pending_inputs,
        ))
        session = Session(
            events=ctx,
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
        for command in build_session_commands(
            session,
            pending_input_count=lambda: ctx.engine.pending_input_count,
        ):
            ctx.commands.register(command)



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
