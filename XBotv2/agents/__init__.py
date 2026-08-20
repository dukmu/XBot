"""Public declarations for the Agent catalog plugin."""

from XBotv2.agents.contracts import (
    AgentCatalog,
    AgentSelection,
    LIST_AGENTS,
    RELOAD_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
__all__ = [
    "AgentCatalog",
    "AgentInfo",
    "AgentListResponse",
    "AgentSelection",
    "AgentSelectionRequest",
    "AgentSelectionResponse",
    "LIST_AGENTS",
    "RELOAD_AGENTS",
    "SELECT_AGENT",
    "SelectAgent",
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
