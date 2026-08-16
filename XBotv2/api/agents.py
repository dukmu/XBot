"""Stable definitions registered by Agent plugins."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

AgentMode = Literal["primary", "subagent", "all"]
_AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Declarative configuration for one primary agent or subagent."""

    name: str
    description: str
    mode: AgentMode = "subagent"
    prompt: str = ""
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    context_window: int | None = None
    max_iterations: int | None = None
    permissions: dict[str, Any] = field(default_factory=dict)
    tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    hidden: bool = False

    def __post_init__(self) -> None:
        if not _AGENT_NAME.fullmatch(self.name):
            raise ValueError(
                "Agent name must use letters, numbers, '.', '_', or '-'"
            )
        if not self.description.strip():
            raise ValueError("Agent description must not be empty")
        if self.mode not in {"primary", "subagent", "all"}:
            raise ValueError("Agent mode must be primary, subagent, or all")
        if self.temperature is not None and self.temperature < 0:
            raise ValueError("Agent temperature must be non-negative")
        for field_name in ("max_output_tokens", "context_window", "max_iterations"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"Agent {field_name} must be positive")


ChildEngineFactory = Callable[[AgentDefinition, str, bool], Awaitable[Any]]


class SubagentAgentError(RuntimeError):
    """Invalid subagent spawn request; the job fails with this error code."""

    code = "agent_not_found"


class SubagentTurnError(RuntimeError):
    """Child turn finished without a usable assistant response."""

    code = "subagent_failed"


@dataclass(frozen=True)
class AgentSessionResult:
    """Outcome of one completed child agent session."""

    final_response: str
    usage: dict[str, Any] = field(default_factory=dict)


class AgentSession(Protocol):
    """One spawned child session owned by an AgentRuntime."""

    async def wait(self) -> AgentSessionResult: ...
    async def cancel(self) -> None: ...


class AgentRuntime(Protocol):
    """Core execution capability exposed to Agent plugins.

    The runtime spawns child sessions; it does not own job lifecycle state.
    Job tracking, waiting, cancellation, and output storage live in the
    shared JobRegistry that the adapter wraps.
    """

    async def spawn(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> AgentSession: ...

    def definitions(self) -> tuple[AgentDefinition, ...]: ...


__all__ = [
    "AgentDefinition",
    "AgentMode",
    "AgentRuntime",
    "AgentSession",
    "AgentSessionResult",
    "ChildEngineFactory",
    "SubagentAgentError",
    "SubagentTurnError",
]
