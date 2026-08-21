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

from XBotv2.agentloop.events import EventPort
from XBotv2.core.tools import GuardDecision, Tool
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.contracts import LIST_TOOLS, ToolCatalog, ToolDescription
from XBotv2.core.operations import EmptyRequest
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
        self._registry = registry
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
    ) -> str:
        """Register one tool; undone automatically when the plugin unloads.

        ``namespace`` is only for functional name scoping (e.g. ``mcp:server``,
        ``skills:scope``); plugin ownership and cleanup are handled by the
        XCore fiber.
        """
        name = self._registry.register(
            tool,
            model_visible=model_visible,
            timeout_seconds=timeout_seconds,
            namespace=namespace,
        )
        bound_effect(lambda: self._registry.unregister(name))
        return name

    def unregister(self, name: str) -> bool:
        return self._registry.unregister(name)

    def enabled(self) -> tuple[Tool, ...]:
        return tuple(self._registry.get_all())

    def resolve(self, name: str, *, include_disabled: bool = False) -> Tool | None:
        entry = (
            self._registry.get_registered(name)
            if include_disabled
            else self._registry.get(name)
        )
        return entry.tool if entry is not None else None

    def names(self) -> tuple[str, ...]:
        return tuple(self._registry.names())

    def registered_names(self) -> tuple[str, ...]:
        return tuple(self._registry.registered_names())

    def registrations(self) -> tuple[Any, ...]:
        return self._registry.registered_entries()

    def restrict(self, selectors: list[str] | None) -> tuple[str, ...]:
        return tuple(self._registry.restrict(selectors))

    def exclude(self, selectors: list[str]) -> tuple[str, ...]:
        return tuple(self._registry.exclude(selectors))

    async def execute_all(
        self,
        tool_calls: list[Any],
        *,
        context_factory: Any = None,
    ) -> list[Any]:
        """Run the full tool-execution guard pipeline.

        Pipeline per call: rewrite-only ``BEFORE_TOOL_CALL`` event, schema
        validation, monotonic guards, dispatch, and ``AFTER_TOOL_CALL``.
        Tool owners bind their runtime dependencies before registration; the
        agent loop only submits calls and receives their ordered results.
        """
        from XBotv2.agentloop.tool_runtime import execute_tools

        return await execute_tools(
            tool_calls,
            self._registry,
            events=self.events,
            guards=self.guards(),
            context_factory=context_factory,
        )

class ToolsComponent:
    """Register the loop-owned tool service."""

    name = "xbot.agentloop.tools"

    def apply(self, ctx: Any, config: Any = None) -> None:
        tool_registry = ToolRegistry()
        service = ToolsService(tool_registry, events=ctx)
        ctx.set(
            "tools",
            service,
        )

        def list_tools(_request: EmptyRequest) -> ToolCatalog:
            enabled = set(tool_registry.names())
            return ToolCatalog(tools=tuple(
                ToolDescription(
                    name=str(getattr(entry.tool, "name", entry.registered_name)),
                    registered_name=entry.registered_name,
                    namespace=entry.namespace,
                    description=str(getattr(entry.tool, "description", "") or ""),
                    parameters=dict(getattr(entry.tool, "parameters", {}) or {}),
                    timeout_seconds=entry.timeout_seconds,
                )
                for entry in tool_registry.registered_entries()
                if entry.model_visible and entry.registered_name in enabled
            ))

        ctx.on(LIST_TOOLS.name, list_tools)


plugin = ToolsComponent()
