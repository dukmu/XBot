"""Typed session operations owned by the Agent capability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.providers import BaseProvider
from XBotv2.core.usage import UsageData
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

AgentMode = Literal["primary", "subagent", "all"]
class AgentDefinition(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    description: str = Field(min_length=1)
    mode: AgentMode = "subagent"
    prompt: str = ""
    provider: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window: int | None = Field(default=None, gt=0)
    max_iterations: int | None = Field(default=None, gt=0)
    permissions: dict[str, JsonValue] = Field(default_factory=dict)
    tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    hidden: bool = False

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Agent description must not be empty")
        return value


class SubagentAgentError(RuntimeError):
    code = "agent_not_found"


class SubagentTurnError(RuntimeError):
    code = "subagent_failed"


@dataclass(frozen=True, slots=True)
class AgentSessionResult:
    final_response: str
    usage: UsageData = field(default_factory=UsageData)


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
__all__ = [
    "AgentCatalog",
    "AgentCreateOptions",
    "AgentDefinition",
    "AgentMode",
    "AgentSession",
    "AgentSessionResult",
    "AgentSelection",
    "LIST_AGENTS",
    "SELECT_AGENT",
    "SelectAgent",
    "SubagentAgentError",
    "SubagentTurnError",
]
