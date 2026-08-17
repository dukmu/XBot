"""Agent-loop service for tool registration and execution.

Creates the tool registry and registers it as ``ctx.tools``. Registration through the
services is a fiber effect: XCore tracks the currently applying fiber
(:func:`xcore.current_fiber`), so anything a plugin registers is undone
automatically when the plugin's fiber unloads — the service itself binds the
cleanup, no loader-side context tracking.

The execution pipeline lives on ``ToolsService``. Tool owners capture their
own invocation dependencies when registering a tool, and guard owners resolve
their own policy before returning a final decision. The service therefore has
no knowledge of individual plugins.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from XBotv2.core.events import EventPort
from XBotv2.core.tools import GuardDecision, Tool
from XBotv2.agentloop.tool_registry import ToolRegistry
from xcore import bound_effect

Guard = Callable[[Any, Any], GuardDecision | None | Awaitable[GuardDecision | None]]


class ToolsService:
    """Plugin-facing tool registry with fiber-scoped auto-unregister.

    Holds the tool registry plus the execution-pipeline guards.  A guard
    receives the ``ToolCall`` and its ``ToolEntry`` and returns ``None`` to
    let the call through, or a final :class:`GuardDecision`. Guards run in
    registration order and own any dependencies needed to reach that result.
    """

    def __init__(
        self,
        registry: Any,
        *,
        events: EventPort | None = None,
    ) -> None:
        self.registry = registry
        self.events = events
        self._guards: list[Guard] = []

    def guard(self, guard: Guard) -> Any:
        """Register one monotonic execution guard.

        The returned disposer (and the registering fiber's unload) removes
        the guard.
        """
        self._guards.append(guard)
        return bound_effect(lambda: self._guards.remove(guard))

    def guards(self) -> tuple[Guard, ...]:
        return tuple(self._guards)

    def register(
        self,
        tool: Tool,
        *,
        model_visible: bool = True,
        timeout_seconds: float | None = None,
        namespace: str | None = None,
        injected: dict[str, Any] | None = None,
    ) -> str:
        """Register one tool; undone automatically when the plugin unloads.

        ``namespace`` is only for functional name scoping (e.g. ``mcp:server``,
        ``skills:scope``); plugin ownership and cleanup are handled by the
        XCore fiber.
        """
        name = self.registry.register(
            tool,
            model_visible=model_visible,
            timeout_seconds=timeout_seconds,
            namespace=namespace,
            injected=injected,
        )
        bound_effect(lambda: self.registry.unregister(name))
        return name

    def unregister(self, name: str) -> bool:
        return self.registry.unregister(name)

    async def execute_all(
        self,
        tool_calls: list[Any],
        *,
        context_factory: Any = None,
    ) -> list[Any]:
        """Run the full tool-execution guard pipeline.

        Pipeline per call: ``BEFORE_TOOL_CALL`` event waterfall, schema
        validation, monotonic guards, dispatch, and ``AFTER_TOOL_CALL``.
        Runtime dependencies belong to this service; the agent loop only
        submits calls and receives their ordered results.
        """
        from XBotv2.agentloop.tool_runtime import execute_tools

        return await execute_tools(
            tool_calls,
            self.registry,
            events=self.events,
            guards=self.guards(),
            context_factory=context_factory,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)


class ToolsComponent:
    """Register the loop-owned tool service."""

    name = "xbot.agentloop.tools"

    def apply(self, ctx: Any, config: Any = None) -> None:
        tool_registry = ToolRegistry()
        ctx.set(
            "tools",
            ToolsService(tool_registry, events=ctx),
        )


plugin = ToolsComponent()
