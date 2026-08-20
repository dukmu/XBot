"""Session-scoped stream event DTOs owned by the session capability."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from XBotv2.protocol.models import WireModel


class ClientMessageData(WireModel):
    message: str
    level: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call_id: str = ""


class HistoryUpdatedData(WireModel):
    history: list[dict[str, Any]] = Field(default_factory=list)
    operation: Literal["undo", "clear"] = "undo"
    turns: int = Field(ge=0)


class AgentConfiguredData(WireModel):
    agent_name: str | None = None
    provider: str | None = None
    model: str | None = None
    model_mode: str | None = None
    context_window: int | None = None