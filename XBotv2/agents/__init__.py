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
    LIST_AGENTS,
    RELOAD_AGENTS,
    SELECT_AGENT,
    SelectAgent,
    SubagentAgentError,
    SubagentTurnError,
)
__all__ = [
    "AgentCatalog",
    "AgentCreateOptions",
    "AgentDefinition",
    "AgentInfo",
    "AgentListResponse",
    "AgentSelection",
    "AgentInitialized",
    "AgentMode",
    "AgentSession",
    "AgentSessionResult",
    "AgentSelectionRequest",
    "AgentSelectionResponse",
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
