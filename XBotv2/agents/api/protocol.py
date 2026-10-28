"""Transport models owned by this plugin."""
from __future__ import annotations
from typing import Literal
from pydantic import Field, JsonValue
from XBotv2.protocol.api.models import WireModel
from XBotv2.agents.api.contracts import AgentDefinition, AgentModelPolicy, AgentToolPolicy
from XBotv2.core.api.domain import AgentExecutionLimits

class AgentInfo(WireModel):
    name: str = Field(min_length=1)
    description: str
    mode: Literal["primary", "subagent", "all"]
    model_policy: AgentModelPolicy
    limits: AgentExecutionLimits
    tool_policy: AgentToolPolicy

    @classmethod
    def from_definition(cls, definition: AgentDefinition) -> "AgentInfo":
        return cls(
            name=definition.name,
            description=definition.description,
            mode=definition.mode,
            model_policy=definition.model_policy,
            limits=definition.limits,
            tool_policy=definition.tool_policy,
        )


class AgentListResponse(WireModel):
    active: str = ""
    agents: list[AgentInfo] = Field(default_factory=list)


class AgentSelectionRequest(WireModel):
    name: str = Field(min_length=1)


class AgentSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str
    model_mode: str = ""
    context_window: int = Field(ge=0)


class AgentDefinitionNames(WireModel):
    names: list[str]


class AgentDocument(WireModel):
    name: str
    scope: Literal["global", "workspace"]
    revision: str
    metadata: dict[str, JsonValue]
    prompt: str
