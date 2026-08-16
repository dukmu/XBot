"""Tools component: the tool and agent registries as XCore services.

Creates the tool-layer registries (ToolRegistry, AgentRegistry) and registers
them as services (``ctx.tools`` / ``ctx.agents``).  Registration through these
services is a fiber effect: XCore tracks the currently applying fiber
(:func:`xcore.current_fiber`), so anything a plugin registers is undone
automatically when the plugin's fiber unloads — the service itself binds the
cleanup, no loader-side context tracking.
"""

from __future__ import annotations

import logging
from typing import Any

from XBotv2.core.tools import Tool
from XBotv2.tools.agents import AgentRegistry
from XBotv2.tools.registry import RegisteredSandboxMode, ToolRegistry
from xcore import current_fiber

logger = logging.getLogger("xbot.tools")


def _current_plugin_name() -> str:
    fiber = current_fiber()
    runtime = getattr(fiber, "runtime", None)
    if runtime is not None:
        return runtime.definition.name
    return "unknown"


def _bind_cleanup(disposer: Any) -> None:
    """Register a disposer on the applying fiber (never raises)."""
    fiber = current_fiber()
    if fiber is None:
        return
    try:
        fiber.effect(lambda: disposer)
    except Exception:  # noqa: BLE001 - cleanup registration must not break setup
        logger.exception("failed to register cleanup effect")


class ToolsService:
    """Plugin-facing tool registry with fiber-scoped auto-unregister."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def register(
        self,
        tool: Tool,
        *,
        sandbox_mode: RegisteredSandboxMode = "host",
        model_visible: bool = True,
        timeout_seconds: float | None = None,
        namespace: str | None = None,
    ) -> str:
        """Register one tool; undone automatically when the plugin unloads.

        ``namespace`` is only for functional name scoping (e.g. ``mcp:server``,
        ``skills:scope``); plugin ownership and cleanup are handled by the
        XCore fiber.
        """
        name = self.registry.register(
            tool,
            sandbox_mode=sandbox_mode,
            model_visible=model_visible,
            timeout_seconds=timeout_seconds,
            namespace=namespace,
        )
        _bind_cleanup(lambda: self.registry.unregister(name))
        return name

    def unregister(self, name: str) -> bool:
        return self.registry.unregister(name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)


class AgentsService:
    """Plugin-facing agent registry with fiber-scoped auto-unregister."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def register(self, definition: Any) -> str:
        owner = _current_plugin_name()
        name = self.registry.register(definition, owner=owner)
        _bind_cleanup(lambda: self.registry.unregister(name, owner=owner))
        return name

    def unregister(self, name: str) -> bool:
        owner = _current_plugin_name()
        return self.registry.unregister(name, owner=owner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)


class ToolsComponent:
    """Register the tool and agent registries as XCore services."""

    name = "xbot.tools"

    def apply(self, ctx: Any, config: Any = None) -> None:
        tool_registry = ToolRegistry()
        agent_registry = AgentRegistry()
        ctx.set("tools", ToolsService(tool_registry))
        ctx.set("agents", AgentsService(agent_registry))


plugin = ToolsComponent()
