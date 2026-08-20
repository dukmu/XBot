"""Public service Protocols for Agent loop construction and Tools."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from XBotv2.agentloop.contracts import ToolRegistration
from XBotv2.core.loop import LoopFactoryOptions
from XBotv2.core.tools import GuardDecision, Tool


class AgentLoopFactoryPort(Protocol):
    def create(self, options: LoopFactoryOptions) -> object: ...


ToolGuard = Callable[
    [object, ToolRegistration],
    GuardDecision | None | Awaitable[GuardDecision | None],
]


class ToolsPort(Protocol):
    def register(
        self,
        tool: Tool,
        *,
        model_visible: bool = True,
        timeout_seconds: float | None = None,
        namespace: str | None = None,
        injected: dict[str, Any] | None = None,
    ) -> str: ...

    def unregister(self, name: str) -> bool: ...

    def guard(self, guard: ToolGuard) -> object: ...

    def enabled(self) -> tuple[Tool, ...]: ...

    def resolve(self, name: str, *, include_disabled: bool = False) -> Tool | None: ...

    def names(self) -> tuple[str, ...]: ...

    def registered_names(self) -> tuple[str, ...]: ...

    def registrations(self) -> tuple[ToolRegistration, ...]: ...

    def restrict(self, selectors: list[str] | None) -> tuple[str, ...]: ...

    def exclude(self, selectors: list[str]) -> tuple[str, ...]: ...

    async def execute_all(
        self,
        tool_calls: list[object],
        *,
        context_factory: object = None,
    ) -> list[object]: ...


__all__ = ["AgentLoopFactoryPort", "ToolGuard", "ToolsPort"]
