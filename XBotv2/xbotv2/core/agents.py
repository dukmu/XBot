"""Ownership-aware registry for plugin-defined agents.

Core owns definition uniqueness, registration rollback, and later execution.
Execution is exposed to Agent plugins through the api-level AgentRuntime
protocol: this module implements the child-session spawner and the session that
drives one child Engine. It owns no job lifecycle state — the shared
JobRegistry does that.
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

from xbotv2.api.agents import (
    AgentDefinition,
    AgentSession,
    AgentSessionResult,
    ChildEngineFactory,
    SubagentAgentError,
    SubagentTurnError,
)
from xbotv2.api.paths import SessionPaths
from xbotv2.config.policy import merge_permission_config


@dataclass(slots=True)
class ChildEngineSession:
    """One spawned child engine; implements the api AgentSession protocol."""

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


class EngineAgentRuntime:
    """Api-level AgentRuntime backed by the bootstrap child-engine factory."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        session_paths: SessionPaths,
        parent_thread_id: str,
        engine_factory: ChildEngineFactory,
    ) -> None:
        self.registry = registry
        self.session_paths = session_paths
        self.parent_thread_id = parent_thread_id
        self.engine_factory = engine_factory

    async def spawn(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> AgentSession:
        del parent_job_id
        definition = self.registry.get(agent)
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
        return session

    def definitions(self) -> tuple[AgentDefinition, ...]:
        return self.registry.definitions()

    def _new_thread_id(self, agent: str) -> str:
        while True:
            thread_id = f"{agent}-{secrets.token_hex(3)}"
            if not self.session_paths.has_thread(thread_id):
                return thread_id


class AgentRegistry:
    """Stores immutable Agent definitions under one plugin owner."""

    def __init__(self) -> None:
        self._definitions: dict[str, AgentDefinition] = {}
        self._owners: dict[str, str] = {}

    def register(self, definition: AgentDefinition, *, owner: str) -> str:
        if definition.name in self._definitions:
            raise ValueError(f"Agent {definition.name!r} is already registered")
        self._definitions[definition.name] = definition
        self._owners[definition.name] = owner
        return definition.name

    def unregister(self, name: str, *, owner: str) -> bool:
        if self._owners.get(name) != owner:
            return False
        self._owners.pop(name, None)
        self._definitions.pop(name, None)
        return True

    def get(self, name: str) -> AgentDefinition | None:
        return self._definitions.get(name)

    def definitions(self) -> tuple[AgentDefinition, ...]:
        return tuple(self._definitions.values())


def apply_agent_definition(config: Any, definition: AgentDefinition) -> None:
    """Apply one resolved Agent definition to a fresh base configuration."""
    config.agent_name = definition.name
    config.agent_role = definition.description
    config.agent_instructions = definition.prompt
    if definition.tools is not None:
        config.tools = list(definition.tools)
    if definition.context_window is not None:
        config.max_context_tokens = definition.context_window
    config.permissions = merge_permission_config(
        config.permissions,
        definition.permissions,
    )


def apply_agent_provider(provider: Any, definition: AgentDefinition) -> None:
    """Apply model request settings to a loaded provider configuration."""
    if definition.model is not None:
        provider.model = definition.model
    if definition.temperature is not None:
        provider.temperature = definition.temperature
    if definition.max_output_tokens is not None:
        provider.max_output_tokens = definition.max_output_tokens


def apply_agent_tools(registry: Any, config: Any, definition: AgentDefinition) -> None:
    """Replace the model-visible tool set for one active Agent."""
    selectors = (
        list(definition.tools)
        if definition.tools is not None
        else list(config.tools) if config.tools else None
    )
    registry.restrict(selectors)
    if definition.disabled_tools:
        registry.exclude(list(definition.disabled_tools))


__all__ = [
    "AgentRegistry",
    "ChildEngineSession",
    "EngineAgentRuntime",
    "apply_agent_definition",
    "apply_agent_provider",
    "apply_agent_tools",
]
