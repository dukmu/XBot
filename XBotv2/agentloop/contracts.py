"""Typed operations owned by the Agent tool capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.tools import Tool


@dataclass(frozen=True, slots=True)
class ToolDescription:
    name: str
    registered_name: str
    namespace: str
    description: str
    parameters: dict[str, object]
    timeout_seconds: float | None


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    tools: tuple[ToolDescription, ...]


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    """Stable inspection view for one registered Tool."""

    tool: Tool
    registered_name: str
    namespace: str = "builtin"
    model_visible: bool = True
    timeout_seconds: float | None = None
    injected: dict[str, Any] | None = None


LIST_TOOLS = Operation("tools/list", EmptyRequest, ToolCatalog)


__all__ = [
    "LIST_TOOLS",
    "ToolCatalog",
    "ToolDescription",
    "ToolRegistration",
]
