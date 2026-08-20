"""Session component: the active session and session-level runtime services.

Provides the session identity, thread paths, Agentloop ``LoopState``, and
session-level runtime services. Persistence may hydrate and observe that state,
but it does not create the state consumed by the loop.
"""

from __future__ import annotations

from typing import Any

from XBotv2.agentloop import LoopState
from XBotv2.core.variables import RuntimeVariables
from XBotv2.core.filesystem.storage import ThreadStorage
from XBotv2.session.session import Session
from XBotv2.session.commands import build_session_commands
from XBotv2.session.types import SessionInfo


class SessionComponent:
    inject = ["runtime_paths", "session_launch", "commands"]
    """Register the session entity and session-level runtime services."""

    name = "xbot.session"

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        launch = ctx.session_launch
        paths = ctx.runtime_paths
        session_id = launch.session_id
        thread_id = launch.thread_id
        workspace_root = launch.workspace_root
        session_paths = launch.session_paths

        thread_paths = session_paths.thread(
            thread_id,
            legacy=(
                thread_id == "agent"
                and (session_paths.root / "state").exists()
                and not session_paths.thread(thread_id).state_dir.exists()
            ),
        )
        data_root = paths.data_dir
        variables = RuntimeVariables.for_thread(
            paths, workspace_root, thread_paths
        )
        state = LoopState(
            session=SessionInfo(
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=str(workspace_root),
                provider="default",
            ),
            media_root=str(thread_paths.state_dir),
        )
        storage = ThreadStorage.create(
            thread_paths,
            workspace_root=str(workspace_root),
        )
        session = Session(
            ctx=ctx,
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=str(workspace_root),
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
        ctx.set("storage", storage)
        for command in build_session_commands(session):
            ctx.commands.register(command)



plugin = SessionComponent()
