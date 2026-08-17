"""Session identity and its spawned child-application hierarchy.

A :class:`Session` is one active conversation (session id + thread id).  It
holds paths, workspace variables, persisted state, and child Agent sessions.
Spawning a subagent asks the application service to start a child application
on its own thread.
"""

from __future__ import annotations

import secrets
from typing import Any

from XBotv2.core.agents import (
    AgentSession,
    SubagentAgentError,
)
from XBotv2.core.paths import SessionPaths


class Session:
    """One active session: identity, session runtime, and the agent hierarchy.

    Child sessions are spawned through :meth:`spawn_subagent` and tracked in
    :attr:`subagents`; application startup owns their construction and close.
    """

    def __init__(
        self,
        *,
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

    def _new_thread_id(self, agent: str) -> str:
        while True:
            thread_id = f"{agent}-{secrets.token_hex(3)}"
            if not self.session_paths.has_thread(thread_id):
                return thread_id


__all__ = ["Session"]
