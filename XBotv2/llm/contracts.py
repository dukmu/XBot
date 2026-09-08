"""Typed provider-catalog operations owned by the LLM capability."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import Message, ModelChunk
from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.providers import BaseProvider
from XBotv2.llm.config import ModelConfig, ProviderConfig
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ModelDescription(BaseModel):
    model: str = Field(min_length=1)
    max_context_tokens: int = Field(ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: str = ""
    effort: tuple[str, ...] = ()
    thinking: str = ""
    input_modalities: tuple[Literal["text", "image"], ...] = ("text",)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderDescription(BaseModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    default_model: str = Field(min_length=1)
    models: tuple[ModelDescription, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderCatalog(BaseModel):
    default: str
    providers: tuple[ProviderDescription, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)


class LlmCatalogPort(Protocol):
    """Read-only provider catalog consumed by commands and transports."""

    def catalog(self) -> ProviderCatalog: ...


class ModelPort(Protocol):
    """Mutable model binding consumed by the Agent loop."""

    def bind_tools(
        self,
        tools: list[dict[str, JsonValue]],
        **kwargs: object,
    ) -> BaseProvider: ...

    def astream(
        self,
        messages: list[Message],
        **kwargs: object,
    ) -> AsyncIterator[ModelChunk]: ...


class LlmServicePort(LlmCatalogPort, Protocol):
    """Provider directory service mounted as ``ctx.llm``."""

    def register(self, provider: str, factory: Callable[..., BaseProvider]) -> None: ...
    def unregister(self, provider: str) -> bool: ...
    def providers(self) -> tuple[str, ...]: ...
    def has(self, provider: str) -> bool: ...
    def configure(
        self,
        default: str | None,
        providers: dict[str, dict[str, JsonValue]] | None,
    ) -> None: ...
    def default_name(self) -> str: ...
    def names(self) -> tuple[str, ...]: ...
    def provider_config(self, name: str, *, require_key: bool = True) -> ProviderConfig: ...
    def create(
        self,
        provider_config: ProviderConfig,
        model_config: ModelConfig | None = None,
        *,
        model: str | None = None,
        artifacts: ArtifactStorePort | None = None,
    ) -> BaseProvider: ...


@dataclass(frozen=True, slots=True)
class SelectProvider:
    name: str
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    provider: str
    model: str
    model_mode: str


@dataclass(frozen=True, slots=True)
class SelectEffort:
    effort: str


@dataclass(frozen=True, slots=True)
class EffortSelection:
    provider: str
    model: str
    reasoning_effort: str
    model_mode: str
    available: tuple[str, ...]


LIST_PROVIDERS = Operation(
    "llm/providers/list",
    EmptyRequest,
    ProviderCatalog,
)
SELECT_PROVIDER = Operation(
    "llm/provider/select",
    SelectProvider,
    ProviderSelection,
    exclusive=True,
)
SELECT_EFFORT = Operation(
    "llm/effort/select",
    SelectEffort,
    EffortSelection,
    exclusive=True,
)


__all__ = [
    "EffortSelection",
    "LIST_PROVIDERS",
    "LlmCatalogPort",
    "LlmServicePort",
    "ModelDescription",
    "ModelPort",
    "ProviderCatalog",
    "ProviderDescription",
    "ProviderSelection",
    "SELECT_EFFORT",
    "SELECT_PROVIDER",
    "SelectEffort",
    "SelectProvider",
]
