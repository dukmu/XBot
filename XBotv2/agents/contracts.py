"""Typed session operations owned by the Agent capability."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.providers import BaseProvider
from XBotv2.core.tools import JsonObject

AgentMode = Literal["primary", "subagent", "all"]
_AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class AgentDefinition:
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
    permissions: JsonObject = field(default_factory=dict)
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
    code = "agent_not_found"


class SubagentTurnError(RuntimeError):
    code = "subagent_failed"


@dataclass(frozen=True, slots=True)
class AgentSessionResult:
    final_response: str
    usage: dict[str, int] = field(default_factory=dict)


class AgentSession(Protocol):
    async def wait(self) -> AgentSessionResult: ...

    async def cancel(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentCreateOptions:
    session_id: str
    thread_id: str
    workspace_root: str
    provider_name: str = "default"
    selected_agent: str | None = None
    agent_definition: AgentDefinition | None = None
    model_override: BaseProvider | None = None
    parent_thread_id: str = ""
    is_subagent: bool = False


@dataclass(frozen=True, slots=True)
class AgentCatalog:
    active: str
    agents: tuple[AgentDefinition, ...]


@dataclass(frozen=True, slots=True)
class SelectAgent:
    name: str


@dataclass(frozen=True, slots=True)
class AgentSelection:
    active: str
    provider: str
    model: str
    model_mode: str
    context_window: int


@dataclass(frozen=True, slots=True)
class AgentInitialized:
    active: str
    provider: str
    model: str
    model_mode: str
    context_window: int


LIST_AGENTS = Operation(
    "agents/list",
    EmptyRequest,
    AgentCatalog,
)
SELECT_AGENT = Operation(
    "agents/select",
    SelectAgent,
    AgentSelection,
    exclusive=True,
)
RELOAD_AGENTS = Operation(
    "agents/reload",
    EmptyRequest,
    AgentCatalog,
    exclusive=True,
)
INITIALIZE_AGENT = Operation(
    "agents/initialize",
    AgentCreateOptions,
    AgentInitialized,
    exclusive=True,
)


__all__ = [
    "AgentCatalog",
    "AgentCreateOptions",
    "AgentDefinition",
    "AgentInitialized",
    "AgentMode",
    "AgentSession",
    "AgentSessionResult",
    "AgentSelection",
    "INITIALIZE_AGENT",
    "LIST_AGENTS",
    "RELOAD_AGENTS",
    "SELECT_AGENT",
    "SelectAgent",
    "SubagentAgentError",
    "SubagentTurnError",
]
