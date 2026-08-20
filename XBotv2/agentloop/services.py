"""Public service Protocols for Agent loop construction and Tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable, Protocol

from XBotv2.agentloop.contracts import ToolRegistration
from XBotv2.core.loop import LoopFactoryOptions, LoopSettings
from XBotv2.core.messages import ImageContent, Message
from XBotv2.core.tools import GuardDecision, Tool


class AgentLoopFactoryPort(Protocol):
    def create(self, options: LoopFactoryOptions) -> object: ...


class AgentLoopDriverPort(Protocol):
    """Session-host surface of one active Agent loop driver."""

    settings: LoopSettings
    messages: list[Message]
    context_window: int
    pending_input_count: int

    def set_wake_driver(self, callback: Callable[[], None]) -> None: ...

    async def start_session(self) -> None: ...

    async def close_session(self) -> None: ...

    async def discard_inputs(self) -> None: ...

    async def followup(self, content: str, **kwargs: Any) -> object: ...

    async def inject(self, content: str, **kwargs: Any) -> object: ...

    async def steer(self, content: str, **kwargs: Any) -> object: ...

    def run_turn(
        self,
        content: str,
        *,
        request_id: str = "",
        images: list[ImageContent] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    def run_pending(
        self,
        *,
        request_id: str = "",
    ) -> AsyncIterator[dict[str, Any]]: ...


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


__all__ = [
    "AgentLoopDriverPort",
    "AgentLoopFactoryPort",
    "ToolGuard",
    "ToolsPort",
]
