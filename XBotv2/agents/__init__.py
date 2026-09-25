"""Public declarations for the Agent catalog plugin."""

from XBotv2.agents.contracts import (
    AgentCatalog,
    AgentCatalogPort,
    AgentCreateOptions,
    AgentDefinition,
    AgentModelPolicy,
    AgentMode,
    AgentRuntimePort,
    AgentSelection,
    AgentToolPolicy,
    InheritGeneration,
    InheritRoute,
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
    "AgentModelPolicy",
    "AgentInfo",
    "AgentListResponse",
    "AgentSelection",
    "AgentToolPolicy",
    "InheritGeneration",
    "InheritRoute",
    "AgentMode",
    "AgentRuntimePort",
    "AgentSelectionRequest",
    "AgentSelectionResponse",
    "LIST_AGENTS",
    "SELECT_AGENT",
    "SelectAgent",
]
