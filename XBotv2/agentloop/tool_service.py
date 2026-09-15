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

from functools import partial
from typing import Callable, Literal

from XBotv2.agentloop.events import EventContext, EventPort
from XBotv2.agentloop.contracts import ToolGuard, ToolsPort
from XBotv2.core.messages import Message
from XBotv2.core.tools import Tool, ToolCall
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.contracts import ToolRegistration
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from xcore import bound_effect, current_plugin_name

class ToolsService(ToolsPort):
    """Plugin-facing tool registry with fiber-scoped auto-unregister.

    Holds the tool registry plus the execution-pipeline guards.  A guard
    receives the ``ToolCall`` and its ``ToolEntry`` and returns ``None`` to
    let the call through, or a final :class:`GuardDecision`. Guards run in
    registration order and own any dependencies needed to reach that result.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        events: EventPort | None = None,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
        ownership: Literal["fiber", "caller"] = "fiber",
    ) -> None:
        self._registry = registry
        self.events = events
        self._log = runtime_log.bind("tools")
        # Composition-level default for registrations made outside a plugin
        # apply: "fiber" stays loud, "caller" says the composer owns release
        # (test harnesses, embedding hosts).
        self._ownership = ownership
        self._guards: list[ToolGuard] = []
        self._approval_guards: list[ToolGuard] = []

    def guard(
        self,
        guard: ToolGuard,
        *,
        approval: bool = False,
        cleanup: Literal["fiber", "caller"] | None = None,
    ) -> bool:
        """Register one monotonic execution guard.

        ``approval=True`` marks a guard that can approve sandbox-escape
        requests (an active approval layer). The execution pipeline fails
        closed: an escaped call without an approval-capable guard is denied
        before dispatch.

        A guard registered outside a plugin ``apply`` has no owning fiber;
        pass ``cleanup="caller"`` to state that the caller releases it.
        """
        if bound_effect(partial(self._remove_guard, guard)) is False:
            if (cleanup or self._ownership) != "caller":
                raise RuntimeError(
                    "Tool guards must be registered from within a plugin "
                    "apply(); pass cleanup='caller' when the caller owns release"
                )
        self._guards.append(guard)
        if approval:
            self._approval_guards.append(guard)
        self._log.debug(
            "tool.guard.registered",
            owner=current_plugin_name(),
            guard=getattr(guard, "__qualname__", type(guard).__qualname__),
            approval=approval,
        )
        return True

    def _remove_guard(self, guard: ToolGuard) -> None:
        if guard in self._guards:
            self._guards.remove(guard)
        if guard in self._approval_guards:
            self._approval_guards.remove(guard)

    def guards(self) -> tuple[ToolGuard, ...]:
        return tuple(self._guards)

    def approval_layer_active(self) -> bool:
        return bool(self._approval_guards)

    def register(
        self,
        tool: Tool,
        *,
        model_visible: bool = True,
        timeout_seconds: float | None = None,
        namespace: str | None = None,
        cleanup: Literal["fiber", "caller"] | None = None,
    ) -> str:
        """Register one tool; undone automatically when the plugin unloads.

        ``namespace`` is only for functional name scoping (e.g. ``mcp:server``,
        ``skills:scope``); plugin ownership and cleanup are handled by the
        XCore fiber. A registration made outside a plugin ``apply`` has no
        owning fiber: it must declare ``cleanup="caller"`` and release itself,
        otherwise the registration is rolled back and reported loudly.
        """
        name = self._registry.register(
            tool,
            model_visible=model_visible,
            timeout_seconds=timeout_seconds,
            namespace=namespace,
        )
        self._log.info(
            "tool.registered",
            name=name,
            owner=current_plugin_name(),
            model_visible=model_visible,
            timeout_seconds=timeout_seconds,
        )
        if bound_effect(partial(self.unregister, name)) is False:
            if (cleanup or self._ownership) != "caller":
                # No owner could ever release this tool: undo it instead of
                # leaving an ownerless registration behind.
                self.unregister(name)
                raise RuntimeError(
                    f"Tool {name!r} was registered outside a plugin apply(); "
                    "pass cleanup='caller' when the caller owns release"
                )
        return name

    def unregister(self, name: str) -> bool:
        removed = self._registry.unregister(name)
        if removed:
            self._log.info("tool.unregistered", name=name)
        return removed

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

    def registrations(self) -> tuple[ToolRegistration, ...]:
        return self._registry.registered_entries()

    def restrict(self, selectors: list[str] | None) -> tuple[str, ...]:
        enabled = tuple(self._registry.restrict(selectors))
        self._log.debug(
            "tool.selection.restricted",
            selectors=selectors or ["*"],
            enabled_count=len(enabled),
        )
        return enabled

    def exclude(self, selectors: list[str]) -> tuple[str, ...]:
        enabled = tuple(self._registry.exclude(selectors))
        self._log.debug(
            "tool.selection.excluded",
            selectors=selectors,
            enabled_count=len(enabled),
        )
        return enabled

    async def execute_all(
        self,
        tool_calls: list[ToolCall],
        *,
        context_factory: Callable[..., EventContext] | None = None,
    ) -> list[Message]:
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
            runtime_log=self._log,
            approval_layer_active=self.approval_layer_active(),
        )
