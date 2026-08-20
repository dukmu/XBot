"""Typed session operations owned by the Agent capability."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.core.agents import AgentCreateOptions, AgentDefinition
from XBotv2.core.operations import EmptyRequest, Operation


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
    "AgentInitialized",
    "AgentSelection",
    "INITIALIZE_AGENT",
    "LIST_AGENTS",
    "RELOAD_AGENTS",
    "SELECT_AGENT",
    "SelectAgent",
]
