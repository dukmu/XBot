"""Public declarations for the Agent catalog plugin."""

from XBotv2.agents.contracts import (
    AgentCatalog,
    AgentCatalogPort,
    AgentCreateOptions,
    AgentDefinition,
    AgentMode,
    AgentRuntimePort,
    AgentSelection,
    LIST_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
from XBotv2.agents.events import AGENT_CONFIGURED, AgentConfigured
from XBotv2.agents.protocol import (
    AgentInfo,
    AgentListResponse,
    AgentSelectionRequest,
    AgentSelectionResponse,
)

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
    "AgentMode",
    "AgentRuntimePort",
    "AgentSelectionRequest",
    "AgentSelectionResponse",
    "LIST_AGENTS",
    "SELECT_AGENT",
    "SelectAgent",
]
