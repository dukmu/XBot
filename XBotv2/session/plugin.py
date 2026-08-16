"""Session component: the active session and session-level runtime services.

Provides ``ctx.session`` (the :class:`Session` entity — the agent hierarchy:
main agent instance plus spawned subagent instances) together with the
session-level runtime services the session owns (``ctx.paths`` /
``ctx.workspace_root`` / ``ctx.data_root`` / ``ctx.variables`` /
``ctx.runtime``).  The per-thread state store is the persistence component's
service (``ctx.state_store``); variables are derived from it, so this
component mounts after persistence.
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.variables import RuntimeVariables
from XBotv2.session.session import Session


class SessionComponent:
    """Register the session entity and session-level runtime services."""

    name = "xbot.session"

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        paths = config["paths"]
        session_id = config["session_id"]
        thread_id = config["thread_id"]
        workspace_root = config["workspace_root"]
        runtime_config = config["runtime_config"]
        session_paths = config["session_paths"]
        parent_thread_id = config.get("parent_thread_id", "")
        engine_factory = config["engine_factory"]

        state_store = ctx.state_store
        data_root = state_store.paths.runtime.data_dir
        variables = RuntimeVariables.for_thread(
            paths, workspace_root, state_store.paths
        )
        session = Session(
            ctx,
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=str(workspace_root),
            paths=paths,
            variables=variables,
            state_store=state_store,
            runtime_config=runtime_config,
            session_paths=session_paths,
            parent_thread_id=parent_thread_id,
            engine_factory=engine_factory,
        )

        ctx.set("session", session)
        ctx.set("paths", paths)
        ctx.set("workspace_root", workspace_root)
        ctx.set("data_root", data_root)
        ctx.set("variables", variables)
        ctx.set("runtime", runtime_config)


plugin = SessionComponent()
