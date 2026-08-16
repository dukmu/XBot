"""Tools component: the tool and agent registries as XCore services.

Creates the tool-layer registries (ToolRegistry, AgentRegistry) and registers
them as services (``ctx.tools`` / ``ctx.agents``).  Registration through these
services is a fiber effect: XCore tracks the currently applying fiber, so
anything a plugin registers is undone automatically when the plugin's fiber
unloads — the tool service itself binds the cleanup.
"""

from __future__ import annotations

from typing import Any

from core.agents import AgentRegistry
from core.effects import _active_fiber, _active_plugin_name, _effect_cleanup
from core.tools import Tool
from tools.registry import RegisteredSandboxMode, ToolRegistry


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
        _effect_cleanup(_active_fiber(), lambda: self.registry.unregister(name))
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
        owner = _active_plugin_name()
        name = self.registry.register(definition, owner=owner)
        _effect_cleanup(
            _active_fiber(),
            lambda: self.registry.unregister(name, owner=owner),
        )
        return name

    def unregister(self, name: str) -> bool:
        owner = _active_plugin_name()
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
