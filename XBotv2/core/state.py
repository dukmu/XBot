"""Strict JSON models for plugin-owned persisted state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict

from XBotv2.core.tools import JsonObject, json_object


class JsonStateModel(BaseModel):
    """One immutable, versioned value stored through XCore StateService."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        return cls.model_validate(dict(value))

    def to_dict(self) -> JsonObject:
        return json_object(self.model_dump(mode="json"))


__all__ = ["JsonStateModel"]
