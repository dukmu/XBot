"""Typed provider-catalog operations owned by the LLM capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from XBotv2.core.operations import EmptyRequest, Operation
from pydantic import BaseModel, ConfigDict, Field


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
