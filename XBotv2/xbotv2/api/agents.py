"""Stable definitions registered by Agent plugins."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from xbotv2.api.tools import ToolResult

AgentMode = Literal["primary", "subagent", "all"]
_AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Declarative configuration for one primary agent or subagent."""

    name: str
    description: str
    mode: AgentMode = "subagent"
    prompt: str = ""
    provider: str | None = None
    permissions: dict[str, Any] = field(default_factory=dict)
    tools: tuple[str, ...] | None = None
    hidden: bool = False

    def __post_init__(self) -> None:
        if not _AGENT_NAME.fullmatch(self.name):
            raise ValueError(
                "Agent name must use letters, numbers, '.', '_', or '-'"
            )
        if not self.description.strip():
            raise ValueError("Agent description must not be empty")
        if self.mode not in {"primary", "subagent", "all"}:
            raise ValueError("Agent mode must be primary, subagent, or all")


class AgentRuntime(Protocol):
    """Core execution capability exposed to Agent plugins."""

    async def run(
        self,
        agent: str,
        prompt: str,
        background: bool = False,
    ) -> ToolResult: ...

    async def list_tasks(self, task_id: str | None = None) -> ToolResult: ...

    async def stop_task(self, task_id: str) -> ToolResult: ...

    def definitions(self) -> tuple[AgentDefinition, ...]: ...


__all__ = ["AgentDefinition", "AgentMode", "AgentRuntime"]
