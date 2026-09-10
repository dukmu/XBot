"""Typed provider-catalog operations owned by the LLM capability."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import Message, ModelChunk
from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.providers import BaseProvider


class ModelConfig(BaseModel):
    """Sampling, capacity, and capability settings for one model."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    temperature: float | None = None
    max_context_tokens: int = Field(default=32_000, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: str | None = None
    effort: list[str] | None = None
    thinking: str | None = Field(default=None, min_length=1)
    extra_body: dict[str, JsonValue] = Field(default_factory=dict)
    input_modalities: list[Literal["text", "image"]] = Field(
        default_factory=lambda: ["text"]
    )
    mock_responses: list[dict[str, JsonValue]] = Field(default_factory=list)

    @field_validator("input_modalities")
    @classmethod
    def _validate_input_modalities(cls, value: list[str]) -> list[str]:
        if "text" not in value:
            raise ValueError("input_modalities must include text")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _validate_effort_tiers(self) -> "ModelConfig":
        if (
            self.effort
            and self.reasoning_effort is not None
            and self.reasoning_effort not in self.effort
        ):
            raise ValueError(
                f"reasoning_effort {self.reasoning_effort!r} must be one of "
                f"the advertised effort tiers: {', '.join(self.effort)}"
            )
        return self

    @property
    def model_mode(self) -> str:
        return self.reasoning_effort or self.thinking or ""


class ProviderConfig(BaseModel):
    """One provider endpoint and its model catalog."""

    model_config = ConfigDict(extra="forbid")

    protocol: str = "openai"
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    default_model: str
    models: list[ModelConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_catalog(self) -> "ProviderConfig":
        if not self.models:
            raise ValueError("models must list at least one model")
        names = {model.model for model in self.models}
        if self.default_model not in names:
            raise ValueError(
                f"default_model {self.default_model!r} is not listed in models: "
                + ", ".join(sorted(names))
            )
        return self

    def resolve(self, model: str | None = None) -> ModelConfig:
        name = model or self.default_model
        for candidate in self.models:
            if candidate.model == name:
                return candidate
        raise ValueError(
            f"Unknown model {name!r} for protocol {self.protocol!r}; "
            "configured models: " + ", ".join(m.model for m in self.models)
        )


class LlmConfig(BaseModel):
    """Tree configuration owned by the LLM plugin."""

    model_config = ConfigDict(extra="forbid")

    default: str = "default"
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


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
    "LlmConfig",
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
