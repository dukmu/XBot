"""HTTP request and response models for live client interactions."""

from typing import Literal

from pydantic import Field, JsonValue

from XBotv2.protocol import WireModel


class UserInputResponseRequest(WireModel):
    request_id: str = Field(min_length=1)
    answer: JsonValue = None


class InteractionResponse(WireModel):
    request_id: str = Field(min_length=1)
    recorded: Literal[True] = True
    pending_interactions: list[str] = Field(default_factory=list)


__all__ = [
    "InteractionResponse",
    "UserInputResponseRequest",
]
