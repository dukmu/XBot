"""Public declarations for the Agent catalog plugin."""

from XBotv2.agents.contracts import (
    AgentCatalog,
    AgentCreateOptions,
    AgentDefinition,
    AgentInitialized,
    AgentMode,
    AgentSession,
    AgentSessionResult,
    AgentSelection,
    INITIALIZE_AGENT,
    LIST_AGENTS,
    RELOAD_AGENTS,
    SELECT_AGENT,
    SelectAgent,
    SubagentAgentError,
    SubagentTurnError,
)
from XBotv2.agents.services import AgentCatalogPort, AgentRuntimePort
from XBotv2.agents.events import AGENT_CONFIGURED, AgentConfigured

__all__ = [
    "AGENT_CONFIGURED",
    "AgentCatalog",
    "AgentCatalogPort",
    "AgentCreateOptions",
    "AgentConfigured",
    "AgentDefinition",
    "AgentInfo",
    "AgentListResponse",
    "AgentSelection",
    "AgentInitialized",
    "AgentMode",
    "AgentRuntimePort",
    "AgentSession",
    "AgentSessionResult",
    "AgentSelectionRequest",
    "AgentSelectionResponse",
    "INITIALIZE_AGENT",
    "LIST_AGENTS",
    "RELOAD_AGENTS",
    "SELECT_AGENT",
    "SelectAgent",
    "SubagentAgentError",
    "SubagentTurnError",
]

_PROTOCOL_EXPORTS = {
    "AgentInfo",
    "AgentListResponse",
    "AgentSelectionRequest",
    "AgentSelectionResponse",
}


def __getattr__(name: str) -> object:
    if name not in _PROTOCOL_EXPORTS:
        raise AttributeError(name)
    from XBotv2.agents import protocol

    return getattr(protocol, name)
