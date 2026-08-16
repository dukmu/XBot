"""Stable contracts for XBotv2 plugins.

Plugins are plain XCore plugins (function / object / class) configured through
the plugin tree (``loader``, a cordis.yaml-style mechanism): the module
exports ``plugin`` with ``name`` / ``Config`` (an ``xcore`` ``S`` schema) /
``inject`` / ``apply(ctx, config)``.  ``apply`` receives the XCore context
with XBotv2 capabilities registered as services (``ctx.tools`` /
``ctx.commands`` / ``ctx.prompts`` / ``ctx.agents`` / ``ctx.state`` /
``ctx.jobs`` / ``ctx.variables`` / ...); registrations are fiber effects and
are undone automatically on unload.

Per-plugin persisted state is ``ctx.state.namespace(name)`` -- an async
``get/set/delete/all/clear`` store satisfying :class:`PluginStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from api.commands import Command


@dataclass(frozen=True, slots=True)
class ToolRegistrationOptions:
    """Public setup-time options for registering one plugin tool."""

    sandbox_mode: Literal["host", "sandboxed"] = "host"
    namespace: str | None = None
    model_visible: bool = True
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.sandbox_mode not in {"host", "sandboxed"}:
            raise ValueError("sandbox_mode must be 'host' or 'sandboxed'")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


class PluginStore(Protocol):
    """Per-plugin persisted key-value state (``ctx.state.namespace(name)``)."""

    async def get(self, key: str, default: Any = None) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def all(self) -> dict[str, Any]: ...
    async def clear(self) -> None: ...


class RuntimePluginContext(Protocol):
    """Legacy runtime-capability protocol (kept for API compatibility).

    The ``ctx.plugin_runtime`` injection mechanism is removed: plugins track
    runtime-discovered registrations themselves and unregister them from their
    disposers.
    """

    def register_tool(
        self,
        tool: Any,
        options: ToolRegistrationOptions | None = None,
    ) -> str: ...
    def unregister_tool(self, registered_name: str) -> bool: ...
    def register_command(self, command: Command) -> str: ...
    def unregister_command(self, name: str) -> bool: ...


__all__ = [
    "PluginStore",
    "RuntimePluginContext",
    "ToolRegistrationOptions",
]
