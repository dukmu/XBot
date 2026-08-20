"""Typed provider-catalog operations owned by the LLM capability."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.core.operations import EmptyRequest, Operation


@dataclass(frozen=True, slots=True)
class ModelDescription:
    model: str
    max_context_tokens: int
    max_output_tokens: int | None
    reasoning_effort: str
    effort: tuple[str, ...]
    thinking: str
    input_modalities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderDescription:
    name: str
    protocol: str
    default_model: str
    models: tuple[ModelDescription, ...]


@dataclass(frozen=True, slots=True)
class ProviderCatalog:
    default: str
    providers: tuple[ProviderDescription, ...]


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
    "ModelDescription",
    "ProviderCatalog",
    "ProviderDescription",
    "ProviderSelection",
    "SELECT_EFFORT",
    "SELECT_PROVIDER",
    "SelectEffort",
    "SelectProvider",
]
