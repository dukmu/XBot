"""Provider-neutral message and model stream types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xbotv2.api.tools import ToolCall, ToolCallDelta


@dataclass
class Message:
    role: str = ""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""
    status: str = ""
    additional_kwargs: dict[str, Any] = field(default_factory=dict)
    response_metadata: dict[str, Any] = field(default_factory=dict)
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    artifact: Any = None


@dataclass
class ModelResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    response_metadata: dict[str, Any] = field(default_factory=dict)
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    additional_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelChunk:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_chunks: list[ToolCallDelta] = field(default_factory=list)
    response_metadata: dict[str, Any] = field(default_factory=dict)
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    additional_kwargs: dict[str, Any] = field(default_factory=dict)


__all__ = ["Message", "ModelChunk", "ModelResponse"]
