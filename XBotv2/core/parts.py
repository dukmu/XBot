"""Canonical message content parts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.core.artifacts import ImageRef
from XBotv2.core.domain import ProviderExtensions


class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReasoningPart(BaseModel):
    kind: Literal["reasoning"] = "reasoning"
    text: str
    provider_extensions: ProviderExtensions | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImagePart(BaseModel):
    kind: Literal["image"] = "image"
    image: ImageRef
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = ["ImagePart", "ReasoningPart", "TextPart"]
