"""Provider-neutral request values produced by the context compiler."""

from __future__ import annotations

from typing import Literal, TypeAlias
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from XBotv2.core.api.artifacts import ArtifactRef
from XBotv2.core.api.domain import ResolvedModelSelection
from XBotv2.core.api.parts import ReasoningPart, TextPart
from XBotv2.core.api.tools import ToolCall


class ResolvedMediaPart(BaseModel):
    kind: Literal["media"] = "media"
    artifact: ArtifactRef
    absolute_path: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderSystem(BaseModel):
    role: Literal["system"] = "system"
    parts: tuple[TextPart, ...]
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderUser(BaseModel):
    role: Literal["user"] = "user"
    parts: tuple[TextPart | ResolvedMediaPart, ...]
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderAssistant(BaseModel):
    role: Literal["assistant"] = "assistant"
    parts: tuple[TextPart | ReasoningPart | ToolCall, ...]
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderTool(BaseModel):
    role: Literal["tool"] = "tool"
    call_id: str = Field(min_length=1)
    parts: tuple[TextPart | ResolvedMediaPart, ...]
    is_error: bool = False
    model_config = ConfigDict(extra="forbid", frozen=True)


ProviderMessage: TypeAlias = ProviderSystem | ProviderUser | ProviderAssistant | ProviderTool


class ToolSchema(BaseModel):
    name: str = Field(min_length=1)
    description: str
    parameters: dict[str, JsonValue]
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelRequest(BaseModel):
    """Complete provider-neutral request sent to one model selection."""

    messages: tuple[ProviderMessage, ...]
    tools: tuple[ToolSchema, ...]
    selection: ResolvedModelSelection
    model_config = ConfigDict(extra="forbid", frozen=True)
