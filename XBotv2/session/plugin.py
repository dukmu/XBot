"""Session component: the active session and session-level runtime services.

Provides the session identity, thread paths, core ``LoopState``, and
session-level runtime services. Persistence may hydrate and observe that state,
but it does not create the state consumed by the loop.
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.loop import LoopState
from XBotv2.core.runtime import SessionInfo
from XBotv2.core.variables import RuntimeVariables
from XBotv2.filesystem.storage import ThreadStorage
from XBotv2.session.session import Session
from XBotv2.session.commands import SESSION_COMMANDS


class SessionComponent:
    inject = ['agents', 'child_applications', 'commands']
    """Register the session entity and session-level runtime services."""

    name = "xbot.session"

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        paths = config["paths"]
        session_id = config["session_id"]
        thread_id = config["thread_id"]
        workspace_root = config["workspace_root"]
        session_paths = config["session_paths"]

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
            agents=ctx.agents,
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=str(workspace_root),
            paths=paths,
            variables=variables,
            state=state,
            session_paths=session_paths,
            child_applications=ctx.child_applications,
        )

        ctx.set("session", session)
        ctx.set("paths", paths)
        ctx.set("workspace_root", workspace_root)
        ctx.set("data_root", data_root)
        ctx.set("variables", variables)
        ctx.set("thread_paths", thread_paths)
        ctx.set("loop_state", state)
        ctx.set("storage", storage)
        for command in SESSION_COMMANDS:
            ctx.commands.register(command)


plugin = SessionComponent()
