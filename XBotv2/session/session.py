"""Session identity and its spawned child-application hierarchy.

A :class:`Session` is one active conversation (session id + thread id).  It
holds paths, workspace variables, persisted state, and child Agent sessions.
Spawning a subagent asks the application service to start a child application
on its own thread.
"""

from __future__ import annotations

import secrets
import shutil
from typing import Any

from XBotv2.core.agents import (
    AgentSession,
    SubagentAgentError,
)
from XBotv2.core.errors import OperationError
from XBotv2.core.events import EventContext, Events
from XBotv2.core.paths import SessionPaths


def fork_persisted_session(paths: Any, source_session_id: str) -> str:
    """Copy one persisted session to a fresh session id."""
    session_id = _new_fork_id()
    while paths.session(session_id).root.exists():
        session_id = _new_fork_id()
    source = paths.session(source_session_id).root
    target = paths.session(session_id).root
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return session_id


def _new_fork_id() -> str:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def require_forkable(*contexts: Any) -> None:
    """Fail when any live context is busy or running background tasks."""
    for ctx in contexts:
        if ctx.turn_lock.locked():
            raise OperationError(
                "thread_busy",
                f"Cannot fork while a turn is active.",
                retryable=True,
            )
        registry = ctx.services.get("jobs")
        if registry is not None and registry.is_busy():
            raise OperationError(
                "thread_busy",
                "Cannot fork while a background task is active.",
                retryable=True,
            )


async def fork_session(
    paths: Any,
    source_session_id: str,
    contexts: list[Any],
) -> str:
    """Persist and copy one idle session while all live threads are locked."""
    from contextlib import AsyncExitStack

    require_forkable(*contexts)
    async with AsyncExitStack() as stack:
        for ctx in sorted(contexts, key=lambda item: item.thread_id):
            await stack.enter_async_context(ctx.turn_lock)
        for ctx in contexts:
            persistence = ctx.services.get("persistence", strict=False)
            if persistence is None:
                raise OperationError(
                    "persistence_unavailable",
                    "Cannot fork a live session while message persistence "
                    "is disabled",
                )
            await persistence.flush()
        return fork_persisted_session(paths, source_session_id)


class Session:
    """One active session: identity, session runtime, and the agent hierarchy.

    Child sessions are spawned through :meth:`spawn_subagent` and tracked in
    :attr:`subagents`; application startup owns their construction and close.
    """

    def __init__(
        self,
        *,
        ctx: Any = None,
        agents: Any,
        session_id: str,
        thread_id: str,
        workspace_root: str,
        paths: Any,
        variables: Any,
        state: Any,
        session_paths: SessionPaths,
        child_applications: Any,
    ) -> None:
        self.ctx = ctx
        self.agents = agents
        self.session_id = session_id
        self.thread_id = thread_id
        self.workspace_root = workspace_root
        self.paths = paths
        self.variables = variables
        self.state = state
        self.session_paths = session_paths
        self.child_applications = child_applications
        self.subagents: list[AgentSession] = []

    # -- session identity (SessionInfo-compatible surface) ------------------

    @property
    def provider(self) -> str:
        return self.state.session.provider

    # -- subagent instances -------------------------------------------------

    async def spawn_subagent(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> AgentSession:
        """Spawn one subagent instance on its own thread (recursive)."""
        del parent_job_id
        definition = self.agents.definition(agent)
        if definition is None or definition.mode == "primary":
            raise SubagentAgentError(f"Unknown subagent: {agent}")
        if not prompt.strip():
            raise SubagentAgentError("Subagent prompt cannot be empty")
        thread_id = self._new_thread_id(definition.name)
        session = await self.child_applications(definition, thread_id, prompt)
        self.subagents.append(session)
        return session

    def definitions(self) -> tuple[Any, ...]:
        return self.agents.definitions()

    # -- history mutations --------------------------------------------------

    async def clear_history(self) -> int:
        """Remove every user turn; caller owns idle-check and turn lock."""
        removed = sum(
            message.role == "user"
            for message in self.ctx.engine.messages
        )
        await self._replace_history([], operation="clear")
        return removed

    async def undo_history(self, count: int) -> list[Any]:
        """Undo complete user turns; caller owns idle-check and turn lock."""
        messages = list(self.ctx.engine.messages)
        user_indexes = [
            index for index, message in enumerate(messages)
            if message.role == "user"
        ]
        if count > len(user_indexes):
            raise OperationError(
                "invalid_undo_count",
                f"Cannot undo {count} turns; session has {len(user_indexes)}.",
            )
        kept = messages[:user_indexes[-count]]
        await self._replace_history(kept, operation="undo", turns=count)
        return kept

    async def _replace_history(
        self,
        messages: list[Any],
        *,
        operation: str,
        turns: int = 0,
    ) -> None:
        state = self.state
        state.replace_messages(messages)
        await self.ctx.emit(Events.STATE_CHANGED, EventContext(
            messages=state.messages,
            session=state.session,
            event={"history_operation": (operation, turns)},
        ))

    def _new_thread_id(self, agent: str) -> str:
        while True:
            thread_id = f"{agent}-{secrets.token_hex(3)}"
            if not self.session_paths.has_thread(thread_id):
                return thread_id


__all__ = ["Session"]
