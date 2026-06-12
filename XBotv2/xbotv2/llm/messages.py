"""XBot-owned message and provider response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    role: str = ""
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""
    status: str = ""
    additional_kwargs: dict[str, Any] = field(default_factory=dict)
    response_metadata: dict[str, Any] = field(default_factory=dict)
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    artifact: Any = None


@dataclass
class XBotModelResponse:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    response_metadata: dict[str, Any] = field(default_factory=dict)
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    additional_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class XBotModelChunk:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_chunks: list[dict[str, Any]] = field(default_factory=list)
    response_metadata: dict[str, Any] = field(default_factory=dict)
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    additional_kwargs: dict[str, Any] = field(default_factory=dict)
