"""The active session: main agent instance plus spawned subagent instances.

A :class:`Session` is one active conversation (session id + thread id).  It
holds the session-level runtime (paths, workspace, variables, state store,
runtime config) and the agent hierarchy: a single *main agent instance* (the
root engine, ``ctx.engine``) and the *subagent instances* spawned from it
(``Session.subagents``).  Spawning a subagent bootstraps a child engine on its
own thread; each child session binds its own runtime the same way the main
agent does.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from XBotv2.core.agents import (
    AgentSessionResult,
    SubagentAgentError,
    SubagentTurnError,
)
from XBotv2.core.paths import SessionPaths


@dataclass(slots=True)
class ChildEngineSession:
    """One spawned subagent instance; implements the AgentSession protocol."""

    child: Any
    prompt: str
    agent: str
    thread_id: str
    session_paths: SessionPaths
    parent_thread_id: str

    async def wait(self) -> AgentSessionResult:
        """Run the child turn, collect its response and usage, then close."""
        await self.child.start_session()
        output = ""
        error = ""
        try:
            async for event in self.child.run_turn(self.prompt):
                event_type = event.get("type")
                data = event.get("data") or {}
                if event_type == "assistant_message":
                    output = str(data.get("content") or "")
                elif event_type == "error":
                    error = str(data.get("message") or "Subagent turn failed")
                elif event_type == "turn_cancelled":
                    error = str(
                        data.get("reason") or "Subagent turn was cancelled"
                    )
        except asyncio.CancelledError:
            with suppress(BaseException):
                await asyncio.shield(self.child.close_session())
            self._record("cancelled", error=error)
            raise
        usage = dict(getattr(self.child, "session_usage", {}) or {})
        close_error = await self._close_child()
        if close_error and not error:
            error = close_error
        if error:
            self._record("failed", error=error)
            raise SubagentTurnError(error)
        if not output:
            error = "Subagent completed without an assistant response"
            self._record("failed", error=error)
            raise SubagentTurnError(error)
        self._record("completed")
        return AgentSessionResult(final_response=output, usage=usage)

    async def cancel(self) -> None:
        """Best-effort release; the registry cancels the runner task, and
        ``wait`` closes the child on CancelledError."""

    async def _close_child(self) -> str:
        try:
            await self.child.close_session()
        except Exception as exc:  # noqa: BLE001 - close errors become state
            return f"Subagent close failed: {exc}"
        return ""

    def _record(self, event: str, *, error: str = "") -> None:
        path = self.session_paths.threads_log
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": event,
            "thread_id": self.thread_id,
            "parent_thread_id": self.parent_thread_id,
            "agent": self.agent,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if error:
            record["error"] = error
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


class Session:
    """One active session: identity, session runtime, and the agent hierarchy.

    The main agent instance is the root engine (``ctx.engine``); subagent
    instances are spawned through :meth:`spawn_subagent` and tracked in
    :attr:`subagents`.  The agent registry is resolved lazily through the
    context so the session plugin can mount before the tools component.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        session_id: str,
        thread_id: str,
        workspace_root: str,
        paths: Any,
        variables: Any,
        state_store: Any,
        session_paths: SessionPaths,
        parent_thread_id: str,
        engine_factory: Any,
    ) -> None:
        self.ctx = ctx
        self.session_id = session_id
        self.thread_id = thread_id
        self.workspace_root = workspace_root
        self.paths = paths
        self.variables = variables
        self.state_store = state_store
        self.session_paths = session_paths
        self.parent_thread_id = parent_thread_id
        self.engine_factory = engine_factory
        self.subagents: list[ChildEngineSession] = []

    # -- session identity (SessionInfo-compatible surface) ------------------

    @property
    def provider(self) -> str:
        return str(getattr(self.state_store, "provider", "") or "")

    @property
    def main_agent(self) -> Any:
        """The main agent instance: the root engine (``ctx.engine``)."""
        return self.ctx.get("engine", strict=False)

    turn_count = 0
    event_count = 0
    status = "active"

    # -- subagent instances -------------------------------------------------

    async def spawn_subagent(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> ChildEngineSession:
        """Spawn one subagent instance on its own thread (recursive)."""
        del parent_job_id
        registry = self.ctx.agents.registry
        definition = registry.get(agent)
        if definition is None or definition.mode == "primary":
            raise SubagentAgentError(f"Unknown subagent: {agent}")
        if not prompt.strip():
            raise SubagentAgentError("Subagent prompt cannot be empty")
        thread_id = self._new_thread_id(definition.name)
        child = await self.engine_factory(definition, thread_id, False)
        session = ChildEngineSession(
            child=child,
            prompt=prompt,
            agent=definition.name,
            thread_id=thread_id,
            session_paths=self.session_paths,
            parent_thread_id=self.parent_thread_id,
        )
        session._record("started")
        self.subagents.append(session)
        return session

    def definitions(self) -> tuple[Any, ...]:
        return self.ctx.agents.registry.definitions()

    def _new_thread_id(self, agent: str) -> str:
        while True:
            thread_id = f"{agent}-{secrets.token_hex(3)}"
            if not self.session_paths.has_thread(thread_id):
                return thread_id


__all__ = ["ChildEngineSession", "Session"]
