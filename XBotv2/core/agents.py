"""Agent definitions, the agent registry, and engine-side application helpers.

Core owns the ``AgentDefinition`` contract, definition uniqueness/rollback
(:class:`AgentRegistry`), and the helpers that apply a resolved definition to
the base config / provider / tool registry.  Child-session spawning lives in
``XBotv2.session`` (``Session.spawn_subagent`` / ``ChildEngineSession``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from XBotv2.config.policy import merge_permission_config

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
    """One spawned child session owned by a Session."""

    async def wait(self) -> AgentSessionResult: ...
    async def cancel(self) -> None: ...


class AgentRuntime(Protocol):
    """Core execution capability exposed to Agent plugins.

    The runtime spawns child sessions; it does not own job lifecycle state.
    Job tracking, waiting, cancellation, and output storage live in the
    shared jobs service (``ctx.jobs``).
    """

    async def spawn(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> AgentSession: ...

    def definitions(self) -> tuple[AgentDefinition, ...]: ...


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
    "AgentDefinition",
    "AgentMode",
    "AgentRegistry",
    "AgentRuntime",
    "AgentSession",
    "AgentSessionResult",
    "ChildEngineFactory",
    "SubagentAgentError",
    "SubagentTurnError",
    "apply_agent_definition",
    "apply_agent_provider",
    "apply_agent_tools",
]
