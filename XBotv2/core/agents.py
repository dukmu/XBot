"""Agent contracts: definitions, modes, and the subagent protocols.

Pure contracts only — the agent registry and child-application lifecycle
implementations live in the application layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

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
    """One spawned child session owned by a Session."""

    async def wait(self) -> AgentSessionResult: ...
    async def cancel(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentCreateOptions:
    """Launcher facts needed to create one Agent instance.

    These are not user configuration.  The agents service resolves them
    against the mounted Agent definitions and runtime services before asking
    the registered loop factory to construct the driver.
    """

    session_id: str
    thread_id: str
    workspace_root: str
    provider_name: str = "default"
    selected_agent: str | None = None
    agent_definition: AgentDefinition | None = None
    model_override: Any = None
    parent_thread_id: str = ""
    is_subagent: bool = False


__all__ = [
    "AgentDefinition",
    "AgentCreateOptions",
    "AgentMode",
    "AgentSession",
    "AgentSessionResult",
    "SubagentAgentError",
    "SubagentTurnError",
]
