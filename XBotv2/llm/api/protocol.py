"""Transport models owned by this plugin."""
from __future__ import annotations
from pydantic import Field
from XBotv2.protocol import WireModel

class ProviderSelectionRequest(WireModel):
    name: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)


class ProviderSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_mode: str = ""


class EffortSelectionRequest(WireModel):
    effort: str = Field(min_length=1)


class EffortSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = ""
    model_mode: str = ""
    available: list[str] = Field(default_factory=list)
