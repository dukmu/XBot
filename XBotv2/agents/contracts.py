"""Typed session operations owned by the Agent capability."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.providers import BaseProvider
from XBotv2.core.variables import RuntimeVariables
from pydantic import BaseModel, ConfigDict, Field, field_validator
from XBotv2.core.domain import (
    AgentExecutionLimits,
    GenerationMode,
    ModelRoute,
)
from XBotv2.agentloop.contracts import AllTools
from XBotv2.permissions.contracts import PermissionPolicy

if TYPE_CHECKING:
    from XBotv2.agentloop import AgentLoopDriverPort
    from XBotv2.config import RuntimeConfig
    from XBotv2.llm import EffortSelection, ProviderSelection

AgentMode = Literal["primary", "subagent", "all"]


class InheritRoute(BaseModel):
    kind: Literal["inherit"] = "inherit"
    model_config = ConfigDict(extra="forbid", frozen=True)


class InheritGeneration(BaseModel):
    kind: Literal["inherit"] = "inherit"
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentModelPolicy(BaseModel):
    route: ModelRoute | InheritRoute = Field(default_factory=InheritRoute)
    generation: GenerationMode | InheritGeneration = Field(
        default_factory=InheritGeneration
    )
    temperature: float | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window: int | None = Field(default=None, gt=0)
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentToolPolicy(BaseModel):
    enabled: tuple[str, ...] | AllTools = Field(default_factory=AllTools)
    disabled: tuple[str, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentDefinition(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    description: str = Field(min_length=1)
    mode: AgentMode = "subagent"
    prompt: str = ""
    model_policy: AgentModelPolicy = Field(default_factory=AgentModelPolicy)
    limits: AgentExecutionLimits = Field(default_factory=AgentExecutionLimits)
    permission_policy: PermissionPolicy = Field(
        default_factory=lambda: PermissionPolicy(default_decision="allow")
    )
    tool_policy: AgentToolPolicy = Field(default_factory=AgentToolPolicy)
    hidden: bool = False

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Agent description must not be empty")
        return value


@dataclass(frozen=True, slots=True)
class AgentCreateOptions:
    session_id: str
    thread_id: str
    workspace_root: str
    provider_name: str | None = None
    selected_agent: str | None = None
    agent_definition: AgentDefinition | None = None
    model_override: BaseProvider | None = None
    parent_thread_id: str = ""
    is_subagent: bool = False


@dataclass(frozen=True, slots=True)
class AgentCatalog:
    active: str
    agents: tuple[AgentDefinition, ...]


class AgentCatalogPort(Protocol):
    def get(self, name: str) -> AgentDefinition | None: ...
    def definitions(self) -> tuple[AgentDefinition, ...]: ...
    def register(self, definition: AgentDefinition, *, overlay: bool = False) -> str: ...
    def register_markdown(
        self,
        directory: Path,
        *,
        variables: RuntimeVariables | None = None,
        overlay: bool = True,
        owner: str | None = None,
    ) -> tuple[str, ...]: ...
    def unregister_owned(
        self,
        owner: str | None = None,
        *,
        overlay: bool = True,
    ) -> list[str]: ...


class AgentRuntimePort(Protocol):
    async def create(self, options: AgentCreateOptions) -> AgentLoopDriverPort: ...
    def active_definition(self) -> AgentDefinition | None: ...
    def current_selection(self) -> AgentSelection: ...
    def runtime_config(
        self,
        definition: AgentDefinition | None = None,
    ) -> RuntimeConfig: ...
    async def select(self, name: str) -> AgentSelection: ...
    async def select_provider(
        self,
        name: str,
        model: str | None = None,
    ) -> ProviderSelection: ...
    async def select_effort(self, value: str) -> EffortSelection: ...


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
    "AgentCatalogPort",
    "AgentCreateOptions",
    "AgentDefinition",
    "AgentMode",
    "AgentModelPolicy",
    "AgentToolPolicy",
    "InheritGeneration",
    "InheritRoute",
    "AgentRuntimePort",
    "AgentSelection",
    "LIST_AGENTS",
    "SELECT_AGENT",
    "SelectAgent",
]
