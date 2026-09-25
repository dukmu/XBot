"""Provider-neutral request values produced by the context compiler."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.artifacts import ImageRef
from XBotv2.core.domain import ResolvedModelSelection
from XBotv2.core.parts import ImagePart, ReasoningPart, TextPart
from XBotv2.core.tools import ToolCall


class ResolvedImagePart(BaseModel):
    kind: Literal["image"] = "image"
    ref: ImageRef
    absolute_path: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderSystem(BaseModel):
    role: Literal["system"] = "system"
    parts: tuple[TextPart, ...]
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderUser(BaseModel):
    role: Literal["user"] = "user"
    parts: tuple[TextPart | ResolvedImagePart, ...]
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderAssistant(BaseModel):
    role: Literal["assistant"] = "assistant"
    parts: tuple[TextPart | ReasoningPart | ToolCall, ...]
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderTool(BaseModel):
    role: Literal["tool"] = "tool"
    call_id: str = Field(min_length=1)
    parts: tuple[TextPart | ResolvedImagePart, ...]
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


__all__ = [
    "ProviderAssistant",
    "ProviderMessage",
    "ProviderSystem",
    "ProviderTool",
    "ProviderUser",
    "ResolvedImagePart",
    "ToolSchema",
    "ModelRequest",
]
